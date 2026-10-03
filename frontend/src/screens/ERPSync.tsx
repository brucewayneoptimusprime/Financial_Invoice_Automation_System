import { useEffect, useState } from "react";
import { ApiError, erpImport, erpPreview } from "../api";
import { Chip, Section } from "../components/common";
import { money, when } from "../format";
import { linkProps } from "../router";
import type { ERPImport, ERPIssue, ERPPreview, ERPRow, ERPVendor } from "../types";

// "Sync from ERP (simulated)": a bundled sample feed stands in for an ERP connection. The preview writes nothing; only the POs the
// person ticks are saved, through the same writer and checks as the PO form. Existing POs are never changed. No model, no cost.

export const ERP_LABEL = "Simulated ERP (demo)";

function VendorText({ v }: { v: ERPVendor | null }) {
  if (!v) return <span className="dim">no vendor</span>;
  if (v.kind === "existing") return <span>existing: {v.name} <span className="dim small">(by {v.matched_by})</span></span>;
  if (v.kind === "new") return <span>new vendor: {v.name} <Chip tone="flag">status new</Chip></span>;
  return <span>{v.name ?? "?"} <span className="dim small">(matches {v.candidates.join(", ")})</span></span>;
}

function Issues({ issues }: { issues: ERPIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="erp-issues">
      {issues.map((i, k) => <li key={k} className={i.blocks ? "error small" : "erp-warn small"}>{i.message}</li>)}
    </ul>
  );
}

function Lines({ row }: { row: ERPRow }) {
  return (
    <span className="dim small">
      {row.lines.length} line{row.lines.length === 1 ? "" : "s"}
      {row.lines.length > 0 && `: ${row.lines.slice(0, 2).map((l) => `${l.quantity ?? "?"} ${l.unit_of_measure ?? ""} ${l.description ?? ""}`.replace(/\s+/g, " ").trim()).join("; ")}`}
      {row.lines.length > 2 && " …"}
    </span>
  );
}

function rowKey(r: ERPRow) { return `${r.index}`; }

export function ERPSyncScreen() {
  const [p, setP] = useState<ERPPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<string>>(new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ERPImport | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const load = () => {
    setError(null);
    erpPreview().then((x) => { setP(x); setTicked(new Set()); })
      .catch((e) => setError(e instanceof ApiError ? e.message : "Could not reach the API."));
  };
  useEffect(load, []);

  if (error) return (
    <div className="erp-sync">
      <a {...linkProps("/pos")} className="back">← Purchase orders</a>
      <h1>Sync from ERP <Chip tone="info">{ERP_LABEL}</Chip></h1>
      <p className="error" role="alert">{error}</p>
    </div>
  );
  if (!p) return <p className="dim">Reading the simulated ERP feed…</p>;

  const groups = { new: p.pos.filter((r) => r.class === "new"), exists: p.pos.filter((r) => r.class === "exists"),
                   problem: p.pos.filter((r) => r.class === "problem") };
  const picked = groups.new.filter((r) => ticked.has(rowKey(r)) && r.po_number).map((r) => r.po_number as string);
  const toggle = (r: ERPRow) => setTicked((t) => { const n = new Set(t); if (n.has(rowKey(r))) n.delete(rowKey(r)); else n.add(rowKey(r)); return n; });

  const doImport = async () => {
    setBusy(true);
    setMessage(null);
    try {
      const r = await erpImport(p.feed.sha256, picked);
      if (r.ok) {
        setResult(r.body);
        setConfirming(false);
        erpPreview().then((x) => { setP(x); setTicked(new Set()); }).catch(() => {});
      } else {
        if (r.body.preview) { setP(r.body.preview); setTicked(new Set()); }
        setMessage(r.body.message ?? "Nothing was imported.");
        setConfirming(false);
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="erp-sync">
      <a {...linkProps("/pos")} className="back">← Purchase orders</a>
      <div className="page-head">
        <div>
          <h1>Sync from ERP <Chip tone="info">{ERP_LABEL}</Chip></h1>
          <p className="dim">
            A bundled sample file stands in for an ERP connection (SAP, Coupa or Oracle style): purchase orders arrive as structured
            data, so nothing is extracted and no model is used. Nothing is saved until you tick POs and confirm. Existing POs are never
            changed.
          </p>
        </div>
      </div>
      <p className="dim small" data-testid="erp-feed">
        Feed {p.feed.name} · {p.feed.system ?? "ERP"} · format {p.feed.format} (adapter {p.feed.adapter})
        {p.feed.exported_at && <> · exported {when(p.feed.exported_at)}</>} · {p.feed.count} purchase order{p.feed.count === 1 ? "" : "s"}
        {p.feed.truncated > 0 && <> · <strong>{p.feed.truncated} not shown</strong> (at most {p.caps.max_pos_per_sync} per sync)</>}
      </p>

      {result && (
        <Section title="Imported" id="erp-result">
          <p role="status">
            {result.imported} imported{result.skipped ? `, ${result.skipped} skipped (already exist)` : ""}{result.refused ? `, ${result.refused} refused` : ""}.
          </p>
          <ul className="erp-results">
            {result.results.map((x) => (
              <li key={x.po_number}>
                {x.outcome === "imported" && <><Chip tone="pass">imported</Chip> <a {...linkProps(`/pos/${x.po_id}`)}>{x.po_number}</a>
                  {x.new_vendor && <span className="dim small"> · new vendor created (status new)</span>}</>}
                {x.outcome === "skipped_exists" && <><Chip tone="muted">skipped</Chip> {x.po_number} already exists
                  {x.existing_po && <> · <a {...linkProps(`/pos/${x.existing_po.id}`)}>open it</a></>}</>}
                {x.outcome === "refused" && <><Chip tone="fail">refused</Chip> {x.po_number} <Issues issues={x.issues} /></>}
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title={`New (${groups.new.length})`} id="erp-new">
        {groups.new.length === 0 ? <p className="dim">No new purchase orders in the feed.</p> : (
          <ul className="erp-list">
            {groups.new.map((r) => (
              <li key={rowKey(r)} data-testid={`erp-new-${r.po_number}`}>
                <label className="erp-row">
                  <input type="checkbox" checked={ticked.has(rowKey(r))} onChange={() => toggle(r)} aria-label={`Import ${r.po_number}`} />
                  <span className="erp-main">
                    <strong>{r.po_number}</strong> · <VendorText v={r.vendor} /> · {r.currency} {r.total ? money(r.total) : "—"}
                    {r.buyer_reference && <span className="dim small"> · {r.buyer_reference}</span>}
                    <br /><Lines row={r} />
                  </span>
                </label>
                <Issues issues={r.issues} />
              </li>
            ))}
          </ul>
        )}
        {groups.new.length > 0 && (
          <div className="settings-actions">
            {!confirming ? (
              <button type="button" className="btn" disabled={picked.length === 0 || busy} onClick={() => setConfirming(true)}>
                Import {picked.length} purchase order{picked.length === 1 ? "" : "s"}
              </button>
            ) : (
              <span role="group" aria-label="Confirm import" className="erp-confirm">
                Save {picked.length} purchase order{picked.length === 1 ? "" : "s"} from the {ERP_LABEL}?{" "}
                <button type="button" className="btn" disabled={busy} onClick={doImport}>{busy ? "Importing…" : "Confirm import"}</button>{" "}
                <button type="button" className="btn-ghost" disabled={busy} onClick={() => setConfirming(false)}>Cancel</button>
              </span>
            )}
            <span className="dim small">None is ticked for you. Warnings are shown; you can still import those POs, as on the PO form.</span>
          </div>
        )}
        {message && <p className="error" role="alert">{message}</p>}
      </Section>

      <Section title={`Already exists (${groups.exists.length})`} id="erp-exists">
        {groups.exists.length === 0 ? <p className="dim">None.</p> : (
          <ul className="erp-list">
            {groups.exists.map((r) => (
              <li key={rowKey(r)} data-testid={`erp-exists-${r.po_number}`}>
                <strong>{r.po_number}</strong> is already stored and is skipped; it is never changed.{" "}
                {r.existing_po && <a {...linkProps(`/pos/${r.existing_po.id}`)}>Open {r.existing_po.po_number}</a>}
              </li>
            ))}
          </ul>
        )}
      </Section>

      <Section title={`Has problems (${groups.problem.length})`} id="erp-problems">
        {groups.problem.length === 0 ? <p className="dim">None.</p> : (
          <ul className="erp-list">
            {groups.problem.map((r) => (
              <li key={rowKey(r)} data-testid={`erp-problem-${r.index}`}>
                <strong>{r.po_number ?? `Entry ${r.index + 1}`}</strong>{r.vendor && <> · <VendorText v={r.vendor} /></>}
                {r.existing_po && <> · <a {...linkProps(`/pos/${r.existing_po.id}`)}>Open existing {r.existing_po.po_number}</a></>}
                <Issues issues={r.issues} />
              </li>
            ))}
          </ul>
        )}
        <p className="dim small">These cannot be imported. Fix them in the ERP, or enter the PO with the form.</p>
      </Section>
    </div>
  );
}
