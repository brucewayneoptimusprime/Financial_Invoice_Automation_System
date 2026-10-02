"""Which parts of an email are attachments, and which of those may be imported (SPEC section 11 item 84).

Works on the Gmail API's message payload shape (the fake and the real client return the same). Only parts with a filename are
listed: body text and unnamed inline images are not attachments. Eligibility uses the same limits as an upload (allowed media types,
max_file_bytes); magic bytes are checked again at import by the unchanged ingest `validate_file`.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from app.config import Settings
from app.ingest.store import safe_display_name

MAX_DEPTH = 10
_IMAGE_EXTENSIONS = {".pdf", ".png", ".jpg", ".jpeg"}
_ZIP_TYPES = {"application/zip", "application/x-zip-compressed", "application/x-7z-compressed", "application/x-rar-compressed"}
_OFFICE_PREFIXES = ("application/msword", "application/vnd.ms-", "application/vnd.openxmlformats-officedocument")


@dataclass(frozen=True)
class PartInfo:
    part_id: str
    filename: str            # cleaned for display (no path, no control characters, at most 200 characters)
    mime_type: str
    size: int
    inline: bool


def _header(part: dict[str, Any], name: str) -> str:
    for h in part.get("headers") or []:
        if str(h.get("name", "")).lower() == name.lower():
            return str(h.get("value", ""))
    return ""


def walk_parts(payload: dict[str, Any] | None) -> list[PartInfo]:
    """Every named part in the MIME tree, depth-first, at most MAX_DEPTH levels deep."""
    found: list[PartInfo] = []

    def visit(part: dict[str, Any], depth: int) -> None:
        if depth > MAX_DEPTH or not isinstance(part, dict):
            return
        name = str(part.get("filename") or "").strip()
        if name:
            body = part.get("body") or {}
            found.append(PartInfo(part_id=str(part.get("partId") or ""), filename=safe_display_name(name),
                                  mime_type=str(part.get("mimeType") or "application/octet-stream").lower(),
                                  size=int(body.get("size") or 0),
                                  inline=_header(part, "Content-Disposition").strip().lower().startswith("inline")))
        for child in part.get("parts") or []:
            visit(child, depth + 1)

    visit(payload or {}, 0)
    return found


def eligibility(part: PartInfo, settings: Settings) -> tuple[bool, str | None, str | None]:
    """(eligible, reason code, plain reason). Never looks at the content."""
    ext = PurePosixPath(part.filename.lower()).suffix
    mime = part.mime_type
    if mime in _ZIP_TYPES or ext in {".zip", ".7z", ".rar"}:
        return False, "type", "ZIP and other archive files are not imported."
    if mime.startswith(_OFFICE_PREFIXES) or ext in {".doc", ".docx", ".xls", ".xlsx"}:
        return False, "type", "Word and Excel files are not imported; only PDF, PNG and JPEG invoices."
    typed_ok = mime in settings.allowed_media_types or (mime == "application/octet-stream" and ext in _IMAGE_EXTENSIONS)
    if not typed_ok:
        return False, "type", f"Only PDF, PNG and JPEG files can be imported (this one is {mime})."
    if part.size <= 0:
        return False, "empty", "The attachment is empty."
    if part.size > settings.max_file_bytes:
        return False, "too_large", f"Larger than the {settings.max_file_bytes / 1_048_576:.0f} MB limit."
    if not part.part_id:
        return False, "type", "The attachment cannot be addressed."
    return True, None, None
