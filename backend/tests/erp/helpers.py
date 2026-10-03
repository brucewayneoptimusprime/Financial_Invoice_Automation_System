"""Shared helpers for the simulated ERP feed tests."""
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from app.config import Settings
from app.erp.preview import build_preview
from app.erp.source import FeedFile, feed_source, parse_feed
from tests.pipeline.helpers import demo_db

SAMPLE = Settings(_env_file=None).erp_feed_path


def sample_doc() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def feed_of(doc: dict, name: str = "test_feed.json") -> FeedFile:
    return parse_feed(json.dumps(doc).encode("utf-8"), name, 1024 * 1024)


def write_feed(tmp: Path, doc: dict, name: str = "feed.json") -> Path:
    p = tmp / name
    p.write_bytes(json.dumps(doc).encode("utf-8"))
    return p


def envelope(*pos: dict) -> dict:
    return {"format": "simerp.po-feed/v1", "system": "SIMERP", "purchase_orders": list(pos)}


def a_po(number="4599990001", **over) -> dict:
    po = {"po_number": number, "buyer_reference": "REQ-T", "erp_status": "RELEASED", "currency": "USD", "issued_date": "2026-09-01",
          "total_amount": "100.00", "vendor": {"name": "SuperStore", "tax_id": None, "address": {"country": "US"}},
          "lines": [{"line_number": 10, "description": "Test item", "quantity": "4", "unit_price": "25.00", "unit_of_measure": "EA",
                     "line_amount": "100.00"}]}
    po.update(over)
    return po


def preview_sample(tmp: Path, settings: Settings | None = None, conn: sqlite3.Connection | None = None) -> dict:
    settings = settings or Settings(_env_file=None)
    if conn is None:
        with closing(demo_db(tmp)) as c:
            return build_preview(c, settings, feed_source(settings).fetch())
    return build_preview(conn, settings, feed_source(settings).fetch())


def by_number(preview: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in preview["pos"]:
        out.setdefault(r["po_number"], []).append(r)
    return out


def codes(row: dict) -> list[str]:
    return [i["code"] for i in row["issues"]]


TABLES = ("vendors", "purchase_orders", "po_lines", "invoices", "ledger_entries", "po_consumption", "runs", "audit_events")


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
