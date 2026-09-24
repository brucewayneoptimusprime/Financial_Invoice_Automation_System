"""Turn "the API refused our structured-output schema" into a clear, actionable message.

A rejected schema fails every call the same way and costs nothing, so instead of a degraded run that looks like a
bad invoice, the command-line tools (the extraction CLI now, the eval script in Stage 5) stop and say exactly what to
change. The pipeline itself still degrades safely (to `review`) if this happens inside a server.
"""
from app.models.extraction_meta import ExtractionMeta

SCHEMA_REJECTED_CODE = "schema_rejected"
SCHEMA_EXIT_CODE = 4


def is_schema_rejection(meta: ExtractionMeta | None) -> bool:
    return meta is not None and meta.degraded and meta.failure_code == SCHEMA_REJECTED_CODE


def schema_rejection_message(meta: ExtractionMeta) -> str:
    return (
        "THE API REJECTED THE EXTRACTION SCHEMA (nothing was spent)\n"
        f"  structured output mode: {meta.structured_output or 'unknown'}\n"
        f"  api message: {meta.failure_reason}\n"
        "\n"
        "This is a configuration problem, not a problem with the invoice. To fix it:\n"
        "  1. Add  LLM_STRUCTURED_OUTPUT=prompt_json  to your .env file. The schema is then sent as prompt text instead of a\n"
        "     strict grammar; the reply is parsed and validated here, with the same one-shot repair retry.\n"
        "  2. Re-run this command.\n"
        "To test both modes cheaply first (text only, no images):  python -m app.llm.probe --schema --all"
    )
