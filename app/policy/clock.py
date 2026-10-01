"""The business clock (policy §15, "Business date").

``as_of`` is ``BUSINESS_DATE + 1 day at BUSINESS_DAY_CUTOFF`` (``app.settings.business_as_of``),
so both values are recovered from it and the engine needs no other clock input. A business
day D runs from D at the cutoff to D+1 just before it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


def business_date(as_of: datetime) -> date:
    """The last complete business day: ``BUSINESS_DATE``."""
    return as_of.date() - timedelta(days=1)


def _cutoff(as_of: datetime) -> timedelta:
    return as_of - datetime.combine(as_of.date(), datetime.min.time())


def business_day(moment: datetime, as_of: datetime) -> date:
    """The business day a timestamp belongs to: the moment minus the cutoff."""
    return (moment - _cutoff(as_of)).date()


def within_window(start: date | datetime, end: date | datetime, length: timedelta) -> bool:
    """The single window convention of the policy (§15): inclusive, ``end - start <= length``.

    Used by GATE-08 (business days of the transaction and ``BUSINESS_DATE``), the GATE-05 pool
    (``LATE_WINDOW_DAYS``), ESC-02 (``business_created_at`` and ``as_of``) and RC_DUPLICATE
    (``DUPLICATE_WINDOW_HOURS`` between the two charges)."""
    return end - start <= length  # type: ignore[operator]


def transaction_within(moment: datetime, as_of: datetime, days: int) -> bool:
    """GATE-08 age: the transaction's business day is at most ``days`` calendar days before
    ``BUSINESS_DATE``. The same function serves ``DISPUTE_WINDOW_DAYS`` and
    ``LATE_WINDOW_DAYS``."""
    return within_window(business_day(moment, as_of), business_date(as_of), timedelta(days=days))
