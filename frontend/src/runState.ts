// Pure reducer: the live event stream -> what the run view shows for each stage. No I/O, no clock (the caller passes `now`).
import { STAGES, type AuditEvent, type Decision, type StageName, type StageStatus } from "./types";

export interface StageState {
  status: StageStatus;
  durationMs: number | null;
  summary: Record<string, unknown>;
  events: AuditEvent[];
  startedAt: number | null;       // client clock when "running" was first seen, for the elapsed-time display
}

export interface RunState {
  stages: Record<StageName, StageState>;
  lastSeq: number;
  waiting: string | null;         // queued | running, before the run row exists
  connection: "connecting" | "open" | "lost";
  decision: Decision | null;
  end: { status: string; decision: string | null } | null;
  rejected: { code: string; message: string } | null;
  error: string | null;
  sourceFile: string | null;
  eventCount: number;
}

export type RunAction =
  | { type: "audit"; event: AuditEvent; now: number }
  | { type: "queued"; state: string }
  | { type: "end"; status: string; decision: string | null }
  | { type: "rejected"; code: string; message: string }
  | { type: "connection"; state: RunState["connection"] }
  | { type: "reset" };

const isStage = (s: unknown): s is StageName => typeof s === "string" && (STAGES as string[]).includes(s);

export function initialRunState(): RunState {
  const stages = {} as Record<StageName, StageState>;
  for (const s of STAGES) stages[s] = { status: "waiting", durationMs: null, summary: {}, events: [], startedAt: null };
  return { stages, lastSeq: -1, waiting: null, connection: "connecting", decision: null, end: null, rejected: null, error: null,
           sourceFile: null, eventCount: 0 };
}

function setStage(state: RunState, name: StageName, patch: Partial<StageState>): RunState {
  return { ...state, stages: { ...state.stages, [name]: { ...state.stages[name], ...patch } } };
}

function failRunning(state: RunState): RunState {
  let next = state;
  for (const s of STAGES) if (next.stages[s].status === "running") next = setStage(next, s, { status: "failed" });
  return next;
}

function applyAudit(state: RunState, e: AuditEvent, now: number): RunState {
  if (e.seq <= state.lastSeq) return state;                // a resumed stream may repeat what we already have
  let next: RunState = { ...state, lastSeq: e.seq, waiting: null, eventCount: state.eventCount + 1 };
  const d = e.detail ?? {};
  if (e.stage === "pipeline") {
    const stage = d.stage;
    if (e.event_type === "stage_started" && isStage(stage)) {
      const cur = next.stages[stage];
      if (cur.status === "waiting") next = setStage(next, stage, { status: "running", startedAt: now });
    } else if (e.event_type === "stage_completed" && isStage(stage)) {
      next = setStage(next, stage, { status: (d.status as StageStatus) ?? "ok", durationMs: (d.duration_ms as number) ?? null,
                                     summary: (d.summary as Record<string, unknown>) ?? {} });
    } else if (e.event_type === "run_started") {
      next = { ...next, sourceFile: (d.source_file as string) ?? null };
    } else if (e.event_type === "pipeline_error") {
      next = failRunning({ ...next, error: e.message });
    }
    return next;
  }
  if (isStage(e.stage)) {
    next = setStage(next, e.stage, { events: [...next.stages[e.stage].events, e] });
    if (e.event_type === "decision_made") next = { ...next, decision: (d.decision as Decision) ?? null };
    if (e.event_type === "decision_escalated") next = { ...next, decision: (d.to as Decision) ?? next.decision };
  }
  return next;
}

export function runReducer(state: RunState, a: RunAction): RunState {
  switch (a.type) {
    case "audit":
      return applyAudit(state, a.event, a.now);
    case "queued":
      return { ...state, waiting: a.state };
    case "end": {
      const next = { ...state, end: { status: a.status, decision: a.decision }, waiting: null,
                     decision: (a.decision as Decision | null) ?? state.decision };
      return a.status === "completed" ? next : failRunning(next);
    }
    case "rejected":
      return { ...state, rejected: { code: a.code, message: a.message }, waiting: null };
    case "connection":
      return { ...state, connection: a.state };
    case "reset":
      return initialRunState();
  }
}

// Rule results as they are written (validate stage), for the live view.
export function liveRules(state: RunState): AuditEvent[] {
  return state.stages.validate.events.filter((e) => e.event_type === "rule_evaluated" || e.event_type === "engine_floor");
}

export const isFinished = (s: RunState) => s.end !== null || s.rejected !== null;
