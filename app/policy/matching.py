"""Deterministic transaction matching: GATE-05 and the RC_DUPLICATE rule of GATE-10.

GATE-05 (policy §5, "Transaction matching"): the details the customer gave are compared with
their transactions; details that find nothing are relaxed in steps (amount with tolerance,
without amount, without amount and date), and what a relaxed step finds is only a list of
candidates for the customer to pick from.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from app.contracts import TransactionField, TransactionRecord, TransactionRef
from app.policy.clock import business_day, transaction_within, within_window

DATE_TOLERANCE_DAYS = 1  # GATE-05: date ±1 business day
# Words that name a kind of business, not the business: ignored when merchants are compared.
GENERIC_MERCHANT_WORDS = frozenset(
    {
        # es
        "el", "la", "los", "las", "de", "del", "y", "en", "un", "una",
        "restaurante", "restaurant", "tienda", "almacen", "supermercado", "super", "farmacia",
        "cafeteria", "panaderia", "bar", "local", "negocio", "comercio", "lugar",
        # pt
        "o", "a", "os", "as", "do", "da", "dos", "das", "e", "em", "um", "uma",
        "loja", "mercado", "padaria", "lanchonete", "estabelecimento",
    }
)  # fmt: skip
# Policy order on a tie when asking for a detail: merchant, date, amount.
DETAIL_ORDER = (TransactionField.MERCHANT, TransactionField.DATE, TransactionField.AMOUNT)


@dataclass(frozen=True)
class Tolerance:
    """GATE-05 tolerance for an approximate amount: the larger of a share of the amount given
    and a USD amount converted to the transaction currency."""

    percent: Decimal
    usd: Decimal


def normalize_merchant(name: str) -> str:
    """Case- and accent-insensitive form of a merchant name, with whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def merchant_words(name: str) -> frozenset[str]:
    """The words of a merchant name that identify the business (generic words removed)."""
    spaced = "".join(char if char.isalnum() else " " for char in normalize_merchant(name))
    return frozenset(spaced.split()) - GENERIC_MERCHANT_WORDS


def merchant_matches(given: str, merchant_name: str | None) -> bool:
    """The customer's words all in the merchant's name, or the other way round; or one name
    contained in the other. A name of generic words only ("restaurante") matches nothing."""
    if merchant_name is None:
        return False
    given_words, record_words = merchant_words(given), merchant_words(merchant_name)
    if not given_words:
        return False
    if record_words and (given_words <= record_words or record_words <= given_words):
        return True
    a, b = normalize_merchant(given), normalize_merchant(merchant_name)
    return a in b or b in a


def amount_matches(txn: TransactionRecord, amount: Decimal, tolerance: Tolerance | None) -> bool:
    """Exact in the transaction currency, or within the tolerance when one is given."""
    if tolerance is None:
        return txn.amount == amount
    allowed = amount * tolerance.percent / 100
    if txn.amount_usd:  # the USD part of the tolerance, in the transaction currency
        allowed = max(allowed, tolerance.usd * txn.amount / txn.amount_usd)
    return abs(txn.amount - amount) <= allowed


def consistent(
    txn: TransactionRecord,
    ref: TransactionRef,
    as_of: datetime,
    tolerance: Tolerance | None = None,
    *,
    use_amount: bool = True,
    use_date: bool = True,
) -> bool:
    """Whether the date, amount and merchant the customer gave (if any) fit the transaction."""
    if use_date and ref.transaction_date is not None:
        gap = abs((business_day(txn.transaction_date, as_of) - ref.transaction_date).days)
        if gap > DATE_TOLERANCE_DAYS:
            return False
    if use_amount and ref.amount is not None and not amount_matches(txn, ref.amount, tolerance):
        return False
    return ref.merchant is None or merchant_matches(ref.merchant, txn.merchant_name)


def _order(txn: TransactionRecord) -> tuple[datetime, str]:
    return txn.transaction_date, txn.transaction_id


@dataclass(frozen=True)
class Match:
    """GATE-05 result: the identified transaction, or the reason it is not identified."""

    transaction: TransactionRecord | None = None
    candidates: list[TransactionRecord] = field(default_factory=list)  # newest first
    note: str | None = None
    # The candidates come from relaxed details (or a qualified amount): the customer must
    # pick, even a single one.
    relaxed: bool = False


@dataclass(frozen=True)
class _Step:
    use_amount: bool
    tolerant: bool
    use_date: bool


def match_transaction(
    ref: TransactionRef | None,
    pool: list[TransactionRecord],
    as_of: datetime,
    late_window_days: int,
    tolerance: Tolerance | None = None,
) -> Match:
    """Apply the GATE-05 matching rule (policy §5, "Transaction matching")."""
    if ref is None:
        return Match(note="transaction_ref_missing")
    approximate = bool(ref.amount_approximate) and ref.amount is not None
    approximate = approximate and tolerance is not None
    if ref.transaction_id is not None:
        by_id = [txn for txn in pool if txn.transaction_id == ref.transaction_id]
        if not by_id:
            return Match(note="transaction_id_not_in_records")
        if not consistent(by_id[0], ref, as_of, tolerance if approximate else None):
            return Match(note="transaction_id_inconsistent")
        return Match(transaction=by_id[0])
    window = [t for t in pool if transaction_within(t.transaction_date, as_of, late_window_days)]

    def search(step: _Step) -> list[TransactionRecord]:
        allowed = tolerance if step.tolerant else None
        found = (
            txn
            for txn in window
            if consistent(
                txn, ref, as_of, allowed, use_amount=step.use_amount, use_date=step.use_date
            )
        )
        return sorted(found, key=_order, reverse=True)

    matches = search(_Step(use_amount=True, tolerant=approximate, use_date=True))
    if len(matches) == 1 and not approximate:
        return Match(transaction=matches[0])
    if matches:
        return Match(candidates=matches, relaxed=approximate)
    for step in _relaxations(ref, approximate, tolerance):
        matches = search(step)
        if matches:
            return Match(candidates=matches, note="relaxed_search", relaxed=True)
    return Match(note="no_matching_transaction")


def _relaxations(
    ref: TransactionRef, approximate: bool, tolerance: Tolerance | None
) -> list[_Step]:
    """Amount with tolerance, without amount, without amount and date; each step keeps at
    least one detail."""
    steps: list[_Step] = []
    if ref.amount is not None:
        if not approximate and tolerance is not None:
            steps.append(_Step(use_amount=True, tolerant=True, use_date=True))
        if ref.transaction_date is not None or ref.merchant is not None:
            steps.append(_Step(use_amount=False, tolerant=False, use_date=True))
    if (ref.amount is not None or ref.transaction_date is not None) and ref.merchant is not None:
        steps.append(_Step(use_amount=False, tolerant=False, use_date=False))
    return steps


def given_details(ref: TransactionRef | None) -> set[TransactionField]:
    """The details of the reference the customer gave."""
    if ref is None:
        return set()
    values = {
        TransactionField.MERCHANT: ref.merchant,
        TransactionField.DATE: ref.transaction_date,
        TransactionField.AMOUNT: ref.amount,
    }
    return {name for name, value in values.items() if value is not None}


def most_useful_detail(
    ref: TransactionRef | None, matches: list[TransactionRecord], as_of: datetime
) -> TransactionField | None:
    """Too many matches: the detail the customer did not give that best narrows them (the most
    distinct values among the matches), or None when they gave all three."""
    distinct: dict[TransactionField, int] = {
        TransactionField.MERCHANT: len(
            {normalize_merchant(t.merchant_name or "") for t in matches}
        ),
        TransactionField.DATE: len({business_day(t.transaction_date, as_of) for t in matches}),
        TransactionField.AMOUNT: len({(t.amount, t.currency) for t in matches}),
    }
    missing = [name for name in DETAIL_ORDER if name not in given_details(ref)]
    if not missing:
        return None
    return max(missing, key=lambda name: distinct[name])


def missing_detail(ref: TransactionRef | None) -> TransactionField:
    """Nothing found: the first detail the customer did not give (merchant, date, amount), or
    the merchant again when they gave all three."""
    given = given_details(ref)
    return next((name for name in DETAIL_ORDER if name not in given), TransactionField.MERCHANT)


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
            and within_window(
                min(other.transaction_date, txn.transaction_date),
                max(other.transaction_date, txn.transaction_date),
                window,
            )
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
