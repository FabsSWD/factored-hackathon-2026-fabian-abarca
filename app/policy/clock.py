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


def age_days(moment: datetime, as_of: datetime) -> int:
    """GATE-08: calendar days between ``BUSINESS_DATE`` and the business day of ``moment``.

    The single age function for ``DISPUTE_WINDOW_DAYS`` and ``LATE_WINDOW_DAYS``."""
    return (business_date(as_of) - business_day(moment, as_of)).days
