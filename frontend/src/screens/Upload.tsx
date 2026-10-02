import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getPO, getRun, isRunView, listRuns, uploadInvoice } from "../api";
import { Chip } from "../components/common";
import { GmailImport } from "../components/GmailImport";
import { DECISION, usd, when } from "../format";
import { linkProps, navigate } from "../router";
import type { Decision, GmailImportOutcome, Health, RunRow } from "../types";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg";
const POLL_MS = 1500;

// One row per file of a multi-file upload. Each file is its OWN run with its own decision; nothing is shared between them.
export interface BatchItem {
  key: string;
  name: string;
  state: "waiting" | "uploading" | "queued" | "running" | "completed" | "failed" | "rejected";
  runId?: string;
  decision?: Decision | null;
  matchedPo?: string | null;
  error?: string;
  note?: string;                    // e.g. "from Gmail" or "already imported: the earlier run"
}

function StatusChip({ run }: { run: RunRow }) {
  if (run.final_decision) {
    const d = DECISION[run.final_decision as Decision];
    return <Chip tone={d.tone}>{d.label}</Chip>;
  }
  const tone = run.status === "failed" || run.status === "rejected" ? "fail" : "muted";
  return <Chip tone={tone}>{run.status}</Chip>;
}

function ItemState({ item }: { item: BatchItem }) {
  if (item.state === "completed" && item.decision) {
    const d = DECISION[item.decision];
    return <Chip tone={d.tone}>{d.label}</Chip>;
  }
  const tone = item.state === "failed" || item.state === "rejected" ? "fail" : "muted";
  const text = { waiting: "waiting", uploading: "uploading…", queued: "queued", running: "running…", completed: "done", failed: "failed",
                 rejected: "not accepted" }[item.state];
  return <Chip tone={tone}>{text}</Chip>;
}

function usePoContext(): { id: number; number: string | null } | null {
  const [ctx, setCtx] = useState<{ id: number; number: string | null } | null>(() => {
    const id = Number(new URLSearchParams(window.location.search).get("po"));
    return Number.isInteger(id) && id > 0 ? { id, number: null } : null;
  });
  useEffect(() => {
    if (!ctx || ctx.number) return;
    getPO(ctx.id).then((d) => setCtx({ id: ctx.id, number: d.po.po_number })).catch(() => setCtx(null));
  }, [ctx]);
  return ctx;
}

export function UploadScreen({ health }: { health: Health | null }) {
  const [drag, setDrag] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const [batch, setBatch] = useState<BatchItem[]>([]);
  const [sending, setSending] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const po = usePoContext();
  const [decision] = useState(() => {
    const d = new URLSearchParams(window.location.search).get("decision") ?? "";
    return (["approve", "review", "request_info", "reject"] as string[]).includes(d) ? (d as Decision) : null;
  });
  const maxMb = ((health?.max_file_bytes ?? 20 * 1024 * 1024) / 1_048_576).toFixed(0);
  const maxFiles = health?.max_files_per_upload ?? 20;

  useEffect(() => {
    let stop = false;
    const load = () => listRuns(decision ? 50 : 12, decision ?? "").then((r) => !stop && setRuns(r.runs)).catch(() => !stop && setRuns([]));
    load();
    const t = window.setInterval(load, 4000);
    return () => { stop = true; window.clearInterval(t); };
  }, [decision]);

  const update = (key: string, patch: Partial<BatchItem>) => setBatch((b) => b.map((x) => (x.key === key ? { ...x, ...patch } : x)));

  // Follow the unfinished runs of this upload (polling: one EventSource per file would exhaust the browser's connections).
  const unfinished = batch.filter((b) => b.runId && (b.state === "queued" || b.state === "running")).map((b) => `${b.key}:${b.runId}`).join(",");
  useEffect(() => {
    if (!unfinished) return;
    let stop = false;
    const t = window.setInterval(() => {
      for (const pair of unfinished.split(",")) {
        const [key, runId] = pair.split(":");
        getRun(runId).then((v) => {
          if (stop) return;
          if (isRunView(v)) {
            const st = v.run.status;
            update(key, { state: st === "completed" ? "completed" : st === "failed" ? "failed" : "running", decision: v.decision,
                          matchedPo: v.match.matched_po });
          } else {
            update(key, v.run.status === "rejected" ? { state: "rejected", error: v.rejection?.message } : { state: v.run.status });
          }
        }).catch(() => { /* keep polling */ });
      }
    }, POLL_MS);
    return () => { stop = true; window.clearInterval(t); };
  }, [unfinished]);

  // Gmail imports join this list exactly like uploaded files (queued runs are followed by the same polling).
  const addImported = useCallback((outcomes: GmailImportOutcome[]) => {
    const stamp = Date.now();
    const notes = { queued: "from Gmail", already_imported: "already imported: the earlier run",
                    already_processed: "same file already processed: that run", refused: "" };
    const items: BatchItem[] = outcomes.map((o, i) => (o.status === "refused" || !o.run_id
      ? { key: `g${stamp}-${i}`, name: o.filename, state: "rejected", error: o.reason ?? "not imported" }
      : { key: `g${stamp}-${i}`, name: o.filename, state: "queued", runId: o.run_id, note: notes[o.status] }));
    setBatch((b) => [...items, ...b]);
  }, []);

  const send = useCallback(async (list: FileList | File[] | null | undefined) => {
    const files = Array.from(list ?? []);
    if (files.length === 0 || sending) return;
    setError(null);
    if (files.length > maxFiles) {
      setError(`At most ${maxFiles} files per upload (you chose ${files.length}).`);
      return;
    }
    if (files.length === 1 && !po) {                                     // one file: straight to its live run view, as before
      const file = files[0];
      if (health && file.size > health.max_file_bytes) {
        setError(`${file.name} is ${(file.size / 1_048_576).toFixed(1)} MB; the limit is ${maxMb} MB.`);
        return;
      }
      setSending(true);
      try {
        const { run_id } = await uploadInvoice(file);
        navigate(`/runs/${run_id}`);
      } catch (e) {
        setError(e instanceof ApiError ? e.message : "The upload failed. Is the API running?");
        setSending(false);
      }
      return;
    }
    const stamp = Date.now();
    const items: BatchItem[] = files.map((f, i) => ({ key: `${stamp}-${i}`, name: f.name, state: "waiting" }));
    setBatch((b) => [...items, ...b]);
    setSending(true);
    for (let i = 0; i < files.length; i++) {                              // one request per file, in the order chosen
      const f = files[i];
      const key = items[i].key;
      if (health && f.size > health.max_file_bytes) {
        update(key, { state: "rejected", error: `larger than ${maxMb} MB` });
        continue;
      }
      update(key, { state: "uploading" });
      try {
        const { run_id } = await uploadInvoice(f);
        update(key, { state: "queued", runId: run_id });
      } catch (e) {
        update(key, { state: "rejected", error: e instanceof ApiError ? e.message : "the upload failed" });
      }
    }
    setSending(false);
  }, [sending, maxFiles, health, maxMb, po]);

  return (
    <div className="upload">
      <div className="intro">
        <h1>Process invoices</h1>
        <p>
          Drop one or more vendor invoices. Each file is read, matched against open purchase orders, checked by every rule and decided
          on its own, with each step shown as it happens. Nothing is paid or sent automatically.
        </p>
      </div>

      <GmailImport onImported={addImported} />

      {po && (
        <div className="po-context" role="note">
          Uploading from purchase order <a {...linkProps(`/pos/${po.id}`)}>{po.number ?? `#${po.id}`}</a>. Matching is automatic: each
          invoice is matched on its own facts, and one that matches another PO, or none, is still processed and shown with its own result.
        </div>
      )}

      <div
        className={`dropzone ${drag ? "drag" : ""} ${sending ? "busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files); }}
      >
        <div className="drop-icon" aria-hidden="true" />
        <p className="drop-title">{sending ? "Uploading…" : "Drop invoices here"}</p>
        <p className="drop-sub">PDF, PNG or JPG · up to {maxMb} MB each · up to {maxFiles} files at once</p>
        <button type="button" className="btn" onClick={() => input.current?.click()} disabled={sending}>Choose files</button>
        <input ref={input} type="file" accept={ACCEPT} multiple hidden onChange={(e) => { send(e.target.files); e.target.value = ""; }}
               aria-label="Invoice files" data-testid="file-input" />
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {health?.mode === "live" && <p className="warn">Live mode: each invoice calls the paid API (about $0.03).</p>}

      {batch.length > 0 && (
        <section className="recent" aria-labelledby="batch-h">
          <h2 id="batch-h">This upload</h2>
          <p className="dim small">Runs go through the queue one at a time. Each has its own decision.</p>
          <ul className="run-list batch-list">
            {batch.map((b) => (
              <li key={b.key}>
                {b.runId ? (
                  <a {...linkProps(`/runs/${b.runId}`)} className="run-link">
                    <span className="run-file">{b.name}</span>
                    <span className="run-meta">{b.note ? `${b.note} · ` : ""}{b.state === "completed" ? (b.matchedPo ? `matched ${b.matchedPo}` : "no PO matched")
                      : b.state === "failed" ? "the run failed" : ""}{po && b.state === "completed" && po.number && b.matchedPo !== po.number ? " (not this PO)" : ""}</span>
                    <ItemState item={b} />
                  </a>
                ) : (
                  <div className="run-link">
                    <span className="run-file">{b.name}</span>
                    <span className="run-meta">{b.error ?? ""}</span>
                    <ItemState item={b} />
                  </div>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="recent" aria-labelledby="recent-h">
        <h2 id="recent-h">{decision ? `Runs the system decided: ${DECISION[decision].label}` : "Recent runs"}</h2>
        {decision && (
          <p className="filter-note">Filtered by the system's decision at run time. <a {...linkProps("/invoices")}>Show all runs</a></p>
        )}
        {runs === null ? <p className="dim">Loading…</p> : runs.length === 0 ? <p className="dim">{decision ? "No run has this decision." : "No runs yet."}</p> : (
          <ul className="run-list">
            {runs.map((r) => (
              <li key={r.id}>
                <a {...linkProps(`/runs/${r.id}`)} className="run-link">
                  <span className="run-file">{r.source_file ?? r.id}</span>
                  <span className="run-meta">{when(r.started_at)}{r.cost_usd ? ` · ${usd(r.cost_usd)}` : ""}</span>
                  <StatusChip run={r} />
                </a>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
