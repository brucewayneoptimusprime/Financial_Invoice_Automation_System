import { useEffect, useState } from "react";
import { getDashboard, getRun, isRunView } from "../api";
import gmailIcon from "../assets/gmail-icon.png";
import { Chip, Section, Stat } from "../components/common";
import { DECISION, humanize, money, usd, when } from "../format";
import { reviewReasons } from "../reasons";
import { linkProps } from "../router";
import type { Dashboard, Decision, RunView } from "../types";

const ORDER: Decision[] = ["approve", "review", "request_info", "reject"];
// Review items are worked in the review queue; the other decisions open the Invoices list filtered to that decision.
export const decisionHref = (d: Decision) => (d === "review" ? "/review" : `/invoices?decision=${d}`);
const PO_TONE: Record<string, string> = { open: "info", partially_billed: "flag", fully_billed: "pass", closed: "muted" };

// A dependency-free proportion bar: one segment per decision, sized by share. The numbers are always written out beside it,
// so the bar is decoration (aria-hidden), never the only way to read the split.
export function DecisionBar({ counts }: { counts: Record<Decision, number> }) {
  const total = ORDER.reduce((n, d) => n + (counts[d] ?? 0), 0);
  if (total === 0) return null;
  return (
    <div className="decision-bar" aria-hidden="true">
      {ORDER.filter((d) => counts[d] > 0).map((d) => (
        <a key={d} {...linkProps(decisionHref(d))} tabIndex={-1} className={`seg seg-${DECISION[d].tone}`} style={{ flexGrow: counts[d] }}
           title={`${DECISION[d].label}: ${counts[d]}`} />
      ))}
    </div>
  );
}

// Where each recent run's file came from, read from its existing run view (`source`, built from the source_gmail audit event).
// A run's source never changes once the run exists, so each run is asked about once per page load (no backend change needed).
type Source = RunView["source"] | null;
const sourceCache = new Map<string, Source>();

function useRunSources(ids: string[]): Map<string, Source> {
  const [, bump] = useState(0);
  const key = ids.join(",");
  useEffect(() => {
    let stop = false;
    for (const id of key ? key.split(",") : []) {
      if (sourceCache.has(id)) continue;
      getRun(id).then((v) => {
        if (!isRunView(v)) return;                        // still queued: ask again on the next refresh
        sourceCache.set(id, v.source ?? null);
        if (!stop) bump((n) => n + 1);
      }).catch(() => { /* the marker is optional: never break the dashboard */ });
    }
    return () => { stop = true; };
  }, [key]);
  return sourceCache;
}

export function GmailMarker({ source }: { source: Source | undefined }) {
  if (source?.kind !== "gmail") return null;
  return (
    <span className="gmail-marker" title={source.sender ? `From Gmail: ${source.sender}` : "From Gmail"}>
      <img src={gmailIcon} alt="" width={14} height={14} />From Gmail
    </span>
  );
}

export function DashboardScreen() {
  const [d, setD] = useState<Dashboard | null>(null);
  const [error, setError] = useState<string | null>(null);

  const sources = useRunSources(d ? d.recent_runs.map((r) => r.id) : []);

  useEffect(() => {
    let stop = false;
    const load = () => getDashboard().then((x) => { if (!stop) { setD(x); setError(null); } })
                                     .catch(() => { if (!stop) setError("Could not load the dashboard. Is the API running?"); });
    load();
    const t = window.setInterval(load, 10000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  if (error && !d) return <p className="error" role="alert">{error}</p>;
  if (!d) return <p className="dim">Loading…</p>;
  const o = d.outcomes;
  const decided = ORDER.reduce((n, k) => n + d.runs.by_decision[k], 0);

  return (
    <div className="dashboard">
      <div className="page-head">
        <div>
          <h1>Dashboard</h1>
          <p className="dim">Read from the audit log and the ledger; nothing here changes data. Updated {when(d.generated_at)}.</p>
        </div>
        <a className="btn" {...linkProps("/invoices")}>Upload invoices</a>
      </div>

      <div className="stats">
        <Stat label="Invoices processed" value={d.runs.processed}
              sub={d.runs.failed > 0 ? <span className="neg">{d.runs.failed} failed</span> : d.runs.running > 0 ? `${d.runs.running} running` : null} />
        <div className="stat stat-wide">
          <div className="label">Decisions (by the system, at run time)</div>
          <DecisionBar counts={d.runs.by_decision} />
          <ul className="decision-counts">
            {ORDER.map((k) => (
              <li key={k}>
                <a {...linkProps(decisionHref(k))} className="decision-link" title={k === "review" ? "Open the review queue" : `Invoices decided: ${DECISION[k].label}`}>
                  <Chip tone={DECISION[k].tone}>{DECISION[k].label}</Chip> <span className="num">{d.runs.by_decision[k]}</span>
                </a>
              </li>
            ))}
          </ul>
          {decided > 0 && (
            <div className="stat-sub">Now, after review: {o.approved} approved, {o.rejected} rejected, {o.in_review} still in review,
              {" "}{o.awaiting_info} awaiting information.</div>
          )}
        </div>
        <a className="stat stat-link" {...linkProps("/review")}>
          <div className="label">Review queue</div>
          <div className="stat-value num">{d.review.open_count}</div>
          <div className="stat-sub">{d.review.open_count === 1 ? "item waiting" : "items waiting"}</div>
        </a>
        <Stat label="LLM spend" value={usd(d.spend.total_usd)}
              sub={`invoice runs ${usd(d.spend.invoice_runs_usd)} · PO drafts ${usd(d.spend.po_drafts_usd)}`} />
      </div>

      <Section title="Purchase orders" id="dash-pos" aside={<a {...linkProps("/pos")}>All purchase orders →</a>}>
        <div className="stats">
          <div className="stat">
            <div className="label">Purchase orders</div>
            <div className="stat-value num">{d.pos.count}</div>
            <div className="chip-row">
              {Object.entries(d.pos.by_status).filter(([, n]) => n > 0).map(([s, n]) => (
                <Chip key={s} tone={PO_TONE[s] ?? "muted"}>{humanize(s)} {n}</Chip>
              ))}
            </div>
          </div>
          {d.pos.currencies.map((c) => (
            <a className="stat stat-link" key={c.currency} {...linkProps(`/pos?currency=${encodeURIComponent(c.currency)}`)}>
              <div className="label">{c.currency} · {c.count} PO{c.count === 1 ? "" : "s"}</div>
              <dl className="mini">
                <div><dt>Total value</dt><dd className="num">{money(c.total_value, c.currency)}</dd></div>
                <div><dt>Consumed</dt><dd className="num">{money(c.consumed, c.currency)}</dd></div>
                <div><dt>Balance</dt><dd className="num">{money(c.balance, c.currency)}</dd></div>
                <div><dt>of which not assigned to a line</dt><dd className="num">{money(c.consumed_without_line, c.currency)}</dd></div>
              </dl>
            </a>
          ))}
        </div>
      </Section>

      <div className="dash-lists">
        <Section title="Recent runs" id="dash-runs" aside={<a {...linkProps("/invoices")}>All invoices →</a>}>
          {d.recent_runs.length === 0 ? (
            <p className="dim">No invoices yet: <a {...linkProps("/invoices")}>upload one</a>.</p>
          ) : (
            <ul className="run-list">
              {d.recent_runs.map((r) => {
                const dec = r.final_decision ? DECISION[r.final_decision as Decision] : null;
                const now = r.invoice_status;
                const changed = now && r.final_decision && !(
                  (r.final_decision === "approve" && now === "approved") || (r.final_decision === "review" && now === "in_review") ||
                  (r.final_decision === "request_info" && now === "awaiting_info") || (r.final_decision === "reject" && now === "rejected"));
                return (
                  <li key={r.id}>
                    <a {...linkProps(`/runs/${r.id}`)} className="run-link">
                      <span className="run-file">{r.vendor ?? r.source_file}{r.invoice_number && <span className="dim"> · {r.invoice_number}</span>}</span>
                      <span className="run-meta">{when(r.started_at)} <GmailMarker source={sources.get(r.id)} /></span>
                      <span className="run-chips">
                        {dec ? <Chip tone={dec.tone}>{dec.label}</Chip> : <Chip tone={r.status === "failed" ? "fail" : "muted"}>{r.status}</Chip>}
                        {changed && <Chip tone={now === "approved" ? "pass" : now === "rejected" ? "fail" : "muted"}>now {humanize(now!)}</Chip>}
                      </span>
                    </a>
                  </li>
                );
              })}
            </ul>
          )}
        </Section>

        <Section title="Waiting for review" id="dash-review" aside={<a {...linkProps("/review")}>Whole queue →</a>}>
          {d.review.oldest_open.length === 0 ? <p className="dim">Nothing is waiting for review.</p> : (
            <ul className="run-list">
              {d.review.oldest_open.map((i) => (
                <li key={i.id}>
                  <a {...linkProps(`/review/${i.id}`)} className="run-link review-link">
                    <span className="run-file">{i.vendor ?? "Unknown vendor"} · {i.invoice_number ?? "(no number)"} · {money(i.total, i.currency)}
                      <span className="queue-reason small">{reviewReasons(i.reason).map((r) => r.text).join(" ")}</span>
                    </span>
                    <span className="run-chips">
                      {!i.can_approve ? <Chip tone="muted">cannot approve</Chip>
                        : i.lines_needing_input > 0 ? <Chip tone="flag">{i.lines_needing_input} line choice{i.lines_needing_input === 1 ? "" : "s"}</Chip>
                        : <Chip tone="info">ready</Chip>}
                    </span>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </div>
  );
}
