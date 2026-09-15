"""Bounded per-request retry timing, independent of cumulative task metering."""

import math
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal


RetryReason = Literal[
    "retry_after_seconds", "retry_after_date", "exponential_jitter",
    "invalid_retry_after_jitter", "expired_retry_after_jitter",
    "retry_after_exceeds_limit",
]


@dataclass(frozen=True)
class ModelRetryDecision:
    # None means a valid server wait exceeds the allowed single wait. Never
    # truncate it and retry earlier than the provider permits.
    delay_seconds: float | None
    reason: RetryReason


def model_retry_decision(
    error: Exception, *, attempt: int, base_seconds: float, max_seconds: float,
    now: datetime | None = None, random_fraction: float | None = None,
) -> ModelRetryDecision:
    maximum = max_seconds if math.isfinite(max_seconds) and max_seconds >= 0 else 0.0
    base = base_seconds if math.isfinite(base_seconds) and base_seconds >= 0 else 0.0
    headers = getattr(getattr(error, "response", None), "headers", None)
    header = headers.get("retry-after") if headers is not None else None
    fallback: RetryReason = "exponential_jitter"
    if header is not None:
        fallback = "invalid_retry_after_jitter"
        value = str(header).strip()
        # Some compatible providers use fractional seconds. Accept only
        # unsigned finite decimal syntax, never NaN, infinity or exponents.
        if re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
            # Python float conversion is bounded even for very long decimals.
            delay = float(value)
            if not math.isfinite(delay) or delay > maximum:
                return ModelRetryDecision(None, "retry_after_exceeds_limit")
            return ModelRetryDecision(delay, "retry_after_seconds")
        if len(value) <= 128:
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=UTC)
                current = now or datetime.now(UTC)
                if current.tzinfo is None:
                    current = current.replace(tzinfo=UTC)
                delay = (date - current).total_seconds()
                if delay > maximum:
                    return ModelRetryDecision(None, "retry_after_exceeds_limit")
                if delay > 0:
                    return ModelRetryDecision(delay, "retry_after_date")
                fallback = "expired_retry_after_jitter"
            except (TypeError, ValueError, OverflowError):
                pass
    try:
        exponential = min(math.ldexp(base, max(0, attempt - 1)), maximum)
    except OverflowError:
        exponential = maximum
    fraction = random.random() if random_fraction is None else random_fraction
    # Injected fractions only make tests deterministic; actual random() is
    # already within [0, 1]. Apply jitter before the local upper-bound clamp.
    fraction = min(max(fraction, 0.0), 1.0)
    return ModelRetryDecision(min(maximum, exponential * (0.8 + 0.4 * fraction)), fallback)
