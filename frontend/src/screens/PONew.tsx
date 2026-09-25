import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, draftFromDocument, draftFromText, draftPageUrl } from "../api";
import { Disclosure } from "../components/common";
import { PageViewer, type PageTarget } from "../components/PageViewer";
import { POForm, type FieldMark } from "../components/POForm";
import { usd } from "../format";
import { linkProps, navigate, setLeaveGuard } from "../router";
import type { Health, PODraftView, POInput } from "../types";

type Tab = "form" | "text" | "document";
const DOC_ACCEPT = ".pdf,.png,.jpg,.jpeg,.docx,.xlsx,.csv";

// Leaving with unsaved changes asks first (browser close/refresh and in-app navigation).
export function useUnsavedGuard(dirty: boolean) {
  useEffect(() => {
    if (!dirty) { setLeaveGuard(null); return; }
    const onBefore = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", onBefore);
    setLeaveGuard(() => window.confirm("Leave without saving? The purchase order has not been saved."));
    return () => { window.removeEventListener("beforeunload", onBefore); setLeaveGuard(null); };
  }, [dirty]);
}

// The model's draft -> the form's starting values. Nothing the source did not state is filled in.
export function draftToForm(d: PODraftView): POInput {
  const v = d.values;
  const s = (x: string | null) => x ?? "";
  return {
    po_number: s(v.po_number), vendor_id: d.suggested_vendor_id, currency: s(v.currency), total: s(v.total), issued_date: s(v.issued_date),
    lines: v.lines.map((l) => ({ description: s(l.description), quantity: s(l.quantity), unit_price: s(l.unit_price), amount: s(l.amount) })),
  };
}

function DraftSummary({ d }: { d: PODraftView }) {
  return (
    <div className="draft-summary">
      <p className="notice-strong">
        Drafted by {d.model ?? "the model"} from the {d.source === "text" ? "text you typed" : "document"}
        {d.cost_usd && d.cost_usd !== "0" ? ` (${usd(d.cost_usd)})` : ""}. <strong>Nothing has been saved.</strong> Check every value, fix
        or fill in what is missing, then save.
      </p>
      {d.warnings.length > 0 && <ul className="issues">{d.warnings.map((w, i) => <li key={i} className="issue issue-warning">{w}</li>)}</ul>}
      {d.notes.length > 0 && (
        <Disclosure summary={<span>Notes from the draft ({d.notes.length})</span>}>
          <ul className="small">{d.notes.map((n, i) => <li key={i}>{n}</li>)}</ul>
        </Disclosure>
      )}
    </div>
  );
}

export function PONewScreen({ health }: { health: Health | null }) {
  const [tab, setTab] = useState<Tab>("form");
  const [dirty, setDirty] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<PODraftView | null>(null);
  const [viewer, setViewer] = useState<PageTarget | null>(null);
  const [drag, setDrag] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const offline = health?.mode === "offline";
  useUnsavedGuard(dirty || (draft !== null && draft.status === "ok"));

  const saved = useCallback((id: number) => { setLeaveGuard(null); navigate(`/pos/${id}`); }, []);

  async function run(make: () => Promise<PODraftView>) {
    setBusy(true);
    setError(null);
    setDraft(null);
    try {
      setDraft(await make());
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The draft could not be made. Is the API running?");
    } finally {
      setBusy(false);
    }
  }

  const choose = (t: Tab) => {
    if (t === tab) return;
    if ((dirty || draft) && !window.confirm("Switch and discard what is on this tab?")) return;
    setTab(t); setDraft(null); setError(null); setDirty(false);
  };

  const openSource = (m: FieldMark, label: string) => {
    if (draft && draft.pages.length > 0 && m.page) setViewer({ page: m.page, label, sourceText: m.source_text });
  };

  return (
    <div className="po-new">
      <div className="run-title">
        <a {...linkProps("/pos")} className="back">← Purchase orders</a>
        <h1>New purchase order</h1>
      </div>
      <div className="tabs" role="tablist">
        {([["form", "Form"], ["text", "Describe in text"], ["document", "Upload a document"]] as [Tab, string][]).map(([t, label]) => (
          <button key={t} type="button" role="tab" aria-selected={tab === t} className={`tab ${tab === t ? "active" : ""}`} onClick={() => choose(t)}>
            {label}
          </button>
        ))}
      </div>

      {tab === "form" && <div className="section"><POForm onSaved={saved} onDirtyChange={setDirty} /></div>}

      {tab !== "form" && !draft && (
        <div className="section draft-input">
          {offline ? (
            <p className="warn">Offline mode: no model is available to draft a purchase order. Use the Form tab instead.</p>
          ) : tab === "text" ? (
            <>
              <label className="fld">
                <span className="fld-label">Describe the purchase order</span>
                <textarea rows={7} value={text} onChange={(e) => setText(e.target.value)} maxLength={8000}
                          placeholder="e.g. PO-1234 to Acme Supplies Ltd, issued 2026-02-01: 40 x Widget A at 50.00, total USD 2,000.00" />
              </label>
              <p className="hint">The model drafts the fields from this text; you check them on the form and nothing is saved until you press Save.
                It never fills in what the text does not say.</p>
              <div><button type="button" className="btn" disabled={busy || !text.trim()} onClick={() => run(() => draftFromText(text))}>
                {busy ? "Drafting…" : "Draft the purchase order"}</button></div>
            </>
          ) : (
            <div className={`dropzone ${drag ? "drag" : ""} ${busy ? "busy" : ""}`}
                 onDragOver={(e) => { e.preventDefault(); setDrag(true); }} onDragLeave={() => setDrag(false)}
                 onDrop={(e) => { e.preventDefault(); setDrag(false); const f = e.dataTransfer.files[0]; if (f) run(() => draftFromDocument(f)); }}>
              <div className="drop-icon" aria-hidden="true" />
              <p className="drop-title">{busy ? "Reading the document…" : "Drop one purchase-order document"}</p>
              <p className="drop-sub">PDF, image, Word (.docx), Excel (.xlsx) or CSV · one purchase order per file</p>
              <button type="button" className="btn" disabled={busy} onClick={() => fileInput.current?.click()}>Choose file</button>
              <input ref={fileInput} type="file" accept={DOC_ACCEPT} hidden aria-label="Purchase order document" data-testid="po-file"
                     onChange={(e) => { const f = e.target.files?.[0]; e.target.value = ""; if (f) run(() => draftFromDocument(f)); }} />
            </div>
          )}
          {busy && <p className="dim" aria-live="polite"><span className="spinner inline" aria-hidden="true" /> The model is drafting; this takes a few seconds.</p>}
          {error && <p className="error" role="alert">{error}</p>}
        </div>
      )}

      {draft && draft.status === "failed" && (
        <div className="section">
          <p className="error" role="alert">The draft could not be made: {draft.failure?.message ?? "unknown reason"}. Nothing was saved.</p>
          <div className="row">
            <button type="button" className="btn-ghost" onClick={() => setDraft(null)}>Try again</button>
            <button type="button" className="btn-ghost" onClick={() => { setDraft(null); setTab("form"); }}>Use the empty form</button>
          </div>
        </div>
      )}

      {draft && draft.status === "ok" && (
        <div className="section">
          <DraftSummary d={draft} />
          <POForm key={draft.draft_id} initial={draftToForm(draft)} initialNewVendor={draft.suggested_vendor_id === null ? draft.new_vendor : null}
                  draftId={draft.draft_id} marks={draft.marks} vendorHint={draft.vendor_hint}
                  onOpenSource={draft.pages.length > 0 ? openSource : undefined} onSaved={saved} onDirtyChange={setDirty} />
          <div className="row"><button type="button" className="linkish" onClick={() => { if (window.confirm("Discard this draft?")) setDraft(null); }}>
            Discard the draft and start over</button></div>
        </div>
      )}

      {viewer && draft && (
        <PageViewer runId={draft.draft_id} target={viewer} pages={draft.pages} onClose={() => setViewer(null)}
                    onPage={(n) => setViewer({ ...viewer, page: n })} src={(n) => draftPageUrl(draft.draft_id, n)} />
      )}
    </div>
  );
}
