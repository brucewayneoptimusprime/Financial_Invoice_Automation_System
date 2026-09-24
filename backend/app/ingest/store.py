"""Hash the file and keep a copy under data/runs/<run_id>/. The stored copy gets a GENERATED name, so a hostile
or odd file name (path separators, `..`, control characters, Unicode) can never influence where anything is written."""
import hashlib
import json
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.ingest.validate import EXTENSIONS, IngestRejected, ValidatedFile
from app.models.extraction_meta import ExtractionMeta, IngestInfo

_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StoredFile:
    run_dir: Path
    original_path: Path
    sha256: str
    original_name: str
    size_bytes: int


def new_run_id() -> str:
    return uuid.uuid4().hex


def check_run_id(run_id: str) -> str:
    if not _RUN_ID.match(run_id):
        raise ValueError(f"invalid run id {run_id!r}: use 1-64 letters, digits, '_' or '-'")
    return run_id


def safe_display_name(name: str) -> str:
    """For metadata and UI only. Strips any path, control characters and over-long names."""
    base = re.split(r"[\\/]", name)[-1]
    base = "".join(ch for ch in base if ch.isprintable())
    return (base.strip() or "unnamed")[:200]


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def store_original(validated: ValidatedFile, run_id: str, runs_dir: Path) -> StoredFile:
    check_run_id(run_id)
    run_dir = Path(runs_dir) / run_id
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise IngestRejected("run_exists", f"Run folder for {run_id} already exists; refusing to overwrite it.") from None
    target = run_dir / f"original.{EXTENSIONS.get(validated.media_type, 'bin')}"
    digest = hashlib.sha256()
    copied = 0
    with validated.path.open("rb") as src, target.open("wb") as dst:
        for chunk in iter(lambda: src.read(_CHUNK), b""):
            digest.update(chunk)
            dst.write(chunk)
            copied += len(chunk)
    if copied != validated.size_bytes:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise IngestRejected("changed_during_copy", "The file changed while it was being copied; please retry.")
    return StoredFile(run_dir=run_dir, original_path=target, sha256=digest.hexdigest(),
                      original_name=safe_display_name(validated.path.name), size_bytes=copied)


def write_run_meta(run_dir: Path, ingest: IngestInfo | None, extraction: ExtractionMeta | None) -> Path:
    """meta.json: system-side facts only (no secrets, no image bytes, no invoice text)."""
    payload = {
        "ingest": None if ingest is None else ingest.model_dump(mode="json"),
        "extraction": None if extraction is None else extraction.model_dump(mode="json"),
    }
    path = Path(run_dir) / "meta.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
    return path
