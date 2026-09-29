"""A turn deadline shared by the model calls of one customer turn (architecture §4)."""

from __future__ import annotations

import time
from collections.abc import Callable


class Deadline:
    """Absolute point on a monotonic clock. Technical time: never the business clock."""

    def __init__(self, seconds: float, monotonic: Callable[[], float] = time.monotonic) -> None:
        if seconds <= 0:
            raise ValueError("a deadline needs a positive number of seconds")
        self._monotonic = monotonic
        self._expires_at = monotonic() + seconds

    def remaining(self) -> float:
        """Seconds left, never negative."""
        return max(0.0, self._expires_at - self._monotonic())

    def expired(self) -> bool:
        return self.remaining() <= 0.0
