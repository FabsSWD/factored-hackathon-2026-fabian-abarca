from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.contracts import TransactionRef
from app.policy.clock import business_date, business_day, transaction_within, within_window
from app.policy.matching import (
    disputed_of,
    duplicate_twins,
    match_transaction,
    merchant_matches,
    nearest_twin,
    normalize_merchant,
)
from tests.policy.conftest import AS_OF, BUSINESS_DATE, TXN_DATE, txn

# --- Business clock --------------------------------------------------------------------------


def test_business_date_is_the_day_before_as_of() -> None:
    assert business_date(AS_OF) == BUSINESS_DATE


@pytest.mark.parametrize(
    ("moment", "expected"),
    [
        (datetime(2026, 6, 17, 6, 0), date(2026, 6, 17)),
        (datetime(2026, 6, 18, 5, 59), date(2026, 6, 17)),  # after midnight, same business day
        (datetime(2026, 6, 17, 5, 59), date(2026, 6, 16)),
    ],
)
def test_business_day_uses_the_cutoff(moment: datetime, expected: date) -> None:
    assert business_day(moment, AS_OF) == expected


def test_transaction_age_counts_calendar_days_to_the_business_date() -> None:
    assert transaction_within(datetime(2026, 6, 18, 5, 0), AS_OF, 0)
    assert transaction_within(datetime(2026, 4, 18, 12, 0), AS_OF, 60)
    assert not transaction_within(datetime(2026, 4, 18, 12, 0), AS_OF, 59)
    assert transaction_within(datetime(2026, 4, 19, 5, 59), AS_OF, 60)  # business day 04-18
    assert not transaction_within(datetime(2026, 4, 18, 5, 59), AS_OF, 60)  # business day 04-17


@pytest.mark.parametrize(
    ("start", "end", "length", "inside"),
    [
        (date(2026, 4, 18), date(2026, 6, 17), timedelta(days=60), True),
        (date(2026, 4, 17), date(2026, 6, 17), timedelta(days=60), False),
        (datetime(2026, 6, 1, 8), datetime(2026, 6, 3, 8), timedelta(hours=48), True),
        (datetime(2026, 6, 1, 8), datetime(2026, 6, 3, 8, 0, 1), timedelta(hours=48), False),
    ],
)
def test_one_inclusive_window_convention(
    start: date, end: date, length: timedelta, inside: bool
) -> None:
    assert within_window(start, end, length) is inside


# --- Merchant names --------------------------------------------------------------------------


def test_merchant_normalization_ignores_case_accents_and_spacing() -> None:
    assert normalize_merchant("  Café   SINTÉTICO ") == "cafe sintetico"


@pytest.mark.parametrize(
    ("given", "merchant", "expected"),
    [
        ("cafe sintetico", "Café Sintético", True),
        ("Sintetico", "Cafe Sintetico", True),  # given contained in the record
        ("Cafe Sintetico Centro", "Cafe Sintetico", True),  # record contained in given
        ("Streaming Plus", "Cafe Sintetico", False),
        ("Cafe", None, False),
    ],
)
def test_merchant_containment(given: str, merchant: str | None, expected: bool) -> None:
    assert merchant_matches(given, merchant) is expected


def test_empty_normalized_names_never_match() -> None:
    assert not merchant_matches("́", "Cafe")  # a lone combining accent normalizes to ""


# --- GATE-05 matching ------------------------------------------------------------------------


POOL = [
    txn("TXN-1", when=TXN_DATE, amount="50", merchant="Cafe Sintetico"),
    txn("TXN-2", when=TXN_DATE + timedelta(days=1), amount="50", merchant="Streaming Plus"),
    txn("TXN-3", when=TXN_DATE + timedelta(days=3), amount="75", merchant="Cafe Sintetico"),
]


def match(ref: TransactionRef | None, pool: list = POOL):  # type: ignore[no-untyped-def,type-arg]
    return match_transaction(ref, pool, AS_OF, 120)


def test_missing_reference() -> None:
    result = match(None)
    assert result.transaction is None and result.note == "transaction_ref_missing"


def test_id_must_be_in_the_records() -> None:
    assert match(TransactionRef(transaction_id="TXN-9")).note == "transaction_id_not_in_records"


def test_id_alone_matches() -> None:
    assert match(TransactionRef(transaction_id="TXN-3")).transaction == POOL[2]


def test_id_inconsistent_with_other_details_is_ambiguous() -> None:
    result = match(TransactionRef(transaction_id="TXN-3", amount=Decimal("50")))
    assert result.transaction is None and result.note == "transaction_id_inconsistent"


def test_id_consistent_with_other_details() -> None:
    ref = TransactionRef(
        transaction_id="TXN-1", transaction_date=date(2026, 6, 10), merchant="cafe"
    )
    assert match(ref).transaction == POOL[0]


def test_any_combination_resolving_one_transaction() -> None:
    assert match(TransactionRef(amount=Decimal("75"))).transaction == POOL[2]
    assert match(TransactionRef(merchant="streaming")).transaction == POOL[1]
    assert match(TransactionRef(merchant="cafe", amount=Decimal("50"))).transaction == POOL[0]


@pytest.mark.parametrize(
    ("day", "expected"),
    [(8, None), (9, "TXN-1"), (10, "TXN-1"), (11, "TXN-1"), (12, "TXN-3")],
)
def test_date_tolerance_is_one_day(day: int, expected: str | None) -> None:
    # The cafe charges are TXN-1 on 06-10 and TXN-3 on 06-13.
    result = match(TransactionRef(transaction_date=date(2026, 6, day), merchant="cafe"))
    found = result.transaction.transaction_id if result.transaction else None
    assert found == expected


def test_amount_is_exact() -> None:
    assert match(TransactionRef(amount=Decimal("50.01"))).note == "no_matching_transaction"


def test_several_matches_are_candidates_newest_first() -> None:
    result = match(TransactionRef(amount=Decimal("50")))
    assert result.transaction is None
    assert [t.transaction_id for t in result.candidates] == ["TXN-2", "TXN-1"]


def test_candidate_order_does_not_depend_on_the_pool_order() -> None:
    ref = TransactionRef(amount=Decimal("50"))
    assert match(ref, list(reversed(POOL))).candidates == match(ref).candidates


def test_matching_without_id_stays_within_the_late_window() -> None:
    old = txn("TXN-OLD", when=datetime(2026, 2, 1, 12), amount="75")
    pool = [old, POOL[2]]
    assert match(TransactionRef(amount=Decimal("75")), pool).transaction == POOL[2]


def test_business_day_is_used_for_the_date() -> None:
    late_night = txn("TXN-N", when=datetime(2026, 6, 12, 2, 0))  # business day 06-11
    result = match_transaction(
        TransactionRef(transaction_date=date(2026, 6, 10)), [late_night], AS_OF, 120
    )
    assert result.transaction == late_night


# --- RC_DUPLICATE pairs ----------------------------------------------------------------------


ORIGINAL = txn("TXN-A", when=TXN_DATE)


@pytest.mark.parametrize(
    ("other", "is_twin"),
    [
        (txn("TXN-B", when=TXN_DATE + timedelta(hours=48)), True),
        (txn("TXN-B", when=TXN_DATE - timedelta(hours=48)), True),
        (txn("TXN-B", when=TXN_DATE + timedelta(hours=48, seconds=1)), False),
        (txn("TXN-B", when=TXN_DATE, product_id="PRD-2"), False),
        (txn("TXN-B", when=TXN_DATE, merchant="Otro"), False),
        (txn("TXN-B", when=TXN_DATE, amount="51"), False),
        (txn("TXN-B", when=TXN_DATE, currency="COP"), False),
        (txn("TXN-B", when=TXN_DATE, status="Reversed"), False),
        (txn("TXN-A", when=TXN_DATE), False),  # same transaction_id is never a duplicate
    ],
)
def test_twin_criteria(other, is_twin: bool) -> None:  # type: ignore[no-untyped-def]
    assert (duplicate_twins(ORIGINAL, [ORIGINAL, other], 48) == [other]) is is_twin


def test_adjustments_without_merchant_can_be_twins() -> None:
    fee = txn("TXN-F1", transaction_type="Adjustment", merchant=None)
    again = txn(
        "TXN-F2", transaction_type="Adjustment", merchant=None, when=TXN_DATE + timedelta(hours=1)
    )
    assert duplicate_twins(again, [fee, again], 48) == [fee]


def test_the_later_charge_is_disputed() -> None:
    early = txn("TXN-A", when=TXN_DATE)
    late = txn("TXN-B", when=TXN_DATE + timedelta(hours=2))
    assert disputed_of(early, late) == late
    assert disputed_of(late, early) == late


def test_nearest_twin_prefers_an_earlier_charge() -> None:
    a = txn("TXN-A", when=TXN_DATE - timedelta(hours=10))
    b = txn("TXN-B", when=TXN_DATE - timedelta(hours=2))
    c = txn("TXN-C", when=TXN_DATE + timedelta(hours=2))
    named = txn("TXN-X", when=TXN_DATE)
    assert nearest_twin(named, [a, b, c]) == b
    assert nearest_twin(named, [c]) == c  # the customer named the original
