import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, listRuns, uploadInvoice } from "../api";
import { Chip } from "../components/common";
import { DECISION, usd, when } from "../format";
import { linkProps, navigate } from "../router";
import type { Decision, Health, RunRow } from "../types";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg";

function StatusChip({ run }: { run: RunRow }) {
  if (run.final_decision) {
    const d = DECISION[run.final_decision as Decision];
    return <Chip tone={d.tone}>{d.label}</Chip>;
  }
  const tone = run.status === "failed" || run.status === "rejected" ? "fail" : "muted";
  return <Chip tone={tone}>{run.status}</Chip>;
}

export function UploadScreen({ health }: { health: Health | null }) {
  const [drag, setDrag] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [runs, setRuns] = useState<RunRow[] | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const maxMb = ((health?.max_file_bytes ?? 20 * 1024 * 1024) / 1_048_576).toFixed(0);

  useEffect(() => {
    let stop = false;
    const load = () => listRuns(12).then((r) => !stop && setRuns(r.runs)).catch(() => !stop && setRuns([]));
    load();
    const t = window.setInterval(load, 4000);
    return () => { stop = true; window.clearInterval(t); };
  }, []);

  const send = useCallback(async (file: File | undefined) => {
    if (!file || busy) return;
    setError(null);
    if (health && file.size > health.max_file_bytes) {
      setError(`${file.name} is ${(file.size / 1_048_576).toFixed(1)} MB; the limit is ${maxMb} MB.`);
      return;
    }
    setBusy(true);
    try {
      const { run_id } = await uploadInvoice(file);
      navigate(`/runs/${run_id}`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "The upload failed. Is the API running?");
      setBusy(false);
    }
  }, [busy, health, maxMb]);

  return (
    <div className="upload">
      <div className="intro">
        <h1>Process an invoice</h1>
        <p>
          Drop one vendor invoice. It is read, matched against open purchase orders, checked by every rule, and decided —
          with each step shown as it happens. Nothing is paid or sent automatically.
        </p>
      </div>

      <div
        className={`dropzone ${drag ? "drag" : ""} ${busy ? "busy" : ""}`}
        onDragOver={(e) => { e.preventDefault(); setDrag(true); }}
        onDragLeave={() => setDrag(false)}
        onDrop={(e) => { e.preventDefault(); setDrag(false); send(e.dataTransfer.files[0]); }}
      >
        <div className="drop-icon" aria-hidden="true" />
        <p className="drop-title">{busy ? "Uploading…" : "Drop an invoice here"}</p>
        <p className="drop-sub">PDF, PNG or JPG · up to {maxMb} MB · one file</p>
        <button type="button" className="btn" onClick={() => input.current?.click()} disabled={busy}>Choose file</button>
        <input ref={input} type="file" accept={ACCEPT} hidden onChange={(e) => { send(e.target.files?.[0]); e.target.value = ""; }}
               aria-label="Invoice file" data-testid="file-input" />
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {health?.mode === "live" && <p className="warn">Live mode: each invoice calls the paid API (about $0.03).</p>}

      <section className="recent" aria-labelledby="recent-h">
        <h2 id="recent-h">Recent runs</h2>
        {runs === null ? <p className="dim">Loading…</p> : runs.length === 0 ? <p className="dim">No runs yet.</p> : (
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
