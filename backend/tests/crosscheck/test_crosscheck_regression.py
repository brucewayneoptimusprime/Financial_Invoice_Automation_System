"""The six real invoices, pinned (stage C0, from `deploy` at 60ae234), and unchanged by a cross-check run in the same database."""
from pathlib import Path

from fastapi.testclient import TestClient

from tests.api.helpers import build_app
from tests.crosscheck.helpers import client_for
from tests.crosscheck.test_c3_api import pdf, post, snapshot
from tests.extraction.real import real_pdf
from tests.gmail.helpers import gmail_settings
from tests.gmail.regression import SIX, HashRuns, outcome

EXPECTED = {  # decision, matched PO, triggered rules: the behaviour of `deploy` before this feature existed
    "superstore_10963": ("review", "PO-SS-001", ["r_po_found:matched_without_reference"]),
    "superstore_24429": ("review", "PO-SS-002", ["r_po_found:matched_without_reference"]),
    "superstore_14021": ("review", "PO-SS-003", ["r_po_found:matched_without_reference"]),
    "superstore_14130": ("review", "PO-SS-005", ["r_po_found:matched_without_reference"]),
    "superstore_6459": ("review", "PO-SS-004", ["r_po_found:matched_without_reference"]),
    "iq_electronics": ("review", "PO-IQ-2025-001", ["engine_floor:floor_applied", "r_extraction_confidence:low_confidence",
                                                     "r_po_found:matched_without_reference"]),
}


def six_outcomes(tmp: Path, between=None, **app_kw) -> dict[str, dict]:
    """Upload the six through the API. `between(client)` runs after the first three and again after all six, so the last three
    are judged after a cross-check has run in the same database."""
    tmp.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp, crosscheck_tmp_dir=tmp / "cc"), run_fn=HashRuns(), **app_kw)
    out = {}
    with TestClient(app) as c:
        c.db_path, c.settings = db, settings
        ids = {}
        for i, (name, (_, _, filename)) in enumerate(SIX.items()):
            if i == 3:
                assert worker.wait_idle(180)
                if between is not None:
                    between(c)
            ids[name] = c.post("/api/runs", files={"file": (filename, real_pdf(name).read_bytes(), "application/octet-stream")}).json()["run_id"]
        assert worker.wait_idle(180)
        if between is not None:
            between(c)
        for name, rid in ids.items():
            out[name] = outcome(c.get(f"/api/runs/{rid}").json())
    return out


def test_the_six_real_invoices_produce_the_decisions_pinned_from_deploy(tmp_path):
    got = six_outcomes(tmp_path)
    for name, (decision, po, triggered) in EXPECTED.items():
        assert (got[name]["decision"], got[name]["matched_po"], got[name]["triggered"]) == (decision, po, triggered), name


def test_the_six_real_invoices_are_unchanged_by_cross_checks_run_in_the_same_database(tmp_path):
    plain = six_outcomes(tmp_path / "plain")
    short, reply = pdf(tmp_path / "src", "short.pdf", vendor="SuperStore", po="PO-SS-001",
                       a=("Hewlett Fax Machine, Color - Copiers, Technology", "3", "1285.44", "3856.32"), b=None, total="3,856.32")
    reports = []

    def cross_check(c):
        before = snapshot(c)
        r = post(c, 1, short)                                                          # PO-SS-001, which invoice 10963 is matched to
        assert r.status_code == 200 and snapshot(c) == before                          # every table, and the file's bytes
        reports.append(r.json()["documents"][0])

    checked = six_outcomes(tmp_path / "checked", between=cross_check, mode="replay", inner_client=client_for(reply, reply))
    assert checked == plain
    for name, (decision, po, triggered) in EXPECTED.items():
        assert (checked[name]["decision"], checked[name]["matched_po"], checked[name]["triggered"]) == (decision, po, triggered), name
    assert len(reports) == 2 and all(d["relevance"]["related"] for d in reports)
    # the report itself: 3 delivered against 4 ordered, and against the 4 on invoice 10963, which is named with its status
    kinds = [(x["type"], x["document"]["value"], x["compared_with"]["value"]) for x in reports[1]["differences"]]
    assert kinds == [("quantity_vs_po", "3", "4"), ("quantity_vs_invoiced", "3", "4")]
    assert reports[1]["differences"][1]["compared_with"]["source"] == "Invoiced on PO line 1: 10963 (in review): 4"
