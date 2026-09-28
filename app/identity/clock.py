"""Real-time clock for sessions. Never the business clock (dispute policy §15)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]
"""Returns the current time as a timezone-aware datetime."""


def utc_now() -> datetime:
    return datetime.now(UTC)
