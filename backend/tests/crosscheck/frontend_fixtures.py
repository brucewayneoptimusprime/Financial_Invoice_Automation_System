"""Records the cross-check section's frontend fixtures from the REAL endpoints (demo database, generated documents, a scripted model
double; no live call).

    cd backend && python -m tests.crosscheck.frontend_fixtures      writes frontend/src/test/fixtures/crosscheck_*.json

`test_crosscheck_frontend_fixtures_match_the_endpoints` regenerates them in memory and fails if the committed files drifted in shape.
"""
import json
import tempfile
from pathlib import Path

from app.config import ROOT_DIR
from app.llm.errors import ReplayMiss
from tests.crosscheck.test_c3_api import pdf, post, start

OUT = ROOT_DIR / "frontend" / "src" / "test" / "fixtures"
NAMES = ("crosscheck_info", "crosscheck_info_offline", "crosscheck_report", "crosscheck_budget")


def generate(tmp: Path) -> dict[str, dict]:
    src = tmp / "src"
    same, r1 = pdf(src, "delivery-note.pdf")
    differs, r2 = pdf(src, "goods-receipt.pdf", a=("Widget A", "8", "62.50", "500.00"), total="1,050.00",
                      extra=("Laminating pouches A4", "3", "12.00", "36.00"), hostile=True)
    r2["lines"][1]["quantity"] = "50"                                     # the page says 5: not confirmed, so not compared
    other, r3 = pdf(src, "other-vendor.pdf", vendor="Acme Trading", po="PO-2001", a=("Stapler, heavy duty", "1", "25.00", "25.00"),
                    b=None, total="25.00")
    unrecorded, _ = pdf(src, "unrecorded.pdf")
    out: dict[str, dict] = {}
    c, po_id = start(tmp / "live", r1, r2, r3, ReplayMiss("No recorded response for this request."))
    out["crosscheck_info"] = c.get(f"/api/pos/{po_id}/crosscheck").json() | {"mode": "live", "message": None}
    out["crosscheck_report"] = post(c, po_id, same, differs, other, ("empty.pdf", b"", "application/pdf"), unrecorded).json()
    out["crosscheck_report"]["analysis"]["mode"] = "live"
    c.__exit__(None, None, None)
    from decimal import Decimal

    from app.llm.budget import CostTracker
    c, po_id = start(tmp / "budget", r1, r1, tracker=CostTracker(Decimal("0.25"), Decimal("0.05")))
    out["crosscheck_budget"] = post(c, po_id, same, differs).json()
    c.__exit__(None, None, None)
    c, po_id = start(tmp / "offline", mode="offline")
    out["crosscheck_info_offline"] = c.get(f"/api/pos/{po_id}/crosscheck").json()
    c.__exit__(None, None, None)
    return out


def write(out: dict[str, dict]) -> None:
    for name, body in out.items():
        (OUT / f"{name}.json").write_bytes((json.dumps(body, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        write(generate(Path(tmp)))
    print(f"wrote {len(NAMES)} fixtures to {OUT}")
