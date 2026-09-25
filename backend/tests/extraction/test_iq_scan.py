"""The real scanned Indian GST invoice (IQ Electronics): a photo with no text layer, GSTIN, CGST 9% + SGST 9% printed as RATES only,
and the currency written in words ("Rupees Four Thousand Nine Hundred only"). The recorded response was captured live with the
earlier prompt (extract-v3); the model computed a tax amount of 882.00 that is not on the page.

No live calls: the recorded reply is played back through a scripted client. The tax-rate fix is a PROMPT change, so what it
does to a live model cannot be shown offline; these tests show (a) the prompt says it, (b) a reply that follows it is handled
end to end, and (c) the safety nets still catch a reply that does not.
"""
import json
from decimal import Decimal

import pytest

from app.config import Settings
from app.enums import GroundingStatus as G
from app.extraction.extractor import extract_invoice
from app.extraction.grounding import ground_invoice
from app.extraction.prompts import SYSTEM_PROMPT
from app.extraction.wire import from_wire
from app.models.extraction import ExtractedInvoice
from tests.engine.real import ev_builtin, pipeline
from tests.extraction.real import IQ, real_extraction, real_ingest, real_reply
from tests.extraction.test_grounding import CFG, fld, ground, invoice
from tests.extraction.wire_convert import entry, set_field
from tests.factories import make_facts, make_po, make_vendor
from tests.llm.fakes import FakeLLMClient, ok_response

D = Decimal
GSTIN = "36AAFCE1683D1ZT"


def rate_only_reply() -> dict:
    """The recorded reply edited into what extract-v4 asks for: no tax amount, the flag says tax is inside the total, the rates
    are in the notes. Everything else is exactly what the live model returned."""
    reply = real_reply(IQ)
    set_field(reply, "tax", found=False, value="", page=0, source_text="", confidence=0, flag="yes")
    reply["extraction_notes"] = ("CGST 9.00% and SGST 9.00% are printed as rates only; no tax amount is printed. The total equals the "
                                 "line total, so the tax appears to be included in the total.")
    return reply


def run_reply(tmp_path, reply):
    ctx, cfg = real_ingest(tmp_path, IQ)
    return ctx, cfg, extract_invoice(ctx.ingest, client=FakeLLMClient(ok_response(json.dumps(reply), input_tokens=5127, output_tokens=1296)),
                                     settings=cfg, run_id=ctx.run_id)


# ------------------------------------------------------------------------------------- the document

def test_the_scan_has_no_text_layer_so_extraction_is_vision_only(tmp_path):
    ctx, cfg, out = real_extraction(tmp_path, IQ)
    assert not ctx.ingest.text_layer.usable and out.meta.path == "vision_only" and not out.meta.degraded


# --------------------------------------------------------------------------------- fix 1: tax rates only

def test_the_prompt_forbids_computing_a_tax_amount_from_a_rate():
    for clause in ("If only tax RATES or percentages are printed", "set tax found to false", "NEVER compute a tax amount yourself",
                   "never multiply a rate by an amount", "Put the printed rates in extraction_notes"):
        assert clause in SYSTEM_PROMPT, clause


def test_the_recorded_reply_is_the_old_prompts_mistake():
    """Documents the input: the live model (extract-v3) computed 9% + 9% of 4,900 = 882.00 itself."""
    tax = entry(real_reply(IQ), "tax")
    assert tax["found"] is True and tax["value"] == "882.00" and tax["source_text"] == "CGST% 9.00 SGST% 9.00"


def test_a_rate_only_reply_gives_null_tax_with_the_flag_kept():
    contract, _ = from_wire(rate_only_reply())
    assert contract["tax"]["value"] is None and contract["tax"]["included_in_total"] is True
    assert contract["tax"]["confidence"] == 0.0 and contract["tax"]["source_text"] is None


@pytest.mark.parametrize("flag,expected", [("yes", True), ("no", False), ("unknown", None)])
def test_the_tax_flag_survives_not_found_but_no_other_flag_does(flag, expected):
    reply = real_reply(IQ)
    set_field(reply, "tax", found=False, value="", page=0, source_text="", confidence=0, flag=flag)
    set_field(reply, "po_reference", found=False, value="", page=0, source_text="", confidence=0, flag=flag)
    set_field(reply, "vendor_name", flag=flag)                                  # a flag on any other name is ignored, as before
    contract, _ = from_wire(reply)
    assert contract["tax"]["included_in_total"] is expected
    assert contract["po_reference"]["explicit"] is None


def test_the_tax_flag_still_works_when_tax_is_found():
    reply = real_reply(IQ)
    set_field(reply, "tax", flag="no")
    assert from_wire(reply)[0]["tax"]["included_in_total"] is False


def test_a_rate_only_reply_end_to_end_tax_is_null_included_and_the_arithmetic_holds(tmp_path):
    ctx, cfg, out = run_reply(tmp_path, rate_only_reply())
    inv = out.invoice
    assert not out.meta.degraded and inv.tax.value is None and inv.tax.included_in_total is True
    assert inv.tax.grounding is None                                             # a null field is not graded
    assert "CGST 9.00% and SGST 9.00%" in inv.extraction_notes
    assert inv.subtotal.value == inv.total.value == D("4900.00") and not inv.adjustments
    from tests.factories import make_ctx
    r = ev_builtin("r_arithmetic", make_ctx(extracted=inv))
    assert r.outcome.value == "pass"
    assert {c["check"] for c in r.detail["checks"]} >= {"total_equals_subtotal_tax_included", "line_math"}


def test_the_old_reply_still_cannot_slip_a_made_up_tax_through(tmp_path):
    """The safety nets, for a model that ignores the prompt: the invented amount is capped at 0.3 and the arithmetic flags it."""
    ctx, cfg, out = real_extraction(tmp_path, IQ)
    inv = out.invoice
    assert inv.tax.value == D("882.00") and inv.tax.grounding is G.VALUE_MISMATCH and inv.tax.confidence == 0.3
    from tests.factories import make_ctx
    r = ev_builtin("r_arithmetic", make_ctx(extracted=inv))
    assert r.outcome.value == "flag" and r.detail["failed"] == ["subtotal_plus_tax_equals_total"]


# ---------------------------------------------------------------------- fix 2: a currency named in words

def test_the_recorded_currency_read_from_words_is_no_longer_a_mismatch(tmp_path):
    """Before: INR from "Rupees Four Thousand Nine Hundred only" was value_mismatch at confidence 0.3."""
    ctx, cfg, out = real_extraction(tmp_path, IQ)
    cur = out.invoice.currency
    assert cur.value == "INR" and cur.source_text == "Rupees   Four Thousand Nine Hundred  only"
    assert cur.grounding is G.UNAVAILABLE                                         # a scan has no text layer to check further
    assert cur.grounding is not G.VALUE_MISMATCH and cur.confidence == cur.model_confidence == 0.85
    assert out.meta.grounding.get("value_mismatch") == 1                          # only the invented tax amount remains


def test_the_scans_other_fields_are_as_the_live_model_read_them(tmp_path):
    ctx, cfg, out = real_extraction(tmp_path, IQ)
    inv = out.invoice
    assert inv.vendor_tax_id.value == GSTIN and inv.invoice_number.value == "1801/24/S-3641" and inv.document_type.value == "invoice"
    assert inv.invoice_date.value.isoformat() == "2025-01-05" and inv.invoice_date.confidence == 0.5       # 05-01-2025 is ambiguous: review
    assert inv.total.value == D("4900.00") and inv.line_items[0].quantity == D("1")


@pytest.mark.parametrize("source,code,status", [
    ("Rupees Four Thousand Nine Hundred only", "INR", G.EXACT), ("INDIAN RUPEES 4,900", "INR", G.EXACT),
    ("Amount in US Dollars", "USD", G.EXACT), ("Australian Dollars", "AUD", G.EXACT), ("Euros", "EUR", G.EXACT),
    ("Pounds Sterling", "GBP", G.EXACT)])
def test_currency_names_agree_with_their_code(source, code, status):
    inv = invoice(currency=fld(code, source))
    ground(inv, {1: source + "\n"})
    assert inv.currency.grounding is status


@pytest.mark.parametrize("source,code", [
    ("Rupees Four Thousand", "USD"), ("Amount in US Dollars", "AUD"), ("Australian Dollars", "USD"), ("Rupeesx four", "INR"),
    ("Four Thousand Nine Hundred only", "INR")])
def test_a_wrong_or_absent_name_is_still_a_mismatch(source, code):
    inv = invoice(currency=fld(code, source))
    ground(inv, {1: source + "\n"})
    assert inv.currency.grounding is G.VALUE_MISMATCH and inv.currency.confidence == 0.3


def test_a_currency_named_elsewhere_on_the_page_is_value_present():
    inv = invoice(currency=fld("INR", "Currency: INR"))
    ground(inv, {1: "Total\nRupees Four Thousand Nine Hundred only\n"})
    assert inv.currency.grounding is G.VALUE_PRESENT


def test_the_name_table_is_config_and_only_consulted_for_currency():
    cfg = Settings(_env_file=None, currency_name_map={"kroner": "DKK"})
    inv = invoice(currency=fld("DKK", "Fire tusind kroner"), vendor_name=fld("Rupees Trading", "Rupees Trading"))
    ground_invoice(inv, {1: "Fire tusind kroner\nRupees Trading\n"}, True, cfg)
    assert inv.currency.grounding is G.EXACT and inv.vendor_name.grounding is G.EXACT
    old = invoice(currency=fld("INR", "Rupees"))
    ground_invoice(old, {1: "Rupees\n"}, True, cfg)
    assert old.currency.grounding is G.VALUE_MISMATCH                              # the default table is replaced, not merged
    assert "rupees" in CFG.currency_name_map and CFG.currency_name_map["rupees"] == "INR"


# ------------------------------------------------------------------- the whole scan through the rules

def iq_facts():
    vendor = make_vendor(7, "Electronics Mart India Limited")
    vendor = vendor.model_copy(update={"tax_id": GSTIN})
    from app.engine.facts import POLineFact
    line = POLineFact(line_no=1, description="APPLE IP 16 PRO MAX SL CS MGS PLM MYYW3Z", quantity=D(1), unit_price=D("4900.00"), amount=D("4900.00"))
    po = make_po(id=7, po_number="PO-IQ-1", vendor_id=7, currency="INR", total="5000.00", lines=(line,))
    return make_facts(vendors=[vendor], pos=[po])


def test_the_rate_only_scan_runs_through_the_rules_and_only_the_ambiguous_date_holds_it_back(tmp_path):
    ctx, cfg, out = run_reply(tmp_path, rate_only_reply())
    ctx_run, res, _ = pipeline(out.invoice, iq_facts(), run_id="iq")
    assert ctx_run.matched_vendor.method == "tax_id" and ctx_run.matched_vendor.vendor_id == 7        # the real GSTIN resolves the vendor
    assert res["r_arithmetic"].outcome.value == "pass" and res["r_currency_mismatch"].outcome.value == "pass"
    assert res["r_vendor_status"].outcome.value == "pass"
    low = res["r_extraction_confidence"]
    assert low.outcome.value == "flag" and [x["field"] for x in low.detail["low_confidence"]] == ["invoice_date"]
    assert ctx_run.decision.value == "review"


def test_the_old_reply_is_held_back_by_the_date_the_tax_and_no_longer_the_currency(tmp_path):
    ctx, cfg, out = real_extraction(tmp_path, IQ)
    ctx_run, res, _ = pipeline(out.invoice, iq_facts(), run_id="iq-old")
    low = res["r_extraction_confidence"]
    assert [x["field"] for x in low.detail["low_confidence"]] == ["invoice_date"]            # currency is not in the list any more
    assert res["r_arithmetic"].outcome.value == "flag" and ctx_run.decision.value == "review"


# ------------------------------------------------------------- the same scan re-recorded live with extract-v4

def v4_reply() -> dict:
    from tests.extraction.real import REAL
    return json.loads((REAL / "iq_electronics.v4.reply.json").read_text(encoding="utf-8"))


def test_the_live_v4_reply_has_null_tax_flagged_included_and_the_rates_in_the_notes(tmp_path):
    """Recorded live with extract-v4 (5,363 in / 1,292 out): the tax fix works on a real model."""
    ctx, cfg, out = run_reply(tmp_path, v4_reply())
    inv = out.invoice
    assert inv.tax.value is None and inv.tax.included_in_total is True
    assert "CGST% and SGST% (9.00 each) are printed as rates" in inv.extraction_notes
    assert inv.total.value == D("4900.00") and inv.vendor_tax_id.value == GSTIN
    assert inv.invoice_date.confidence == 0.5 and out.meta.grounding == {"unavailable": 8}


def test_the_live_v4_reply_did_not_return_the_currency_which_the_rules_treat_as_missing(tmp_path):
    """A real finding: the words "Rupees ... only" were not returned as a currency this time (the v3 call did return INR).
    Currency is a required field, so this scan would be asked back from the vendor although the currency is printed."""
    ctx, cfg, out = run_reply(tmp_path, v4_reply())
    assert out.invoice.currency.value is None and out.invoice.subtotal.value is None
    ctx_run, res, _ = pipeline(out.invoice, iq_facts(), run_id="iq-v4")
    assert res["r_arithmetic"].outcome.value == "pass"                       # null tax + included + no subtotal: only line math runs
    required = res["r_required_fields"]
    assert required.outcome.value == "flag" and required.detail["missing"] == ["currency"]
    assert ctx_run.decision.value == "request_info"

