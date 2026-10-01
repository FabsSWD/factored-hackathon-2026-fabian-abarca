"""Estimated cost of a turn from the tokens of its model calls (architecture §9).

Rates are configured (``LLM_INPUT_USD_PER_MTOK``, ``LLM_OUTPUT_USD_PER_MTOK``, and
``LLM_CACHED_INPUT_USD_PER_MTOK`` for input served from the provider's prompt cache); without
the first two the cost is unknown (``None``), never guessed. Cached input is priced apart only
when the API reports it and its rate is set; otherwise all input is charged at the normal rate,
so the estimate may be overestimated. Kev runs inside the deployment and adds no per-token cost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.contracts import ModelCall
from app.settings import Settings

PRICED_PROVIDER = "openai"
_MILLION = Decimal(1_000_000)
_PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class TokenRates:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal
    cached_input_usd_per_mtok: Decimal | None = None


def rates_from_settings(settings: Settings) -> TokenRates | None:
    if settings.llm_input_usd_per_mtok is None or settings.llm_output_usd_per_mtok is None:
        return None
    return TokenRates(
        input_usd_per_mtok=settings.llm_input_usd_per_mtok,
        output_usd_per_mtok=settings.llm_output_usd_per_mtok,
        cached_input_usd_per_mtok=settings.llm_cached_input_usd_per_mtok,
    )


def estimate_cost(calls: Sequence[ModelCall], rates: TokenRates | None) -> Decimal | None:
    """USD for the priced calls; a call without token counts contributes nothing."""
    if rates is None:
        return None
    total = Decimal(0)
    for call in calls:
        if call.provider != PRICED_PROVIDER:
            continue
        input_tokens = call.input_tokens or 0
        cached = call.cached_input_tokens or 0
        if rates.cached_input_usd_per_mtok is not None and 0 < cached <= input_tokens:
            total += Decimal(cached) * rates.cached_input_usd_per_mtok / _MILLION
            input_tokens -= cached
        total += Decimal(input_tokens) * rates.input_usd_per_mtok / _MILLION
        total += Decimal(call.output_tokens or 0) * rates.output_usd_per_mtok / _MILLION
    return total.quantize(_PRECISION)
