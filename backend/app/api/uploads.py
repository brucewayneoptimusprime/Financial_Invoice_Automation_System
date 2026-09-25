"""Uploaded files: a safe on-disk name, a size-capped copy, and the same acceptance check as ingest (magic bytes, empty, size)."""
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from app.config import Settings
from app.ingest.store import safe_display_name
from app.ingest.validate import IngestRejected, validate_file

_CHUNK = 1024 * 1024
_UNSAFE = re.compile(r"[^A-Za-z0-9 ._()\-]+")
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MULTIPART_SLACK_BYTES = 64 * 1024          # multipart headers and boundaries on top of the file itself

# IngestRejected code -> HTTP status
STATUS_FOR_CODE = {"empty_file": 400, "too_large": 413, "unsupported_type": 415, "not_found": 400, "not_a_file": 400}


def disk_name(original: str | None) -> str:
    """A name that is safe to create on any OS (no path, no device names, plain characters). Display uses `display_name`."""
    base = _UNSAFE.sub("_", safe_display_name(original or "")).strip(" .")[:100]
    stem = base.split(".")[0].strip().lower()
    if not base or stem in _WINDOWS_RESERVED or set(base) <= {"_", ".", " "}:
        suffix = Path(base).suffix.lower() if base else ""
        return "upload" + (suffix if suffix in (".pdf", ".png", ".jpg", ".jpeg") else "")
    return base


def display_name(original: str | None) -> str:
    return safe_display_name(original or "") if original else "upload"


@dataclass
class SavedUpload:
    folder: Path
    path: Path
    display_name: str
    media_type: str
    size_bytes: int


def save_upload(stream: BinaryIO, original_name: str | None, folder: Path, settings: Settings) -> SavedUpload:
    """Copy at most `max_file_bytes` into `folder` and check it. Raises IngestRejected (and removes the folder) on rejection."""
    folder.mkdir(parents=True, exist_ok=False)
    dest = folder / disk_name(original_name)
    try:
        written = 0
        with dest.open("wb") as out:
            while chunk := stream.read(_CHUNK):
                written += len(chunk)
                if written > settings.max_file_bytes:
                    raise IngestRejected("too_large", f"{display_name(original_name)} is larger than the "
                                                      f"{settings.max_file_bytes / 1_048_576:.1f} MB limit.")
                out.write(chunk)
        validated = validate_file(dest, settings)
    except BaseException:
        remove_upload(folder)
        raise
    return SavedUpload(folder=folder, path=dest, display_name=display_name(original_name), media_type=validated.media_type,
                       size_bytes=validated.size_bytes)


def remove_upload(folder: Path) -> None:
    shutil.rmtree(folder, ignore_errors=True)
