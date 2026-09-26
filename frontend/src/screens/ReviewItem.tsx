import { useCallback, useEffect, useMemo, useState } from "react";
import { ApiError, approveItem, getReviewItem, listReview, rejectItem } from "../api";
import { checkChoices, initialChoices } from "../allocation";
import { Chip } from "../components/common";
import { ReasonItem } from "../components/Result";
import { DECISION, money } from "../format";
import { plainReason, reviewReasons } from "../reasons";
import { linkProps } from "../router";
import type { AllocRowView, ApprovePreview, ApproveResult, LineChoice, NeedsInput, ReviewDetail } from "../types";

function rowTarget(r: AllocRowView): string {
  if (r.kind === "remainder") return r.label ?? "the PO total";
  return r.po_line_no ? `PO line ${r.po_line_no}` : "the PO total (no specific line)";
}

function AllocationTable({ rows, currency }: { rows: AllocRowView[]; currency: string | null }) {
  if (rows.length === 0) return null;
  return (
    <div className="table-wrap">
      <table className="table">
        <thead><tr><th>From</th><th>To</th><th className="num">Amount</th><th>How</th></tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>{r.invoice_line_no ? `Invoice line ${r.invoice_line_no}` : <span className="dim">not on a line</span>}</td>
              <td>{rowTarget(r)}</td>
              <td className="num">{money(r.amount, currency)}</td>
              <td className="dim">{r.matched_by === "auto" ? "automatic" : "your choice"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LineCard({ n, choice, onChoose, check, serverProblem, currency }: {
  n: NeedsInput; choice: LineChoice | undefined; onChoose: (c: LineChoice) => void; check: { ok: boolean; message: string | null } | undefined;
  serverProblem?: string; currency: string | null;
}) {
  const name = `line-${n.invoice_line_id}`;
  const others = n.other_lines;
  const selectedOther = choice?.target === "po_line" && others.some((o) => o.po_line_id === choice.po_line_id) ? String(choice.po_line_id) : "";
  return (
    <fieldset className={`line-card ${check && !check.ok ? "bad" : ""}`}>
      <legend>
        Invoice line {n.invoice_line_no}: <strong>{n.description ?? "(no description)"}</strong>
        {n.quantity && <> · qty {n.quantity}</>} · {money(n.amount, currency)}
      </legend>
      <p className="dim small">Needs your choice: {n.why}.</p>
      <div className="options" role="radiogroup" aria-label={`Allocation for invoice line ${n.invoice_line_no}`}>
        {n.candidates.map((o, i) => (
          <label key={o.po_line_id} className={`option ${o.fits ? "" : "no-fit"}`}>
            <input type="radio" name={name} disabled={!o.fits} checked={choice?.target === "po_line" && choice.po_line_id === o.po_line_id}
                   onChange={() => onChoose({ invoice_line_id: n.invoice_line_id, target: "po_line", po_line_id: o.po_line_id })} />
            <span className="option-text">
              <span>PO line {o.po_line_no}: {o.description ?? "(no description)"} {i === 0 && <span className="tag">best match</span>}</span>
              <span className="dim small">
                score {o.score?.toFixed(2)} · {o.remaining_amount ? `${money(o.remaining_amount, currency)} remaining` : "no amount on this line"}
                {!o.fits && (o.code === "po_line_has_no_amount" ? " · cannot be checked" :
                  ` · does not fit (${o.remaining} remaining + ${o.allowance} allowance)`)}
              </span>
            </span>
          </label>
        ))}
        {others.length > 0 && (
          <label className="option">
            <input type="radio" name={name} checked={selectedOther !== ""} onChange={() => {
              const first = others.find((o) => o.fits);
              if (first) onChoose({ invoice_line_id: n.invoice_line_id, target: "po_line", po_line_id: first.po_line_id });
            }} disabled={!others.some((o) => o.fits)} />
            <span className="option-text">
              <span>Another line of this PO</span>
              <select aria-label={`Another PO line for invoice line ${n.invoice_line_no}`} value={selectedOther}
                      onChange={(e) => onChoose({ invoice_line_id: n.invoice_line_id, target: "po_line", po_line_id: Number(e.target.value) })}>
                <option value="" disabled>Choose a line…</option>
                {others.map((o) => (
                  <option key={o.po_line_id} value={o.po_line_id} disabled={!o.fits}>
                    Line {o.po_line_no}: {o.description ?? "(no description)"} · {o.remaining_amount ?? "no amount"} remaining{o.fits ? "" : " (does not fit)"}
                  </option>
                ))}
              </select>
            </span>
          </label>
        )}
        <label className="option">
          <input type="radio" name={name} checked={choice?.target === "unassigned"}
                 onChange={() => onChoose({ invoice_line_id: n.invoice_line_id, target: "unassigned" })} />
          <span className="option-text">
            <span>No specific line: deduct from the PO total</span>
            <span className="dim small">Counted against the PO's balance and shown on the PO page as "consumed, not assigned to a line".</span>
          </span>
        </label>
      </div>
      {(serverProblem || (check && !check.ok && check.message)) && <p className="issue issue-error">{serverProblem ?? check?.message}</p>}
    </fieldset>
  );
}

function ApprovePanel({ detail, preview, onDone, setPreview }: {
  detail: ReviewDetail; preview: ApprovePreview; onDone: (r: ApproveResult) => void; setPreview: (p: ApprovePreview, message: string) => void;
}) {
  const [choices, setChoices] = useState<Record<number, LineChoice>>(() => initialChoices(preview.needs_input));
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [problems, setProblems] = useState<Record<number, string>>({});
  const currency = detail.invoice?.currency ?? null;
  const checks = useMemo(() => preview.tolerance ? checkChoices(preview.needs_input, choices, preview.tolerance) : {}, [preview, choices]);
  const allOk = preview.needs_input.every((n) => checks[n.invoice_line_id]?.ok);

  async function submit() {
    setBusy(true); setError(null); setProblems({});
    const allocations = preview.needs_input.map((n) => choices[n.invoice_line_id]).filter(Boolean);
    try {
      const r = await approveItem(detail.item.id, preview.state_token, allocations, note);
      if (r.ok) { onDone(r.body as ApproveResult); return; }
      if (r.status === 409 && r.body.preview) { setPreview(r.body.preview, r.body.message ?? "The numbers changed."); return; }
      if (r.body.error === "allocation_invalid" && r.body.problems) {
        setProblems(Object.fromEntries(r.body.problems.map((p) => [p.invoice_line_id, p.message])));
      }
      if (r.body.error === "allocation_required" && r.body.needs_input) {
        setPreview({ ...preview, needs_input: r.body.needs_input }, r.body.message ?? "Some lines need a choice.");
      }
      setError(r.body.message ?? `The server answered ${r.status}.`);
    } catch {
      setError("Could not reach the API.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="section action-panel" aria-labelledby="approve-h">
      <h2 id="approve-h">Approve</h2>
      {preview.po && (
        <div className="stats compact">
          <div className="stat"><div className="label">Commit to {preview.po.po_number}</div><div className="stat-value num">{money(preview.commit_amount, currency)}</div></div>
          <div className="stat"><div className="label">PO balance</div>
            <div className="stat-value num">{money(preview.po.balance_before, currency)} <span aria-hidden="true">→</span><span className="sr">to</span> {money(preview.po.balance_after, currency)}</div></div>
        </div>
      )}
      {preview.warnings.map((w, i) => <p key={i} className="warn">{w}</p>)}
      {preview.automatic.length > 0 && (<>
        <h3 className="sub">Allocated automatically</h3>
        <AllocationTable rows={preview.automatic} currency={currency} />
      </>)}
      {preview.needs_input.length > 0 && (<>
        <h3 className="sub">Your choice for {preview.needs_input.length} line{preview.needs_input.length === 1 ? "" : "s"}</h3>
        {preview.needs_input.map((n) => (
          <LineCard key={n.invoice_line_id} n={n} choice={choices[n.invoice_line_id]} currency={currency} check={checks[n.invoice_line_id]}
                    serverProblem={problems[n.invoice_line_id]}
                    onChoose={(c) => setChoices((prev) => ({ ...prev, [n.invoice_line_id]: c }))} />
        ))}
      </>)}
      {preview.remainder && <p className="dim">{money(preview.remainder.amount, currency)} ({preview.remainder.label}) goes to the PO total.</p>}
      {preview.notes.map((n, i) => <p key={i} className="dim small">{n}</p>)}
      <label className="fld">
        <span className="fld-label">Note <span className="opt">optional</span></span>
        <input value={note} maxLength={500} onChange={(e) => setNote(e.target.value)} placeholder="e.g. checked against the delivery note" />
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <div><button type="button" className="btn" disabled={busy || !allOk} onClick={submit}>
        {busy ? "Approving…" : "Confirm approval"}</button></div>
    </section>
  );
}

function RejectPanel({ id, onDone }: { id: number; onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (!open) return <div><button type="button" className="btn-ghost" onClick={() => setOpen(true)}>Reject…</button></div>;
  return (
    <section className="section action-panel" aria-labelledby="reject-h">
      <h2 id="reject-h">Reject</h2>
      <p className="notice">No ledger entry and no allocation will be written. No email is sent or drafted.</p>
      <label className="fld">
        <span className="fld-label">Reason <span className="opt">optional</span></span>
        <textarea rows={2} maxLength={500} value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <div className="row">
        <button type="button" className="btn" disabled={busy} onClick={async () => {
          setBusy(true);
          const r = await rejectItem(id, reason).catch(() => null);
          setBusy(false);
          if (r?.ok) onDone(); else setError(r?.body.message ?? "Could not reach the API.");
        }}>{busy ? "Rejecting…" : "Confirm rejection"}</button>
        <button type="button" className="btn-ghost" onClick={() => setOpen(false)}>Cancel</button>
      </div>
    </section>
  );
}

export function ReviewItemScreen({ id }: { id: number }) {
  const [detail, setDetail] = useState<ReviewDetail | null>(null);
  const [preview, setPreviewState] = useState<ApprovePreview | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [done, setDone] = useState<{ kind: "approved"; result: ApproveResult } | { kind: "rejected" } | null>(null);
  const [missing, setMissing] = useState(false);
  const [nextId, setNextId] = useState<number | null>(null);
  const [version, setVersion] = useState(0);

  const load = useCallback(() => {
    getReviewItem(id).then((d) => { setDetail(d); setPreviewState(d.approve); }).catch((e) => {
      if (e instanceof ApiError && e.status === 404) setMissing(true);
    });
  }, [id]);
  useEffect(load, [load]);
  useEffect(() => {
    if (!done) return;
    listReview("open").then((r) => setNextId(r.items.find((i) => i.id !== id)?.id ?? null)).catch(() => setNextId(null));
  }, [done, id]);

  if (missing) return <div className="empty"><h1>Review item not found</h1><p><a {...linkProps("/review")}>Back to the queue</a></p></div>;
  if (!detail || !preview) return <p className="dim">Loading…</p>;
  const cur = detail.invoice?.currency ?? null;
  const ex = detail.run?.explanation;
  const reasons = ex ? ex.reasons.map((r) => plainReason(r.text, r.facts)) : reviewReasons(detail.item.reason);
  const resolved = detail.item.status === "resolved" || done !== null;

  return (
    <div className="review-item">
      <div className="run-title">
        <a {...linkProps("/review")} className="back">← Review queue</a>
        <h1>Invoice {detail.invoice?.invoice_number ?? "(no number)"} · {money(detail.invoice?.total, cur)}</h1>
        <div className="run-sub">
          <span>{detail.run?.source_file}</span>
          {detail.run?.decision && <Chip tone={DECISION[detail.run.decision].tone}>system: {DECISION[detail.run.decision].label}</Chip>}
          {detail.item.status === "resolved" && <Chip tone={detail.item.resolution === "approved" ? "pass" : "fail"}>{detail.item.resolution}</Chip>}
          {detail.run && <a {...linkProps(`/runs/${detail.run.id}`)}>Open the full run</a>}
        </div>
      </div>
      <div className="run-grid">
        <aside className="run-side">
          <section className="section">
            <h2>Why it was held for review</h2>
            <ul className="reasons">{reasons.map((r, i) => <ReasonItem key={i} reason={r} />)}</ul>
          </section>
        </aside>
        <div className="run-main">
          {notice && <p className="warn" role="status">{notice}</p>}
          {done?.kind === "approved" && (
            <section className="section done" role="status">
              <h2>Approved</h2>
              <p>{money(done.result.amount, cur)} committed to <a {...linkProps(`/pos/${done.result.po.id}`)}>{done.result.po.po_number}</a>;
                balance {money(done.result.po.balance_before, cur)} → {money(done.result.po.balance_after, cur)} ({done.result.po.status.replace("_", " ")}).</p>
              <AllocationTable rows={done.result.allocations} currency={cur} />
              {nextId ? <a className="btn" {...linkProps(`/review/${nextId}`)}>Next item</a> : <a {...linkProps("/review")}>Back to the queue</a>}
            </section>
          )}
          {done?.kind === "rejected" && (
            <section className="section done" role="status">
              <h2>Rejected</h2>
              <p>No ledger entry and no allocation were written.</p>
              {nextId ? <a className="btn" {...linkProps(`/review/${nextId}`)}>Next item</a> : <a {...linkProps("/review")}>Back to the queue</a>}
            </section>
          )}
          {!resolved && !preview.possible && (
            <section className="section action-panel">
              <h2>Approve</h2>
              <p>This invoice cannot be approved:</p>
              <ul>{preview.blocked_by.map((b) => <li key={b.code}>{b.message}</li>)}</ul>
            </section>
          )}
          {!resolved && preview.possible && (
            <ApprovePanel key={`${preview.state_token}-${version}`} detail={detail} preview={preview}
                          onDone={(result) => setDone({ kind: "approved", result })}
                          setPreview={(p, message) => { setPreviewState(p); setNotice(message); setVersion((v) => v + 1); }} />
          )}
          {!resolved && <RejectPanel id={detail.item.id} onDone={() => setDone({ kind: "rejected" })} />}
          {detail.item.status === "resolved" && !done && <p className="dim">This item was resolved ({detail.item.resolution}).</p>}
        </div>
      </div>
    </div>
  );
}
