from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from app.config import load_merchant_categories
from app.contracts import TransactionField, TransactionRef
from app.policy.clock import business_date, business_day, transaction_within, within_window
from app.policy.matching import (
    MerchantKind,
    Tolerance,
    amount_matches,
    consistent,
    disputed_of,
    duplicate_twins,
    given_details,
    match_transaction,
    merchant_criterion,
    merchant_matches,
    missing_detail,
    most_useful_detail,
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


@pytest.mark.parametrize(
    ("given", "merchant", "expected"),
    [
        ("el buen sabor", "Restaurante El Buen Sabor", True),
        ("Restaurante el Buen Sabor", "El Buen Sabor", True),
        ("buen sabor restaurante", "Restaurante El Buen Sabor", True),  # any word order
        ("sabor", "Restaurante El Buen Sabor", True),
        ("restaurante", "Restaurante El Buen Sabor", False),  # only a kind of business
        ("la tienda", "Tienda La Esquina", False),
        ("loja do zé", "Loja do Zé", True),
        ("buen gusto", "Restaurante El Buen Sabor", False),
    ],
)
def test_merchant_by_words_without_generic_words(given: str, merchant: str, expected: bool) -> None:
    assert merchant_matches(given, merchant) is expected


TOLERANCE = Tolerance(percent=Decimal("10"), usd=Decimal("5"))


@pytest.mark.parametrize(
    ("record", "record_usd", "given", "expected"),
    [
        ("38.50", "38.50", "40", True),
        ("45.00", "45.00", "40", True),  # 5 USD beats 10% of 40
        ("45.01", "45.01", "40", False),
        ("110.00", "110.00", "100", True),  # 10% of 100 beats 5 USD
        ("110.01", "110.01", "100", False),
        ("14.90", "14.90", "10", True),  # 5 USD beats 10% of 10
        ("15.01", "15.01", "10", False),
        ("158000", "40", "160000", True),  # COP: 10% of 160,000
        ("100000", "25", "120000", True),  # COP: 5 USD = 20,000 COP at this rate
        ("39", None, "40", True),  # without a USD amount only the percent applies
    ],
)
def test_approximate_amount_tolerance(
    record: str, record_usd: str | None, given: str, expected: bool
) -> None:
    charge = txn(amount=record, amount_usd=record_usd)
    assert amount_matches(charge, Decimal(given), TOLERANCE) is expected
    assert amount_matches(charge, Decimal(record), None)


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


def test_amount_is_exact_and_a_near_amount_is_only_a_candidate() -> None:
    assert match(TransactionRef(amount=Decimal("50.01"))).note == "no_matching_transaction"
    near = match_transaction(TransactionRef(amount=Decimal("50.01")), POOL, AS_OF, 120, TOLERANCE)
    assert near.transaction is None and near.relaxed
    assert [t.transaction_id for t in near.candidates] == ["TXN-2", "TXN-1"]


# --- GATE-05: relaxed search (policy 0.4.7) -------------------------------------------------

BUEN_SABOR = txn(
    "TXN-BS",
    when=datetime(2026, 6, 16, 15),
    amount="38.50",
    amount_usd="38.50",
    merchant="Restaurante El Buen Sabor",
)
OTHER_DAY = txn("TXN-OD", when=datetime(2026, 6, 14, 12), amount="12", merchant="Kiosko 24")


def relaxed(ref: TransactionRef, pool: list | None = None):  # type: ignore[no-untyped-def,type-arg]
    return match_transaction(ref, pool or [BUEN_SABOR, OTHER_DAY, *POOL], AS_OF, 120, TOLERANCE)


def test_the_manual_test_finds_el_buen_sabor_as_a_candidate() -> None:
    said = TransactionRef(merchant="el buen sabor", amount=Decimal("40"), amount_approximate=True)
    result = relaxed(said)
    assert result.transaction is None and result.relaxed
    assert result.candidates == [BUEN_SABOR]  # listed, even alone: the customer picks it


def test_an_exact_amount_that_finds_nothing_is_searched_with_tolerance() -> None:
    result = relaxed(TransactionRef(merchant="el buen sabor", amount=Decimal("40")))
    assert result.candidates == [BUEN_SABOR] and result.note == "relaxed_search"


def test_a_qualified_amount_never_identifies_on_its_own() -> None:
    exact = relaxed(TransactionRef(merchant="el buen sabor", amount=Decimal("38.50")))
    assert exact.transaction == BUEN_SABOR
    qualified = TransactionRef(
        merchant="el buen sabor", amount=Decimal("38.50"), amount_approximate=True
    )
    assert relaxed(qualified).candidates == [BUEN_SABOR]


def test_relaxation_drops_the_amount_then_the_date() -> None:
    wrong_amount = TransactionRef(merchant="Kiosko", amount=Decimal("90"))
    assert relaxed(wrong_amount).candidates == [OTHER_DAY]
    wrong_both = TransactionRef(
        merchant="Kiosko", amount=Decimal("90"), transaction_date=date(2026, 6, 1)
    )
    assert relaxed(wrong_both).candidates == [OTHER_DAY]
    # Without a merchant, dropping amount and date would leave nothing: no match.
    only_numbers = TransactionRef(amount=Decimal("999"), transaction_date=date(2026, 6, 1))
    assert relaxed(only_numbers).note == "no_matching_transaction"


def test_a_wrong_merchant_alone_finds_nothing() -> None:
    result = relaxed(TransactionRef(merchant="Zapateria Inventada"))
    assert result.note == "no_matching_transaction"


def test_an_approximate_id_check_uses_the_tolerance() -> None:
    picked = TransactionRef(transaction_id="TXN-BS", amount=Decimal("40"), amount_approximate=True)
    assert relaxed(picked).transaction == BUEN_SABOR
    exact = TransactionRef(transaction_id="TXN-BS", amount=Decimal("40"))
    assert relaxed(exact).note == "transaction_id_inconsistent"


def test_the_detail_that_best_narrows_many_matches() -> None:
    names = ["Cafe A", "Cafe B", "Cafe C", "Cafe D"]
    same_day = [
        txn(f"TXN-{i}", when=datetime(2026, 6, 10, 9 + i), amount=str(10 + i), merchant=name)
        for i, name in enumerate(names)
    ]
    by_date = TransactionRef(transaction_date=date(2026, 6, 10))
    assert most_useful_detail(by_date, same_day, AS_OF) is TransactionField.MERCHANT
    one_shop = [txn(f"TXN-{i}", when=datetime(2026, 6, 1 + i, 12), amount="10") for i in range(4)]
    by_amount = TransactionRef(amount=Decimal("10"))
    assert most_useful_detail(by_amount, one_shop, AS_OF) is TransactionField.DATE
    everything = TransactionRef(
        merchant="cafe", amount=Decimal("10"), transaction_date=date(2026, 6, 1)
    )
    assert most_useful_detail(everything, one_shop, AS_OF) is None


def test_the_detail_asked_for_when_nothing_is_found() -> None:
    assert missing_detail(None) is TransactionField.MERCHANT
    assert missing_detail(TransactionRef(merchant="x")) is TransactionField.DATE
    dated = TransactionRef(merchant="x", transaction_date=date(2026, 6, 1))
    assert missing_detail(dated) is TransactionField.AMOUNT
    everything = TransactionRef(
        merchant="x", amount=Decimal("1"), transaction_date=date(2026, 6, 1)
    )
    assert missing_detail(everything) is TransactionField.MERCHANT


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


# --- GATE-05: merchant categories and periods (policy 0.4.8) --------------------------------

CATEGORIES = load_merchant_categories()
FOOD = txn(
    "TXN-F", when=datetime(2026, 6, 16, 13), amount="38.50", amount_usd="38.50",
    merchant="Restaurante El Buen Sabor",
)  # fmt: skip
FOOD = FOOD.model_copy(update={"merchant_category": "Food"})
FOOD_FAR = txn("TXN-FF", when=datetime(2026, 6, 16, 9), amount="120", merchant="Super Ahorro")
FOOD_FAR = FOOD_FAR.model_copy(update={"merchant_category": "Food"})
CINEMA = txn("TXN-C", when=datetime(2026, 6, 16, 20), amount="39", merchant="Cine Premium")
CINEMA = CINEMA.model_copy(update={"merchant_category": "Entertainment"})
SHOPS = [FOOD, FOOD_FAR, CINEMA]


def by_category(ref: TransactionRef):  # type: ignore[no-untyped-def]
    return match_transaction(ref, SHOPS, AS_OF, 120, TOLERANCE, CATEGORIES)


@pytest.mark.parametrize(
    ("given", "kind", "category"),
    [
        ("el buen sabor", MerchantKind.NAME, None),
        ("un restaurante", MerchantKind.CATEGORY, "Food"),
        ("Restaurante", MerchantKind.CATEGORY, "Food"),
        ("una farmacia", MerchantKind.CATEGORY, "Health"),
        ("uma farmácia", MerchantKind.CATEGORY, "Health"),
        ("una tienda", MerchantKind.ABSENT, None),  # Food and Other in the data: no category
        ("uma loja", MerchantKind.ABSENT, None),
        ("un lugar", MerchantKind.ABSENT, None),
        ("un restaurante o una farmacia", MerchantKind.ABSENT, None),  # two categories
        ("Restaurante El Buen Sabor", MerchantKind.NAME, None),
    ],
)
def test_what_a_merchant_is_compared_with(
    given: str, kind: MerchantKind, category: str | None
) -> None:
    merchant = merchant_criterion(given, CATEGORIES)
    assert merchant.kind is kind and merchant.category == category


def test_the_manual_test_3_restaurant_of_about_40() -> None:
    said = TransactionRef(merchant="un restaurante", amount=Decimal("40"), amount_approximate=True)
    result = by_category(said)
    assert result.candidates == [FOOD] and result.relaxed  # the cinema of 39 is not Food


def test_a_merchant_without_category_is_left_out_of_the_search() -> None:
    said = TransactionRef(merchant="una tienda", amount=Decimal("39"))
    assert by_category(said).transaction == CINEMA  # searched by the amount alone
    assert TransactionField.MERCHANT not in given_details(said, CATEGORIES)
    assert TransactionField.MERCHANT in given_details(
        TransactionRef(merchant="un restaurante"), CATEGORIES
    )


def test_a_purchase_without_category_is_not_found_by_category() -> None:
    uncategorized = FOOD.model_copy(update={"merchant_category": None})
    ref = TransactionRef(merchant="restaurante", amount=Decimal("38.50"))
    result = match_transaction(ref, [uncategorized], AS_OF, 120, TOLERANCE, CATEGORIES)
    assert result.transaction is None


def test_category_words_are_generic_when_a_name_is_compared() -> None:
    ref = TransactionRef(merchant="restaurante el buen sabor")
    assert by_category(ref).transaction == FOOD


@pytest.mark.parametrize(
    ("first", "last", "found"),
    [
        (date(2026, 6, 15), date(2026, 6, 17), True),  # the cinema on 06-16 is in it
        (date(2026, 6, 17), date(2026, 6, 17), True),  # 06-16 is within ±1 day
        (date(2026, 6, 1), date(2026, 6, 14), False),  # then relaxed: only a candidate
        (date(2026, 5, 1), date(2026, 6, 10), True),  # more than 31 days: no date filter
    ],
)
def test_a_period_matches_its_business_days(first: date, last: date, found: bool) -> None:
    ref = TransactionRef(date_from=first, date_to=last, merchant="Cine Premium")
    result = match_transaction(ref, SHOPS, AS_OF, 120, TOLERANCE, CATEGORIES)
    assert (result.transaction == CINEMA) is found


def test_a_short_period_is_a_date_detail_and_a_long_one_is_not() -> None:
    short = TransactionRef(date_from=date(2026, 6, 15), date_to=date(2026, 6, 19))
    long = TransactionRef(date_from=date(2026, 5, 1), date_to=date(2026, 6, 10))
    assert TransactionField.DATE in given_details(short)
    assert TransactionField.DATE not in given_details(long)


def test_a_wrong_period_is_relaxed_away() -> None:
    ref = TransactionRef(
        merchant="Cine Premium", date_from=date(2026, 6, 1), date_to=date(2026, 6, 5)
    )
    result = match_transaction(ref, SHOPS, AS_OF, 120, TOLERANCE, CATEGORIES)
    assert result.candidates == [CINEMA] and result.relaxed


def test_a_category_is_dropped_before_the_amount() -> None:
    # "un restaurante" + "como de 40" + a period: El Buen Sabor has no category in the data.
    uncategorized = FOOD.model_copy(update={"merchant_category": None})
    said = TransactionRef(
        merchant="un restaurante",
        amount=Decimal("40"),
        amount_approximate=True,
        date_from=date(2026, 6, 15),
        date_to=date(2026, 6, 17),
    )
    result = match_transaction(said, [uncategorized, FOOD_FAR], AS_OF, 120, TOLERANCE, CATEGORIES)
    assert result.transaction is None and result.relaxed  # only a candidate to pick
    assert result.candidates == [uncategorized]  # not Super Ahorro (Food, 120)


def test_a_merchant_name_is_dropped_after_the_amount() -> None:
    # A concrete name is a strong detail: "without amount" comes first and finds it.
    renamed = FOOD.model_copy(update={"merchant_name": "Restaurante Otro", "amount": Decimal("90")})
    said = TransactionRef(
        merchant="Restaurante Otro",
        amount=Decimal("40"),
        amount_approximate=True,
        date_from=date(2026, 6, 15),
        date_to=date(2026, 6, 17),
    )
    result = match_transaction(said, [renamed, CINEMA], AS_OF, 120, TOLERANCE, CATEGORIES)
    assert result.candidates == [renamed]  # by name and period, not the cinema of 39


def test_without_amount_or_date_the_merchant_is_never_dropped() -> None:
    uncategorized = FOOD.model_copy(update={"merchant_category": None})
    result = match_transaction(
        TransactionRef(merchant="un restaurante"),
        [uncategorized],
        AS_OF,
        120,
        TOLERANCE,
        CATEGORIES,
    )
    assert result.note == "no_matching_transaction"


def test_the_last_step_keeps_the_exact_amount_without_a_tolerance() -> None:
    other = FOOD.model_copy(update={"merchant_category": None})
    near = TransactionRef(merchant="un restaurante", amount=Decimal("40"))
    exact = TransactionRef(merchant="un restaurante", amount=Decimal("38.50"))
    assert match_transaction(near, [other], AS_OF, 120, None, CATEGORIES).candidates == []
    assert match_transaction(exact, [other], AS_OF, 120, None, CATEGORIES).candidates == [other]


# --- Transaction types named as the merchant (M18 run 1: S034 "cajero", S043/R021 "depósito") --

ATM = txn("TXN-ATM", transaction_type="Withdrawal", merchant=None, amount="80")
DEPOSIT = txn("TXN-DEP", transaction_type="Deposit", merchant=None, amount="200")
SHOP = txn("TXN-SHOP", amount="80")
TYPES_POOL = [ATM, DEPOSIT, SHOP]
WORDS = load_merchant_categories()


@pytest.mark.parametrize(
    ("merchant", "amount", "expected"),
    [
        ("un cajero", "80", "TXN-ATM"),
        ("el cajero automático", "80", "TXN-ATM"),
        ("caixa eletrônico", "80", "TXN-ATM"),
        ("depósito", "200", "TXN-DEP"),
        ("transferência", "80", None),  # no transfer in the pool: never the purchase
    ],
)
def test_a_transaction_type_given_as_merchant(
    merchant: str, amount: str, expected: str | None
) -> None:
    ref = TransactionRef(
        transaction_date=TXN_DATE.date(), amount=Decimal(amount), merchant=merchant
    )
    found = match_transaction(ref, TYPES_POOL, AS_OF, 120, None, WORDS)
    assert (found.transaction.transaction_id if found.transaction else None) == expected
    assert not consistent(SHOP, ref, AS_OF, categories=WORDS)


def test_a_charge_in_general_is_not_a_merchant() -> None:
    ref = TransactionRef(
        transaction_date=TXN_DATE.date(), amount=Decimal("80"), merchant="la compra"
    )
    assert consistent(SHOP, ref, AS_OF, categories=WORDS)
