from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from app.llm_adapter.dates import (
    MAX_DELIVERY_DISTANCE_DAYS,
    MAX_PAST_DAYS,
    DateOutOfSchemaError,
    parse_date_parts,
    resolve_date,
    resolve_delivery_date,
    within_window,
)

BUSINESS_DATE = date(2026, 6, 17)


@pytest.mark.parametrize(
    ("day", "month", "year", "expected"),
    [
        (16, 6, None, date(2026, 6, 16)),
        (17, 6, None, date(2026, 6, 17)),
        (18, 6, None, date(2025, 6, 18)),
        (20, 6, None, date(2025, 6, 20)),
        (1, 1, None, date(2026, 1, 1)),
        (31, 12, None, date(2025, 12, 31)),
        (16, 6, 2026, date(2026, 6, 16)),
        (18, 6, 2026, None),  # future
        (12, 5, 2025, None),  # 401 days back
        (13, 5, 2025, date(2025, 5, 13)),  # exactly 400 days back
        (31, 2, 2026, None),  # impossible
        (31, 4, None, None),  # 31 April never exists
    ],
)
def test_resolve_date(day: int, month: int, year: int | None, expected: date | None) -> None:
    assert resolve_date(day, month, year, BUSINESS_DATE) == expected


def test_29_february() -> None:
    assert resolve_date(29, 2, None, date(2028, 6, 17)) == date(2028, 2, 29)
    assert resolve_date(29, 2, None, date(2028, 2, 28)) is None  # 2024-02-29: 730 days back
    assert resolve_date(29, 2, None, BUSINESS_DATE) is None  # 2024-02-29: 474 days back
    assert resolve_date(29, 2, 2024, date(2024, 12, 31)) == date(2024, 2, 29)


def test_without_business_date() -> None:
    assert resolve_date(16, 6, None, None) is None
    assert resolve_date(16, 6, 2019, None) == date(2019, 6, 16)  # no window to check


def test_window() -> None:
    assert MAX_PAST_DAYS == 400
    assert within_window(BUSINESS_DATE, BUSINESS_DATE)
    assert not within_window(date(2026, 6, 18), BUSINESS_DATE)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ({"day": 16, "month": 6, "year": None}, (16, 6, None)),
        ({"day": 16, "month": 6, "year": 2026}, (16, 6, 2026)),
    ],
)
def test_parse_date_parts(value: Any, expected: tuple[int, int, int | None] | None) -> None:
    assert parse_date_parts(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "2026-06-16",
        {"day": 0, "month": 6, "year": None},
        {"day": 16, "month": 0, "year": None},
        {"day": 16, "month": 6, "year": 1800},
        {"day": 16.0, "month": 6, "year": None},
        {"day": 16, "month": 6},
        {"day": 16, "month": 6, "year": None, "extra": 1},
        {"day": False, "month": 6, "year": None},
    ],
)
def test_malformed_parts(value: Any) -> None:
    with pytest.raises(DateOutOfSchemaError):
        parse_date_parts(value)


# --- Expected delivery dates: closest occurrence, past or future -------------------------------


@pytest.mark.parametrize(
    ("day", "month", "year", "expected"),
    [
        (1, 6, None, date(2026, 6, 1)),  # recent past
        (20, 6, None, date(2026, 6, 20)),  # near future: valid for a delivery
        (17, 6, None, date(2026, 6, 17)),  # the business date itself
        (17, 12, None, date(2025, 12, 17)),  # 182 days back beats 183 days ahead
        (18, 12, None, date(2025, 12, 18)),  # 181 days back beats 184 days ahead
        (1, 1, None, date(2026, 1, 1)),  # 167 days back beats 2027-01-01 (198 ahead)
        (1, 11, None, date(2026, 11, 1)),  # 137 days ahead beats 2025-11-01 (228 back)
        (1, 6, 2026, date(2026, 6, 1)),  # full date
        (17, 6, 2027, date(2027, 6, 17)),  # exactly 365 days ahead
        (17, 6, 2025, date(2025, 6, 17)),  # exactly 365 days back
        (18, 6, 2027, None),  # 366 days ahead
        (16, 6, 2025, None),  # 366 days back
        (31, 2, None, None),  # impossible
        (31, 2, 2026, None),  # impossible, with a year
    ],
)
def test_resolve_delivery_date(
    day: int, month: int, year: int | None, expected: date | None
) -> None:
    assert resolve_delivery_date(day, month, year, BUSINESS_DATE) == expected


def test_delivery_exact_tie_takes_the_past_occurrence() -> None:
    business_date = date(2023, 8, 31)  # 2023-03-01 is 183 days back, 2024-03-01 183 ahead
    assert resolve_delivery_date(1, 3, None, business_date) == date(2023, 3, 1)


def test_delivery_29_february_and_missing_business_date() -> None:
    assert resolve_delivery_date(29, 2, None, date(2028, 6, 17)) == date(2028, 2, 29)
    assert resolve_delivery_date(29, 2, None, BUSINESS_DATE) is None  # no leap year within ±1
    assert resolve_delivery_date(1, 6, None, None) is None
    assert resolve_delivery_date(1, 6, 2019, None) == date(2019, 6, 1)
    assert MAX_DELIVERY_DISTANCE_DAYS == 365
