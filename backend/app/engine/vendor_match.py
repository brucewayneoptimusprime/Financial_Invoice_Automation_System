"""Resolve the invoice's vendor name to a known vendor. Pure and deterministic.

Names are normalised (case, accents, punctuation, generic legal suffixes from config) and compared
against each vendor's name and aliases: equal after normalisation = 1.0 (exact_name / alias), otherwise
edit / token similarity, accepted from `vendor_fuzzy_min`. Ties and near-ties between DIFFERENT vendors are
reported as ambiguous instead of guessed - except when one vendor matches exactly and the others don't.
"""
from typing import Iterable

from app.config import MatchConfig
from app.engine.facts import VendorFact
from app.engine.normalize import is_missing, normalize_name, token_similarity
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


def resolve_vendor(name: str | None, vendors: Iterable[VendorFact], cfg: MatchConfig | None = None) -> VendorMatch:
    cfg = cfg or MatchConfig()
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
