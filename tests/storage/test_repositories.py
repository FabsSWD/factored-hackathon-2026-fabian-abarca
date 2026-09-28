"""Read functions used by the Tool Layer: a query for one customer never returns another's."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.contracts import CaseStatus, ProvisionalCreditFlag, ReasonCode, Tier
from app.storage.models import Case
from app.storage.repositories import CoreBankingRepository
from tests.conftest import Pipeline
from tests.fixtures.core_banking import (
    CARD,
    CUSTOMER,
    OTHER_CARD,
    OTHER_CUSTOMER,
    SUSPENDED_CUSTOMER,
    TRANSACTIONS,
)


@pytest.fixture
def repo(loaded: Pipeline, db_session: Session) -> CoreBankingRepository:
    return CoreBankingRepository(db_session)


def _case(db: Session, case_id: str, customer_id: str, transaction_id: str) -> None:
    db.add(
        Case(
            case_id=case_id,
            customer_id=customer_id,
            transaction_id=transaction_id,
            reason_code="RC_UNRECOGNIZED",
            status="Open",
            tier="T1",
            amount=Decimal("50.00"),
            currency="USD",
            amount_usd=Decimal("50.00"),
            provisional_credit_flag="eligible",
            idempotency_key=f"{transaction_id}:RC_UNRECOGNIZED",
            business_created_at=datetime(2026, 6, 18, 6, 0),
        )
    )
    db.flush()


# --- Customers and products ---------------------------------------------------


def test_get_customer(repo: CoreBankingRepository) -> None:
    record = repo.get_customer(CUSTOMER)
    assert record is not None
    assert record.customer_status == "Active"
    suspended = repo.get_customer(SUSPENDED_CUSTOMER)
    assert suspended is not None
    assert suspended.customer_status == "Suspended"
    assert repo.get_customer("CLI-NOBODY") is None


def test_list_products_only_own(repo: CoreBankingRepository) -> None:
    products = repo.list_products(CUSTOMER)
    assert {p.customer_id for p in products} == {CUSTOMER}
    assert len(products) == 4
    assert OTHER_CARD not in {p.product_id for p in products}


def test_products_are_masked(repo: CoreBankingRepository) -> None:
    card = repo.get_product(CUSTOMER, CARD)
    assert card is not None
    assert card.product_number_masked == "****4821"
    assert card.product_type == "Tarjeta Crédito"


def test_other_customers_product_looks_like_a_missing_one(repo: CoreBankingRepository) -> None:
    assert repo.get_product(CUSTOMER, OTHER_CARD) is None
    assert repo.get_product(CUSTOMER, "PRD-DOESNOTEXIST") is None
    assert repo.get_product(OTHER_CUSTOMER, OTHER_CARD) is not None


# --- Transactions -------------------------------------------------------------


def test_get_transaction(repo: CoreBankingRepository) -> None:
    record = repo.get_transaction(CUSTOMER, "TRX-COP-FX")
    assert record is not None
    assert record.amount == Decimal("200000.00")
    assert record.amount_usd == Decimal("50.00")
    assert record.transaction_date == datetime(2026, 6, 17, 15, 0)
    assert record.transaction_date.tzinfo is None


def test_transaction_with_unknown_usd_and_score(repo: CoreBankingRepository) -> None:
    missing = repo.get_transaction(CUSTOMER, "TRX-ARS-MISSING")
    assert missing is not None
    assert missing.amount_usd is None
    no_score = repo.get_transaction(CUSTOMER, "TRX-NO-SCORE")
    assert no_score is not None
    assert no_score.fraud_score is None
    high = repo.get_transaction(CUSTOMER, "TRX-HIGH-FRAUD")
    assert high is not None
    assert high.fraud_score == 91.5


def test_other_customers_transaction_looks_like_a_missing_one(
    repo: CoreBankingRepository,
) -> None:
    assert repo.get_transaction(CUSTOMER, "TRX-OTHER-CUSTOMER") is None
    assert repo.get_transaction(CUSTOMER, "TRX-DOESNOTEXIST") is None
    assert repo.get_transaction(OTHER_CUSTOMER, "TRX-OTHER-CUSTOMER") is not None


def test_find_without_filters_returns_all_own_newest_first(repo: CoreBankingRepository) -> None:
    found = repo.find_transactions(CUSTOMER)
    own = [tx for tx in TRANSACTIONS if tx.customer_id == CUSTOMER]
    assert len(found) == len(own)
    assert {t.customer_id for t in found} == {CUSTOMER}
    dates = [t.transaction_date for t in found]
    assert dates == sorted(dates, reverse=True)


@pytest.mark.parametrize("customer_id", [CUSTOMER, OTHER_CUSTOMER, SUSPENDED_CUSTOMER])
def test_find_never_returns_another_customers_rows(
    repo: CoreBankingRepository, customer_id: str
) -> None:
    # Same merchant and amount exist for several customers.
    found = repo.find_transactions(customer_id, merchant_contains="Cafe", amount=None)
    assert found
    assert {t.customer_id for t in found} == {customer_id}


def test_find_by_date_amount_and_merchant(repo: CoreBankingRepository) -> None:
    found = repo.find_transactions(
        CUSTOMER, on_date=date(2026, 6, 16), amount=Decimal("50.00"), merchant_contains="cafe"
    )
    assert [t.transaction_id for t in found] == ["TRX-T1-PURCHASE"]


def test_date_tolerance(repo: CoreBankingRepository) -> None:
    exact = repo.find_transactions(CUSTOMER, on_date=date(2026, 6, 15), merchant_contains="Electro")
    assert [t.transaction_id for t in exact] == ["TRX-T3-PURCHASE"]
    assert (
        repo.find_transactions(CUSTOMER, on_date=date(2026, 6, 16), merchant_contains="Electro")
        == []
    )
    tolerant = repo.find_transactions(
        CUSTOMER, on_date=date(2026, 6, 16), date_tolerance_days=1, merchant_contains="Electro"
    )
    assert [t.transaction_id for t in tolerant] == ["TRX-T3-PURCHASE"]


def test_negative_tolerance_is_rejected(repo: CoreBankingRepository) -> None:
    with pytest.raises(ValueError, match="date_tolerance_days"):
        repo.find_transactions(CUSTOMER, on_date=date(2026, 6, 16), date_tolerance_days=-1)


def test_merchant_filter_escapes_wildcards(repo: CoreBankingRepository) -> None:
    assert repo.find_transactions(CUSTOMER, merchant_contains="%") == []
    assert repo.find_transactions(CUSTOMER, merchant_contains="_") == []


def test_duplicate_candidate_query(repo: CoreBankingRepository) -> None:
    found = repo.find_transactions(
        CUSTOMER,
        product_id=CARD,
        merchant_name="Streaming Plus",
        amount=Decimal("18.90"),
        currency="USD",
        transaction_status="Approved",
        start=datetime(2026, 6, 14, 8, 0),
        end=datetime(2026, 6, 16, 8, 0),
        exclude_transaction_id="TRX-DUP-SECOND",
    )
    assert [t.transaction_id for t in found] == ["TRX-DUP-FIRST"]


def test_filters_by_type_and_limit(repo: CoreBankingRepository) -> None:
    fees = repo.find_transactions(CUSTOMER, transaction_type="Adjustment")
    assert [t.transaction_id for t in fees] == ["TRX-FEE"]
    assert len(repo.find_transactions(CUSTOMER, limit=3)) == 3


def test_unknown_customer_has_no_records(repo: CoreBankingRepository) -> None:
    assert repo.find_transactions("CLI-NOBODY") == []
    assert repo.list_products("CLI-NOBODY") == []
    assert repo.list_cases("CLI-NOBODY") == []


# --- Cases --------------------------------------------------------------------


def test_list_cases_only_own(repo: CoreBankingRepository, db_session: Session) -> None:
    _case(db_session, "CASE-A1", CUSTOMER, "TRX-T1-PURCHASE")
    _case(db_session, "CASE-A2", CUSTOMER, "TRX-HIGH-FRAUD")
    _case(db_session, "CASE-B1", OTHER_CUSTOMER, "TRX-OTHER-CUSTOMER")

    cases = repo.list_cases(CUSTOMER)
    assert {c.case_id for c in cases} == {"CASE-A1", "CASE-A2"}
    one = repo.list_cases(CUSTOMER, transaction_id="TRX-T1-PURCHASE")
    assert [c.case_id for c in one] == ["CASE-A1"]
    record = one[0]
    assert record.reason_code is ReasonCode.UNRECOGNIZED
    assert record.status is CaseStatus.OPEN
    assert record.tier is Tier.T1
    assert record.provisional_credit_flag is ProvisionalCreditFlag.ELIGIBLE
    assert record.created_at.tzinfo is not None
    assert record.business_created_at == datetime(2026, 6, 18, 6, 0)
    # Another customer's case is invisible even when queried by its transaction.
    assert repo.list_cases(CUSTOMER, transaction_id="TRX-OTHER-CUSTOMER") == []


def test_case_without_credit_flag(repo: CoreBankingRepository, db_session: Session) -> None:
    _case(db_session, "CASE-A3", CUSTOMER, "TRX-FEE")
    stored = db_session.get(Case, "CASE-A3")
    assert stored is not None
    stored.provisional_credit_flag = None
    stored.tier = "T2"
    db_session.flush()
    (record,) = repo.list_cases(CUSTOMER, transaction_id="TRX-FEE")
    assert record.provisional_credit_flag is None
