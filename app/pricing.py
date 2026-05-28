from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger("ai_serving.pricing")

MODEL_PRICING_USD_PER_1M: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "claude-sonnet-4-6": (Decimal("3.00"), Decimal("15.00")),
    "claude-3-5-haiku-latest": (Decimal("0.80"), Decimal("4.00")),
}
_WARNED_UNKNOWN_MODELS: set[str | None] = set()


def calculate_cost(model: str | None, prompt_tokens: int, completion_tokens: int) -> Decimal:
    if not model or model not in MODEL_PRICING_USD_PER_1M:
        if model not in _WARNED_UNKNOWN_MODELS:
            _WARNED_UNKNOWN_MODELS.add(model)
            logger.warning("Unknown model for pricing: %s", model)
        return Decimal("0")

    input_price, output_price = MODEL_PRICING_USD_PER_1M[model]
    return (
        (Decimal(prompt_tokens) * input_price)
        + (Decimal(completion_tokens) * output_price)
    ) / Decimal(1_000_000)
