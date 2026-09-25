"""From a model draft to what the confirmation form shows: pre-filled values, per-field marks, a vendor SUGGESTION (never a
created vendor), the same deterministic issues as the form, and the draft file. Nothing here writes to the database."""
import sqlite3
from typing import Any

from app.config import Settings
from app.engine.vendor_match import resolve_vendor
from app.po import drafts as po_drafts
from app.po.drafter import POModelDraft
from app.po.models import NewVendorIn, POCreate, POLineIn
from app.po.validate import lines_sum, load_vendor_facts, validate_po


def _cap(value: str | None, n: int) -> str | None:
    return None if value is None else value[:n]


def _mark(item: dict[str, Any]) -> dict[str, Any]:
    return {"found": item.get("value") is not None, "confidence": item.get("confidence", 0.0), "source_text": item.get("source_text"),
            "page": item.get("page"), "grounding": item.get("grounding")}


def build_draft_view(draft: POModelDraft, *, draft_id: str, source: str, provenance: dict[str, Any], conn: sqlite3.Connection,
                     settings: Settings, pages: list[int], extra_warnings: list[str] | None = None) -> dict[str, Any]:
    warnings = list(extra_warnings or [])
    values: dict[str, Any] = {"po_number": None, "currency": None, "total": None, "issued_date": None, "vendor_name": None,
                              "vendor_tax_id": None, "lines": []}
    marks: dict[str, Any] = {}
    suggested_vendor_id, vendor_hint, new_vendor = None, None, None
    issues, sum_of_lines = [], None

    if draft.status == "ok":
        f = draft.fields
        for k in ("po_number", "currency", "total", "issued_date", "vendor_name", "vendor_tax_id"):
            values[k] = f[k]["value"]
        values["lines"] = [{k: line[k] for k in ("description", "quantity", "unit_price", "amount")} for line in draft.lines]
        for k in ("po_number", "currency", "total", "issued_date"):
            marks[k] = _mark(f[k])
        marks["vendor"] = _mark(f["vendor_name"])
        for i, line in enumerate(draft.lines):
            marks[f"lines[{i}]"] = {"found": True, "confidence": line["confidence"], "source_text": line["source_text"],
                                    "page": line["page"], "grounding": line["grounding"]}

        vendors = load_vendor_facts(conn)
        if values["vendor_name"] or values["vendor_tax_id"]:
            vm = resolve_vendor(values["vendor_name"], vendors, settings.match, tax_id=values["vendor_tax_id"])
            if vm.vendor_id is not None and not vm.ambiguous:
                suggested_vendor_id = vm.vendor_id
                name = next(v.name for v in vendors if v.id == vm.vendor_id)
                how = {"tax_id": "its tax ID", "exact_name": "its name", "alias": "a known alias", "fuzzy": "a similar name"}.get(vm.method, vm.method)
                vendor_hint = f"Suggested: existing vendor {name}, recognised by {how} (score {vm.score:.2f}). Check it before saving."
            else:
                if vm.ambiguous:
                    warnings.append("The vendor could match more than one existing vendor; choose one or create a new vendor.")
                new_vendor = {"name": values["vendor_name"] or "", "tax_id": values["vendor_tax_id"] or "", "country": ""}
                vendor_hint = "No existing vendor matched: a NEW vendor (status new) is proposed. Pick an existing vendor instead if it is one."
        if draft.other_pos_present:
            warnings.append("The input seems to contain more than one purchase order; only the first was drafted. Enter the others separately.")
        if draft.injection_suspected:
            warnings.append("The input contains text addressed to an AI reader. It was treated as data; check every value before saving.")
        if values["currency"] is None:
            warnings.append("No currency is stated in the source. Choose it yourself: it is never guessed.")

        create = POCreate(po_number=_cap(values["po_number"], 64), vendor_id=suggested_vendor_id, currency=_cap(values["currency"], 10),
                          total=_cap(values["total"], 40), issued_date=_cap(values["issued_date"], 20),
                          lines=[POLineIn(description=_cap(line["description"], 500), quantity=_cap(line["quantity"], 40),
                                          unit_price=_cap(line["unit_price"], 40), amount=_cap(line["amount"], 40))
                                 for line in values["lines"][: settings.po_max_lines]])
        nv = (NewVendorIn(name=_cap(new_vendor["name"], 200), tax_id=_cap(new_vendor["tax_id"], 60) or None)
              if new_vendor and new_vendor["name"] else None)
        found_issues, _ = validate_po(create, conn, settings, nv)
        issues = [i.as_dict() for i in found_issues]
        sum_of_lines = lines_sum(create)

    record = {"source": source, "status": draft.status, "values": values, "suggested_vendor_id": suggested_vendor_id,
              "failure": None if draft.status == "ok" else {"code": draft.failure_code, "message": draft.failure_message},
              "marks": marks, "notes": draft.notes,
              "provenance": {**provenance, "model": draft.model, "prompt_version": draft.prompt_version, "tokens_in": draft.tokens_in,
                             "tokens_out": draft.tokens_out, "cost_usd": str(draft.cost_usd), "attempts": draft.attempts,
                             "injection_suspected": draft.injection_suspected}}
    po_drafts.write_draft(settings, draft_id, record)
    return {"draft_id": draft_id, "status": draft.status, "source": source,
            "failure": record["failure"], "values": values, "marks": marks, "suggested_vendor_id": suggested_vendor_id,
            "new_vendor": new_vendor, "vendor_hint": vendor_hint, "warnings": warnings, "notes": draft.notes, "issues": issues,
            "lines_sum": sum_of_lines, "pages": pages, "model": draft.model, "tokens_in": draft.tokens_in, "tokens_out": draft.tokens_out,
            "cost_usd": str(draft.cost_usd)}
