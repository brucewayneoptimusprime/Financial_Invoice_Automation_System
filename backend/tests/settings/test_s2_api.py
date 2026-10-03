"""Rules settings, stage S2: the settings API (global defaults, PO overrides, history), ranges, locked rules, "looser than
default", and one settings_events row per changed value. Offline; no model; behind ACCESS_TOKEN."""
import json

import pytest

from tests.api.helpers import api, api_settings
from tests.gmail.helpers import table_counts
from tests.po.export_helpers import db
from tests.settings.helpers import events

KEYS = ["tolerance_pct", "tolerance_abs", "tolerance_mode", "confidence_threshold", "duplicate_days", "duplicate_amount"]


def history(c, **params):
    return c.get("/api/settings/history", params=params).json()["events"]


# ------------------------------------------------------------------------------------------ global defaults

def test_the_global_view_lists_values_with_ranges_rules_locked_rules_and_floors(tmp_path):
    with api(tmp_path) as c:
        g = c.get("/api/settings").json()
    by = {v["key"]: v for v in g["values"]}
    assert list(by) == KEYS
    assert (by["tolerance_pct"]["value"], by["tolerance_pct"]["min"], by["tolerance_pct"]["max"], by["tolerance_pct"]["step"]) == (2.0, "0", "25", "0.01")
    assert (by["tolerance_abs"]["value"], by["tolerance_abs"]["builtin"], by["tolerance_abs"]["max"]) == ("50.00", "50.00", "1000000")
    assert by["tolerance_mode"]["value"] == "lesser_of" and by["tolerance_mode"]["options"] == ["lesser_of", "greater_of"]
    assert (by["confidence_threshold"]["value"], by["confidence_threshold"]["min"], by["confidence_threshold"]["max"]) == (0.8, "0.5", "0.99")
    assert (by["duplicate_days"]["value"], by["duplicate_amount"]["value"]) == (7, "0.00")
    rules = {r["id"]: r for r in g["rules"]}
    assert len(rules) == 14 and sum(r["switchable"] for r in rules.values()) == 12
    assert rules["r_duplicate_exact"]["locked"] and "exact duplicate" in rules["r_duplicate_exact"]["reason"]
    assert rules["r_vendor_status"]["locked"] and not rules["r_tolerance_pct"]["locked"]
    assert [f["id"] for f in g["floors"]] == ["engine_floor", "engine_floor_reference"] and g["actor"] == "unauthenticated demo user"


def test_changing_a_global_default_writes_the_rule_and_one_log_entry_per_changed_value(tmp_path):
    with api(tmp_path) as c:
        r = c.post("/api/settings/global", json={"values": {"tolerance_pct": 3, "tolerance_abs": "75.5", "duplicate_days": 7,
                                                            "confidence_threshold": 0.85}, "rules": {"r_arithmetic": False}})
        with db(c) as conn:
            params = json.loads(conn.execute("SELECT params FROM rules WHERE id = 'r_tolerance_pct'").fetchone()[0])
            threshold = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'confidence_threshold'").fetchone()[0])
            arithmetic = conn.execute("SELECT enabled FROM rules WHERE id = 'r_arithmetic'").fetchone()[0]
        log = history(c)
    assert r.status_code == 200 and r.json()["changed"] == 4                        # duplicate_days 7 is unchanged
    assert (params["pct"], params["abs"], threshold, arithmetic) == (3.0, 75.5, 0.85, 0)
    assert [e["key"] for e in log] == ["rule:r_arithmetic", "confidence_threshold", "tolerance_abs", "tolerance_pct"]
    pct = log[-1]
    assert (pct["scope"], pct["po_id"], pct["old_value"], pct["new_value"], pct["actor"]) == ("global", None, 2.0, 3.0, "unauthenticated demo user")
    assert pct["message"] == "Tolerance over the PO balance (percent) (global default) changed: 2.00% → 3.00%." and pct["created_at"]


def test_a_save_that_changes_nothing_writes_nothing(tmp_path):
    with api(tmp_path) as c:
        r = c.post("/api/settings/global", json={"values": {"tolerance_pct": 2.0, "tolerance_mode": "lesser_of"}, "rules": {"r_arithmetic": True}})
        assert r.json()["changed"] == 0 and history(c) == []


def test_restore_puts_a_value_back_to_the_built_in_default(tmp_path):
    with api(tmp_path) as c:
        c.post("/api/settings/global", json={"values": {"tolerance_pct": 9}})
        r = c.post("/api/settings/global", json={"restore": ["tolerance_pct"]})
        log = history(c)
    assert {v["key"]: v["value"] for v in r.json()["values"]}["tolerance_pct"] == 2.0
    assert "restored to the built-in default" in log[0]["message"] and log[0]["new_value"] == 2.0


@pytest.mark.parametrize("values, key, reason", [
    ({"tolerance_pct": 25.01}, "tolerance_pct", "between 0 and 25"),
    ({"tolerance_pct": -1}, "tolerance_pct", "between 0 and 25"),
    ({"tolerance_pct": 2.005}, "tolerance_pct", "multiple of 0.01"),
    ({"tolerance_abs": "10.001"}, "tolerance_abs", "multiple of 0.01"),
    ({"tolerance_abs": 1000000.01}, "tolerance_abs", "between 0 and 1000000"),
    ({"tolerance_mode": "both"}, "tolerance_mode", "lesser_of"),
    ({"confidence_threshold": 0.49}, "confidence_threshold", "between 0.5 and 0.99"),
    ({"confidence_threshold": 1}, "confidence_threshold", "between 0.5 and 0.99"),
    ({"duplicate_days": 2.5}, "duplicate_days", "multiple of 1"),
    ({"duplicate_days": 91}, "duplicate_days", "between 0 and 90"),
    ({"duplicate_amount": "abc"}, "duplicate_amount", "a number"),
    ({"tolerance_pct": True}, "tolerance_pct", "a number"),
    ({"severity": 0}, "severity", "not an editable setting"),
])
def test_out_of_range_values_are_refused_and_nothing_is_written(tmp_path, values, key, reason):
    with api(tmp_path) as c:
        before = table_counts(c.db_path)
        with db(c) as conn:
            params_before = conn.execute("SELECT params FROM rules ORDER BY id").fetchall()
        r = c.post("/api/settings/global", json={"values": {**values, "duplicate_days": values.get("duplicate_days", 14)}})
        r_po = c.post("/api/settings/pos/1", json={"values": values})
        with db(c) as conn:
            params_after = conn.execute("SELECT params FROM rules ORDER BY id").fetchall()
        after = table_counts(c.db_path)
    for resp in (r, r_po):
        assert resp.status_code == 422 and resp.json()["error"] == "invalid" and reason in resp.json()["problems"][key]
    assert before == after and [tuple(x) for x in params_before] == [tuple(x) for x in params_after]   # all or nothing


@pytest.mark.parametrize("rules, key, reason", [
    ({"r_duplicate_exact": False}, "rule:r_duplicate_exact", "locked"),
    ({"r_vendor_status": False}, "rule:r_vendor_status", "locked"),
    ({"engine_floor": False}, "rule:engine_floor", "engine floor"),
    ({"r_nonexistent": False}, "rule:r_nonexistent", "not a rule"),
    ({"r_arithmetic": "off"}, "rule:r_arithmetic", "true (on) or false (off)"),
])
def test_locked_rules_and_floors_cannot_be_switched_off(tmp_path, rules, key, reason):
    with api(tmp_path) as c:
        g = c.post("/api/settings/global", json={"rules": rules})
        p = c.post("/api/settings/pos/1", json={"rules": rules})
        assert history(c) == []
    for resp in (g, p):
        assert resp.status_code == 422 and reason in resp.json()["problems"][key]


# ------------------------------------------------------------------------------------------ PO overrides

def test_a_po_override_is_marked_custom_and_looser_and_reset_returns_it_to_the_default(tmp_path):
    with api(tmp_path) as c:
        assert all(not p["custom"] for p in c.get("/api/settings/pos").json()["pos"])
        r = c.post("/api/settings/pos/1", json={"values": {"tolerance_pct": 5, "confidence_threshold": 0.9}, "rules": {"r_po_line_price": False}})
        view = r.json()
        listed = {p["id"]: p for p in c.get("/api/settings/pos").json()["pos"]}
        custom_only = c.get("/api/settings/pos", params={"custom": "true"}).json()["pos"]
        reset = c.post("/api/settings/pos/1", json={"values": {"tolerance_pct": None, "confidence_threshold": None},
                                                    "rules": {"r_po_line_price": None}}).json()
        with db(c) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM po_settings").fetchone()[0], conn.execute("SELECT COUNT(*) FROM po_rule_switches").fetchone()[0]
        log = history(c, po_id=1)
    vals = {v["key"]: v for v in view["values"]}
    assert vals["tolerance_pct"] == {**vals["tolerance_pct"], "value": 5.0, "default": 2.0, "source": "overridden", "looser": True}
    assert vals["confidence_threshold"]["looser"] is False and vals["confidence_threshold"]["source"] == "overridden"   # 0.9 > 0.8: stricter
    assert vals["tolerance_abs"]["source"] == "inherits" and vals["tolerance_abs"]["value"] == "50.00"
    line = next(r for r in view["rules"] if r["id"] == "r_po_line_price")
    assert (line["enabled"], line["source"], line["looser"]) == (False, "overridden", True) and view["looser"] and view["overrides"] == 3
    assert listed[1]["custom"] and listed[1]["looser"] and listed[1]["overrides"] == 3 and not listed[2]["custom"]
    assert [p["id"] for p in custom_only] == [1]
    assert all(v["source"] == "inherits" for v in reset["values"]) and reset["overrides"] == 0 and rows == (0, 0)
    assert log[0]["message"].endswith("reset to the default (on).") and log[0]["new_value"] is None
    assert any(e["message"] == "Tolerance over the PO balance (percent) for PO-SS-001: inherits 2.00% → 5.00%." for e in log)


@pytest.mark.parametrize("values, looser", [
    ({"tolerance_pct": 1.5}, False), ({"tolerance_pct": 2.5}, True), ({"tolerance_abs": "40.00"}, False), ({"tolerance_abs": "60.00"}, True),
    ({"tolerance_mode": "greater_of"}, True), ({"confidence_threshold": 0.7}, True), ({"confidence_threshold": 0.95}, False),
    ({"duplicate_days": 3}, True), ({"duplicate_days": 30}, False),
])
def test_the_looser_than_default_marker(tmp_path, values, looser):
    with api(tmp_path) as c:
        view = c.post("/api/settings/pos/2", json={"values": values}).json()
        listed = {p["id"]: p for p in c.get("/api/settings/pos").json()["pos"]}
    key = next(iter(values))
    assert next(v for v in view["values"] if v["key"] == key)["looser"] is looser and view["looser"] is looser
    assert listed[2]["looser"] is looser


def test_switching_a_rule_on_that_is_off_globally_is_not_looser(tmp_path):
    with api(tmp_path) as c:
        c.post("/api/settings/global", json={"rules": {"r_arithmetic": False}})
        view = c.post("/api/settings/pos/1", json={"rules": {"r_arithmetic": True}}).json()
    rule = next(r for r in view["rules"] if r["id"] == "r_arithmetic")
    assert (rule["enabled"], rule["default"], rule["looser"]) == (True, False, False)


def test_a_later_global_change_does_not_touch_an_overridden_key(tmp_path):
    with api(tmp_path) as c:
        c.post("/api/settings/pos/1", json={"values": {"tolerance_pct": 5}})
        c.post("/api/settings/global", json={"values": {"tolerance_pct": 3, "tolerance_abs": "80.00"}})
        vals = {v["key"]: v for v in c.get("/api/settings/pos/1").json()["values"]}
        other = {v["key"]: v for v in c.get("/api/settings/pos/2").json()["values"]}
    assert (vals["tolerance_pct"]["value"], vals["tolerance_pct"]["default"], vals["tolerance_abs"]["value"]) == (5.0, 3.0, "80.00")
    assert other["tolerance_pct"]["value"] == 3.0 and other["tolerance_pct"]["source"] == "inherits"


def test_an_unknown_po_is_a_404(tmp_path):
    with api(tmp_path) as c:
        assert c.get("/api/settings/pos/999").status_code == 404
        assert c.post("/api/settings/pos/999", json={"values": {"tolerance_pct": 3}}).status_code == 404


def test_the_history_filters_by_scope_and_po(tmp_path):
    with api(tmp_path) as c:
        c.post("/api/settings/global", json={"values": {"tolerance_pct": 3}})
        c.post("/api/settings/pos/1", json={"values": {"duplicate_days": 10}})
        c.post("/api/settings/pos/2", json={"values": {"duplicate_days": 11}})
        assert len(history(c)) == 3 and [e["scope"] for e in history(c, scope="global")] == ["global"]
        assert [e["po_number"] for e in history(c, scope="po")] == ["PO-SS-002", "PO-SS-001"]
        assert [e["new_value"] for e in history(c, po_id=1)] == [10]


def test_an_override_made_through_the_api_is_what_the_next_run_uses(tmp_path):
    from tests.api.helpers import run_and_wait
    from tests.review.helpers import SS_10963, runs_with_scenarios
    with api(tmp_path, run_fn=runs_with_scenarios()) as c:
        c.post("/api/settings/pos/1", json={"values": {"tolerance_abs": "12.34"}, "rules": {"r_document_type": False}})
        rid = run_and_wait(c, SS_10963)
        with db(c) as conn:
            [applied] = events(conn, rid, "settings_applied")
            skipped = [e["rule_id"] for e in events(conn, rid, "rule_skipped")]
    assert applied["detail"]["values"]["tolerance_abs"] == "12.34" and applied["detail"]["sources"]["tolerance_abs"] == "override"
    assert applied["detail"]["rules_enabled"]["r_document_type"] is False and "r_document_type" in skipped
    assert "r_document_type switched off" in applied["message"]


def test_the_settings_routes_are_behind_the_access_token(tmp_path):
    with api(tmp_path, settings=api_settings(tmp_path, access_token="tok-s")) as c:
        for method, path in (("get", "/api/settings"), ("post", "/api/settings/global"), ("get", "/api/settings/pos"),
                             ("get", "/api/settings/pos/1"), ("post", "/api/settings/pos/1"), ("get", "/api/settings/history")):
            r = getattr(c, method)(path, **({"json": {}} if method == "post" else {}))
            assert r.status_code == 401, path
        assert c.get("/api/settings", headers={"Authorization": "Bearer tok-s"}).status_code == 200
