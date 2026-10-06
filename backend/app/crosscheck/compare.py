"""Relevance and differences between ONE document's facts and ONE purchase order. Pure and deterministic: no I/O, no model.

It states; it never judges. There is no severity, no pass or fail and no recommendation, and comparison is EXACT (owner decision
4): quantities and unit prices as Decimal, money in integer cents through app.money. A value the grounding check could not confirm
in the document's own text is never compared; it is listed under `unconfirmed`. Everything that could not be compared is listed
under `not_compared` with the reason, so nothing is silently dropped.

Relevance: a document is related when at least one signal holds (PO number, an invoice number of this PO, the vendor, enough of
its lines). Each signal is reported with both values whether it holds or not.

Tying a document line to a PO line uses the DESCRIPTION only (`line_matching.description_signal`: similarity, containment, item
codes). Quantity and price are deliberately left out of the tie, because they are what is being compared.
"""
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.crosscheck.facts import POContext, POLine
from app.crosscheck.reader import DocumentFacts
from app.engine.line_matching import description_signal
from app.engine.normalize import is_missing, normalize_identifier, normalize_name, token_similarity
from app.money import from_minor, to_minor

TIED, AMBIGUOUS, NOT_ON_PO, NO_DESCRIPTION, UNCONFIRMED = "tied", "ambiguous", "not_on_po", "no_description", "unconfirmed"
_STATUS_WORDS = {"approved": "approved", "in_review": "in review", "awaiting_info": "awaiting information", "pending": "pending"}


def qty_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


def money_text(minor: int) -> str:
    return f"{from_minor(minor):.2f}"


def _ok(item: dict[str, Any] | None) -> bool:
    return item is not None and item.get("confirmed", True)


def _doc(value: str, *items: dict[str, Any]) -> dict[str, Any]:
    """The document side of a row: the value, and where it is printed."""
    sources = [i["source_text"] for i in items if i.get("source_text")]
    pages = [i["page"] for i in items if i.get("page")]
    return {"value": value, "page": pages[0] if pages else None, "source_text": " | ".join(dict.fromkeys(sources)) or None}


def _line_label(line: POLine) -> str:
    return f"PO line {line.line_no}"


# ----------------------------------------------------------------------------------------------------- line ties

def tie_lines(lines: list[dict[str, Any]], po: POContext, settings: Settings) -> list[dict[str, Any]]:
    """One entry per document line: status, the PO line it is tied to (if any), and the candidates with their scores."""
    cfg = settings.line_match
    described = [pl for pl in po.lines if not is_missing(pl.description)]
    ties = []
    for number, line in enumerate(lines, start=1):
        tie: dict[str, Any] = {"document_line": number, "status": TIED, "po_line_no": None, "score": None, "candidates": []}
        if not line.get("confirmed", True):
            tie["status"] = UNCONFIRMED
        elif is_missing(line["description"]):
            tie["status"] = NO_DESCRIPTION
        else:
            scored = []
            for pl in described:
                score, reasons = description_signal(line["description"], line["item_code"], pl.description, cfg)
                if score >= cfg.description_min:
                    scored.append((round(score, 6), pl.line_no, reasons))
            scored.sort(key=lambda t: (-t[0], t[1]))
            tie["candidates"] = [{"po_line_no": n, "score": s, "reasons": r} for s, n, r in scored[: cfg.max_candidates]]
            if not scored:
                tie["status"] = NOT_ON_PO
            else:
                top, runner = scored[0], scored[1] if len(scored) > 1 else None
                clear = runner is None or (top[0] - runner[0]) >= cfg.ambiguity_margin or (top[0] == 1.0 and runner[0] < 1.0)
                if top[0] >= cfg.min_score and clear:
                    tie["po_line_no"], tie["score"] = top[1], top[0]
                else:
                    tie["status"], tie["score"] = AMBIGUOUS, top[0]
        ties.append(tie)
    return ties


# ----------------------------------------------------------------------------------------------------- relevance

def _vendor(name: str, po: POContext, settings: Settings) -> tuple[bool, str]:
    cfg = settings.match
    doc = normalize_name(name, cfg.legal_suffixes)
    best = 0.0
    for label in (po.vendor_name, *po.vendor_aliases):
        candidate = normalize_name(label, cfg.legal_suffixes)
        if not candidate:
            continue
        if candidate == doc:
            return True, ("the same name" if label == po.vendor_name else f"the vendor's other name \"{label}\"") + \
                " once case, punctuation and legal suffixes are ignored"
        best = max(best, token_similarity(doc, candidate))
    if best >= cfg.vendor_fuzzy_min:
        return True, f"a very similar name (similarity {best:.2f}, at least {cfg.vendor_fuzzy_min:.2f} is needed)"
    return False, f"a different name (similarity {best:.2f}, at least {cfg.vendor_fuzzy_min:.2f} is needed)"


def _mentions(facts: DocumentFacts, kind: str) -> list[dict[str, Any]]:
    return [m for m in facts.mentions if m["kind"] == kind and _ok(m)]


def relevance(facts: DocumentFacts, po: POContext, ties: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    signals = []

    po_numbers = _mentions(facts, "po_number")
    this_po = normalize_identifier(po.po_number)
    holds = any(normalize_identifier(m["value"]) == this_po for m in po_numbers)
    printed = ", ".join(dict.fromkeys(m["value"] for m in po_numbers)) or None
    signals.append({"signal": "po_number", "holds": holds, "document_value": printed, "po_value": po.po_number,
                    "explanation": ("The document mentions this PO's number." if holds else
                                    "The document mentions a purchase-order number, but not this PO's." if po_numbers else
                                    "The document mentions no purchase-order number.")})

    inv_numbers = _mentions(facts, "invoice_number")
    ours = {normalize_identifier(n): n for n, _ in po.invoices if n}
    hit = [m["value"] for m in inv_numbers if normalize_identifier(m["value"]) in ours]
    signals.append({"signal": "invoice_number", "holds": bool(hit), "po_value": ", ".join(ours.values()) or None,
                    "document_value": ", ".join(dict.fromkeys(m["value"] for m in inv_numbers)) or None,
                    "explanation": (f"The document mentions invoice {', '.join(dict.fromkeys(hit))}, which is matched to this PO." if hit else
                                    "The document mentions an invoice number, but none of this PO's invoices has it." if inv_numbers else
                                    "The document mentions no invoice number.")})

    vendor = facts.fields.get("vendor_name")
    if _ok(vendor):
        v_holds, how = _vendor(vendor["value"], po, settings)
        signals.append({"signal": "vendor", "holds": v_holds, "document_value": vendor["value"], "po_value": po.vendor_name,
                        "explanation": f"The vendor on the document is {how}."})
    else:
        v_holds = False
        signals.append({"signal": "vendor", "holds": False, "document_value": None, "po_value": po.vendor_name,
                        "explanation": "No vendor name could be read and confirmed on the document."})

    described = [t for t in ties if t["status"] in (TIED, AMBIGUOUS, NOT_ON_PO)]
    tied = [t for t in described if t["status"] == TIED]
    share = settings.crosscheck_min_line_share
    l_holds = bool(tied) and len(tied) >= share * len(described)
    signals.append({"signal": "lines", "holds": l_holds, "document_value": f"{len(tied)} of {len(described)}",
                    "po_value": f"{len(po.lines)} line(s)",
                    "explanation": ("The document has no described item lines to compare." if not described else
                                    f"{len(tied)} of the document's {len(described)} described line(s) match a line of this PO "
                                    f"(at least {share:.0%} is needed).")})

    related = any(s["holds"] for s in signals)
    return {"related": related, "vendor_only": related and v_holds and not any(s["holds"] for s in signals if s["signal"] != "vendor"),
            "signals": signals}


# --------------------------------------------------------------------------------------------------- differences

def _invoiced_text(entries) -> str:
    return ", ".join(f"{e.invoice_number or '(no number)'} ({_STATUS_WORDS.get(e.status, e.status)}): {qty_text(e.quantity)}" for e in entries)


def differences(facts: DocumentFacts, po: POContext, ties: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    diffs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    by_no = {pl.line_no: pl for pl in po.lines}

    def add(kind: str, document: dict[str, Any], value: str, source: str, pl: POLine | None = None) -> None:
        diffs.append({"type": kind, "document": document, "compared_with": {"value": value, "source": source},
                      "po_line_no": None if pl is None else pl.line_no, "po_line_description": None if pl is None else pl.description})

    def skip(what: str, reason: str) -> None:
        skipped.append({"what": what, "reason": reason})

    # 6. PO number: numbers are mentioned and none is this PO's.
    po_numbers = _mentions(facts, "po_number")
    if po_numbers and not any(normalize_identifier(m["value"]) == normalize_identifier(po.po_number) for m in po_numbers):
        add("po_number", _doc(", ".join(dict.fromkeys(m["value"] for m in po_numbers)), *po_numbers), po.po_number, "This PO's number")

    # 5. Vendor name.
    vendor = facts.fields.get("vendor_name")
    if _ok(vendor) and not _vendor(vendor["value"], po, settings)[0]:
        add("vendor_name", _doc(vendor["value"], vendor), po.vendor_name, "This PO's vendor")

    # 7. Currency. With different currencies no money is compared (no conversion: SPEC section 11 item 4).
    currency = facts.fields.get("currency")
    money_ok = True
    if _ok(currency) and currency["value"].upper() != po.currency.upper():
        add("currency", _doc(currency["value"], currency), po.currency, "This PO's currency")
        money_ok = False
        skip("Unit prices, amounts and the total", f"the document is in {currency['value']} and the PO in {po.currency}; nothing is converted")

    # 8. Items not on the PO, and lines that could not be tied.
    groups: dict[int, list[dict[str, Any]]] = {}
    for tie, line in zip(ties, facts.lines):
        label = f"Document line {tie['document_line']}" + (f" ({line['description']})" if line["description"] else "")
        if tie["status"] == TIED:
            groups.setdefault(tie["po_line_no"], []).append(line)
        elif tie["status"] == NOT_ON_PO:
            parts = [line["description"]] + [f"{k.replace('_', ' ')} {line[k]}" for k in ("quantity", "unit_price", "amount") if line[k]]
            add("item_not_on_po", _doc(", ".join(parts), line), "No PO line has a similar description",
                f"This PO's {len(po.lines)} line(s)")
        elif tie["status"] == AMBIGUOUS:
            cands = ", ".join(f"line {c['po_line_no']}" for c in tie["candidates"])
            skip(label, f"it could not be tied to a single PO line (possible: {cands})")
        elif tie["status"] == NO_DESCRIPTION:
            skip(label, "the line has no description")

    expected_total: int | None = 0
    total_gap: str | None = None
    if any(t["status"] in (AMBIGUOUS, NOT_ON_PO, NO_DESCRIPTION, UNCONFIRMED) for t in ties):
        expected_total, total_gap = None, "not every line of the document is tied to a PO line"
    for line_no in sorted(groups):
        pl, lines = by_no[line_no], groups[line_no]
        where = _line_label(pl)

        # 1 and 2. Quantity against the PO line and against what was invoiced on it.
        qty: Decimal | None = None
        if all(l["quantity"] is not None for l in lines):
            qty = sum((Decimal(l["quantity"]) for l in lines), Decimal(0))
            if pl.quantity is None:
                skip(f"Quantity on {where}", "the PO line has no quantity")
            elif qty != pl.quantity:
                add("quantity_vs_po", _doc(qty_text(qty), *lines), qty_text(pl.quantity), f"{where}, ordered quantity", pl)
            entries = po.invoiced.get(pl.id, ())
            if entries:
                invoiced = sum((e.quantity for e in entries), Decimal(0))
                if qty != invoiced:
                    add("quantity_vs_invoiced", _doc(qty_text(qty), *lines), qty_text(invoiced), f"Invoiced on {where}: {_invoiced_text(entries)}", pl)
            else:
                skip(f"Quantity on {where} against invoices",
                     "no invoice quantity is tied to this PO line" if po.invoices else "no invoice is matched to this PO")
        else:
            skip(f"Quantity on {where}", "the document prints no quantity for this line")

        if not money_ok:
            continue

        # 3. Unit price, per document line, when printed.
        for l in lines:
            if l["unit_price"] is None:
                continue
            if pl.unit_price is None:
                skip(f"Unit price on {where}", "the PO line has no unit price")
            elif Decimal(l["unit_price"]) != pl.unit_price:
                add("unit_price", _doc(l["unit_price"], l), format(pl.unit_price, "f"), f"{where}, unit price", pl)

        # 4. Amount against the expected amount, in integer cents.
        expected: int | None = None
        source = ""
        if qty is None or (pl.quantity is not None and qty == pl.quantity):
            if pl.amount_minor is not None:
                expected, source = pl.amount_minor, f"{where}, amount"
        if expected is None and qty is not None and pl.unit_price is not None:
            try:
                expected = to_minor(qty * pl.unit_price)
                source = f"Expected for {qty_text(qty)} at the PO price {format(pl.unit_price, 'f')} ({where})"
            except ValueError:
                expected = None
        if expected is None:
            if expected_total is not None:
                expected_total, total_gap = None, f"no expected amount could be worked out for {where}"
        elif expected_total is not None:
            expected_total += expected
        if any(l["amount"] is not None for l in lines):
            if not all(l["amount"] is not None for l in lines):
                skip(f"Amount on {where}", "not every document line for it prints an amount")
            elif expected is None:
                skip(f"Amount on {where}", "the PO line has no amount, and quantity x PO price is not exact to the cent")
            else:
                try:
                    printed = sum(to_minor(Decimal(l["amount"])) for l in lines)
                except ValueError:
                    skip(f"Amount on {where}", "the printed amount has more than 2 decimals")
                else:
                    if printed != expected:
                        add("amount", _doc(money_text(printed), *lines), money_text(expected), source, pl)

    # 9. Document total against what the PO says those lines cost.
    total = facts.fields.get("total")
    if _ok(total) and money_ok:
        if not facts.lines:
            skip("Document total", "the document has no item lines to work an expected total from")
        elif expected_total is None:
            skip("Document total", total_gap or "no expected total could be worked out")
        else:
            try:
                printed = to_minor(Decimal(total["value"]))
            except ValueError:
                skip("Document total", "the printed total has more than 2 decimals")
            else:
                if printed != expected_total:
                    add("total", _doc(money_text(printed), total), money_text(expected_total),
                        "Sum of the expected amounts of the document's lines, from this PO's quantities and prices")

    # 10. PO lines no document line is tied to: informational, never a difference.
    absent = [{"po_line_no": pl.line_no, "description": pl.description, "quantity": None if pl.quantity is None else qty_text(pl.quantity)}
              for pl in po.lines if pl.line_no not in groups]
    return {"differences": diffs, "absent_po_lines": absent, "not_compared": skipped}


# -------------------------------------------------------------------------------------------- one document's report

def _unconfirmed(facts: DocumentFacts) -> list[dict[str, Any]]:
    out = []
    for name, item in facts.fields.items():
        if item is not None and not item["confirmed"]:
            out.append({"what": name, "value": item["value"], "grounding": item["grounding"], "source_text": item["source_text"]})
    for m in facts.mentions:
        if not m["confirmed"]:
            out.append({"what": m["kind"], "value": m["value"], "grounding": m["grounding"], "source_text": m["source_text"]})
    for i, line in enumerate(facts.lines, start=1):
        if not line["confirmed"]:
            out.append({"what": f"line {i}", "value": line["description"] or "", "grounding": line["grounding"],
                        "source_text": line["source_text"]})
    return out


def compare_document(facts: DocumentFacts, po: POContext, settings: Settings) -> dict[str, Any]:
    """The report for one successfully read document. Differences are computed only for a related document."""
    ties = tie_lines(facts.lines, po, settings)
    rel = relevance(facts, po, ties, settings)
    result = differences(facts, po, ties, settings) if rel["related"] else {"differences": [], "absent_po_lines": [], "not_compared": []}
    notices = []
    if facts.injection_suspected:
        notices.append("This document contains text addressed to an AI reader. It was treated as data and changed nothing.")
    if any(i is not None and i["grounding"] == "unavailable" for i in [*facts.fields.values(), *facts.mentions, *facts.lines]):
        notices.append("Read from the image: this document has no text layer, so its values could not be checked against text.")
    notices += [f"Left out: {n}." for n in facts.notes]
    return {
        "facts": {"document_kind": facts.document_kind, "fields": facts.fields, "mentions": facts.mentions,
                  "lines": [{**line, "tie": tie} for line, tie in zip(facts.lines, ties)], "model_notes": facts.model_notes or None},
        "relevance": rel, **result, "unconfirmed": _unconfirmed(facts), "notices": notices,
    }
