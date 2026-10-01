"""Deterministic transaction matching: GATE-05 and the RC_DUPLICATE rule of GATE-10."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.contracts import TransactionRecord, TransactionRef
from app.policy.clock import age_days, business_day

DATE_TOLERANCE_DAYS = 1  # GATE-05: date ±1 business day


def normalize_merchant(name: str) -> str:
    """Case- and accent-insensitive form of a merchant name, with whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def merchant_matches(given: str, merchant_name: str | None) -> bool:
    """One name contained in the other, after normalization."""
    if merchant_name is None:
        return False
    a, b = normalize_merchant(given), normalize_merchant(merchant_name)
    return bool(a) and bool(b) and (a in b or b in a)


def consistent(txn: TransactionRecord, ref: TransactionRef, as_of: datetime) -> bool:
    """Whether the date, amount and merchant the customer gave (if any) fit the transaction."""
    if ref.transaction_date is not None:
        gap = abs((business_day(txn.transaction_date, as_of) - ref.transaction_date).days)
        if gap > DATE_TOLERANCE_DAYS:
            return False
    if ref.amount is not None and txn.amount != ref.amount:
        return False
    return ref.merchant is None or merchant_matches(ref.merchant, txn.merchant_name)


def _order(txn: TransactionRecord) -> tuple[datetime, str]:
    return txn.transaction_date, txn.transaction_id


@dataclass(frozen=True)
class Match:
    """GATE-05 result: the identified transaction, or the reason it is not identified."""

    transaction: TransactionRecord | None = None
    candidates: list[TransactionRecord] = field(default_factory=list)  # 2+ matches, newest first
    note: str | None = None


def match_transaction(
    ref: TransactionRef | None,
    pool: list[TransactionRecord],
    as_of: datetime,
    late_window_days: int,
) -> Match:
    """Apply the GATE-05 matching rule (policy §5, "Transaction matching")."""
    if ref is None:
        return Match(note="transaction_ref_missing")
    if ref.transaction_id is not None:
        by_id = [txn for txn in pool if txn.transaction_id == ref.transaction_id]
        if not by_id:
            return Match(note="transaction_id_not_in_records")
        if not consistent(by_id[0], ref, as_of):
            return Match(note="transaction_id_inconsistent")
        return Match(transaction=by_id[0])
    matches = sorted(
        (
            txn
            for txn in pool
            if age_days(txn.transaction_date, as_of) <= late_window_days
            and consistent(txn, ref, as_of)
        ),
        key=_order,
        reverse=True,
    )
    if len(matches) == 1:
        return Match(transaction=matches[0])
    if not matches:
        return Match(note="no_matching_transaction")
    return Match(candidates=matches)


def duplicate_twins(
    txn: TransactionRecord, pool: list[TransactionRecord], window_hours: int
) -> list[TransactionRecord]:
    """Other approved charges with the same product, merchant, amount and currency within
    ``DUPLICATE_WINDOW_HOURS`` (GATE-10), ordered by time."""
    window = timedelta(hours=window_hours)
    return sorted(
        (
            other
            for other in pool
            if other.transaction_id != txn.transaction_id
            and other.product_id == txn.product_id
            and other.merchant_name == txn.merchant_name
            and other.amount == txn.amount
            and other.currency == txn.currency
            and other.transaction_status == "Approved"
            and abs(other.transaction_date - txn.transaction_date) <= window
        ),
        key=_order,
    )


def nearest_twin(txn: TransactionRecord, twins: list[TransactionRecord]) -> TransactionRecord:
    """The twin to show the customer: the nearest earlier one, so the charge they named is the
    disputed one; otherwise they named the original, and the nearest later twin is shown."""
    earlier = [twin for twin in twins if _order(twin) < _order(txn)]
    return earlier[-1] if earlier else twins[0]


def disputed_of(txn: TransactionRecord, twin: TransactionRecord) -> TransactionRecord:
    """RC_DUPLICATE: the earlier charge is legitimate and the later one is disputed."""
    return max(txn, twin, key=_order)
