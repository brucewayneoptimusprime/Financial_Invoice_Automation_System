"""The cross-check section's frontend fixtures are recorded from the real endpoints; this fails if the committed files drifted in
shape, and pins what the frontend tests rely on."""
import json

from tests.crosscheck.frontend_fixtures import NAMES, OUT, generate
from tests.gmail.test_stage5_backend import _shape


def test_crosscheck_frontend_fixtures_match_the_endpoints(tmp_path):
    fresh = generate(tmp_path)
    assert set(fresh) == set(NAMES)
    for name in NAMES:
        committed = json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))
        assert _shape(committed) == _shape(fresh[name]), f"{name}.json drifted: run `python -m tests.crosscheck.frontend_fixtures`"
    docs = fresh["crosscheck_report"]["documents"]
    assert [d["status"] for d in docs] == ["analysed", "analysed", "analysed", "failed", "failed"]
    assert [d["relevance"]["related"] for d in docs[:3]] == [True, True, False] and docs[0]["differences"] == []
    assert [x["type"] for x in docs[1]["differences"]] == ["item_not_on_po", "quantity_vs_po", "unit_price", "amount"]
    assert [u["what"] for u in docs[1]["unconfirmed"]] == ["line 2"] and len(docs[1]["notices"]) == 1
    assert [d["failure"]["code"] for d in docs[3:]] == ["empty_file", "replay_miss"]
    assert fresh["crosscheck_budget"]["error"] == "budget" and fresh["crosscheck_info_offline"]["available"] is False
