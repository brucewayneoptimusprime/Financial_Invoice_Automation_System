"""The ingest stage: validate -> hash + store -> render -> assess text layer.

Rejected files raise IngestRejected BEFORE anything is created (no run folder, no side effects). Accepted but
unreadable documents (password-protected, blank, corrupt, no pages) do NOT raise: the stage reports a failed
IngestInfo with a failure kind, so the run can continue with a degraded extraction.
"""
from pathlib import Path

from app.config import Settings, get_settings
from app.enums import Outcome, StageStatus
from app.ingest.render import render_document
from app.ingest.store import check_run_id, store_original, write_run_meta
from app.ingest.textlayer import assess_text_layer
from app.ingest.validate import validate_file
from app.models.audit import AuditEvent
from app.models.extraction_meta import IngestInfo
from app.models.run import RunContext, StageResult

INGEST_STAGE = "ingest"


def _event(event_type: str, outcome: Outcome, message: str, detail: dict) -> AuditEvent:
    return AuditEvent(stage=INGEST_STAGE, event_type=event_type, outcome=outcome, message=message, detail=detail)


def run_ingest_stage(ctx: RunContext, source_path: Path | str, settings: Settings | None = None) -> StageResult:
    settings = settings or get_settings()
    check_run_id(ctx.run_id)
    validated = validate_file(source_path, settings)                    # raises IngestRejected: nothing has been created yet
    events = [_event("file_validated", Outcome.INFO,
                     f"Accepted {validated.path.name} as {validated.media_type} ({validated.size_bytes} bytes).",
                     {"media_type": validated.media_type, "size_bytes": validated.size_bytes})]

    stored = store_original(validated, ctx.run_id, settings.runs_dir)
    events.append(_event("file_stored", Outcome.INFO, f"Stored a copy and computed SHA-256 {stored.sha256[:12]}...",
                         {"sha256": stored.sha256, "run_dir": str(stored.run_dir)}))
    ctx.file_hash = stored.sha256

    pages_dir = stored.run_dir / "pages"
    rendered = render_document(stored.original_path, validated.media_type, pages_dir, settings)

    text_paths: list[str | None] = []
    if rendered.failure is None:
        text_dir = stored.run_dir / "text"
        text_dir.mkdir(exist_ok=True)
        for page, text in zip(rendered.pages, rendered.texts):
            if text is None:
                text_paths.append(None)
                continue
            path = text_dir / f"page-{page.number}.txt"
            path.write_text(text, encoding="utf-8", newline="\n")
            text_paths.append(str(path))
    layer = assess_text_layer(rendered.texts, settings, rendered.truncated_text_pages) if rendered.failure is None \
        else assess_text_layer([], settings)

    issues = list(rendered.issues)
    if rendered.failure is not None:
        issues.append(rendered.failure.code)
    if rendered.truncated:
        issues.append(f"pages_truncated:{settings.max_pages}/{rendered.pages_total}")

    info = IngestInfo(
        media_type=validated.media_type, size_bytes=stored.size_bytes, sha256=stored.sha256,
        original_name=stored.original_name, run_dir=str(stored.run_dir), original_path=str(stored.original_path),
        pages_total=rendered.pages_total, pages_processed=len(rendered.pages), truncated=rendered.truncated,
        pages=rendered.pages, text_paths=text_paths, text_layer=layer, issues=issues,
        failure_kind=None if rendered.failure is None else rendered.failure.kind,
        failure_code=None if rendered.failure is None else rendered.failure.code,
        failure_reason=None if rendered.failure is None else rendered.failure.message,
    )
    ctx.ingest = info

    if rendered.failure is not None:
        events.append(_event("ingest_failed", Outcome.FAIL, rendered.failure.message,
                             {"code": rendered.failure.code, "failure_kind": rendered.failure.kind}))
    else:
        events.append(_event(
            "document_rendered", Outcome.PASS,
            f"Rendered {info.pages_processed} of {info.pages_total} page(s); text layer: {layer.reason}.",
            {"pages_total": info.pages_total, "pages_processed": info.pages_processed, "text_layer": layer.model_dump(mode="json"),
             "blank_pages": [p.number for p in rendered.pages if p.blank]}))
        if rendered.truncated:
            events.append(_event("pages_truncated", Outcome.FLAG,
                                 f"The document has {rendered.pages_total} pages; only the first {settings.max_pages} were processed.",
                                 {"pages_total": rendered.pages_total, "max_pages": settings.max_pages}))
    write_run_meta(stored.run_dir, info, None)

    status = StageStatus.FAILED if rendered.failure else StageStatus.FLAGGED if rendered.truncated else StageStatus.OK
    return StageResult(stage=INGEST_STAGE, status=status, events=events, outputs={
        "media_type": info.media_type, "sha256": info.sha256, "pages_total": info.pages_total,
        "pages_processed": info.pages_processed, "truncated": info.truncated, "text_layer_usable": layer.usable,
        "failure_kind": info.failure_kind, "failure_code": info.failure_code, "failure_reason": info.failure_reason, "run_dir": info.run_dir})
