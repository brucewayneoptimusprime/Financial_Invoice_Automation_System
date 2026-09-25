import { useEffect, useState, type ReactNode } from "react";
import { ApiError, getPO } from "../api";
import { Chip, Disclosure, Section } from "../components/common";
import { DECISION, FIELD_LABEL, humanize, money, score, usd, when } from "../format";
import { linkProps } from "../router";
import { StatusChip } from "./POList";
import type { Decision, PODetail } from "../types";

function DecisionChip({ d }: { d: Decision | null }) {
  if (!d) return <span className="dim">—</span>;
  return <Chip tone={DECISION[d].tone}>{DECISION[d].label}</Chip>;
}

function Stat({ label, value, tone }: { label: string; value: ReactNode; tone?: string }) {
  return <div className={`stat ${tone ? `stat-${tone}` : ""}`}><div className="label">{label}</div><div className="stat-value num">{value}</div></div>;
}

function Provenance({ p }: { p: Record<string, unknown> }) {
  const source = String(p.source ?? (p.demo ? "seed" : "unknown"));
  const edited = (p.edited_fields as string[] | undefined) ?? [];
  return (
    <dl className="kv">
      <div className="kv-row"><dt>Entered by</dt><dd>{{ manual: "Form", text: "Typed text, drafted by the model, confirmed by a person",
        document: "Uploaded document, drafted by the model, confirmed by a person", seed: "Demo dataset" }[source] ?? humanize(source)}</dd></div>
      {typeof p.entered_at === "string" && <div className="kv-row"><dt>Entered</dt><dd>{when(p.entered_at)}</dd></div>}
      {typeof p.file_name === "string" && <div className="kv-row"><dt>Document</dt><dd>{p.file_name}</dd></div>}
      {typeof p.model === "string" && <div className="kv-row"><dt>Model</dt><dd>{p.model}{p.cost_usd ? ` · ${usd(p.cost_usd)}` : ""}</dd></div>}
      {p.draft_id !== undefined && (
        <div className="kv-row"><dt>Changed by the person</dt><dd>{edited.length === 0 ? "nothing (the draft was saved as proposed)"
          : edited.map((f) => FIELD_LABEL[f] ?? humanize(f.replace(/\[(\d+)\]/, (_, n) => ` ${Number(n) + 1}`))).join(", ")}</dd></div>
      )}
      {typeof p.text === "string" && (
        <div className="kv-row"><dt>Typed text</dt><dd><Disclosure summary={<span>Show</span>}><p className="pre">{p.text}</p></Disclosure></dd></div>
      )}
    </dl>
  );
}

export function PODetailScreen({ id }: { id: number }) {
  const [d, setD] = useState<PODetail | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop = false;
    const load = () => getPO(id).then((v) => { if (!stop) setD(v); }).catch((e) => {
      if (stop) return;
      if (e instanceof ApiError && e.status === 404) setMissing(true);
      else setError("Could not load this purchase order. Is the API running?");
    });
    load();
    const t = window.setInterval(load, 5000);                    // invoices uploaded meanwhile appear here
    return () => { stop = true; window.clearInterval(t); };
  }, [id]);

  if (missing) return <div className="empty"><h1>Purchase order not found</h1><p><a {...linkProps("/pos")}>All purchase orders</a></p></div>;
  if (error && !d) return <p className="error" role="alert">{error}</p>;
  if (!d) return <p className="dim">Loading…</p>;
  const cur = d.po.currency;
  return (
    <div className="po-detail">
      <div className="run-title">
        <a {...linkProps("/pos")} className="back">← Purchase orders</a>
        <h1>{d.po.po_number}</h1>
        <div className="run-sub">
          <span>{d.po.vendor}</span>
          {d.po.vendor_status !== "approved" && <Chip tone={d.po.vendor_status === "blocked" ? "fail" : "flag"}>vendor {d.po.vendor_status}</Chip>}
          <StatusChip status={d.po.status} />
          <span>{cur}</span>
          {d.po.issued_date && <span>issued {d.po.issued_date}</span>}
        </div>
      </div>

      <div className="stats">
        <Stat label="Total" value={money(d.amounts.total, cur)} />
        <Stat label="Committed (approved invoices)" value={money(d.amounts.committed, cur)} />
        <Stat label="Balance" value={money(d.amounts.balance, cur)} tone={d.amounts.over_billed ? "fail" : "accent"} />
        <Stat label="Awaiting review" value={money(d.amounts.awaiting_review, cur)} tone="flag" />
      </div>
      {d.amounts.over_billed && <p className="warn">Over-billed: approved invoices exceed the PO total (within the tolerance the rules allowed).</p>}

      <Section title="Invoices matched to this PO" id="po-invoices"
               aside={<a className="btn-ghost" {...linkProps(`/?po=${d.po.id}`)}>Upload invoices</a>}>
        <p className="dim small">Matching is automatic. An invoice appears here only when the matcher confidently matched it to this PO; review
          items do not use the balance until someone approves them.</p>
        {d.invoices.length === 0 ? <p className="dim">No invoice has been matched to this PO yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Invoice</th><th>File</th><th className="num">Total</th><th>Decision</th><th>Status now</th><th>When</th></tr></thead>
              <tbody>
                {d.invoices.map((i) => (
                  <tr key={i.invoice_id}>
                    <th scope="row">{i.run_id ? <a {...linkProps(`/runs/${i.run_id}`)}>{i.invoice_number ?? "(no number)"}</a> : i.invoice_number ?? "—"}</th>
                    <td>{i.historic ? <span className="dim">historic, no run</span> : i.source_file}</td>
                    <td className="num">{money(i.total, i.currency)}</td>
                    <td>{i.historic ? <span className="dim">—</span> : <DecisionChip d={i.decision} />}</td>
                    <td>{humanize(i.status)}</td>
                    <td className="dim">{when(i.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Lines" id="po-lines">
        {d.lines.length === 0 ? <p className="dim">No lines on this PO.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>#</th><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th></tr></thead>
              <tbody>
                {d.lines.map((l) => (
                  <tr key={l.line_no}><td className="dim">{l.line_no}</td><td>{l.description ?? "—"}</td><td className="num">{l.quantity ?? "—"}</td>
                    <td className="num">{money(l.unit_price)}</td><td className="num">{money(l.amount)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Section>

      <Section title="Ledger" id="po-ledger">
        {d.ledger.length === 0 ? <p className="dim">Nothing committed yet.</p> : (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>Type</th><th className="num">Amount</th><th>Invoice id</th><th>When</th></tr></thead>
              <tbody>{d.ledger.map((e) => <tr key={e.id}><td>{humanize(e.type)}</td><td className="num">{money(e.amount, cur)}</td><td>{e.invoice_id}</td><td className="dim">{when(e.created_at)}</td></tr>)}</tbody>
            </table>
          </div>
        )}
      </Section>

      {d.considered_in.length > 0 && (
        <Section title="Also considered in (not matched)" id="po-considered">
          <p className="dim small">Runs where this PO was ranked as a candidate but the invoice was not matched to it.</p>
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>File</th><th className="num">Score here</th><th>Outcome</th><th>Decision</th><th>When</th></tr></thead>
              <tbody>
                {d.considered_in.map((c) => (
                  <tr key={c.run_id}>
                    <th scope="row"><a {...linkProps(`/runs/${c.run_id}`)}>{c.source_file}</a></th>
                    <td className="num">{score(c.score)}</td>
                    <td>{c.matched_po ? `matched ${c.matched_po}` : humanize(c.match_status ?? "not matched")}</td>
                    <td><DecisionChip d={c.decision} /></td>
                    <td className="dim">{when(c.started_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Section>
      )}

      <Section title="Where this PO came from" id="po-provenance"><Provenance p={d.provenance} /></Section>
    </div>
  );
}
