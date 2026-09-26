import { useEffect, useReducer, useState } from "react";
import { ApiError, getRun, isRunView, streamRun } from "../api";
import { Chip } from "../components/common";
import { ResultView } from "../components/Result";
import { StageTimeline } from "../components/StageTimeline";
import { DECISION, OUTCOME, STAGE_LABEL } from "../format";
import { linkProps } from "../router";
import { initialRunState, liveRules, runReducer, type RunState } from "../runState";
import { STAGES, type Decision, type RunView } from "../types";

function LivePanel({ state }: { state: RunState }) {
  const current = STAGES.find((s) => state.stages[s].status === "running");
  const rules = liveRules(state);
  return (
    <div className="live" aria-live="polite">
      <div className="live-head">
        <span className="spinner" aria-hidden="true" />
        <div>
          <div className="live-title">
            {state.waiting === "queued" ? "Waiting in the queue" : current ? `${STAGE_LABEL[current]}…` : "Starting…"}
          </div>
          <div className="dim">Each stage is written to the audit log as it finishes; this view reads that log.</div>
        </div>
      </div>
      {state.decision && (
        <p>Decision so far: <Chip tone={DECISION[state.decision].tone}>{DECISION[state.decision].label}</Chip> (the explanation and actions follow).</p>
      )}
      {rules.length > 0 && (
        <ul className="live-rule-list">
          {rules.map((e) => (
            <li key={e.seq}><Chip tone={OUTCOME[e.outcome].tone}>{OUTCOME[e.outcome].label}</Chip> {e.message}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function RunScreen({ runId }: { runId: string }) {
  const [state, dispatch] = useReducer(runReducer, undefined, initialRunState);
  const [view, setView] = useState<RunView | null>(null);
  const [missing, setMissing] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    const stop = streamRun(runId, {
      onAudit: (event) => dispatch({ type: "audit", event, now: Date.now() }),
      onQueued: (s) => dispatch({ type: "queued", state: s }),
      onEnd: (e) => dispatch({ type: "end", status: e.status, decision: e.decision }),
      onRejected: (r) => dispatch({ type: "rejected", code: r.code, message: r.message }),
      onConnection: (c) => dispatch({ type: "connection", state: c }),
    });
    return stop;
  }, [runId]);

  // The result view is loaded once the stream has ended (and whenever the connection is lost, to tell 404 from a network drop).
  useEffect(() => {
    if (!state.end && state.connection !== "lost") return;
    let stop = false;
    getRun(runId).then((v) => {
      if (stop) return;
      if (isRunView(v)) setView(v);
    }).catch((e) => {
      if (stop) return;
      if (e instanceof ApiError && e.status === 404) setMissing(true);
      else setLoadError("Could not load the result. Is the API running?");
    });
    return () => { stop = true; };
  }, [runId, state.end, state.connection]);

  if (missing) {
    return <div className="empty"><h1>Run not found</h1><p>There is no run {runId}. <a {...linkProps("/invoices")}>Upload an invoice</a></p></div>;
  }

  const file = view?.run.source_file ?? state.sourceFile;
  const decision = (view?.decision ?? state.decision) as Decision | null;
  const status = view?.run.status ?? (state.rejected ? "rejected" : state.end?.status ?? (state.waiting ? "queued" : "running"));
  return (
    <div className="run">
      <div className="run-title">
        <a {...linkProps("/invoices")} className="back">← New invoice</a>
        <h1>{file ?? "Invoice"}</h1>
        <div className="run-sub">
          <span className="mono" title="Run id">{runId.slice(0, 8)}</span>
          {status === "completed" && decision ? <Chip tone={DECISION[decision].tone}>{DECISION[decision].label}</Chip>
            : <Chip tone={status === "failed" || status === "rejected" ? "fail" : "muted"}>{status}</Chip>}
          {state.connection === "connecting" && !state.end && state.eventCount > 0 && <span className="dim">reconnecting…</span>}
        </div>
      </div>

      <div className="run-grid">
        <aside className="run-side">
          <StageTimeline state={state} />
        </aside>
        <div className="run-main">
          {state.rejected && (
            <div className="banner banner-failed" role="alert">
              <div className="banner-icon" aria-hidden="true">✕</div>
              <div><div className="banner-title">Could not start</div><p className="banner-text">{state.rejected.message}</p></div>
            </div>
          )}
          {loadError && <p className="error" role="alert">{loadError}</p>}
          {view ? <ResultView view={view} /> : !state.rejected && <LivePanel state={state} />}
        </div>
      </div>
    </div>
  );
}
