import { useEffect, useRef, useState } from "react";
import { listPOs } from "../api";
import { Chip } from "../components/common";
import { ExportMenu } from "../components/ExportMenu";
import { POExportButton } from "../components/POExportButton";
import { downloadFile } from "../download";
import { humanize, money } from "../format";
import { linkProps } from "../router";
import type { POListRow } from "../types";

const STATUS_TONE: Record<string, string> = { open: "info", partially_billed: "flag", fully_billed: "pass", closed: "muted" };

export function StatusChip({ status }: { status: string }) {
  return <Chip tone={STATUS_TONE[status] ?? "muted"}>{humanize(status)}</Chip>;
}

// The summary export: the ticked rows, or (nothing ticked) everything the list currently shows under its search and filters.
export function summaryExportPath(format: string, f: { q: string; status: string; currency: string; ids: number[] }): string {
  const params = new URLSearchParams({ format });
  if (f.q) params.set("q", f.q);
  if (f.status) params.set("status", f.status);
  if (f.currency) params.set("currency", f.currency);
  if (f.ids.length > 0) params.set("ids", f.ids.join(","));
  return `/api/pos/export?${params}`;
}

function SelectAll({ total, ticked, onChange }: { total: number; ticked: number; onChange(all: boolean): void }) {
  const box = useRef<HTMLInputElement>(null);
  useEffect(() => { if (box.current) box.current.indeterminate = ticked > 0 && ticked < total; }, [ticked, total]);
  return <input ref={box} type="checkbox" aria-label="Select all shown" checked={total > 0 && ticked === total}
                onChange={(e) => onChange(e.target.checked)} onClick={(e) => e.stopPropagation()} />;
}

export function POListScreen() {
  const params = new URLSearchParams(window.location.search);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [currency, setCurrency] = useState((params.get("currency") ?? "").toUpperCase());
  const [rows, setRows] = useState<POListRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ticked, setTicked] = useState<Set<number>>(new Set());   // ticks apply ONLY to the summary export

  // A tick on a row that the current search / filter no longer shows is dropped, so an export never holds a PO you cannot see.
  useEffect(() => {
    if (rows === null) return;
    const shown = new Set(rows.map((r) => r.id));
    setTicked((t) => (Array.from(t).every((id) => shown.has(id)) ? t : new Set(Array.from(t).filter((id) => shown.has(id)))));
  }, [rows]);

  const toggle = (id: number) => setTicked((t) => {
    const next = new Set(t);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });
  const shownIds = rows?.map((r) => r.id) ?? [];
  const tickedIds = shownIds.filter((id) => ticked.has(id));        // in the list's order

  useEffect(() => {
    let stop = false;
    const t = window.setTimeout(() => {
      listPOs(q, status, currency).then((r) => { if (!stop) { setRows(r.pos); setError(null); } })
                        .catch(() => { if (!stop) setError("Could not load purchase orders. Is the API running?"); });
    }, 200);
    return () => { stop = true; window.clearTimeout(t); };
  }, [q, status, currency]);

  return (
    <div className="po-list">
      <div className="page-head">
        <div>
          <h1>Purchase orders</h1>
          <p className="dim">Invoices are matched against these automatically. Balances are derived from the ledger.</p>
        </div>
        <a className="btn" {...linkProps("/pos/new")}>New purchase order</a>
      </div>
      <div className="filters">
        <input type="search" placeholder="Search PO number or vendor" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} aria-label="Status">
          <option value="">All statuses</option>
          <option value="open">Open</option>
          <option value="partially_billed">Partially billed</option>
          <option value="fully_billed">Fully billed</option>
          <option value="closed">Closed</option>
        </select>
        {currency && (
          <span className="filter-note">Currency: <strong>{currency}</strong>{" "}
            <button type="button" className="linkish" onClick={() => { setCurrency(""); window.history.replaceState(null, "", "/pos"); }}>
              show all currencies</button></span>
        )}
        <span className="filters-end">
          <ExportMenu label={tickedIds.length > 0 ? `Export ${tickedIds.length} selected` : `Export all ${shownIds.length} shown`}
                      disabled={!rows || rows.length === 0}
                      onExport={(format) => downloadFile(summaryExportPath(format, { q, status, currency, ids: tickedIds }),
                                                         `purchase-orders.${format}`)} />
        </span>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {rows === null ? <p className="dim">Loading…</p> : rows.length === 0 ? <p className="dim">No purchase orders match.</p> : (
        <div className="section">
          <div className="table-wrap">
            <table className="table po-table">
              <thead>
                <tr><th className="tick"><SelectAll total={shownIds.length} ticked={tickedIds.length}
                                                    onChange={(all) => setTicked(all ? new Set(shownIds) : new Set())} /></th>
                  <th>PO number</th><th>Vendor</th><th className="num">Total</th><th className="num">Balance</th><th>Status</th><th className="num">Invoices</th><th>Entered</th>
                  <th className="row-actions"><span className="sr-only">Export</span></th></tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id} className={ticked.has(p.id) ? "ticked" : undefined}>
                    <td className="tick"><input type="checkbox" aria-label={`Select ${p.po_number}`} checked={ticked.has(p.id)}
                                                onChange={() => toggle(p.id)} onClick={(e) => e.stopPropagation()} /></td>
                    <th scope="row"><a {...linkProps(`/pos/${p.id}`)}>{p.po_number}</a></th>
                    <td>{p.vendor}{p.vendor_status !== "approved" && <span className="tag">{p.vendor_status}</span>}</td>
                    <td className="num">{money(p.total, p.currency)}</td>
                    <td className={`num ${p.balance.startsWith("-") ? "neg" : ""}`}>{money(p.balance, p.currency)}</td>
                    <td><StatusChip status={p.status} /></td>
                    <td className="num">{p.invoice_count}</td>
                    <td className="dim">{p.source ? humanize(p.source) : "—"}</td>
                    <td className="row-actions"><POExportButton poId={p.id} poNumber={p.po_number} size="small" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
