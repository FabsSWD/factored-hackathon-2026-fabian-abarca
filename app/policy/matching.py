"""Deterministic transaction matching: GATE-05 and the RC_DUPLICATE rule of GATE-10.

GATE-05 (policy §5, "Transaction matching"): the details the customer gave are compared with
their transactions; details that find nothing are relaxed in steps (amount with tolerance,
without amount, without amount and date, and last without the merchant), and what a relaxed
step finds is only a list of candidates for the customer to pick from. A merchant named only
with generic words is a category ("un restaurante" -> Food) or, without one, no detail at
all; a period of days ("entre el 15 y el 19 de junio") matches its business days, ±1 day.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.contracts import TransactionField, TransactionRecord, TransactionRef
from app.policy.clock import business_day, transaction_within, within_window

DATE_TOLERANCE_DAYS = 1  # GATE-05: date ±1 business day
MAX_RANGE_DAYS = 31  # a longer period does not filter by date
# Articles and prepositions: never part of what identifies a business.
_FILLER_WORDS = frozenset(
    {
        "el", "la", "los", "las", "lo", "al", "de", "del", "y", "en", "un", "una", "unos",
        "o", "a", "os", "as", "do", "da", "dos", "das", "e", "em", "um", "uma", "no", "na",
    }
)  # fmt: skip
# Words for a place of business that say nothing about which one, with no category.
_VAGUE_WORDS = frozenset(
    {
        "tienda", "almacen", "local", "negocio", "comercio", "lugar", "sitio", "loja",
        "estabelecimento", "establecimiento",
    }
)  # fmt: skip
# Kinds of business ignored when two names are compared word by word; with the categories of
# config/merchant_categories.yaml, the words of each category are ignored as well.
GENERIC_MERCHANT_WORDS = _FILLER_WORDS | _VAGUE_WORDS | frozenset(
    {"restaurante", "restaurant", "supermercado", "farmacia", "cafeteria", "panaderia",
     "mercado", "padaria", "lanchonete"}
)  # fmt: skip
# Policy order on a tie when asking for a detail: merchant, date, amount.
DETAIL_ORDER = (TransactionField.MERCHANT, TransactionField.DATE, TransactionField.AMOUNT)


@dataclass(frozen=True)
class Tolerance:
    """GATE-05 tolerance for an approximate amount: the larger of a share of the amount given
    and a USD amount converted to the transaction currency."""

    percent: Decimal
    usd: Decimal


class MerchantKind(StrEnum):
    NAME = "name"  # "el buen sabor": compared with the merchant's name
    CATEGORY = "category"  # "un restaurante": compared with the merchant's category
    ABSENT = "absent"  # "una tienda": says nothing, the merchant is left out


@dataclass(frozen=True)
class Merchant:
    kind: MerchantKind
    category: str | None = None


def normalize_merchant(name: str) -> str:
    """Case- and accent-insensitive form of a merchant name, with whitespace collapsed."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def _words(name: str) -> frozenset[str]:
    spaced = "".join(char if char.isalnum() else " " for char in normalize_merchant(name))
    return frozenset(spaced.split())


def merchant_words(name: str, generic: frozenset[str] = GENERIC_MERCHANT_WORDS) -> frozenset[str]:
    """The words of a merchant name that identify the business (generic words removed)."""
    return _words(name) - generic


def merchant_criterion(given: str, categories: dict[str, str]) -> Merchant:
    """What a merchant the customer gave can be compared with: its name, its category (only
    generic words, one category among them), or nothing."""
    words = _words(given) - _FILLER_WORDS
    if words - _VAGUE_WORDS - GENERIC_MERCHANT_WORDS - set(categories):
        return Merchant(MerchantKind.NAME)
    found = {categories[word] for word in words if word in categories}
    if len(found) == 1:
        return Merchant(MerchantKind.CATEGORY, found.pop())
    return Merchant(MerchantKind.ABSENT)


def merchant_matches(
    given: str, merchant_name: str | None, generic: frozenset[str] = GENERIC_MERCHANT_WORDS
) -> bool:
    """The customer's words all in the merchant's name, or the other way round; or one name
    contained in the other. A name of generic words only ("restaurante") matches nothing."""
    if merchant_name is None:
        return False
    given_words, record_words = (
        merchant_words(given, generic),
        merchant_words(merchant_name, generic),
    )
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


def range_filters(ref: TransactionRef) -> bool:
    """A period the customer gave that narrows the search (at most MAX_RANGE_DAYS)."""
    if ref.date_from is None or ref.date_to is None:
        return False
    return (ref.date_to - ref.date_from).days <= MAX_RANGE_DAYS


def _date_fits(txn: TransactionRecord, ref: TransactionRef, as_of: datetime) -> bool:
    day = business_day(txn.transaction_date, as_of)
    tolerance = timedelta(days=DATE_TOLERANCE_DAYS)
    if ref.transaction_date is not None:
        return abs(day - ref.transaction_date) <= tolerance
    if range_filters(ref):
        assert ref.date_from is not None and ref.date_to is not None
        return ref.date_from - tolerance <= day <= ref.date_to + tolerance
    return True


def consistent(
    txn: TransactionRecord,
    ref: TransactionRef,
    as_of: datetime,
    tolerance: Tolerance | None = None,
    *,
    use_amount: bool = True,
    use_date: bool = True,
    use_merchant: bool = True,
    categories: dict[str, str] | None = None,
) -> bool:
    """Whether the date, amount and merchant the customer gave (if any) fit the transaction."""
    if use_date and not _date_fits(txn, ref, as_of):
        return False
    if use_amount and ref.amount is not None and not amount_matches(txn, ref.amount, tolerance):
        return False
    if ref.merchant is None or not use_merchant:
        return True
    words = categories or {}
    merchant = merchant_criterion(ref.merchant, words)
    if merchant.kind is MerchantKind.CATEGORY:
        return txn.merchant_category == merchant.category
    if merchant.kind is MerchantKind.ABSENT:
        return True
    return merchant_matches(ref.merchant, txn.merchant_name, GENERIC_MERCHANT_WORDS | set(words))


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
    use_merchant: bool = True


def match_transaction(
    ref: TransactionRef | None,
    pool: list[TransactionRecord],
    as_of: datetime,
    late_window_days: int,
    tolerance: Tolerance | None = None,
    categories: dict[str, str] | None = None,
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
        allowed = tolerance if approximate else None
        if not consistent(by_id[0], ref, as_of, allowed, categories=categories):
            return Match(note="transaction_id_inconsistent")
        return Match(transaction=by_id[0])
    window = [t for t in pool if transaction_within(t.transaction_date, as_of, late_window_days)]

    def search(step: _Step) -> list[TransactionRecord]:
        allowed = tolerance if step.tolerant else None
        found = (
            txn
            for txn in window
            if consistent(
                txn,
                ref,
                as_of,
                allowed,
                use_amount=step.use_amount,
                use_date=step.use_date,
                use_merchant=step.use_merchant,
                categories=categories,
            )
        )
        return sorted(found, key=_order, reverse=True)

    matches = search(_Step(use_amount=True, tolerant=approximate, use_date=True))
    if len(matches) == 1 and not approximate:
        return Match(transaction=matches[0])
    if matches:
        return Match(candidates=matches, relaxed=approximate)
    given = given_details(ref, categories)
    merchant = merchant_criterion(ref.merchant, categories or {}) if ref.merchant else None
    weak = merchant is not None and merchant.kind is MerchantKind.CATEGORY
    for step in _relaxations(given, approximate, tolerance, weak_merchant=weak):
        matches = search(step)
        if matches:
            return Match(candidates=matches, note="relaxed_search", relaxed=True)
    return Match(note="no_matching_transaction")


def _relaxations(
    given: set[TransactionField],
    approximate: bool,
    tolerance: Tolerance | None,
    *,
    weak_merchant: bool = False,
) -> list[_Step]:
    """Amount with tolerance, without amount, without amount and date, and last without the
    merchant (name or category, which may be missing in the data) but with the amount, with
    tolerance, and the date; each step keeps at least one detail. A merchant that is only a
    category is the weakest detail: then the step without it goes right after the tolerance."""
    has_merchant, has_date = TransactionField.MERCHANT in given, TransactionField.DATE in given
    has_amount = TransactionField.AMOUNT in given
    no_merchant = _Step(
        use_amount=True, tolerant=tolerance is not None, use_date=True, use_merchant=False
    )
    drop_merchant = has_merchant and (has_amount or has_date)
    steps: list[_Step] = []
    if has_amount and not approximate and tolerance is not None:
        steps.append(_Step(use_amount=True, tolerant=True, use_date=True))
    if drop_merchant and weak_merchant:
        steps.append(no_merchant)
    if has_amount and (has_date or has_merchant):
        steps.append(_Step(use_amount=False, tolerant=False, use_date=True))
    if drop_merchant:
        steps.append(_Step(use_amount=False, tolerant=False, use_date=False))
        if not weak_merchant:
            steps.append(no_merchant)
    return steps


def given_details(
    ref: TransactionRef | None, categories: dict[str, str] | None = None
) -> set[TransactionField]:
    """The details of the reference that narrow the search: a merchant by name or category, a
    day or a period of at most MAX_RANGE_DAYS, an amount."""
    if ref is None:
        return set()
    given: set[TransactionField] = set()
    merchant = merchant_criterion(ref.merchant, categories or {}) if ref.merchant else None
    if merchant is not None and merchant.kind is not MerchantKind.ABSENT:
        given.add(TransactionField.MERCHANT)
    if ref.transaction_date is not None or range_filters(ref):
        given.add(TransactionField.DATE)
    if ref.amount is not None:
        given.add(TransactionField.AMOUNT)
    return given


def most_useful_detail(
    ref: TransactionRef | None,
    matches: list[TransactionRecord],
    as_of: datetime,
    categories: dict[str, str] | None = None,
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
    given = given_details(ref, categories)
    missing = [name for name in DETAIL_ORDER if name not in given]
    if not missing:
        return None
    return max(missing, key=lambda name: distinct[name])


def missing_detail(
    ref: TransactionRef | None, categories: dict[str, str] | None = None
) -> TransactionField:
    """Nothing found: the first detail the customer did not give (merchant, date, amount), or
    the merchant again when they gave all three."""
    given = given_details(ref, categories)
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
