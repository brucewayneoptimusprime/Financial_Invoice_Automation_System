import { useEffect, useRef, useState } from "react";
import { ApiError, crossCheckAnalyse, crossCheckInfo } from "../api";
import { humanize, usd } from "../format";
import type { CrossCheckDocument, CrossCheckInfo, CrossCheckReport } from "../types";
import { Chip, Section } from "./common";

// Cross-check documents (CROSSCHECK_PLAN; SPEC section 11 item 99). REPORT ONLY: this section shows where supporting documents
// agree or disagree with the PO and its invoices. It has no approve / reject control and sends nothing that could change one.
// Nothing leaves the browser until "Analyze" is clicked. Neutral wording and neutral chips only: no verdict, no score.

const ACCEPT = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg";
const EXTENSIONS = /\.(pdf|png|jpe?g)$/i;

const SIGNAL: Record<string, string> = { po_number: "PO number", invoice_number: "Invoice number", vendor: "Vendor", lines: "Lines" };
const DIFFERENCE: Record<string, string> = {
  quantity_vs_po: "Quantity, against the PO line", quantity_vs_invoiced: "Quantity, against what was invoiced", unit_price: "Unit price",
  amount: "Amount", vendor_name: "Vendor name", po_number: "PO number", currency: "Currency", item_not_on_po: "Item not on the PO",
  total: "Document total",
};
const KIND: Record<string, string> = { delivery_note: "Delivery note", goods_receipt: "Goods receipt", shipping_document: "Shipping document",
                                       invoice: "Invoice", purchase_order: "Purchase order", other: "Other document", unknown: "Unknown type" };
const UNCONFIRMED: Record<string, string> = { value_mismatch: "the value does not agree with the text quoted for it",
                                              not_found: "neither the quoted text nor the value was found in the document's text",
                                              no_source: "no text was quoted for it" };

// Estimates and ceilings are round figures: two decimals. The actual cost after an analysis keeps the shared four-decimal format.
const est = (value: number | string) => `$${Number(value).toFixed(2)}`;

function isInfo(v: unknown): v is CrossCheckInfo {
  const i = v as CrossCheckInfo | null;
  return !!i && i.enabled === true && typeof i.max_documents === "number" && typeof i.label === "string";
}

function Where({ page, text }: { page: number | null; text: string | null }) {
  if (!page && !text) return null;
  return (
    <div className="src cc-where">
      {page ? <span className="page-tag">p. {page}</span> : null} {text ? <q>{text}</q> : null}
    </div>
  );
}

function DocumentReport({ d }: { d: CrossCheckDocument }) {
  if (d.status === "failed" || !d.relevance) {
    return (
      <article className="cc-doc" aria-label={d.file_name}>
        <h3>{d.file_name} <Chip tone="muted">Not analysed</Chip></h3>
        <p className="warn" role="status">{d.failure?.message ?? "This document could not be analysed."}</p>
      </article>
    );
  }
  const f = d.facts!;
  const rel = d.relevance;
  const diffs = d.differences ?? [];
  return (
    <article className="cc-doc" aria-label={d.file_name}>
      <h3>{d.file_name}{" "}
        <Chip tone="info">{rel.related ? (rel.vendor_only ? "Related by vendor only" : "Related") : "Not related"}</Chip>{" "}
        <span className="dim small">{KIND[f.document_kind] ?? humanize(f.document_kind)}
          {f.fields.document_type ? ` · "${f.fields.document_type.value}"` : ""}{d.pages ? ` · ${d.pages} page${d.pages === 1 ? "" : "s"}` : ""} · {usd(d.cost_usd)}</span>
      </h3>
      {(d.notices ?? []).map((n) => <p className="notice" key={n}>{n}</p>)}

      <h4>{rel.related ? "Why it is considered related to this PO" : "Why it is not considered related to this PO"}</h4>
      <ul className="cc-signals">
        {rel.signals.map((s) => (
          <li key={s.signal}>
            <Chip tone={s.holds ? "info" : "muted"}>{SIGNAL[s.signal] ?? humanize(s.signal)}: {s.holds ? "yes" : "no"}</Chip>{" "}
            {s.explanation}
            {(s.document_value || s.po_value) && (
              <span className="dim small"> Document: {s.document_value ?? "—"} · This PO: {s.po_value ?? "—"}</span>
            )}
          </li>
        ))}
      </ul>

      {rel.related && (diffs.length === 0 ? <h4>No differences found</h4> : (
        <>
          <h4>Differences found ({diffs.length})</h4>
          <div className="table-wrap">
            <table className="table cc-table">
              <thead><tr><th>What</th><th>On the document</th><th>Compared with</th></tr></thead>
              <tbody>
                {diffs.map((x, i) => (
                  <tr key={i}>
                    <th scope="row">{DIFFERENCE[x.type] ?? humanize(x.type)}
                      {x.po_line_no ? <div className="dim small">PO line {x.po_line_no}{x.po_line_description ? `: ${x.po_line_description}` : ""}</div> : null}</th>
                    <td><strong>{x.document.value}</strong><Where page={x.document.page} text={x.document.source_text} /></td>
                    <td><strong>{x.compared_with.value}</strong><div className="dim small">{x.compared_with.source}</div></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ))}

      {(d.absent_po_lines ?? []).length > 0 && (
        <>
          <h4>PO lines not on this document (for information)</h4>
          <ul className="cc-list">{d.absent_po_lines!.map((l) => (
            <li key={l.po_line_no}>Line {l.po_line_no}: {l.description ?? "(no description)"}{l.quantity ? `, ordered quantity ${l.quantity}` : ""}</li>
          ))}</ul>
        </>
      )}
      {(d.not_compared ?? []).length > 0 && (
        <>
          <h4>Not compared</h4>
          <ul className="cc-list">{d.not_compared!.map((n, i) => <li key={i}>{n.what}: {n.reason}.</li>)}</ul>
        </>
      )}
      {(d.unconfirmed ?? []).length > 0 && (
        <>
          <h4>Could not be confirmed in the document text (not compared)</h4>
          <ul className="cc-list">{d.unconfirmed!.map((u, i) => (
            <li key={i}>{humanize(u.what)} "{u.value}": {UNCONFIRMED[u.grounding] ?? humanize(u.grounding)}.</li>
          ))}</ul>
        </>
      )}

      <details className="cc-facts">
        <summary>What was read from this document</summary>
        <dl className="kv">
          {Object.entries(f.fields).map(([name, v]) => (
            <div className="kv-row" key={name}><dt>{humanize(name)}</dt>
              <dd>{v ? <>{v.value}<Where page={v.page} text={v.source_text} /></> : <span className="dim">not printed</span>}</dd></div>
          ))}
          {f.mentions.map((m, i) => (
            <div className="kv-row" key={i}><dt>{humanize(m.kind)}{m.label ? ` (${m.label})` : ""}</dt>
              <dd>{m.value}<Where page={m.page} text={m.source_text} /></dd></div>
          ))}
        </dl>
        {f.lines.length > 0 && (
          <div className="table-wrap">
            <table className="table">
              <thead><tr><th>#</th><th>Description</th><th className="num">Qty</th><th>Unit</th><th className="num">Unit price</th>
                <th className="num">Amount</th><th>PO line</th></tr></thead>
              <tbody>
                {f.lines.map((l, i) => (
                  <tr key={i}><td className="dim">{i + 1}</td><td>{l.description ?? "—"}<Where page={l.page} text={l.source_text} /></td>
                    <td className="num">{l.quantity ?? "—"}</td><td>{l.unit ?? "—"}</td><td className="num">{l.unit_price ?? "—"}</td>
                    <td className="num">{l.amount ?? "—"}</td>
                    <td className="dim">{l.tie.po_line_no ? `line ${l.tie.po_line_no}` : humanize(l.tie.status)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {f.model_notes && <p className="dim small">Reader's note (not used for anything): {f.model_notes}</p>}
      </details>
    </article>
  );
}

export function CrossCheck({ poId }: { poId: number }) {
  const [info, setInfo] = useState<CrossCheckInfo | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<CrossCheckReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let stop = false;
    setInfo(null);
    setFiles([]);
    setReport(null);
    setError(null);
    crossCheckInfo(poId).then((v) => { if (!stop && isInfo(v)) setInfo(v); }).catch(() => { /* switched off or unreachable: no section */ });
    return () => { stop = true; };
  }, [poId]);

  if (!info) return null;
  const max = info.max_documents;
  const maxBytes = info.max_file_mb * 1_048_576;

  function add(list: FileList | null) {
    if (!list) return;
    const refused: string[] = [];
    const next = [...files];
    for (const f of Array.from(list)) {
      if (!EXTENSIONS.test(f.name)) refused.push(`${f.name} is not a PDF, PNG or JPG file`);
      else if (f.size === 0) refused.push(`${f.name} is empty`);
      else if (f.size > maxBytes) refused.push(`${f.name} is larger than ${info!.max_file_mb} MB`);
      else if (next.length >= max) refused.push(`${f.name} was left out: at most ${max} documents can be analysed at once`);
      else next.push(f);
    }
    setFiles(next);
    setProblem(refused.length ? refused.join(". ") + "." : null);
  }

  async function analyse() {
    setBusy(true);
    setError(null);
    setReport(null);
    try {
      setReport(await crossCheckAnalyse(poId, files));
      crossCheckInfo(poId).then((v) => { if (isInfo(v)) setInfo(v); }).catch(() => undefined);   // the budget left, after the spend
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The analysis could not be run. Is the API running?");
    } finally {
      setBusy(false);
    }
  }

  const n = files.length;
  const typical = Number(info.typical_cost_per_document_usd);
  return (
    <Section title="Cross-check documents (report only)" id="po-crosscheck">
      <p className="notice notice-strong" data-testid="cc-label">{info.label}</p>
      <p className="dim small">Attach a delivery note, goods receipt, shipping document or anything else that belongs to this purchase order.
        A model reads each document; ordinary code then compares what it read with this PO and its invoices. Nothing is sent until you
        click Analyze.</p>
      {info.message && <p className="warn" role="status">{info.message}</p>}

      <div className="cc-pick">
        <button type="button" className="btn-ghost" disabled={busy || !info.available || n >= max} onClick={() => input.current?.click()}>
          Choose documents</button>
        <input ref={input} type="file" accept={ACCEPT} multiple hidden aria-label="Documents to cross-check"
               onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
        <span className="dim small">Up to {max} files · {info.accepted.join(", ")} · at most {info.max_file_mb} MB each</span>
      </div>
      {problem && <p className="warn" role="alert">{problem}</p>}
      {n > 0 && (
        <ul className="run-list staged-list" aria-label="Documents to analyse">
          {files.map((f, i) => (
            <li key={`${f.name}-${i}`}>
              <div className="run-link">
                <span className="run-file">{f.name}</span>
                <span className="dim small">{(f.size / 1024).toFixed(0)} KB</span>
                <span />
                <button type="button" className="linkish" aria-label={`Remove ${f.name}`} disabled={busy}
                        onClick={() => setFiles((cur) => cur.filter((_, j) => j !== i))}>Remove</button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <p className="cost-line small" data-testid="cc-before">
        Estimated cost: about {est(typical)} per document{n > 0 ? `, so about ${est(typical * n)} for ${n} document${n === 1 ? "" : "s"}` : ""}.
        {" "}Never more than {est(info.ceiling_per_document_usd)} per document.
        {info.budget_remaining_usd !== null ? ` Budget left this session: ${est(info.budget_remaining_usd)}.` : ""}
      </p>
      <div className="form-foot">
        <button type="button" className="btn" disabled={busy || n === 0 || !info.available} onClick={analyse}>
          {busy ? "Analyzing…" : n > 1 ? `Analyze ${n} documents` : "Analyze"}</button>
        {n > 0 && !busy && <button type="button" className="btn-ghost" onClick={() => { setFiles([]); setProblem(null); }}>Clear</button>}
      </div>
      {error && <p className="error" role="alert">{error}</p>}

      {report && (
        <div className="cc-report" aria-live="polite">
          <p className="cost-line small" data-testid="cc-after">
            This analysis cost {usd(report.analysis.cost_usd)} ({report.analysis.analysed} of {report.analysis.documents} document
            {report.analysis.documents === 1 ? "" : "s"} analysed, by Claude). {report.label}
          </p>
          {report.documents.map((d, i) => <DocumentReport d={d} key={`${d.file_name}-${i}`} />)}
        </div>
      )}
    </Section>
  );
}
