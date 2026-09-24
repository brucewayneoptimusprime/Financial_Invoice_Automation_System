"""Resolve the invoice's vendor name to a known vendor. Pure and deterministic.

A stated tax id is tried FIRST (`method="tax_id"`, score 1.0): tax ids are compared ignoring case, spaces, hyphens and
dots, and a leading letters-only label or country prefix ("EIN", "GB") on ONE side is ignored ("EIN 12-3456789" equals
"12-3456789"; "GB123456789" does not equal "DE123456789"). A tax id and a name that resolve to DIFFERENT vendors are
reported as ambiguous with both as candidates; the resolved vendor is then the tax-id one, and a human decides.

Names are normalised (case, accents, punctuation, generic legal suffixes from config) and compared
against each vendor's name and aliases: equal after normalisation = 1.0 (exact_name / alias), otherwise
edit / token similarity, accepted from `vendor_fuzzy_min`. Ties and near-ties between DIFFERENT vendors are
reported as ambiguous instead of guessed - except when one vendor matches exactly and the others don't.
"""
import re
from typing import Iterable

from app.config import MatchConfig
from app.engine.facts import VendorFact
from app.engine.normalize import is_missing, normalize_name, normalize_tax_id, token_similarity
from app.models.run import VendorMatch


def _score_vendor(name_norm: str, vendor: VendorFact, cfg: MatchConfig) -> tuple[float, str] | None:
    """Best (score, method) of this vendor against the normalised invoice name, or None if below threshold."""
    best: tuple[float, str] | None = None
    for label, method in [(vendor.name, "exact_name"), *[(a, "alias") for a in vendor.aliases]]:
        candidate = normalize_name(label, cfg.legal_suffixes)
        if not candidate:
            continue
        if candidate == name_norm:
            score, how = 1.0, method
        else:
            sim = token_similarity(name_norm, candidate)
            if sim < cfg.vendor_fuzzy_min:
                continue
            score, how = round(sim, 6), "fuzzy"
        if best is None or score > best[0] or (score == best[0] and how == "exact_name"):
            best = (score, how)
    return best


_LABEL_PREFIX = re.compile(r"^[A-Z]+(?=[A-Z]*\d)")
_MIN_TAX_ID_BODY = 5


def _split_prefix(tax_id: str) -> tuple[str, str]:
    """('EIN', '123456789'): a leading run of letters (a label or country code) and the rest."""
    m = _LABEL_PREFIX.match(tax_id)
    return (m.group(), tax_id[m.end():]) if m else ("", tax_id)


def tax_ids_match(a: str | None, b: str | None) -> bool:
    if is_missing(a) or is_missing(b):
        return False
    x, y = normalize_tax_id(a), normalize_tax_id(b)
    if not x or not y:
        return False
    if x == y:
        return True
    (px, bx), (py, by) = _split_prefix(x), _split_prefix(y)
    if px and not py:
        return bx == y and len(bx) >= _MIN_TAX_ID_BODY
    if py and not px:
        return by == x and len(by) >= _MIN_TAX_ID_BODY
    return False                                        # two different prefixes (GB vs DE) are two different registrations


def _resolve_by_name(name: str | None, vendors: list[VendorFact], cfg: MatchConfig) -> VendorMatch:
    if is_missing(name):
        return VendorMatch(vendor_id=None, score=0.0, method="none")
    name_norm = normalize_name(name, cfg.legal_suffixes)
    scored: list[tuple[float, int, str]] = []          # (score, vendor id, method)
    for vendor in vendors:
        result = _score_vendor(name_norm, vendor, cfg)
        if result is not None:
            scored.append((result[0], vendor.id, result[1]))
    if not scored:
        return VendorMatch(vendor_id=None, score=0.0, method="none")

    scored.sort(key=lambda t: (-t[0], t[1]))            # best score first, lowest id breaks ties
    top = scored[0]
    runner = scored[1] if len(scored) > 1 else None
    ambiguous = False
    tied: list[int] = []
    if runner is not None:
        both_exact = top[0] == 1.0 and runner[0] == 1.0
        near_tie = top[0] < 1.0 and (top[0] - runner[0]) < cfg.vendor_ambiguity_margin
        ambiguous = both_exact or near_tie
        if both_exact:
            tied = sorted(vid for score, vid, _ in scored if score == 1.0)
        elif near_tie:
            tied = sorted(vid for score, vid, _ in scored if (top[0] - score) < cfg.vendor_ambiguity_margin)
    return VendorMatch(vendor_id=top[1], score=top[0], method=top[2], ambiguous=ambiguous,
                       runner_up_score=None if runner is None else runner[0], candidate_vendor_ids=tied)


def resolve_vendor(name: str | None, vendors: Iterable[VendorFact], cfg: MatchConfig | None = None,
                   tax_id: str | None = None) -> VendorMatch:
    cfg = cfg or MatchConfig()
    vendors = list(vendors)
    by_name = _resolve_by_name(name, vendors, cfg)
    by_tax = sorted(v.id for v in vendors if tax_ids_match(tax_id, v.tax_id))
    if not by_tax:
        return by_name                                   # an unknown or absent tax id says nothing: fall back to the name

    if len(by_tax) > 1:                                  # the same registration on several vendor records
        return VendorMatch(vendor_id=by_tax[0], score=1.0, method="tax_id", ambiguous=True, runner_up_score=1.0,
                           candidate_vendor_ids=by_tax)
    (vendor_id,) = by_tax
    name_ids = set(by_name.candidate_vendor_ids) if by_name.ambiguous else set()
    if by_name.vendor_id is not None:
        name_ids.add(by_name.vendor_id)
    others = sorted(name_ids - {vendor_id})
    if by_name.vendor_id is not None and others and by_name.vendor_id != vendor_id:
        # the name points somewhere else (possibly among several): tax id and name disagree
        return VendorMatch(vendor_id=vendor_id, score=1.0, method="tax_id", ambiguous=True,
                           runner_up_score=by_name.score, candidate_vendor_ids=sorted({vendor_id, *others}))
    return VendorMatch(vendor_id=vendor_id, score=1.0, method="tax_id")      # tax id alone, or agreeing with the name
