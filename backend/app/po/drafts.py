"""Model drafts of a PO are FILES, never database rows (owner decision 5): `data/po_drafts/<draft_id>/draft.json`.

A draft is what the model proposed. It is never saved as a PO by itself: the person confirms values on the form and Save posts
those. When Save names a draft, `edited_fields` records which fields the person changed from it.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings
from app.extraction.parsing import normalize_amount_string
from app.ingest.store import check_run_id
from app.po.models import SaveRequest

DRAFT_FILE = "draft.json"
HEADER_KEYS = ("po_number", "currency", "total", "issued_date")


def new_draft_id() -> str:
    return uuid.uuid4().hex


def draft_dir(settings: Settings, draft_id: str) -> Path:
    return Path(settings.po_drafts_dir) / check_run_id(draft_id)


def write_draft(settings: Settings, draft_id: str, record: dict[str, Any]) -> Path:
    folder = draft_dir(settings, draft_id)
    folder.mkdir(parents=True, exist_ok=True)
    record = {"draft_id": draft_id, "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), **record}
    path = folder / DRAFT_FILE
    path.write_text(json.dumps(record, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


def load_draft(settings: Settings, draft_id: str) -> dict[str, Any] | None:
    try:
        path = draft_dir(settings, draft_id) / DRAFT_FILE
    except ValueError:
        return None
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _same(a: Any, b: Any, money: bool = False) -> bool:
    a = None if a in ("", None) else str(a).strip()
    b = None if b in ("", None) else str(b).strip()
    if money and a is not None and b is not None:
        na, nb = normalize_amount_string(a), normalize_amount_string(b)
        if na is not None and nb is not None:
            try:
                from decimal import Decimal
                return Decimal(na) == Decimal(nb)
            except ArithmeticError:
                pass
    if a is not None and b is not None:
        return a.casefold() == b.casefold()
    return a == b


def edited_fields(draft: dict[str, Any], req: SaveRequest) -> list[str]:
    """Which fields the person changed from the model's draft (names as on the form)."""
    values = draft.get("values") or {}
    po = req.po
    changed = [k for k in HEADER_KEYS if not _same(values.get(k), getattr(po, k), money=(k == "total"))]
    suggested = draft.get("suggested_vendor_id")
    if req.new_vendor is not None:
        if suggested is not None or not _same(values.get("vendor_name"), req.new_vendor.name):
            changed.append("vendor")
    elif po.vendor_id != suggested:
        changed.append("vendor")
    d_lines = values.get("lines") or []
    if len(d_lines) != len(po.lines):
        changed.append("lines")
    else:
        for i, (d, c) in enumerate(zip(d_lines, po.lines)):
            for k in ("description", "quantity", "unit_price", "amount"):
                if not _same(d.get(k), getattr(c, k), money=(k != "description")):
                    changed.append(f"lines[{i}].{k}")
    return changed
