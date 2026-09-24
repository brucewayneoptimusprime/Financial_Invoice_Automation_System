"""Shared setup for the end-to-end tests on the two REAL invoices, with hand-written procurement facts.

ingest (real PDF, real text layer) -> extract (recorded model reply, played back offline) -> grounding -> match -> validate -> decide.
"""
import json
from decimal import Decimal

from app.engine.engine import run_decide_stage, run_validate_stage
from app.engine.facts import POLineFact
from app.engine.matching import run_match_stage
from app.extraction.stage import run_extract_stage
from tests.engine.real import BUILTIN
from tests.extraction.real import real_ingest, real_reply
from tests.factories import make_facts, make_po, make_vendor
from tests.llm.fakes import FakeLLMClient, ok_response

D = Decimal

# what each real invoice bought (from the printed line item)
LINES = {
    "superstore_10963": POLineFact(line_no=1, description="Hewlett Fax Machine, Color", quantity=D(4), unit_price=D("1285.44"), amount=D("5141.76")),
    "superstore_24429": POLineFact(line_no=1, description="Hon Rocking Chair, Black", quantity=D(4), unit_price=D("461.48"), amount=D("1845.94")),
}
PO_TOTALS = {"superstore_10963": "6000.00", "superstore_24429": "2500.00"}      # comfortably more than the invoice


def facts_for(name: str, *, with_po: bool = True, po_total: str | None = None, net_committed: str = "0.00"):
    """A SuperStore vendor and (optionally) one matching, open PO with enough balance. Nothing is hard-coded in the app."""
    vendor = make_vendor(1, "SuperStore")
    pos = [make_po(id=1, po_number="PO-SS-1", vendor_id=1, currency="USD", total=po_total or PO_TOTALS[name],
                   net_committed=net_committed, lines=(LINES[name],))] if with_po else []
    return make_facts(vendors=[vendor], pos=pos)


def run_real(tmp_path, name: str, facts):
    """Returns the finished RunContext."""
    ctx, cfg = real_ingest(tmp_path, name)
    client = FakeLLMClient(ok_response(json.dumps(real_reply(name), ensure_ascii=False), input_tokens=6800, output_tokens=920))
    run_extract_stage(ctx, client, cfg)
    ctx.facts = facts
    run_match_stage(ctx)
    run_validate_stage(ctx, list(BUILTIN.values()))
    run_decide_stage(ctx)
    return ctx


def trail(ctx) -> list[tuple[str, str, int, str]]:
    """The full rule trail: (rule_id, outcome, severity, outcome_key) for every rule and floor, in engine order."""
    return [(r.rule_id, r.outcome.value, r.severity, r.outcome_key) for r in ctx.rule_results]
