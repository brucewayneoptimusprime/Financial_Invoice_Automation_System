import { describe, expect, it } from "vitest";
import { initialRunState, isFinished, liveRules, runReducer, type RunAction, type RunState } from "../runState";
import { STAGES } from "../types";
import { auditOf, endOf, STREAMS, type Frame } from "./fixtures";

function play(frames: Frame[], start: RunState = initialRunState()): RunState {
  let s = start;
  let now = 1000;
  for (const f of frames) {
    let a: RunAction | null = null;
    if (f.event === "audit") a = { type: "audit", event: f.data as never, now: (now += 10) };
    else if (f.event === "queued") a = { type: "queued", state: (f.data as { state: string }).state };
    else if (f.event === "end") a = { type: "end", ...(f.data as { status: string; decision: string | null }) };
    else if (f.event === "rejected") a = { type: "rejected", ...(f.data as { code: string; message: string }) };
    if (a) s = runReducer(s, a);
  }
  return s;
}

describe("a complete run (real invoice 10963, replay)", () => {
  const s = play(STREAMS.ss10963);
  it("ends with every stage finished and the decision", () => {
    expect(STAGES.map((n) => s.stages[n].status)).toEqual(["ok", "ok", "ok", "flagged", "ok", "ok", "ok"]);
    expect(s.decision).toBe("review");
    expect(s.end).toEqual({ status: "completed", decision: "review" });
    expect(isFinished(s)).toBe(true);
    expect(s.sourceFile).toBe("invoice_Scot Wooten_10963.pdf");
  });
  it("keeps each stage's own events and the summaries", () => {
    expect(liveRules(s)).toHaveLength(15);
    expect(s.stages.match.summary.matched_po).toBe("PO-SS-001");
    expect(s.stages.extract.events.some((e) => e.event_type === "llm_call")).toBe(true);
    expect(s.stages.explain.events.map((e) => e.event_type)).toEqual(["explanation"]);
    expect(s.eventCount).toBe(auditOf(STREAMS.ss10963).length);
    expect(s.lastSeq).toBe(auditOf(STREAMS.ss10963).length - 1);
  });
  it("has durations for every stage", () => {
    for (const n of STAGES) expect(s.stages[n].durationMs).not.toBeNull();
  });
});

describe("mid-run", () => {
  const audit = STREAMS.ss10963.filter((f) => f.event === "audit");
  const cut = audit.findIndex((f) => (f.data as { event_type: string; detail: { stage?: string } }).event_type === "stage_started"
                                     && (f.data as { detail: { stage?: string } }).detail.stage === "validate");
  const half = play(audit.slice(0, cut + 1));
  it("shows the stage in progress as running and later ones waiting", () => {
    expect(half.stages.match.status).toBe("ok");
    expect(half.stages.validate.status).toBe("running");
    expect(half.stages.validate.startedAt).not.toBeNull();
    expect(half.stages.decide.status).toBe("waiting");
    expect(isFinished(half)).toBe(false);
  });
  it("resumes after a reconnect that repeats events, with the same result as one pass", () => {
    const resumed = play(STREAMS.ss10963, half);                       // the whole stream again: duplicates are ignored
    const once = play(STREAMS.ss10963);
    expect(resumed.eventCount).toBe(once.eventCount);
    expect(STAGES.map((n) => resumed.stages[n].events.length)).toEqual(STAGES.map((n) => once.stages[n].events.length));
    expect(resumed.end).toEqual(once.end);
  });
});

describe("other outcomes", () => {
  it("IQ scan (real, replay): review with a flagged validate stage", () => {
    const s = play(STREAMS.iq);
    expect(s.decision).toBe("review");
    expect(s.stages.validate.status).toBe("flagged");
    expect(liveRules(s).filter((e) => e.outcome === "flag").map((e) => e.rule_id)).toEqual(
      expect.arrayContaining(["r_extraction_confidence", "r_po_found", "engine_floor"]));
  });
  it("synthetic approve variant", () => {
    const s = play(STREAMS.approve);
    expect(s.decision).toBe("approve");
    expect(s.stages.act.summary.rows_written).toMatchObject({ ledger_entries: 1 });
  });
  it("synthetic request_info variant", () => {
    expect(play(STREAMS.requestInfo).decision).toBe("request_info");
  });
  it("a failed run marks the act stage failed and records the error", () => {
    const s = play(STREAMS.failed);
    expect(endOf(STREAMS.failed)).toEqual({ status: "failed", decision: null });
    expect(s.stages.act.status).toBe("failed");
    expect(s.stages.explain.status).toBe("ok");
    expect(s.error).toBe("The run failed: RuntimeError.");
  });
});

describe("waiting, rejection, connection", () => {
  it("a queued state is cleared by the first event", () => {
    let s = runReducer(initialRunState(), { type: "queued", state: "queued" });
    expect(s.waiting).toBe("queued");
    s = runReducer(s, { type: "audit", event: auditOf(STREAMS.ss10963)[0], now: 1 });
    expect(s.waiting).toBeNull();
  });
  it("rejected finishes the run", () => {
    const s = runReducer(initialRunState(), { type: "rejected", code: "internal_error", message: "The run could not start." });
    expect(isFinished(s)).toBe(true);
    expect(s.rejected?.code).toBe("internal_error");
  });
  it("an older event never overwrites a newer state", () => {
    const events = auditOf(STREAMS.ss10963);
    const s = play(STREAMS.ss10963);
    expect(runReducer(s, { type: "audit", event: events[3], now: 99 })).toBe(s);
  });
  it("tracks the connection", () => {
    expect(runReducer(initialRunState(), { type: "connection", state: "lost" }).connection).toBe("lost");
  });
});
