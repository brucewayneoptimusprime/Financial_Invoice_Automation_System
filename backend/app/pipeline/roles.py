"""The shared model-call loop for the explainer and the drafter: one call, one repair retry, then give up (the caller falls back to
its deterministic template). Same client, same cost ceilings and per-run accounting as extraction; no images, no document text.
"""
import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from app.extraction.extractor import parse_reply
from app.llm.errors import LLMError, LLMRefusedError, LLMTruncatedError
from app.llm.types import LLMClient, LLMRequest, text_part
from app.pipeline.prompts import correction_text

logger = logging.getLogger("app.pipeline")


@dataclass
class RoleResult:
    reply: dict | None = None                 # the accepted reply, or None (use the template)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    attempts: int = 0
    model: str | None = None
    fallback_reason: str | None = None        # why no reply was accepted
    problems: tuple[str, ...] = ()            # the last set of check problems (for the audit trail)


def call_role(client: LLMClient, *, system: str, payload: dict[str, Any], schema: dict[str, Any], model: str, max_output_tokens: int,
              run_id: str | None, purpose: str, check: Callable[[Any], list[str]], repair_retries: int = 1) -> RoleResult:
    """Ask, check, and repair once. Never raises for a model, network, ceiling or key problem."""
    out = RoleResult()
    payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    last_problems: list[str] = []
    for attempt in range(1, 1 + 1 + repair_retries):
        parts = (text_part(payload_text),) + ((text_part(correction_text(last_problems)),) if attempt > 1 else ())
        request = LLMRequest(system=system, parts=parts, model=model, max_output_tokens=max_output_tokens, schema=schema, run_id=run_id,
                             purpose=purpose)
        out.attempts = attempt
        try:
            response = client.complete(request)
        except LLMError as exc:
            out.fallback_reason = f"{exc.code}: {exc.message[:160]}"
            return out
        out.model = response.model
        out.tokens_in += response.usage.total_input
        out.tokens_out += response.usage.output_tokens
        out.cost_usd += response.cost_usd if response.cost_usd is not None else Decimal(0)
        try:
            response.ensure_usable()
            reply = parse_reply(response.text)
        except LLMRefusedError as exc:
            out.fallback_reason = f"{exc.code}: {exc.message[:160]}"
            return out
        except LLMTruncatedError:
            last_problems = ["the reply was cut off before the JSON was complete"]
            continue
        except ValueError:                                     # JSONDecodeError
            last_problems = ["the reply was not valid JSON"]
            continue
        last_problems = check(reply)
        if not last_problems:
            out.reply, out.problems = reply, ()
            return out
    out.problems = tuple(last_problems)
    out.fallback_reason = "the reply failed the checks: " + "; ".join(last_problems[:4])
    return out
