"""The settings UI's frontend fixtures are recorded from the real endpoints; this fails if the committed files drifted in shape."""
import json

from tests.gmail.test_stage5_backend import _shape
from tests.settings.frontend_fixtures import NAMES, OUT, generate


def test_settings_frontend_fixtures_match_the_endpoints(tmp_path):
    fresh = generate(tmp_path)
    assert set(fresh) == set(NAMES)
    for name in NAMES:
        committed = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert _shape(committed) == _shape(fresh[name]), f"{name}.json drifted: run `python -m tests.settings.frontend_fixtures`"


def test_the_run_view_carries_the_settings_used(tmp_path):
    used = generate(tmp_path)["settings_run_view"]["settings_used"]
    assert used["scope"] == "po" and used["po_number"] == "PO-SS-001"
    assert used["message"].startswith("Settings used: PO-SS-001's settings (4 overridden:")
    assert {k for k, s in used["sources"].items() if s == "override"} == {"tolerance_pct", "tolerance_abs", "confidence_threshold"}
    assert used["rules_enabled"]["r_po_line_price"] is False and used["rule_sources"]["r_po_line_price"] == "override"
