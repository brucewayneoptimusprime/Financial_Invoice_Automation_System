"""Decide whether a file may enter the pipeline. The type is decided by MAGIC BYTES, never by the extension,
so a renamed executable is rejected and a PNG named `.pdf` is treated as the PNG it is."""
from dataclasses import dataclass
from pathlib import Path

from app.config import Settings, get_settings

PDF, PNG, JPEG = "application/pdf", "image/png", "image/jpeg"
_HEAD_BYTES = 1024
EXTENSIONS = {PDF: "pdf", PNG: "png", JPEG: "jpg"}

_KNOWN_UNSUPPORTED = [
    (b"GIF87a", "a GIF image"), (b"GIF89a", "a GIF image"), (b"MZ", "a Windows executable"),
    (b"PK\x03\x04", "a ZIP archive or Office document"), (b"{\\rtf", "an RTF document"),
    (b"\xd0\xcf\x11\xe0", "a legacy Office document"), (b"II*\x00", "a TIFF image"), (b"MM\x00*", "a TIFF image"),
    (b"RIFF", "a RIFF/WebP container"), (b"<html", "an HTML page"), (b"<!DOCTYPE", "an HTML page"),
]


class IngestRejected(Exception):
    """The file cannot enter the pipeline at all (no run is created). `code` is stable and machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class ValidatedFile:
    path: Path
    media_type: str
    size_bytes: int


def sniff_media_type(head: bytes) -> str | None:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return PNG
    if head.startswith(b"\xff\xd8\xff"):
        return JPEG
    if b"%PDF-" in head[:_HEAD_BYTES]:              # the PDF header may be preceded by up to 1 KiB of junk
        return PDF
    return None


def _describe(head: bytes) -> str:
    lowered = head.lstrip()[:16].lower()
    for magic, label in _KNOWN_UNSUPPORTED:
        if head.startswith(magic) or lowered.startswith(magic.lower()):
            return label
    try:
        head[:512].decode("utf-8")
        return "a text file" if head.strip() else "an unrecognised format"
    except UnicodeDecodeError:
        return "an unrecognised binary format"


def validate_file(path: Path | str, settings: Settings | None = None) -> ValidatedFile:
    settings = settings or get_settings()
    path = Path(path)
    if not path.exists():
        raise IngestRejected("not_found", f"File not found: {path.name}")
    if not path.is_file():
        raise IngestRejected("not_a_file", f"Not a file: {path.name}")
    size = path.stat().st_size
    if size == 0:
        raise IngestRejected("empty_file", f"{path.name} is empty (0 bytes).")
    if size > settings.max_file_bytes:
        raise IngestRejected("too_large", f"{path.name} is {size / 1_048_576:.1f} MB; the limit is "
                                          f"{settings.max_file_bytes / 1_048_576:.1f} MB.")
    with path.open("rb") as handle:
        head = handle.read(_HEAD_BYTES)
    media_type = sniff_media_type(head)
    if media_type is None:
        raise IngestRejected("unsupported_type", f"{path.name} looks like {_describe(head)}; only PDF, PNG and JPEG are accepted.")
    if media_type not in settings.allowed_media_types:
        raise IngestRejected("unsupported_type", f"{path.name} is {media_type}, which is not in the allowed types "
                                                 f"({', '.join(settings.allowed_media_types)}).")
    return ValidatedFile(path=path, media_type=media_type, size_bytes=size)
