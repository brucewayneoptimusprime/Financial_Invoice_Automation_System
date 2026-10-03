import { useEffect, useState } from "react";
import { DetailList, Disclosure, Chip } from "./common";
import { duration, OUTCOME, STAGE_HINT, STAGE_LABEL, stageLine } from "../format";
import type { RunState, StageState } from "../runState";
import { STAGES, type AuditEvent, type StageName } from "../types";

const STATUS_TEXT: Record<StageState["status"], string> = {
  waiting: "Waiting", running: "Running", ok: "Done", flagged: "Flagged", failed: "Failed",
};

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(t);
  }, []);
  return <span className="elapsed">{((now - since) / 1000).toFixed(1)} s</span>;
}

function EventRow({ e }: { e: AuditEvent }) {
  const hasDetail = e.detail && Object.keys(e.detail).length > 0;
  const head = (
    <>
      <span className={`dot tone-${OUTCOME[e.outcome].tone}`} aria-label={OUTCOME[e.outcome].label} />
      <span className="ev-msg">{e.message}</span>
      {e.rule_id && <code className="ev-rule">{e.rule_id}</code>}
    </>
  );
  if (!hasDetail) return <li className="ev ev-plain">{head}</li>;
  return (
    <li className="ev">
      <Disclosure summary={head}>
        <DetailList data={e.detail} />
      </Disclosure>
    </li>
  );
}

function LiveRules({ events }: { events: AuditEvent[] }) {
  const rules = events.filter((e) => e.event_type === "rule_evaluated" || e.event_type === "engine_floor");
  if (rules.length === 0) return null;
  return (
    <ul className="live-rules" aria-label="Rule results so far">
      {rules.map((e) => (
        <li key={e.seq}>
          <Chip tone={OUTCOME[e.outcome].tone}>{OUTCOME[e.outcome].label}</Chip>
          <code>{e.rule_id}</code>
          {Number(e.detail.severity ?? 0) > 0 && <span className="sev">sev {String(e.detail.severity)}</span>}
        </li>
      ))}
    </ul>
  );
}

// The settings the run was judged under (one settings_applied event in the validate stage; SPEC section 11 item 93).
function SettingsUsedLine({ events }: { events: AuditEvent[] }) {
  const e = events.find((x) => x.event_type === "settings_applied");
  return e ? <p className="stage-line settings-used">{e.message}</p> : null;
}

export function StageCard({ name, stage, index }: { name: StageName; stage: StageState; index: number }) {
  const [open, setOpen] = useState(false);
  const line = stage.status === "running" || stage.status === "waiting" ? STAGE_HINT[name] : stageLine(name, stage.summary);
  const canOpen = stage.events.length > 0;
  return (
    <li className={`stage stage-${stage.status}`} aria-current={stage.status === "running" ? "step" : undefined}>
      <div className="stage-rail" aria-hidden="true">
        <span className="stage-icon">{stage.status === "ok" ? "✓" : stage.status === "flagged" ? "!" : stage.status === "failed" ? "✕" : index + 1}</span>
      </div>
      <div className="stage-body">
        <button type="button" className="stage-head" onClick={() => canOpen && setOpen(!open)} aria-expanded={canOpen ? open : undefined}
                disabled={!canOpen}>
          <span className="stage-name">{STAGE_LABEL[name]}</span>
          <span className={`stage-status status-${stage.status}`}>{STATUS_TEXT[stage.status]}</span>
          <span className="stage-time">
            {stage.status === "running" && stage.startedAt !== null ? <Elapsed since={stage.startedAt} /> : duration(stage.durationMs)}
          </span>
          {canOpen && <span className={`caret ${open ? "up" : ""}`} aria-hidden="true" />}
        </button>
        <p className="stage-line">{line}</p>
        {name === "validate" && <SettingsUsedLine events={stage.events} />}
        {name === "validate" && !open && <LiveRules events={stage.events} />}
        {open && (
          <ul className="events" aria-label={`${STAGE_LABEL[name]} events`}>
            {stage.events.map((e) => <EventRow key={e.seq} e={e} />)}
          </ul>
        )}
      </div>
    </li>
  );
}

export function StageTimeline({ state }: { state: RunState }) {
  return (
    <ol className="timeline" aria-label="Pipeline stages">
      {STAGES.map((s, i) => <StageCard key={s} name={s} stage={state.stages[s]} index={i} />)}
    </ol>
  );
}
