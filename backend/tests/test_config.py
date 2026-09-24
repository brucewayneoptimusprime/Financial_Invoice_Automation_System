import pytest
from pydantic import ValidationError

from app.builtin_rules import builtin_rules
from app.config import DEFAULT_DECISION_SEVERITY, Settings


def _settings(**kw):
    return Settings(_env_file=None, **kw)


def test_api_key_comes_from_environment_and_is_not_leaked(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-secret")
    s = _settings()
    assert s.anthropic_api_key.get_secret_value() == "sk-test-secret"
    assert "sk-test-secret" not in repr(s) and "sk-test-secret" not in str(s)


def test_api_key_absent_is_allowed(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert _settings().anthropic_api_key is None


def test_model_name_and_threshold_are_configurable(monkeypatch):
    monkeypatch.setenv("MODEL_NAME", "some-other-model")
    monkeypatch.setenv("CONFIDENCE_THRESHOLD", "0.65")
    s = _settings()
    assert s.model_name == "some-other-model" and s.confidence_threshold == 0.65


def test_confidence_threshold_bounds():
    with pytest.raises(ValidationError):
        _settings(confidence_threshold=1.5)


def test_severity_order_is_one_config_constant():
    assert _settings().decision_severity == DEFAULT_DECISION_SEVERITY
    assert DEFAULT_DECISION_SEVERITY == {"approve": 0, "review": 1, "request_info": 2, "reject": 3}


@pytest.mark.parametrize("bad", [
    {"approve": 0, "review": 1, "request_info": 2},                     # missing a decision
    {"approve": 1, "review": 1, "request_info": 2, "reject": 3},       # approve must be 0
    {"approve": 0, "review": 0, "request_info": 2, "reject": 3},       # non-approve must be > 0
])
def test_invalid_severity_order_rejected(bad):
    with pytest.raises(ValidationError):
        _settings(decision_severity=bad)


def test_tolerance_defaults_flow_into_builtin_rule_params():
    rules = {r.id: r for r in builtin_rules(_settings(tolerance_pct=5.0, tolerance_abs=75.0))}
    assert rules["r_tolerance_pct"].params == {"pct": 5.0, "abs": 75.0}


def test_all_builtin_rules_are_valid_escalate_only_rules():
    rules = builtin_rules(_settings())
    assert len(rules) == len({r.id for r in rules}) == 10
    assert all(r.source.value == "builtin" and r.severity_on_trigger >= 1 for r in rules)
