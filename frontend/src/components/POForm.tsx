// The ONE purchase-order form. The manual path starts it empty; the text and document paths start it from a model draft.
// Nothing is saved until the person presses "Save purchase order", and Save sends the values shown here, never the draft.
import { useEffect, useMemo, useRef, useState } from "react";
import { listVendors, SaveRefused, savePO, validatePO } from "../api";
import { Chip } from "./common";
import { GROUNDING } from "../format";
import type { NewVendorInput, POInput, POIssue, POLineInput, Vendor } from "../types";

export interface FieldMark {           // set for fields the model filled in
  found: boolean;
  confidence: number;
  source_text: string | null;
  page: number | null;
  grounding: string | null;
}

export interface POFormProps {
  initial?: Partial<POInput>;
  initialNewVendor?: NewVendorInput | null;
  draftId?: string | null;
  marks?: Record<string, FieldMark>;          // "po_number", "vendor", "currency", "total", "issued_date", "lines[0]"...
  vendorHint?: string | null;                 // e.g. "Suggested from the document's tax ID (score 1.00)"
  onOpenSource?: (mark: FieldMark, label: string) => void;
  onSaved: (poId: number) => void;
  onDirtyChange?: (dirty: boolean) => void;
}

const EMPTY_LINE: POLineInput = { description: "", quantity: "", unit_price: "", amount: "" };
export const emptyPO = (): POInput => ({ po_number: "", vendor_id: null, currency: "", total: "", issued_date: "", lines: [] });

function IssueList({ issues }: { issues: POIssue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul className="issues">
      {issues.map((i, k) => <li key={k} className={`issue issue-${i.level}`}>{i.message}</li>)}
    </ul>
  );
}

function Mark({ mark, label, onOpen }: { mark?: FieldMark; label: string; onOpen?: (m: FieldMark, l: string) => void }) {
  if (!mark) return null;
  if (!mark.found) return <span className="mark mark-missing">not in the source</span>;
  const g = mark.grounding ? GROUNDING[mark.grounding] : null;
  return (
    <span className="mark">
      <span className="mark-model">from the model · {Math.round(mark.confidence * 100)}%</span>
      {g && <Chip tone={g.tone} title={g.help}>{g.label}</Chip>}
      {mark.source_text && (
        onOpen ? <button type="button" className="src-link" onClick={() => onOpen(mark, label)}>
                   {mark.page ? <span className="page-tag">p.{mark.page}</span> : null} <q>{mark.source_text}</q>
                 </button>
               : <q>{mark.source_text}</q>
      )}
    </span>
  );
}

export function POForm({ initial, initialNewVendor = null, draftId = null, marks = {}, vendorHint, onOpenSource, onSaved, onDirtyChange }: POFormProps) {
  const [po, setPo] = useState<POInput>(() => ({ ...emptyPO(), ...initial, lines: initial?.lines ? [...initial.lines] : [] }));
  const [newVendor, setNewVendor] = useState<NewVendorInput | null>(initialNewVendor);
  const [vendors, setVendors] = useState<Vendor[]>([]);
  const [issues, setIssues] = useState<POIssue[]>([]);
  const [canSave, setCanSave] = useState(false);
  const [linesSum, setLinesSum] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const seq = useRef(0);

  useEffect(() => { listVendors().then((r) => setVendors(r.vendors)).catch(() => setVendors([])); }, []);
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

  // Validate as the person types (debounced). Nothing is saved by this.
  useEffect(() => {
    const mine = ++seq.current;
    const t = window.setTimeout(() => {
      validatePO(po, newVendor).then((v) => {
        if (mine !== seq.current) return;
        setIssues(v.issues);
        setCanSave(v.can_save);
        setLinesSum(v.lines_sum);
      }).catch(() => { if (mine === seq.current) setCanSave(false); });
    }, 300);
    return () => window.clearTimeout(t);
  }, [po, newVendor]);

  const byField = useMemo(() => {
    const m: Record<string, POIssue[]> = {};
    for (const i of issues) (m[i.field] ??= []).push(i);
    return m;
  }, [issues]);
  const at = (f: string) => byField[f] ?? [];
  const lineIssues = (i: number) => issues.filter((x) => x.field === `lines[${i}]` || x.field.startsWith(`lines[${i}].`));
  const hasError = (f: string) => at(f).some((i) => i.level === "error");

  const set = <K extends keyof POInput>(k: K, v: POInput[K]) => { setPo((p) => ({ ...p, [k]: v })); setDirty(true); };
  const setLine = (i: number, k: keyof POLineInput, v: string) => {
    setPo((p) => ({ ...p, lines: p.lines.map((l, j) => (j === i ? { ...l, [k]: v } : l)) }));
    setDirty(true);
  };

  async function save() {
    setSaving(true);
    setSaveError(null);
    try {
      const r = await savePO(po, newVendor, draftId);
      setDirty(false);
      onDirtyChange?.(false);
      onSaved(r.po_id);
    } catch (e) {
      if (e instanceof SaveRefused) {
        setSaveError(e.message);
        if (e.issues.length) setIssues(e.issues);
      } else {
        setSaveError("Could not reach the API.");
      }
      setSaving(false);
    }
  }

  const errors = issues.filter((i) => i.level === "error").length;
  const warnings = issues.filter((i) => i.level === "warning").length;
  const sumMismatch = issues.some((i) => i.code === "lines_sum");

  return (
    <form className="po-form" onSubmit={(e) => { e.preventDefault(); if (canSave && !saving) save(); }} noValidate>
      <div className="form-grid">
        <label className={`fld ${hasError("po_number") ? "bad" : ""}`}>
          <span className="fld-label">PO number <span className="req">required</span></span>
          <input value={po.po_number} onChange={(e) => set("po_number", e.target.value)} autoComplete="off" />
          <Mark mark={marks.po_number} label="PO number" onOpen={onOpenSource} />
          <IssueList issues={at("po_number")} />
        </label>

        <div className={`fld fld-wide ${hasError("vendor") ? "bad" : ""}`}>
          <span className="fld-label">Vendor <span className="req">required</span></span>
          {newVendor === null ? (
            <div className="row">
              <select aria-label="Vendor" value={po.vendor_id ?? ""} onChange={(e) => set("vendor_id", e.target.value ? Number(e.target.value) : null)}>
                <option value="">Choose a vendor…</option>
                {vendors.map((v) => <option key={v.id} value={v.id}>{v.name}{v.status !== "approved" ? ` (${v.status})` : ""}</option>)}
              </select>
              <button type="button" className="btn-ghost" onClick={() => { setNewVendor({ name: "", tax_id: "", country: "" }); setDirty(true); }}>
                New vendor…
              </button>
            </div>
          ) : (
            <div className="new-vendor">
              <div className="row">
                <input aria-label="New vendor name" placeholder="Vendor name" value={newVendor.name}
                       onChange={(e) => { setNewVendor({ ...newVendor, name: e.target.value }); setDirty(true); }} />
                <input aria-label="New vendor tax ID" placeholder="Tax ID (optional)" value={newVendor.tax_id}
                       onChange={(e) => { setNewVendor({ ...newVendor, tax_id: e.target.value }); setDirty(true); }} />
                <input aria-label="New vendor country" placeholder="Country (optional)" value={newVendor.country}
                       onChange={(e) => { setNewVendor({ ...newVendor, country: e.target.value }); setDirty(true); }} />
              </div>
              <p className="hint">Created with status <strong>new</strong>: its invoices go to review until someone approves the vendor.
                {" "}<button type="button" className="linkish" onClick={() => { setNewVendor(null); setDirty(true); }}>Pick an existing vendor instead</button></p>
            </div>
          )}
          {vendorHint && <p className="hint">{vendorHint}</p>}
          <Mark mark={marks.vendor} label="Vendor" onOpen={onOpenSource} />
          <IssueList issues={at("vendor")} />
        </div>

        <label className={`fld ${hasError("currency") ? "bad" : ""}`}>
          <span className="fld-label">Currency <span className="req">required</span></span>
          <input value={po.currency} onChange={(e) => set("currency", e.target.value.toUpperCase())} maxLength={3} placeholder="e.g. USD"
                 autoComplete="off" />
          <Mark mark={marks.currency} label="Currency" onOpen={onOpenSource} />
          <IssueList issues={at("currency")} />
        </label>

        <label className={`fld ${hasError("total") ? "bad" : ""}`}>
          <span className="fld-label">Total <span className="req">required</span></span>
          <input value={po.total} onChange={(e) => set("total", e.target.value)} inputMode="decimal" placeholder="0.00" autoComplete="off" />
          <Mark mark={marks.total} label="Total" onOpen={onOpenSource} />
          <IssueList issues={at("total")} />
          {sumMismatch && linesSum !== null && (
            <button type="button" className="btn-ghost small-btn" onClick={() => set("total", linesSum)}>Use the sum of the lines ({linesSum})</button>
          )}
        </label>

        <label className={`fld ${hasError("issued_date") ? "bad" : ""}`}>
          <span className="fld-label">Issue date <span className="opt">optional</span></span>
          <input type="date" value={po.issued_date} onChange={(e) => set("issued_date", e.target.value)} />
          <Mark mark={marks.issued_date} label="Issue date" onOpen={onOpenSource} />
          <IssueList issues={at("issued_date")} />
        </label>
      </div>

      <div className="lines-editor">
        <div className="section-head">
          <h3>Lines <span className="opt">optional</span></h3>
          <button type="button" className="btn-ghost" onClick={() => { set("lines", [...po.lines, { ...EMPTY_LINE }]); }}>Add line</button>
        </div>
        {po.lines.length === 0 ? <p className="dim">No lines. Matching works best when the lines carry the product text printed on the invoices.</p> : (
          <div className="table-wrap">
            <table className="table line-table">
              <thead><tr><th>#</th><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th><th /></tr></thead>
              <tbody>
                {po.lines.map((l, i) => (
                  <tr key={i} className={lineIssues(i).some((x) => x.level === "error") ? "bad" : undefined}>
                    <td className="dim">{i + 1}</td>
                    <td>
                      <input aria-label={`Line ${i + 1} description`} value={l.description} onChange={(e) => setLine(i, "description", e.target.value)} />
                      <Mark mark={marks[`lines[${i}]`]} label={`Line ${i + 1}`} onOpen={onOpenSource} />
                      <IssueList issues={lineIssues(i)} />
                    </td>
                    <td><input aria-label={`Line ${i + 1} quantity`} className="num" inputMode="decimal" value={l.quantity} onChange={(e) => setLine(i, "quantity", e.target.value)} /></td>
                    <td><input aria-label={`Line ${i + 1} unit price`} className="num" inputMode="decimal" value={l.unit_price} onChange={(e) => setLine(i, "unit_price", e.target.value)} /></td>
                    <td><input aria-label={`Line ${i + 1} amount`} className="num" inputMode="decimal" value={l.amount} onChange={(e) => setLine(i, "amount", e.target.value)} /></td>
                    <td><button type="button" className="btn-ghost" aria-label={`Remove line ${i + 1}`} onClick={() => set("lines", po.lines.filter((_, j) => j !== i))}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="form-foot">
        <div className="dim">
          {errors > 0 ? `${errors} problem${errors === 1 ? "" : "s"} to fix before saving.` : warnings > 0 ? `${warnings} warning${warnings === 1 ? "" : "s"}: check them, then save.` : "Ready to save."}
          {draftId && " Nothing has been saved yet: this form holds the model's draft until you save it."}
        </div>
        {saveError && <p className="error" role="alert">{saveError}</p>}
        <button type="submit" className="btn" disabled={!canSave || saving}>{saving ? "Saving…" : "Save purchase order"}</button>
      </div>
    </form>
  );
}
