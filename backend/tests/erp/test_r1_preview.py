"""ERP feed stage R1: the source, the simerp-v1 adapter, the registry and the read-only preview (ERP_PLAN sections 1, 2, 5)."""
import subprocess
from contextlib import closing
from decimal import Decimal

import pytest

from app.config import ROOT_DIR, Settings
from app.db.connection import connect
from app.erp import LABEL
from app.erp.adapters import FeedPO, FeedVendor, adapter_for
from app.erp.preview import build_preview, classify, duplicate_keys
from app.erp.source import FeedError, FileFeedSource, parse_feed
from app.po.models import NewVendorIn, POCreate, POLineIn
from app.po.validate import validate_po
from tests.erp.helpers import (SAMPLE, a_po, by_number, codes, counts, envelope, feed_of, preview_sample, sample_doc, write_feed)
from tests.pipeline.helpers import demo_db


# ----------------------------------------------------------------------------------------------------- the bundled sample feed

def test_the_sample_feed_is_bundled_labelled_and_found_through_config():
    assert SAMPLE == ROOT_DIR / "data" / "erp_feed_sample.json" and SAMPLE.is_file()
    assert "Simulated ERP (demo)" in sample_doc()["_SIMULATED"]
    ignored = subprocess.run(["git", "check-ignore", "-q", str(SAMPLE)], cwd=ROOT_DIR, capture_output=True)
    assert ignored.returncode == 1, "the sample feed must not be gitignored: a deployed build reads it from its checkout"


def test_the_feed_path_is_not_moved_under_data_dir(tmp_path):
    s = Settings(_env_file=None, data_dir=tmp_path)
    assert s.db_path == tmp_path / "app.db" and s.erp_feed_path == SAMPLE


def test_every_sample_case_is_classified_with_its_reason(tmp_path):
    p = preview_sample(tmp_path)
    rows = by_number(p)
    assert p["label"] == LABEL and p["counts"] == {"new": 5, "exists": 1, "problem": 7}
    assert p["feed"] | {"sha256": None} == {"name": "erp_feed_sample.json", "sha256": None, "format": "simerp.po-feed/v1",
                                            "adapter": "simerp-v1", "system": "SIMERP", "exported_at": "2026-10-03T09:00:00Z",
                                            "count": 13, "shown": 13, "truncated": 0}
    for n in ("4500012001", "4500012002", "4500012003", "4500012004", "4500012005"):
        assert rows[n][0]["class"] == "new", n
    [exists] = rows["PO-SS-005"]
    assert exists["class"] == "exists" and exists["existing_po"]["po_number"] == "PO-SS-005" and codes(exists) == ["exists"]
    [look] = rows["po-ss-002"]
    assert look["class"] == "problem" and look["existing_po"]["po_number"] == "PO-SS-002" and codes(look) == ["similar"]
    assert "looks like existing PO-SS-002" in look["issues"][0]["message"]
    assert codes(rows["4500012006"][0]) == ["required"] and rows["4500012006"][0]["issues"][0]["field"] == "currency"
    assert codes(rows["4500012007"][0]) == ["line_math"] and rows["4500012007"][0]["class"] == "problem"
    assert codes(rows["4500012008"][0]) == ["negative"] and "cannot be negative (-3)" in rows["4500012008"][0]["issues"][0]["message"]
    assert [r["class"] for r in rows["4500012009"]] == ["problem", "problem"]
    assert all(codes(r) == ["duplicate_in_feed"] for r in rows["4500012009"])
    assert codes(rows["4500012010"][0]) == ["not_released"] and "CANCELLED" in rows["4500012010"][0]["issues"][0]["message"]
    assert all(i["blocks"] for r in p["pos"] if r["class"] != "new" for i in r["issues"])


def test_vendors_are_resolved_like_the_rest_of_the_system(tmp_path):
    rows = by_number(preview_sample(tmp_path))
    assert rows["4500012001"][0]["vendor"] | {"id": 0} == {"kind": "existing", "id": 0, "name": "SuperStore", "status": "approved",
                                                           "matched_by": "name"}
    assert rows["4500012002"][0]["vendor"]["matched_by"] == "alias"                   # "Super Store"
    assert rows["4500012003"][0]["vendor"]["matched_by"] == "tax ID"                  # "Electronics Mart India Ltd" + GSTIN
    nw = rows["4500012004"][0]
    assert nw["vendor"] == {"kind": "new", "name": "Northwind Office Supplies Ltd", "tax_id": "GB123456789", "country": "GB",
                            "status": "new"}
    assert codes(nw) == ["new_vendor"] and nw["issues"][0]["blocks"] is False          # the form's warning, shown, not blocking
    assert rows["4500012005"][0]["currency"] == "EUR"


def test_the_preview_writes_nothing(tmp_path):
    db = tmp_path / "app.db"
    with closing(demo_db(tmp_path)) as conn:
        before_counts = counts(conn)
    before = db.read_bytes()
    s = Settings(_env_file=None)
    with closing(connect(db)) as conn:
        build_preview(conn, s, FileFeedSource(s).fetch())
        build_preview(conn, s, FileFeedSource(s).fetch())
        assert counts(conn) == before_counts
    assert db.read_bytes() == before


# ----------------------------------------------------------------------------------------------------- parity with the PO form

def _same(row_issues, form_issues):
    return [(i["field"], i["level"], i["code"], i["message"]) for i in row_issues] == \
           [(i.field, i.level, i.code, i.message) for i in form_issues]


def test_the_preview_reports_exactly_the_forms_issues(tmp_path):
    s = Settings(_env_file=None)
    rows = by_number(preview_sample(tmp_path))
    with closing(demo_db(tmp_path, "form.db")) as conn:
        # line math (warning on the form, a problem for a feed PO), as the person would type it on the form
        form, _ = validate_po(POCreate(po_number="4500012007", vendor_id=1, currency="USD", total="109.95", issued_date="2026-09-28",
                                       lines=[POLineIn(description="Ballpoint pens, blue, box of 50", quantity="5", unit_price="19.99",
                                                       amount="109.95")]), conn, s)
        assert _same(rows["4500012007"][0]["issues"], form)
        form, _ = validate_po(POCreate(po_number="4500012006", vendor_id=1, total="150.00", issued_date="2026-09-28",
                                       lines=[POLineIn(description="Desk organiser, mesh", quantity="10", unit_price="15.00",
                                                       amount="150.00")]), conn, s)
        assert _same(rows["4500012006"][0]["issues"], form)
        form, _ = validate_po(POCreate(po_number="4500012004", currency="USD", total="2150.00", issued_date="2026-09-28",
                                       lines=[POLineIn(description="A4 copier paper, 80 gsm, 5-ream carton", quantity="50",
                                                       unit_price="43.00", amount="2150.00")]),
                              conn, s, NewVendorIn(name="Northwind Office Supplies Ltd", tax_id="GB123456789", country="GB"))
        assert _same(rows["4500012004"][0]["issues"], form)
        form, _ = validate_po(POCreate(po_number="po-ss-002", vendor_id=1, currency="USD", total="95.00", issued_date="2026-09-28",
                                       lines=[POLineIn(description="Stapler, heavy duty", quantity="5", unit_price="19.00",
                                                       amount="95.00")]), conn, s)
        assert _same(rows["po-ss-002"][0]["issues"], form)


@pytest.mark.parametrize("over, field, code", [
    ({"po_number": None}, "po_number", "required"),
    ({"total_amount": "12.345"}, "total", "too_many_decimals"),
    ({"total_amount": "-5.00"}, "total", "negative"),
    ({"issued_date": "28/09/2026"}, "issued_date", "invalid"),
    ({"currency": "JPY"}, "currency", "unsupported"),
    ({"currency": "dollars"}, "currency", "invalid"),
])
def test_field_rules_come_from_the_form(tmp_path, over, field, code):
    with closing(demo_db(tmp_path)) as conn:
        row = build_preview(conn, Settings(_env_file=None), feed_of(envelope(a_po(**over))))["pos"][0]
    assert row["class"] == "problem" and (field, code) in [(i["field"], i["code"]) for i in row["issues"]]


def test_lines_not_adding_up_to_the_total_is_a_problem_for_a_feed_po(tmp_path):
    with closing(demo_db(tmp_path)) as conn:
        row = build_preview(conn, Settings(_env_file=None), feed_of(envelope(a_po(total_amount="150.00"))))["pos"][0]
    assert row["class"] == "problem" and codes(row) == ["lines_sum"]


def test_a_blocked_vendor_is_a_warning_on_a_new_row_as_on_the_form(tmp_path):
    with closing(demo_db(tmp_path)) as conn:
        conn.execute("UPDATE vendors SET status = 'blocked' WHERE name = 'SuperStore'")
        conn.commit()
        row = build_preview(conn, Settings(_env_file=None), feed_of(envelope(a_po())))["pos"][0]
    assert row["class"] == "new" and codes(row) == ["blocked"]


def test_an_ambiguous_vendor_is_a_problem_not_a_guess(tmp_path):
    with closing(demo_db(tmp_path)) as conn:
        conn.execute("INSERT INTO vendors (name, aliases, tax_id, country, status) VALUES ('Northwind Traders', '[]', 'GB999', 'GB', 'approved')")
        conn.execute("INSERT INTO vendors (name, aliases, tax_id, country, status) VALUES ('Northwind Trading', '[]', NULL, 'GB', 'approved')")
        conn.commit()
        doc = envelope(a_po(vendor={"name": "Northwind Trading", "tax_id": "GB999"}))
        row = build_preview(conn, Settings(_env_file=None), feed_of(doc))["pos"][0]
    assert row["class"] == "problem" and codes(row) == ["ambiguous"] and row["vendor"]["kind"] == "ambiguous"
    assert "choose the vendor" in row["issues"][0]["message"]


def test_no_vendor_at_all_is_the_forms_required_error(tmp_path):
    with closing(demo_db(tmp_path)) as conn:
        row = build_preview(conn, Settings(_env_file=None), feed_of(envelope(a_po(vendor=None))))["pos"][0]
    assert row["class"] == "problem" and codes(row) == ["required"] and row["issues"][0]["field"] == "vendor"


def test_zero_quantity_and_an_exact_existing_number_after_trimming(tmp_path):
    lines = [{"line_number": 10, "description": "x", "quantity": "0", "unit_price": "1.00", "line_amount": "0.00"}]
    with closing(demo_db(tmp_path)) as conn:
        p = build_preview(conn, Settings(_env_file=None), feed_of(envelope(a_po(lines=lines, total_amount="0.00"), a_po(" PO-SS-001 "))))
    assert "not_positive" in codes(p["pos"][0]) and p["pos"][1]["class"] == "exists"


# ----------------------------------------------------------------------------------------------------- robustness

@pytest.mark.parametrize("raw, code", [(b"not json", "not_json"), (b"[1, 2]", "not_an_object"), (b"\xff\xfe\x00", "not_json")])
def test_unreadable_feeds_are_refused_with_a_message(raw, code):
    with pytest.raises(FeedError) as e:
        parse_feed(raw, "f.json", 1024)
    assert e.value.code == code


def test_a_feed_over_the_size_cap_is_refused_before_it_is_read(tmp_path):
    path = write_feed(tmp_path, {"format": "simerp.po-feed/v1", "purchase_orders": [a_po()] * 20})
    s = Settings(_env_file=None, erp_feed_path=path, erp_feed_max_bytes=1024)
    with pytest.raises(FeedError) as e:
        FileFeedSource(s).fetch()
    assert e.value.code == "too_large"


def test_a_missing_feed_file_is_a_message_naming_the_file_not_a_path(tmp_path):
    with pytest.raises(FeedError) as e:
        FileFeedSource(Settings(_env_file=None, erp_feed_path=tmp_path / "nowhere.json")).fetch()
    assert e.value.code == "not_found" and str(tmp_path) not in e.value.message


@pytest.mark.parametrize("doc, code", [({"format": "sap.idoc/ORDERS05", "purchase_orders": []}, "unknown_format"),
                                       ({"format": "simerp.po-feed/v1"}, "no_purchase_orders"),
                                       ({"format": "simerp.po-feed/v1", "purchase_orders": {"a": 1}}, "no_purchase_orders")])
def test_envelope_problems(tmp_path, doc, code):
    with closing(demo_db(tmp_path)) as conn, pytest.raises(FeedError) as e:
        build_preview(conn, Settings(_env_file=None), feed_of(doc))
    assert e.value.code == code


def test_malformed_entries_become_problem_rows_never_a_crash(tmp_path):
    long_number = "4" * 70
    doc = envelope("not an object", a_po("4521", vendor="SuperStore"), a_po("4522", lines="none"),
                   a_po("4523", lines=[1, {"description": "ok", "quantity": {"x": 1}, "unit_price": "2", "line_amount": "2"}]),
                   a_po(long_number), a_po("4525", erp_status=None), a_po("4526", total_amount=True))
    with closing(demo_db(tmp_path)) as conn:
        rows = build_preview(conn, Settings(_env_file=None), feed_of(doc))["pos"]
    assert [r["class"] for r in rows] == ["problem"] * 7
    assert codes(rows[0]) == ["not_an_object"]
    assert "not_an_object" in codes(rows[1]) and "not_a_list" in codes(rows[2])
    assert {"not_an_object", "not_a_value"} <= set(codes(rows[3]))
    assert "too_long" in codes(rows[4]) and len(rows[4]["po_number"]) == 64
    assert codes(rows[5]) == ["required"] and rows[5]["issues"][0]["field"] == "erp_status"
    assert "not_a_value" in codes(rows[6])


# ----------------------------------------------------------------------------------------------------- caps

def test_the_po_cap_shows_the_first_n_and_says_how_many_were_left_out(tmp_path):
    p = preview_sample(tmp_path, Settings(_env_file=None, erp_max_pos_per_sync=3))
    assert len(p["pos"]) == 3 and p["feed"]["truncated"] == 10 and p["feed"]["count"] == 13


def test_a_duplicate_beyond_the_cap_still_marks_both_copies(tmp_path):
    doc = envelope(a_po("4511"), a_po("4512"), a_po("4511"))
    with closing(demo_db(tmp_path)) as conn:
        p = build_preview(conn, Settings(_env_file=None, erp_max_pos_per_sync=1), feed_of(doc))
    assert codes(p["pos"][0]) == ["duplicate_in_feed"]


def test_the_line_cap_is_a_problem(tmp_path):
    lines = [{"line_number": i, "description": f"item {i}", "quantity": "1", "unit_price": "1.00", "line_amount": "1.00"} for i in range(5)]
    with closing(demo_db(tmp_path)) as conn:
        row = build_preview(conn, Settings(_env_file=None, po_max_lines=3), feed_of(envelope(a_po(lines=lines, total_amount="5.00"))))["pos"][0]
    assert row["class"] == "problem" and "too_many" in codes(row) and len(row["lines"]) == 3


# ----------------------------------------------------------------------------------------------------- mapping and registry

def test_simerp_mapping_keeps_money_exact_and_erp_facts(tmp_path):
    raw = ('{"format": "simerp.po-feed/v1", "purchase_orders": [{"po_number": "45", "erp_status": "approved", "currency": "usd", '
           '"total_amount": 0.1, "vendor": {"name": "SuperStore"}, "lines": [{"line_number": 10, "description": "a", "quantity": 1, '
           '"unit_price": 0.1, "unit_of_measure": "EA", "line_amount": 0.1}]}]}').encode()
    feed = parse_feed(raw, "n.json", 4096)
    assert feed.doc["purchase_orders"][0]["total_amount"] == Decimal("0.1")              # no float on the way in
    [fp] = adapter_for(feed.doc).parse(feed.doc, max_lines=200)
    assert (fp.create.total, fp.create.lines[0].unit_price, fp.erp_status, fp.line_uom, fp.erp_line_numbers) == \
           ("0.1", "0.1", "approved", ["EA"], [10])
    with closing(demo_db(tmp_path)) as conn:
        row = build_preview(conn, Settings(_env_file=None), feed)["pos"][0]
        assert row["class"] == "new" and row["lines"][0] | {} == {"line_number": 10, "description": "a", "quantity": "1",
                                                                  "unit_price": "0.1", "amount": "0.1", "unit_of_measure": "EA"}


class _CoupaLike:
    """A test-only second adapter: the shape a second ERP format takes (different field names, same FeedPO out)."""
    key = "coupa-like-test"
    formats = ("coupa-like/test",)

    def parse(self, doc, *, max_lines):
        return [FeedPO(index=i, create=POCreate(po_number=o["poNumber"], currency=o["currency"]["code"], total=o["total"],
                                                lines=[POLineIn(description=ln["description"], quantity=ln["quantity"],
                                                                unit_price=ln["price"], amount=ln["total"]) for ln in o["orderLines"]]),
                       vendor=FeedVendor(name=o["supplier"]["displayName"], tax_id=None, country=None), erp_status="RELEASED")
                for i, o in enumerate(doc["orders"])]


def test_a_second_adapter_plugs_into_the_same_classification(tmp_path):
    doc = {"format": "coupa-like/test", "orders": [{"poNumber": "C-1", "currency": {"code": "USD"}, "total": "50.00",
                                                     "supplier": {"displayName": "SuperStore"},
                                                     "orderLines": [{"description": "x", "quantity": "2", "price": "25.00", "total": "50.00"}]}]}
    adapter = adapter_for(doc, adapters=(_CoupaLike(),))
    pos = adapter.parse(doc, max_lines=200)
    with closing(demo_db(tmp_path)) as conn:
        c = classify(conn, Settings(_env_file=None), pos[0], duplicate_keys(pos))
    assert c.row["class"] == "new" and c.row["vendor"]["name"] == "SuperStore" and c.parsed.total_minor == 5000
    with pytest.raises(FeedError):
        adapter_for(doc)                                    # not registered in the real registry


def test_no_model_is_involved():
    for p in (ROOT_DIR / "backend" / "app" / "erp").rglob("*.py"):
        src = p.read_text(encoding="utf-8")
        assert not any(k in src for k in ("anthropic", "llm", "client.messages", "MeteredClient")), p.name
