import { useEffect, useState } from "react";
import { listPOs } from "../api";
import { Chip } from "../components/common";
import { humanize, money } from "../format";
import { linkProps } from "../router";
import type { POListRow } from "../types";

const STATUS_TONE: Record<string, string> = { open: "info", partially_billed: "flag", fully_billed: "pass", closed: "muted" };

export function StatusChip({ status }: { status: string }) {
  return <Chip tone={STATUS_TONE[status] ?? "muted"}>{humanize(status)}</Chip>;
}

export function POListScreen() {
  const params = new URLSearchParams(window.location.search);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState(params.get("status") ?? "");
  const [currency, setCurrency] = useState((params.get("currency") ?? "").toUpperCase());
  const [rows, setRows] = useState<POListRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

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
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {rows === null ? <p className="dim">Loading…</p> : rows.length === 0 ? <p className="dim">No purchase orders match.</p> : (
        <div className="section">
          <div className="table-wrap">
            <table className="table po-table">
              <thead>
                <tr><th>PO number</th><th>Vendor</th><th className="num">Total</th><th className="num">Balance</th><th>Status</th><th className="num">Invoices</th><th>Entered</th></tr>
              </thead>
              <tbody>
                {rows.map((p) => (
                  <tr key={p.id}>
                    <th scope="row"><a {...linkProps(`/pos/${p.id}`)}>{p.po_number}</a></th>
                    <td>{p.vendor}{p.vendor_status !== "approved" && <span className="tag">{p.vendor_status}</span>}</td>
                    <td className="num">{money(p.total, p.currency)}</td>
                    <td className={`num ${p.balance.startsWith("-") ? "neg" : ""}`}>{money(p.balance, p.currency)}</td>
                    <td><StatusChip status={p.status} /></td>
                    <td className="num">{p.invoice_count}</td>
                    <td className="dim">{p.source ? humanize(p.source) : "—"}</td>
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
