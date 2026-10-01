"""Estimated cost of a turn from the tokens of its model calls (architecture §9).

Rates are configured (``LLM_INPUT_USD_PER_MTOK``, ``LLM_OUTPUT_USD_PER_MTOK``); without them the
cost is unknown (``None``), never guessed. Kev runs inside the deployment and adds no per-token
cost.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.contracts import ModelCall

PRICED_PROVIDER = "openai"
_MILLION = Decimal(1_000_000)
_PRECISION = Decimal("0.000001")


@dataclass(frozen=True)
class TokenRates:
    input_usd_per_mtok: Decimal
    output_usd_per_mtok: Decimal


def estimate_cost(calls: Sequence[ModelCall], rates: TokenRates | None) -> Decimal | None:
    """USD for the priced calls; a call without token counts contributes nothing."""
    if rates is None:
        return None
    total = Decimal(0)
    for call in calls:
        if call.provider != PRICED_PROVIDER:
            continue
        total += Decimal(call.input_tokens or 0) * rates.input_usd_per_mtok / _MILLION
        total += Decimal(call.output_tokens or 0) * rates.output_usd_per_mtok / _MILLION
    return total.quantize(_PRECISION)
