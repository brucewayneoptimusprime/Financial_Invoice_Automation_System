"""The extract stage: ctx.ingest -> ctx.extracted + ctx.extraction_meta, with artefacts written to the run folder."""
import json
from pathlib import Path

from app.config import Settings, get_settings
from app.enums import StageStatus
from app.extraction.extractor import EXTRACT_STAGE, extract_invoice
from app.ingest.store import write_run_meta
from app.llm.types import LLMClient
from app.models.run import RunContext, StageResult


def run_extract_stage(ctx: RunContext, client: LLMClient | None = None, settings: Settings | None = None) -> StageResult:
    """Never raises for a failed extraction: it degrades (see extractor.py) and the run flows on."""
    settings = settings or get_settings()
    if ctx.ingest is None:
        raise ValueError("the ingest stage must run before the extract stage")
    outcome = extract_invoice(ctx.ingest, client=client, settings=settings, run_id=ctx.run_id)
    ctx.extracted, ctx.extraction_meta = outcome.invoice, outcome.meta

    run_dir = Path(ctx.ingest.run_dir)
    (run_dir / "extracted.json").write_text(json.dumps(outcome.invoice.model_dump(mode="json"), indent=2, ensure_ascii=False),
                                             encoding="utf-8", newline="\n")
    if outcome.raw_replies:
        llm_dir = run_dir / "llm"
        llm_dir.mkdir(exist_ok=True)
        for index, reply in enumerate(outcome.raw_replies, start=1):
            (llm_dir / f"reply-{index}.txt").write_text(reply, encoding="utf-8", newline="\n")
    write_run_meta(run_dir, ctx.ingest, outcome.meta)

    meta = outcome.meta
    return StageResult(
        stage=EXTRACT_STAGE, status=StageStatus.FAILED if meta.degraded else StageStatus.OK, events=outcome.events,
        outputs={"path": meta.path, "degraded": meta.degraded, "failure_kind": meta.failure_kind,
                 "failure_code": meta.failure_code, "failure_reason": meta.failure_reason, "attempts": meta.attempts,
                 "tokens_in": meta.tokens_in, "tokens_out": meta.tokens_out, "cost_usd": str(meta.cost_usd),
                 "model": meta.model, "text_layer_usable": meta.text_layer_usable})
