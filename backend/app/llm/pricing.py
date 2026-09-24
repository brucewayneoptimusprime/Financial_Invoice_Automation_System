"""Token accounting: exact Decimal cost from real `usage`, and a conservative pre-call projection."""
import io
import math
from decimal import Decimal
from typing import Mapping

from app.config import ModelPrice
from app.llm.types import LLMRequest, LLMUsage
from app.llm.errors import PriceNotConfigured

_MILLION = Decimal(1_000_000)
_FALLBACK_IMAGE_TOKENS = 1600
_CHARS_PER_TOKEN = 3.0  # deliberately pessimistic (real text is ~3.5-4) so the projection over- not under-estimates


def price_for(model: str, prices: Mapping[str, ModelPrice]) -> ModelPrice:
    try:
        return prices[model]
    except KeyError:
        raise PriceNotConfigured(
            f"No price is configured for model {model!r}; add it to llm_prices so cost ceilings can be enforced."
        ) from None


def cost_usd(usage: LLMUsage, price: ModelPrice) -> Decimal:
    """Exact cost. `input_tokens` are the UNCACHED input tokens (as the API reports them); cache reads and
    writes bill at multiples of the input price."""
    per_input = price.input_per_mtok / _MILLION
    return (
        Decimal(usage.input_tokens) * per_input
        + Decimal(usage.output_tokens) * (price.output_per_mtok / _MILLION)
        + Decimal(usage.cache_read_tokens) * per_input * price.cache_read_mult
        + Decimal(usage.cache_write_tokens) * per_input * price.cache_write_mult
    )


def image_tokens(data: bytes) -> int:
    """Approximate input tokens for one image: width * height / 750 (dimensions read from the header only)."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
        return max(1, math.ceil(width * height / 750))
    except Exception:  # noqa: BLE001 - an unreadable image just gets the fallback estimate
        return _FALLBACK_IMAGE_TOKENS


def estimate_input_tokens(request: LLMRequest) -> int:
    chars = len(request.system)
    if request.schema is not None:
        chars += len(repr(request.schema))
    tokens = 0
    for part in request.parts:
        if part.kind == "text":
            chars += len(part.text)
        else:
            tokens += image_tokens(part.data)
    return tokens + math.ceil(chars / _CHARS_PER_TOKEN)


def worst_case_cost(request: LLMRequest, price: ModelPrice) -> Decimal:
    """Upper bound used BEFORE a call: estimated input plus the full output-token cap. Cache writes are
    assumed when system-prompt caching is on (the dearest case)."""
    input_price = price.input_per_mtok * (price.cache_write_mult if request.cache_system else 1)
    return (Decimal(estimate_input_tokens(request)) * input_price
            + Decimal(request.max_output_tokens) * price.output_per_mtok) / _MILLION
