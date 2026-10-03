"""ERP feed stage R2: the routes, the import through save_po, idempotence, no overwrite, vendors, the gate, the six-invoice regression."""
import json
import shutil
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.db.connection import connect
from app.erp import importer
from app.po import store as po_store
from tests.api.helpers import api, api_settings, build_app
from tests.erp.helpers import SAMPLE, a_po, by_number, counts, envelope, write_feed
from tests.extraction.real import real_pdf
from tests.gmail.helpers import gmail_settings
from tests.gmail.regression import SIX, HashRuns, outcome
from tests.settings.test_s1_loader import EXPECTED

NEW = ["4500012001", "4500012002", "4500012003", "4500012004", "4500012005"]


def preview(c) -> dict:
    r = c.get("/api/erp/preview")
    assert r.status_code == 200, r.text
    return r.json()


def do_import(c, numbers, sha=None, **extra):
    sha = sha or preview(c)["feed"]["sha256"]
    return c.post("/api/erp/import", json={"feed_sha256": sha, "po_numbers": numbers, "confirm": True, **extra})


def db_counts(c) -> dict:
    with closing(connect(c.db_path)) as conn:
        return counts(conn)


def po_snapshot(db: Path, number: str) -> dict:
    with closing(connect(db)) as conn:
        po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (number,)).fetchone()
        pid = po["id"]
        return {"po": tuple(po), **{t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} WHERE po_id = ? ORDER BY id", (pid,))]
                                    for t in ("po_lines", "ledger_entries", "po_consumption", "invoices")}}


# ----------------------------------------------------------------------------------------------------- preview route

def test_the_preview_route_is_read_only(tmp_path):
    with api(tmp_path) as c:
        before, raw = db_counts(c), c.db_path.read_bytes()
        p = preview(c)
        assert p["label"] == "Simulated ERP (demo)" and p["counts"] == {"new": 5, "exists": 1, "problem": 7}
        assert db_counts(c) == before and c.db_path.read_bytes() == raw


def test_the_preview_never_calls_the_writer(tmp_path, monkeypatch):
    monkeypatch.setattr(po_store, "save_po", lambda *a, **k: pytest.fail("the preview must not save"))
    monkeypatch.setattr(importer, "save_po", lambda *a, **k: pytest.fail("the preview must not save"))
    with api(tmp_path) as c:
        preview(c)


# ----------------------------------------------------------------------------------------------------- import

def test_importing_the_new_pos_saves_them_through_save_po_with_erp_provenance(tmp_path, monkeypatch):
    calls = []
    real = importer.save_po
    monkeypatch.setattr(importer, "save_po", lambda *a, **k: calls.append(k["provenance"]["source"]) or real(*a, **k))
    with api(tmp_path) as c:
        before = db_counts(c)
        r = do_import(c, NEW)
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["imported"], body["skipped"], body["refused"]) == (5, 0, 0) and calls == ["erp"] * 5
        assert [x["po_number"] for x in body["results"]] == NEW
        after = db_counts(c)
        assert after["purchase_orders"] == before["purchase_orders"] + 5 and after["vendors"] == before["vendors"] + 1
        assert {k: after[k] for k in ("invoices", "ledger_entries", "po_consumption", "runs")} == \
               {k: before[k] for k in ("invoices", "ledger_entries", "po_consumption", "runs")}
        listed = {p["po_number"]: p for p in c.get("/api/pos").json()["pos"]}
        assert all(listed[n]["source"] == "erp" and listed[n]["status"] == "open" for n in NEW)
        pid = next(x["po_id"] for x in body["results"] if x["po_number"] == "4500012001")
        d = c.get(f"/api/pos/{pid}").json()
        prov = d["provenance"]
        assert prov["source"] == "erp" and prov["erp_label"] == "Simulated ERP (demo)" and prov["adapter"] == "simerp-v1"
        assert prov["feed_file"] == "erp_feed_sample.json" and prov["synced_at"] == body["synced_at"] and prov["entered_at"]
        assert prov["buyer_reference"] == "REQ-7781 / J. Rao" and prov["erp_status"] == "RELEASED"
        assert prov["line_uom"] == ["EA"] and prov["erp_line_numbers"] == [10]
        assert d["amounts"]["total"] == "486.00" and d["lines"][0]["unit_price"] == "40.50" and d["lines"][0]["amount"] == "486.00"


def test_a_new_vendor_is_created_once_with_status_new(tmp_path):
    with api(tmp_path) as c:
        body = do_import(c, ["4500012004", "4500012005"]).json()
        assert [x["new_vendor"] for x in body["results"]] == [True, False]       # the second resolves to the vendor just created
        with closing(connect(c.db_path)) as conn:
            rows = conn.execute("SELECT id, status, country, tax_id FROM vendors WHERE name = 'Northwind Office Supplies Ltd'").fetchall()
        assert len(rows) == 1 and tuple(rows[0])[1:] == ("new", "GB", "GB123456789")
        assert {x["vendor_id"] for x in body["results"]} == {rows[0]["id"]}


def test_existing_vendors_are_reused(tmp_path):
    with api(tmp_path) as c:
        body = do_import(c, ["4500012001", "4500012002", "4500012003"]).json()
        with closing(connect(c.db_path)) as conn:
            names = [conn.execute("SELECT name FROM vendors WHERE id = ?", (x["vendor_id"],)).fetchone()[0] for x in body["results"]]
    assert names == ["SuperStore", "SuperStore", "Electronics Mart India Limited"]


def test_resyncing_is_idempotent(tmp_path):
    with api(tmp_path) as c:
        do_import(c, NEW)
        mid = db_counts(c)
        p = preview(c)
        rows = by_number(p)
        assert all(rows[n][0]["class"] == "exists" for n in NEW) and p["counts"] == {"new": 0, "exists": 6, "problem": 7}
        again = do_import(c, NEW).json()
        assert [x["outcome"] for x in again["results"]] == ["skipped_exists"] * 5 and again["imported"] == 0
        assert db_counts(c) == mid


def test_an_existing_po_with_invoices_and_ledger_is_never_overwritten(tmp_path):
    with api(tmp_path) as c:
        before = po_snapshot(c.db_path, "PO-SS-005")
        assert before["invoices"] and before["ledger_entries"]                  # the seeded historic invoice and its commit
        preview(c)
        body = do_import(c, ["PO-SS-005"]).json()
        assert body["results"] == [{"po_number": "PO-SS-005", "outcome": "skipped_exists",
                                    "existing_po": {"id": json.loads(json.dumps(before["po"][0])), "po_number": "PO-SS-005"}}]
        assert po_snapshot(c.db_path, "PO-SS-005") == before


@pytest.mark.parametrize("number, code", [("po-ss-002", "similar"), ("4500012006", "required"), ("4500012007", "line_math"),
                                          ("4500012008", "negative"), ("4500012009", "duplicate_in_feed"), ("4500012010", "not_released")])
def test_a_problem_po_is_refused_at_import_too(tmp_path, number, code):
    with api(tmp_path) as c:
        before = db_counts(c)
        [res] = do_import(c, [number]).json()["results"]
        assert res["outcome"] == "refused" and code in [i["code"] for i in res["issues"]]
        assert db_counts(c) == before


def test_a_po_created_between_preview_and_import_is_skipped(tmp_path, monkeypatch):
    """A concurrent save of the same number: the unique key wins, the import reports skipped_exists, one row exists."""
    real = importer.save_po

    def racing(conn, parsed, **kw):
        real(conn, parsed, provenance={"source": "manual"}, new_vendor=kw.get("new_vendor"))   # someone else saves it first
        return real(conn, parsed, **kw)
    monkeypatch.setattr(importer, "save_po", racing)
    with api(tmp_path) as c:
        [res] = do_import(c, ["4500012001"]).json()["results"]
        assert res["outcome"] == "skipped_exists"
        with closing(connect(c.db_path)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM purchase_orders WHERE po_number = '4500012001'").fetchone()[0] == 1


# ----------------------------------------------------------------------------------------------------- refusals (nothing written)

def test_refusals_write_nothing(tmp_path):
    with api(tmp_path) as c:
        before = db_counts(c)
        sha = preview(c)["feed"]["sha256"]
        r = c.post("/api/erp/import", json={"feed_sha256": sha, "po_numbers": NEW})
        assert r.status_code == 400 and r.json()["error"] == "not_confirmed"
        assert do_import(c, [], sha).json()["error"] == "nothing_picked"
        r = do_import(c, ["4500012001", "NOPE-1"], sha)
        assert r.status_code == 422 and r.json()["error"] == "not_in_feed" and r.json()["po_numbers"] == ["NOPE-1"]
        assert do_import(c, ["4500012001", "4500012001"], sha).json()["error"] == "duplicate_pick"
        assert c.post("/api/erp/import", json={"feed_sha256": "x", "po_numbers": NEW, "confirm": True}).status_code == 422
        assert db_counts(c) == before


def test_a_changed_feed_is_refused_with_a_fresh_preview(tmp_path):
    feed = tmp_path / "feed.json"
    shutil.copy(SAMPLE, feed)
    with api(tmp_path, settings=api_settings(tmp_path, erp_feed_path=feed)) as c:
        before = db_counts(c)
        sha = preview(c)["feed"]["sha256"]
        doc = json.loads(feed.read_text(encoding="utf-8"))
        doc["purchase_orders"][0]["total_amount"] = "487.00"
        feed.write_text(json.dumps(doc), encoding="utf-8")
        r = do_import(c, ["4500012001"], sha)
        assert r.status_code == 409 and r.json()["error"] == "feed_changed" and r.json()["preview"]["feed"]["sha256"] != sha
        assert db_counts(c) == before


def test_the_import_cap(tmp_path):
    with api(tmp_path, settings=api_settings(tmp_path, erp_max_import_per_action=2)) as c:
        r = do_import(c, NEW[:3])
        assert r.status_code == 422 and r.json()["error"] == "too_many"


def test_a_po_beyond_the_preview_cap_cannot_be_imported(tmp_path):
    with api(tmp_path, settings=api_settings(tmp_path, erp_max_pos_per_sync=2)) as c:
        assert do_import(c, ["4500012003"]).json()["error"] == "not_in_feed"


def test_feed_errors_are_422_with_a_message(tmp_path):
    bad = write_feed(tmp_path, {"format": "nope", "purchase_orders": []})
    with api(tmp_path, settings=api_settings(tmp_path, erp_feed_path=bad)) as c:
        r = c.get("/api/erp/preview")
        assert r.status_code == 422 and r.json()["error"] == "unknown_format"
    with api(tmp_path / "b", settings=api_settings(tmp_path / "b", erp_feed_path=tmp_path / "missing.json")) as c:
        assert c.get("/api/erp/preview").json()["error"] == "not_found"


def test_switched_off_means_404(tmp_path):
    with api(tmp_path, settings=api_settings(tmp_path, erp_feed_enabled=False)) as c:
        assert c.get("/api/erp/preview").status_code == 404
        assert c.post("/api/erp/import", json={"feed_sha256": "0" * 64, "po_numbers": ["x"], "confirm": True}).status_code == 404


def test_both_routes_are_behind_the_access_token(tmp_path):
    token = "t" * 32
    with api(tmp_path, settings=api_settings(tmp_path, access_token=token)) as c:
        assert c.get("/api/erp/preview").status_code == 401
        assert c.post("/api/erp/import", json={}).status_code == 401
        auth = {"Authorization": f"Bearer {token}"}
        sha = c.get("/api/erp/preview", headers=auth).json()["feed"]["sha256"]
        r = c.post("/api/erp/import", headers=auth, json={"feed_sha256": sha, "po_numbers": ["4500012001"], "confirm": True})
        assert r.status_code == 200 and r.json()["imported"] == 1


def test_a_small_feed_with_one_good_po(tmp_path):
    feed = write_feed(tmp_path, envelope(a_po("4599990001")))
    with api(tmp_path, settings=api_settings(tmp_path, erp_feed_path=feed)) as c:
        assert do_import(c, ["4599990001"]).json()["imported"] == 1


# ----------------------------------------------------------------------------------------------------- six real invoices

def six_outcomes(tmp: Path, *, import_feed: bool) -> dict[str, dict]:
    tmp.mkdir(parents=True, exist_ok=True)
    app, worker, db, settings = build_app(tmp, settings=gmail_settings(tmp), run_fn=HashRuns())
    out = {}
    with TestClient(app) as c:
        if import_feed:
            p = c.get("/api/erp/preview").json()
            numbers = [r["po_number"] for r in p["pos"] if r["class"] == "new"]
            body = c.post("/api/erp/import", json={"feed_sha256": p["feed"]["sha256"], "po_numbers": numbers, "confirm": True}).json()
            assert body["imported"] == 5
        ids = {}
        for name, (_, _, filename) in SIX.items():
            r = c.post("/api/runs", files={"file": (filename, real_pdf(name).read_bytes(), "application/octet-stream")})
            ids[name] = r.json()["run_id"]
        assert worker.wait_idle(180)
        for name, rid in ids.items():
            view = c.get(f"/api/runs/{rid}").json()
            out[name] = outcome(view) | {"candidates": [x["po_number"] for x in view["match"]["candidates"]]}
    return out


def test_the_six_real_invoices_are_unchanged_before_and_after_importing_the_whole_feed(tmp_path):
    before = six_outcomes(tmp_path / "before", import_feed=False)
    after = six_outcomes(tmp_path / "after", import_feed=True)
    for name, (decision, po, triggered) in EXPECTED.items():
        assert (before[name]["decision"], before[name]["matched_po"], before[name]["triggered"]) == (decision, po, triggered), name
        b = {k: v for k, v in before[name].items() if k not in ("candidates",)}
        a = {k: v for k, v in after[name].items() if k not in ("candidates",)}
        assert a == b, name
        assert after[name]["match_status"] == "matched"
