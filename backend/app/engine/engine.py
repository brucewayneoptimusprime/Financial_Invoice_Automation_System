"""The rules engine: runs ALL enabled rules (no short-circuit), then the engine floor, then aggregates.

Guarantees:
  * deterministic: rules are put in a canonical order, evaluators are pure, events carry no timestamps;
  * escalate-only: final severity is a max over triggered results (see severity.py); an evaluator that
    raises, returns something invalid, has bad params or an unknown type yields a FLAG at the rule's
    default severity - never a silent pass;
  * complete trail: every rule (and the floor) produces exactly one result and one AuditEvent; disabled
    and locked-rule handling produce info events.
"""
import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.config import Settings, get_settings
from app.engine.evaluators import registry as _registry
from app.engine.floor import FloorConfig, evaluate_floor, evaluate_reference_floor
from app.engine.severity import aggregate, decision_for, is_triggered
from app.enums import Decision, Outcome, RuleSource, StageStatus
from app.models.audit import AuditEvent
from app.models.rules import Rule, RuleResult
from app.models.run import RunContext, StageResult

VALIDATE_STAGE = "validate"
DECIDE_STAGE = "decide"
_SOURCE_ORDER = {RuleSource.BUILTIN: 0, RuleSource.USER: 1, RuleSource.NL: 2}


@dataclass(frozen=True)
class EngineConfig:
    required_fields: tuple[str, ...]
    decision_severity: dict[str, int]
    locked_rule_ids: frozenset[str]
    floor_severity: int
    amount_compare_field: str = "total"

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "EngineConfig":
        s = settings or get_settings()
        return cls(
            required_fields=tuple(s.required_fields), decision_severity=dict(s.decision_severity),
            locked_rule_ids=frozenset(s.locked_rule_ids), floor_severity=s.engine_floor_severity,
            amount_compare_field=s.amount_compare_field,
        )

    @property
    def floor(self) -> FloorConfig:
        return FloorConfig(self.required_fields, self.floor_severity, self.amount_compare_field)

    @property
    def max_severity(self) -> int:
        return max(self.decision_severity.values())


def jsonable(value: Any) -> Any:
    """Make an evaluator's detail dict JSON-safe (Decimal -> exact string, date -> ISO) and canonical."""
    def default(o: Any) -> Any:
        if isinstance(o, Decimal):
            return format(o, "f")
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, (set, frozenset)):
            return sorted(o, key=str)
        raise TypeError(f"not JSON serialisable: {type(o).__name__}")

    return json.loads(json.dumps(value, default=default))


def canonical_order(rules: list[Rule]) -> list[Rule]:
    """Deterministic order regardless of the order rules were supplied in."""
    return sorted(rules, key=lambda r: (
        _SOURCE_ORDER[r.source], r.id, r.type, json.dumps(r.params, sort_keys=True, default=str),
        r.severity_on_trigger, r.enabled, r.original_text or "",
    ))


def _event(result: RuleResult, event_type: str = "rule_evaluated", stage: str = VALIDATE_STAGE) -> AuditEvent:
    return AuditEvent(
        stage=stage, event_type=event_type, rule_id=result.rule_id, outcome=result.outcome, message=result.message,
        detail={**result.detail, "severity": result.severity, "outcome_key": result.outcome_key},
    )


def _info_event(rule_id: str | None, event_type: str, message: str, detail: dict | None = None,
                stage: str = VALIDATE_STAGE) -> AuditEvent:
    return AuditEvent(stage=stage, event_type=event_type, rule_id=rule_id, outcome=Outcome.INFO,
                      message=message, detail=jsonable(detail or {}))


def _error_result(rule: Rule, config: EngineConfig, key: str, message: str, detail: dict) -> RuleResult:
    """Fail-safe: a rule that cannot be evaluated escalates at its default severity."""
    severity = rule.severity_on_trigger
    if not 1 <= severity <= config.max_severity:
        severity = config.max_severity
    return RuleResult(rule_id=rule.id, outcome=Outcome.FLAG, severity=severity, outcome_key=key,
                      message=f"Rule {rule.id} could not be evaluated ({message}); escalated for safety.",
                      detail=jsonable({"error": key, **detail}))


def evaluate_rule(ctx: RunContext, rule: Rule, config: EngineConfig,
                  registry: dict[str, _registry.EvaluatorSpec]) -> RuleResult:
    spec = registry.get(rule.type)
    if spec is None:
        return _error_result(rule, config, "unknown_rule_type", f"no evaluator for type {rule.type!r}", {"type": rule.type})
    reserved = sorted(_registry.RESERVED_PARAM_KEYS & set(rule.params))
    if reserved:
        return _error_result(rule, config, "invalid_params", "reserved param names used", {"reserved": reserved})
    try:
        params = spec.validate_params(rule.params)
    except ValidationError as exc:
        errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
        return _error_result(rule, config, "invalid_params", "invalid params", {"errors": errors})
    params["severity_on_trigger"] = rule.severity_on_trigger

    try:
        raw = spec.fn(ctx.model_copy(deep=True), params)  # each evaluator gets its own copy: no cross-talk
    except Exception as exc:  # noqa: BLE001 - any evaluator failure must escalate, never crash the run
        return _error_result(rule, config, "evaluator_exception", type(exc).__name__,
                             {"error_type": type(exc).__name__, "error_message": str(exc)})
    if not isinstance(raw, RuleResult):
        return _error_result(rule, config, "invalid_result", "evaluator did not return a RuleResult",
                             {"returned_type": type(raw).__name__})
    try:
        result = RuleResult.model_validate(raw.model_dump())  # re-validate: catches model_construct() cheats
    except ValidationError as exc:
        return _error_result(rule, config, "invalid_result", "evaluator returned an invalid result",
                             {"errors": [e["msg"] for e in exc.errors()]})
    return result.model_copy(update={"rule_id": rule.id, "detail": jsonable(result.detail)})


def run_validate_stage(
    ctx: RunContext, rules: list[Rule], *, registry: dict[str, _registry.EvaluatorSpec] | None = None,
    config: EngineConfig | None = None,
) -> StageResult:
    """Run every enabled rule (plus locked ones), then the engine floor. Sets ctx.rule_results."""
    registry = _registry.REGISTRY if registry is None else registry
    config = config or EngineConfig.from_settings()

    results: list[RuleResult] = []
    events: list[AuditEvent] = []
    skipped: list[str] = []
    for rule in canonical_order(rules):
        if not rule.enabled:
            if rule.id in config.locked_rule_ids:
                events.append(_info_event(rule.id, "locked_rule_enabled",
                                          f"Rule {rule.id} is locked: enabled=false is ignored and the rule was run.",
                                          {"locked": True}))
            else:
                skipped.append(rule.id)
                events.append(_info_event(rule.id, "rule_skipped", f"Rule {rule.id} is disabled and was skipped.",
                                          {"enabled": False}))
                continue
        result = evaluate_rule(ctx, rule, config, registry)
        results.append(result)
        events.append(_event(result))

    for floor in (evaluate_floor(ctx, config.floor), evaluate_reference_floor(ctx, config.floor)):
        results.append(floor)
        events.append(_event(floor, event_type="engine_floor"))

    final_severity = aggregate(results)
    decision = decision_for(final_severity, config.decision_severity)
    triggered = [{"rule_id": r.rule_id, "severity": r.severity} for r in results if is_triggered(r)]
    events.append(_info_event(None, "severity_aggregated",
                              f"Final severity {final_severity} ({decision.value}) = max over {len(triggered)} triggered result(s).",
                              {"final_severity": final_severity, "decision": decision.value, "triggered": triggered}))

    ctx.rule_results = results
    return StageResult(
        stage=VALIDATE_STAGE,
        status=StageStatus.FLAGGED if triggered else StageStatus.OK,
        outputs={"final_severity": final_severity, "decision": decision.value,
                 "triggered_rule_ids": [t["rule_id"] for t in triggered], "skipped_rule_ids": skipped},
        events=events,
    )


def decide(ctx: RunContext, config: EngineConfig | None = None) -> tuple[int, Decision]:
    """Pure: final severity and decision from the rule results already on the context."""
    config = config or EngineConfig.from_settings()
    severity = aggregate(ctx.rule_results)
    return severity, decision_for(severity, config.decision_severity)


def run_decide_stage(ctx: RunContext, config: EngineConfig | None = None) -> StageResult:
    """Minimal decide stage for M1: pick the decision. (Explanation and actions arrive in M3.)"""
    severity, decision = decide(ctx, config)
    ctx.decision = decision
    event = _info_event(None, "decision_made", f"Decision: {decision.value} (severity {severity}).",
                        {"final_severity": severity, "decision": decision.value}, stage=DECIDE_STAGE)
    return StageResult(stage=DECIDE_STAGE, status=StageStatus.OK,
                       outputs={"final_severity": severity, "decision": decision.value}, events=[event])
