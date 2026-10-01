"""ACT-01 reads: only the session customer's records, and GATE-04 non-disclosure."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.contracts import AccessDeniedError, CaseRecord, ReasonCode, Tier, VerifiedFact
from app.interfaces import ToolLayer
from app.tools import DatabaseToolLayer, provenance, verified_fact
from app.tools.provenance import Record
from tests.fixtures.core_banking import (
    ACCOUNT,
    CARD,
    CUSTOMER,
    OTHER_CARD,
    TRANSACTIONS,
)
from tests.tools.conftest import (
    AS_OF,
    ToolFactory,
    add_transaction,
    security_events,
)

OWN_TRANSACTIONS = {tx.transaction_id for tx in TRANSACTIONS if tx.customer_id == CUSTOMER}


def test_implements_the_protocol(tools: DatabaseToolLayer) -> None:
    assert isinstance(tools, ToolLayer)


def test_get_customer(tools: DatabaseToolLayer) -> None:
    assert tools.get_customer().customer_id == CUSTOMER


def test_list_products_only_own(tools: DatabaseToolLayer) -> None:
    products = tools.list_products()
    assert {p.customer_id for p in products} == {CUSTOMER}
    assert OTHER_CARD not in {p.product_id for p in products}


def test_get_own_product(tools: DatabaseToolLayer) -> None:
    assert tools.get_product(CARD).product_number_masked == "****4821"


def test_get_own_transaction(tools: DatabaseToolLayer) -> None:
    assert tools.get_transaction("TRX-T1-PURCHASE").amount_usd is not None


@pytest.mark.parametrize(
    ("read", "foreign", "missing"),
    [
        ("get_product", OTHER_CARD, "PRD-NOBODY"),
        ("get_transaction", "TRX-OTHER-CUSTOMER", "TRX-NOBODY"),
        ("list_cases", "TRX-OTHER-CUSTOMER", "TRX-NOBODY"),
        ("transaction_candidates", "TRX-OTHER-CUSTOMER", "TRX-NOBODY"),
    ],
)
def test_foreign_and_missing_records_are_indistinguishable(
    tools: DatabaseToolLayer, db_session: Session, read: str, foreign: str, missing: str
) -> None:
    errors = []
    for record_id in (foreign, missing):
        with pytest.raises(AccessDeniedError) as caught:
            getattr(tools, read)(record_id)
        errors.append((type(caught.value), str(caught.value), caught.value.args))
    assert errors[0] == errors[1]
    events = security_events(db_session)
    assert len(events) == 2
    # The two events differ only in the requested ID: nothing says whether it exists.
    assert {k: v for k, v in events[0].items() if k != "requested_id"} == {
        k: v for k, v in events[1].items() if k != "requested_id"
    }
    assert events[0]["kind"] == "access_denied"
    assert events[0]["customer_id"] == CUSTOMER


def test_unauthenticated_reads_are_denied(make_tools: ToolFactory, db_session: Session) -> None:
    tools = make_tools(None)
    for read in (tools.get_customer, tools.list_products, tools.list_cases):
        with pytest.raises(AccessDeniedError):
            read()
    with pytest.raises(AccessDeniedError):
        tools.transaction_candidates()
    events = security_events(db_session)
    assert len(events) == 4
    assert {e["reason"] for e in events} == {"unauthenticated"}
    assert {e["customer_id"] for e in events} == {None}


# --- Transaction pool for the Policy Engine ------------------------------------------------


def test_pool_has_only_own_transactions_before_as_of(tools: DatabaseToolLayer) -> None:
    pool = tools.transaction_candidates()
    assert {t.customer_id for t in pool} == {CUSTOMER}
    assert {t.transaction_id for t in pool} == OWN_TRANSACTIONS
    dates = [t.transaction_date for t in pool]
    assert dates == sorted(dates, reverse=True)


def test_pool_window_is_late_window_days_of_business_days(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    # Business day BUSINESS_DATE - 120 starts at that day's 06:00 cutoff.
    first_inside = AS_OF - timedelta(days=121)
    add_transaction(db_session, "TRX-EDGE-IN", first_inside)
    add_transaction(db_session, "TRX-EDGE-OUT", first_inside - timedelta(seconds=1))
    ids = {t.transaction_id for t in tools.transaction_candidates()}
    assert "TRX-EDGE-IN" in ids
    assert "TRX-EDGE-OUT" not in ids


def test_pool_adds_an_own_transaction_referenced_by_id(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    add_transaction(db_session, "TRX-OLD", datetime(2025, 1, 10, 12))
    pool = tools.transaction_candidates("TRX-OLD")
    assert "TRX-OLD" in {t.transaction_id for t in pool}
    assert len(tools.transaction_candidates("TRX-T1-PURCHASE")) == len(OWN_TRANSACTIONS)


# --- Cases ---------------------------------------------------------------------------------


def test_list_cases_own_and_by_transaction(tools: DatabaseToolLayer) -> None:
    created = tools.create_case("TRX-T1-PURCHASE", *_t1())
    cases = tools.list_cases()
    assert [c.case_id for c in cases] == [created.record_id]
    assert tools.list_cases("TRX-T1-PURCHASE")[0].case_id == created.record_id
    assert tools.list_cases("TRX-NO-SCORE") == []


def test_other_customer_does_not_see_the_case(
    tools: DatabaseToolLayer, other_tools: DatabaseToolLayer
) -> None:
    tools.create_case("TRX-T1-PURCHASE", *_t1())
    assert other_tools.list_cases() == []


def _t1() -> tuple[ReasonCode, Tier]:
    return ReasonCode.UNRECOGNIZED, Tier.T1


# --- DATA-04 provenance --------------------------------------------------------------------


def test_every_record_names_its_source_and_id(tools: DatabaseToolLayer) -> None:
    created = tools.create_case("TRX-T1-PURCHASE", *_t1())
    case: CaseRecord = tools.list_cases()[0]
    records: list[tuple[Record, str, str | None]] = [
        (tools.get_customer(), "customers", CUSTOMER),
        (tools.get_product(ACCOUNT), "products", ACCOUNT),
        (tools.get_transaction("TRX-FEE"), "transactions", "TRX-FEE"),
        (case, "cases", created.record_id),
    ]
    for record, source, record_id in records:
        assert provenance(record) == (source, record_id)
    fact = verified_fact("Card ending 4821 is Active", tools.get_product(CARD))
    assert fact == VerifiedFact(
        fact="Card ending 4821 is Active", source="products", record_id=CARD
    )


def test_customer_country_is_presentation_only(
    tools: DatabaseToolLayer, make_tools: ToolFactory
) -> None:
    assert tools.customer_country() == "Colombia"
    assert make_tools(None).customer_country() is None
    assert "country" not in tools.get_customer().model_dump()  # never a decision input
