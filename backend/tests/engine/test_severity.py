import pytest
from pydantic import ValidationError

from app.config import DEFAULT_DECISION_SEVERITY, Settings
from app.engine.severity import aggregate, decision_for, is_triggered
from app.enums import Decision, Outcome
from app.models.rules import RuleResult


def R(outcome, severity, rule_id="r"):
    return RuleResult(rule_id=rule_id, outcome=outcome, severity=severity, message="m")


def test_no_triggered_results_means_zero_and_approve():
    assert aggregate([]) == 0
    assert aggregate([R("pass", 0), R("info", 0)]) == 0
    assert decision_for(0, DEFAULT_DECISION_SEVERITY) is Decision.APPROVE


def test_final_severity_is_the_max_over_triggered_results():
    assert aggregate([R("flag", 1), R("fail", 3), R("flag", 2), R("pass", 0)]) == 3
    assert aggregate([R("flag", 1), R("pass", 0)]) == 1


def test_order_of_results_does_not_matter():
    results = [R("flag", 2, "a"), R("fail", 3, "b"), R("flag", 1, "c"), R("pass", 0, "d")]
    assert aggregate(results) == aggregate(list(reversed(results))) == 3


@pytest.mark.parametrize("sev,decision", [(0, "approve"), (1, "review"), (2, "request_info"), (3, "reject")])
def test_decision_for_uses_the_configured_order(sev, decision):
    assert decision_for(sev, DEFAULT_DECISION_SEVERITY).value == decision


def test_decision_order_comes_from_config_only():
    swapped = {"approve": 0, "review": 1, "request_info": 3, "reject": 2}  # the open question, flipped
    assert decision_for(3, swapped) is Decision.REQUEST_INFO
    assert decision_for(2, swapped) is Decision.REJECT
    assert Settings(_env_file=None, decision_severity=swapped).decision_severity == swapped


def test_config_rejects_ambiguous_severity_order():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, decision_severity={"approve": 0, "review": 1, "request_info": 1, "reject": 3})


def test_unknown_severity_raises():
    with pytest.raises(ValueError):
        decision_for(9, DEFAULT_DECISION_SEVERITY)


@pytest.mark.parametrize("outcome,expected", [("pass", False), ("info", False), ("flag", True), ("fail", True)])
def test_triggered_outcomes(outcome, expected):
    sev = 1 if expected else 0
    assert is_triggered(R(outcome, sev)) is expected


@pytest.mark.parametrize("outcome,severity", [
    ("flag", 0), ("fail", 0), ("flag", 4), ("pass", 1), ("info", 2), ("pass", 3),
])
def test_result_severity_must_match_outcome(outcome, severity):
    with pytest.raises(ValidationError):
        R(outcome, severity)
