"""Invoice line -> PO line matching (line-item consumption stage 2). Hand-built facts plus all six real invoices end to end."""
import json
from contextlib import closing
from decimal import Decimal

import pytest

from app.config import LineMatchConfig
from app.engine.facts import POFact, POLineFact
from app.engine.line_matching import codes_in, description_signal, match_lines
from app.engine.loader import load_facts
from app.enums import Decision, LineMatchStatus
from app.models.extraction import ExtractedInvoice, ExtractedLineItem
from tests.extraction.real import REAL
from tests.pipeline.helpers import controlled_variant_reply, demo_db, run_real

D = Decimal
CFG = LineMatchConfig()
SIX = ["superstore_10963", "superstore_24429", "superstore_14021", "superstore_6459", "superstore_14130", "iq_electronics"]


def po(*lines, number="PO-T-1"):
    return POFact(id=9, po_number=number, vendor_id=1, currency="USD", total_amount=D("100000"), status="open",
                  lines=tuple(POLineFact(line_no=i, id=100 + i, **ln) for i, ln in enumerate(lines, start=1)))


def inv(*lines):
    return ExtractedInvoice(line_items=[ExtractedLineItem(**ln) for ln in lines])


def L(desc, qty=None, price=None, amount=None, **kw):
    return {"description": desc, "quantity": None if qty is None else D(qty), "unit_price": None if price is None else D(price),
            "amount": None if amount is None else D(amount), **kw}


# ------------------------------------------------------------------------------------------ description measure

def test_containment_lifts_a_shorter_po_description():
    v, reasons = description_signal("Hon Rocking Chair, Black - Chairs, Furniture, FUR-CH-4682", None, "Hon Rocking Chair, Black", CFG)
    assert v == 1.0 and "(contained)" in reasons[0]


def test_different_item_codes_separate_similar_names():
    v, reasons = description_signal("Hewlett Fax Machine, Color; Copiers, Technology, TEC-CO-4575", None,
                                    "Canon Wireless Fax, Laser - Copiers, Technology, TEC-CO-3710", CFG)
    assert v == CFG.code_conflict_cap and "code:conflict" in reasons


def test_a_shared_item_code_is_decisive():
    v, reasons = description_signal("Chair black FUR-CH-4682", None, "Rocking chair (FUR-CH-4682)", CFG)
    assert v == 1.0 and any(r.startswith("code:exact") for r in reasons)
    assert description_signal("Widget", "WID-A1", "Widget large WID-A1", CFG)[0] == 1.0          # the line's own item_code counts


def test_codes_need_letters_and_digits():
    assert codes_in("TEC-CO-4575, well-known, 12-34, A-B") == {"TEC-CO-4575"}


def test_short_descriptions_do_not_use_containment():
    assert description_signal("Chair", None, "Chair with arms and wheels", CFG)[0] < 0.6


# ------------------------------------------------------------------------------------------ per-line results

def test_a_clean_line_for_line_match():
    s = match_lines(inv(L("Widget A", "10", "60.00", "600.00"), L("Widget B", "5", "80.00", "400.00")),
                    po({**L("Widget A", "10", "60.00", "600.00")}, {**L("Widget B", "5", "80.00", "400.00")},
                       {**L("Widget C", "1", "10.00", "10.00")}))
    assert s.mode == "line_level" and [(ln.status, ln.po_line_no, ln.po_line_id) for ln in s.lines] == [
        (LineMatchStatus.MATCHED, 1, 101), (LineMatchStatus.MATCHED, 2, 102)]
    assert s.lines[0].score == 1.0 and set(s.lines[0].candidates[0].breakdown) == {"description", "price", "quantity", "amount"}


def test_black_and_red_are_told_apart_by_price():
    s = match_lines(inv(L("Hon Rocking Chair, Black", "2", "461.48", "922.96")),
                    po({**L("Hon Rocking Chair, Black", "4", "461.48", "1845.92")}, {**L("Hon Rocking Chair, Red", "4", "399.00", "1596.00")}))
    assert s.lines[0].status is LineMatchStatus.MATCHED and s.lines[0].po_line_no == 1


def test_two_identical_po_lines_are_ambiguous():
    s = match_lines(inv(L("Widget A", "10", "60.00", "600.00")),
                    po({**L("Widget A (blue)", "10", "60.00", "600.00")}, {**L("Widget A (green)", "10", "60.00", "600.00")}))
    ln = s.lines[0]
    assert ln.status is LineMatchStatus.AMBIGUOUS and ln.po_line_id is None and len(ln.candidates) == 2 and s.mode == "partial"


def test_a_bundled_line_against_an_itemised_po_is_total_only():
    s = match_lines(inv(L("Goods as per purchase order", "1", "1000.00", "1000.00")),
                    po({**L("Widget A", "10", "60.00", "600.00")}, {**L("Widget B", "5", "80.00", "400.00")}))
    assert s.mode == "total_only" and s.bundled_hint and s.lines[0].status is LineMatchStatus.NO_MATCH and s.lines[0].candidates == []


def test_a_wrong_price_still_matches_confidently():
    s = match_lines(inv(L("Widget A", "10", "66.00", "660.00")), po({**L("Widget A", "10", "60.00", "600.00")}))
    ln = s.lines[0]
    # description 0.60 + price 0.15 x (1 - 10%/25%) + quantity 0.15 + amount 0.10 x (1 - 60/600)
    assert ln.status is LineMatchStatus.MATCHED and ln.score == pytest.approx(0.60 + 0.09 + 0.15 + 0.09)


def test_missing_prices_and_quantities_are_neutral():
    s = match_lines(inv(L("Widget A")), po({**L("Widget A")}))
    assert s.lines[0].score == pytest.approx(0.60 + 0.075 + 0.075 + 0.05) and s.lines[0].status is LineMatchStatus.MATCHED


def test_two_invoice_lines_on_one_po_line_share_its_remaining_quantity():
    s = match_lines(inv(L("Widget A", "6", "60.00", "360.00"), L("Widget A", "6", "60.00", "360.00")),
                    po({**L("Widget A", "10", "60.00", "600.00")}))
    first, second = s.lines
    assert first.status is LineMatchStatus.MATCHED and first.score == 1.0
    assert second.candidates[0].breakdown["quantity"] == pytest.approx(0.15 * (1 - 2 / 4))    # 6 wanted, 4 left


def test_line_assigned_consumption_reduces_a_line_but_total_only_does_not():
    consumed = po({**L("Widget A", "10", "60.00", "600.00"), "consumed_quantity": D("10"), "consumed_amount": D("600.00")})
    s = match_lines(inv(L("Widget A", "1", "60.00", "60.00")), consumed)
    assert s.lines[0].candidates[0].breakdown["quantity"] == 0 and "quantity:none_remaining" in s.lines[0].candidates[0].reasons


def test_not_evaluable_cases():
    assert match_lines(inv(L("x")), None).mode == "not_evaluable"
    assert match_lines(inv(), po({**L("Widget")})).reason == "the invoice has no line items"
    s = match_lines(inv(L("Widget")), po({**L(None)}))
    assert s.mode == "not_evaluable" and s.lines[0].status is LineMatchStatus.NOT_EVALUABLE
    s = match_lines(inv(L(None, "1", "1.00", "1.00"), L("Widget", "1", "1.00", "1.00")), po({**L("Widget", "1", "1.00", "1.00")}))
    assert [ln.status for ln in s.lines] == [LineMatchStatus.NOT_EVALUABLE, LineMatchStatus.MATCHED] and s.mode == "line_level"


def test_weights_come_from_config_and_are_deterministic():
    cfg = LineMatchConfig(weight_description=0.7, weight_price=0.1, weight_quantity=0.1, weight_amount=0.1)
    a = match_lines(inv(L("Widget A", "10", "66.00", "660.00")), po({**L("Widget A", "10", "60.00", "600.00")}), cfg)
    assert a.lines[0].candidates[0].breakdown["description"] == 0.7
    assert a == match_lines(inv(L("Widget A", "10", "66.00", "660.00")), po({**L("Widget A", "10", "60.00", "600.00")}), cfg)
    with pytest.raises(ValueError):
        LineMatchConfig(weight_description=0.9)


# ------------------------------------------------------------------------------------------ loader

def test_the_loader_reads_line_assigned_consumption_only(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        line = c.execute("SELECT pl.id FROM po_lines pl JOIN purchase_orders p ON p.id = pl.po_id WHERE p.po_number = 'PO-SS-005'").fetchone()[0]
        facts = load_facts(c)
        pl = next(p for p in facts.purchase_orders if p.po_number == "PO-SS-005").lines[0]
        assert pl.id == line and pl.consumed_quantity == 0 and pl.consumed_amount == 0             # the legacy row is against the total
        with c:
            c.execute("INSERT INTO ledger_entries (id, po_id, invoice_id, amount, type) VALUES (50, 5, 1, 30000, 'commit')")
            c.execute("INSERT INTO po_consumption (ledger_entry_id, po_id, po_line_id, invoice_id, quantity, amount, type, matched_by) "
                      "VALUES (50, 5, ?, 1, '2', 30000, 'commit', 'manual_reviewer')", (line,))
        pl = next(p for p in load_facts(c).purchase_orders if p.po_number == "PO-SS-005").lines[0]
        assert (pl.consumed_quantity, pl.consumed_amount, pl.remaining_quantity) == (D("2"), D("300.00"), D("5"))


# ------------------------------------------------------------------------------------------ the six real invoices

def reply(name, version):
    return json.loads((REAL / f"{name}.{version}.reply.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", SIX)
def test_each_real_invoice_matches_its_po_line_confidently_and_keeps_its_decision(tmp_path, name):
    """The current recordings (extract-v5, live 2026-09-25): all six review, each line matched to its PO's line."""
    with closing(demo_db(tmp_path)) as c:
        r = run_real(c, tmp_path, name, reply(name, "v5"))
        lm = r.ctx.line_matches
        assert r.decision is Decision.REVIEW                                   # unchanged: all six are review
        assert lm.mode == "line_level" and len(lm.lines) == 1
        ln = lm.lines[0]
        assert ln.status is LineMatchStatus.MATCHED and ln.po_line_no == 1 and ln.score >= 0.93
        ev = c.execute("SELECT detail FROM audit_events WHERE run_id = ? AND event_type = 'po_lines_matched'", (r.run_id,)).fetchone()
        assert json.loads(ev[0])["mode"] == "line_level"


def test_the_synthetic_approve_variant_still_approves(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        r = run_real(c, tmp_path, "superstore_24429", controlled_variant_reply("superstore_24429", "PO-SS-002"))
    assert r.decision is Decision.APPROVE and r.ctx.line_matches.mode == "line_level"


def test_no_line_matching_without_a_matched_po(tmp_path):
    with closing(demo_db(tmp_path)) as c:
        with c:
            c.execute("UPDATE purchase_orders SET vendor_id = 2 WHERE po_number LIKE 'PO-SS-%'")   # nothing left to match SuperStore
        r = run_real(c, tmp_path, "superstore_10963")
    assert r.ctx.line_matches.mode == "not_evaluable" and r.ctx.line_matches.po_id is None


@pytest.mark.parametrize("name", SIX)
def test_the_v4_replies_pinned_in_the_six_invoice_test_behave_the_same(tmp_path, name):
    """tests/pipeline/test_six_invoices.py pins the extract-v4 replies: IQ (no currency in v4) has no matched PO, so no line matching."""
    with closing(demo_db(tmp_path)) as c:
        r = run_real(c, tmp_path, name, reply(name, "v4"))
    if name == "iq_electronics":
        assert r.decision is Decision.REQUEST_INFO and r.ctx.line_matches.mode == "not_evaluable"
    else:
        assert r.decision is Decision.REVIEW and r.ctx.line_matches.mode == "line_level"
