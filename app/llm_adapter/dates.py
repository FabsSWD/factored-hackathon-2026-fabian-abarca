"""Transaction dates from what the customer says: the year is resolved in code, not by the model.

The model returns the date as given: day and month, with the year only when the customer said
it. Relative expressions ("ayer", "el lunes pasado") are resolved by the model against the
business date in the context. Here:

- a date without a year takes the most recent occurrence not after the business date
  (16 June with business date 2026-06-17 -> 2026-06-16; 20 June -> 2025-06-20; 29 February
  -> the latest leap year that fits);
- any resulting date after the business date, or more than MAX_PAST_DAYS before it, is
  discarded (``transaction_date_discarded``), as is an impossible date (31 February).

Dates are on the business clock (policy §15), never the real clock.
"""

from __future__ import annotations

from datetime import date
from typing import Any

MAX_PAST_DAYS = 400
_LEAP_SEARCH_YEARS = 8


class DateOutOfSchemaError(ValueError):
    """The model's date object has the wrong shape or out-of-range parts."""


def _part(value: Any, name: str, low: int, high: int, required: bool) -> int | None:
    if value is None and not required:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DateOutOfSchemaError(f"{name} must be an integer in [{low}, {high}]")
    return value


def parse_date_parts(value: Any) -> tuple[int, int, int | None] | None:
    """Validate ``{day, month, year|null}``; ``None`` when the model gave no date."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"day", "month", "year"}:
        raise DateOutOfSchemaError("a date must be an object with day, month and year")
    day = _part(value["day"], "day", 1, 31, required=True)
    month = _part(value["month"], "month", 1, 12, required=True)
    year = _part(value["year"], "year", 1900, 2100, required=False)
    assert day is not None and month is not None
    return day, month, year


def resolve_date(day: int, month: int, year: int | None, business_date: date | None) -> date | None:
    """The calendar date, or ``None`` if it cannot be resolved or falls outside the window."""
    if year is not None:
        try:
            resolved = date(year, month, day)
        except ValueError:
            return None
    else:
        if business_date is None:
            return None
        resolved_or_none = _latest_occurrence(day, month, business_date)
        if resolved_or_none is None:
            return None
        resolved = resolved_or_none
    if business_date is not None and not within_window(resolved, business_date):
        return None
    return resolved


def within_window(value: date, business_date: date) -> bool:
    return value <= business_date and (business_date - value).days <= MAX_PAST_DAYS


def _latest_occurrence(day: int, month: int, business_date: date) -> date | None:
    for year in range(business_date.year, business_date.year - _LEAP_SEARCH_YEARS, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue  # 29 February in a common year, or an impossible day
        if candidate <= business_date:
            return candidate
    return None
