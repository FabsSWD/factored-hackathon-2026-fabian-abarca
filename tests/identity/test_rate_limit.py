from __future__ import annotations

import pytest

from app.identity.rate_limit import RateLimiter


class Ticker:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_allows_up_to_the_limit_then_blocks() -> None:
    ticker = Ticker()
    limiter = RateLimiter(3, 60, ticker)
    assert [limiter.hit("k") for _ in range(3)] == [None, None, None]
    assert limiter.hit("k") == 60


def test_window_slides() -> None:
    ticker = Ticker()
    limiter = RateLimiter(2, 60, ticker)
    limiter.hit("k")
    ticker.now += 30
    limiter.hit("k")
    ticker.now += 10
    assert limiter.hit("k") == 20  # the first hit leaves the window in 20 s
    ticker.now += 20
    assert limiter.hit("k") is None


def test_keys_are_independent() -> None:
    limiter = RateLimiter(1, 60, Ticker())
    assert limiter.hit("a") is None
    assert limiter.hit("b") is None
    assert limiter.hit("a") is not None


def test_retry_after_is_at_least_one_second() -> None:
    ticker = Ticker()
    limiter = RateLimiter(1, 60, ticker)
    limiter.hit("k")
    ticker.now += 59.9
    assert limiter.hit("k") == 1


@pytest.mark.parametrize(("limit", "window"), [(0, 60), (1, 0)])
def test_invalid_configuration(limit: int, window: float) -> None:
    with pytest.raises(ValueError):
        RateLimiter(limit, window)
