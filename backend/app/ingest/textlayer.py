"""Embedded-text handling: clean it, and decide whether a document's text layer is usable."""
import re
import unicodedata

from app.config import Settings, get_settings
from app.models.extraction_meta import TextLayerInfo

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLANK_RUNS = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_REPLACEMENT_LIMIT = 0.02


def clean_text(raw: str, max_chars: int) -> tuple[str, bool]:
    """Normalise newlines, drop control characters, collapse blank runs, cap the length. Returns (text, truncated)."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = _TRAILING_SPACE.sub("\n", text)
    text = _BLANK_RUNS.sub("\n\n", text).strip()
    if len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _is_wordlike(ch: str) -> bool:
    if ch.isalnum():
        return True
    cat = unicodedata.category(ch)
    return cat[0] in ("P", "S") and cat != "Co" and ch != "�"


def assess_text_layer(texts: list[str | None], settings: Settings | None = None,
                      truncated_pages: list[int] | None = None) -> TextLayerInfo:
    """`texts` has one entry per PROCESSED page (None = the page has no text layer, e.g. an image file)."""
    settings = settings or get_settings()
    n = len(texts)
    chars = [len((t or "").strip()) for t in texts]
    without = [i + 1 for i, c in enumerate(chars) if c == 0]
    common = dict(chars_by_page=chars, pages_without_text=without, truncated_pages=truncated_pages or [])
    if n == 0 or all(t is None for t in texts):
        return TextLayerInfo(present=False, usable=False, reason="no_text_layer", **common)
    if sum(chars) == 0:
        return TextLayerInfo(present=False, usable=False, reason="no_text_layer", **common)

    body = "".join(t for t in texts if t)
    visible = [ch for ch in body if not ch.isspace()]
    ratio = (sum(_is_wordlike(ch) for ch in visible) / len(visible)) if visible else 0.0
    replacement = body.count("�") / max(1, len(body))
    if sum(chars) / n < settings.text_min_chars_per_page:
        return TextLayerInfo(present=True, usable=False, reason="too_little_text", wordlike_ratio=round(ratio, 4), **common)
    if ratio < settings.text_min_wordlike_ratio or replacement >= _REPLACEMENT_LIMIT:
        return TextLayerInfo(present=True, usable=False, reason="garbled_text", wordlike_ratio=round(ratio, 4), **common)
    return TextLayerInfo(present=True, usable=True, reason="usable", wordlike_ratio=round(ratio, 4), **common)
