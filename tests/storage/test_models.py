"""Every model can be written and read, and the database enforces its constraints."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.storage.models import (
    AuditLog,
    Base,
    Case,
    Customer,
    HandoffPacketRow,
    Product,
    SessionRow,
    Transaction,
)

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
LINEAGE = {"source_file": "raw/test.csv", "ingested_at": datetime(2026, 9, 28, 12, 0)}


def customer(**overrides: Any) -> Customer:
    return Customer(
        **{"customer_id": "CLI-M1", "customer_status": "Active", **LINEAGE, **overrides}
    )


def product(**overrides: Any) -> Product:
    return Product(
        **{
            "product_id": "PRD-M1",
            "customer_id": "CLI-M1",
            "product_type": "Tarjeta Crédito",
            "product_number_last4": "4821",
            "currency": "USD",
            "product_status": "Active",
            **LINEAGE,
            **overrides,
        }
    )


def transaction(**overrides: Any) -> Transaction:
    return Transaction(
        **{
            "transaction_id": "TRX-M1",
            "customer_id": "CLI-M1",
            "product_id": "PRD-M1",
            "transaction_date": datetime(2026, 6, 16, 10, 0),
            "process_date": date(2026, 6, 16),
            "transaction_type": "Purchase",
            "amount": Decimal("10.00"),
            "currency": "USD",
            "amount_usd": Decimal("10.00"),
            "amount_usd_source": "identity",
            "transaction_status": "Approved",
            **LINEAGE,
            **overrides,
        }
    )


def case(**overrides: Any) -> Case:
    return Case(
        **{
            "case_id": "CASE-M1",
            "customer_id": "CLI-M1",
            "transaction_id": "TRX-M1",
            "reason_code": "RC_UNRECOGNIZED",
            "status": "Open",
            "tier": "T1",
            "amount": Decimal("10.00"),
            "currency": "USD",
            "amount_usd": Decimal("10.00"),
            "provisional_credit_flag": "eligible",
            "idempotency_key": "TRX-M1:RC_UNRECOGNIZED",
            **overrides,
        }
    )


def session_row(**overrides: Any) -> SessionRow:
    return SessionRow(
        **{
            "session_id": "SES-M1",
            "customer_id": "CLI-M1",
            "auth_method": "test_otp",
            "created_at": NOW,
            "last_activity_at": NOW,
            **overrides,
        }
    )


def handoff(**overrides: Any) -> HandoffPacketRow:
    return HandoffPacketRow(
        **{
            "handoff_id": "HO-20260928-000001",
            "customer_id": "CLI-M1",
            "transaction_id": "TRX-M1",
            "queue": "fraud",
            "priority": "high",
            "policy_version": "0.3.0",
            "packet": {"triggered_rules": ["ESC-03"]},
            **overrides,
        }
    )


def audit(**overrides: Any) -> AuditLog:
    return AuditLog(
        **{"event_type": "turn_trace", "trace_id": "TR-M1", "payload": {"outcome": "RESOLVE"}}
        | overrides
    )


def _base(db: Session) -> None:
    db.add(customer())
    db.flush()
    db.add(product())
    db.flush()
    db.add(transaction())
    db.flush()


def _fails(db: Session, row: Base) -> None:
    db.add(row)
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# --- Round trips --------------------------------------------------------------


def test_every_model_round_trips(db_session: Session) -> None:
    _base(db_session)
    db_session.add_all([case(), session_row()])
    db_session.flush()
    db_session.add_all([handoff(), audit(session_id="SES-M1")])
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(Case, "CASE-M1")
    assert stored is not None
    assert stored.created_at.tzinfo is not None
    assert db_session.get(Transaction, "TRX-M1") is not None
    packet = db_session.get(HandoffPacketRow, "HO-20260928-000001")
    assert packet is not None
    assert packet.status == "pending"
    assert packet.packet == {"triggered_rules": ["ESC-03"]}
    log = db_session.scalar(select(AuditLog).where(AuditLog.trace_id == "TR-M1"))
    assert log is not None
    assert log.payload["outcome"] == "RESOLVE"


def test_transaction_timestamps_stay_naive(db_session: Session) -> None:
    _base(db_session)
    db_session.expire_all()
    stored = db_session.get(Transaction, "TRX-M1")
    assert stored is not None
    assert stored.transaction_date == datetime(2026, 6, 16, 10, 0)
    assert stored.transaction_date.tzinfo is None


def test_security_event_without_trace_id(db_session: Session) -> None:
    db_session.add(audit(event_type="security_event", trace_id=None, payload={"kind": "otp"}))
    db_session.flush()


# --- Constraints --------------------------------------------------------------

CORE_VIOLATIONS: list[tuple[str, Callable[[], Base]]] = [
    ("customer status", lambda: customer(customer_id="CLI-X", customer_status="Activo")),
    ("product type", lambda: product(product_id="PRD-X", product_type="Tarjeta Prepago")),
    ("product status", lambda: product(product_id="PRD-X", product_status="Frozen")),
    ("product currency", lambda: product(product_id="PRD-X", currency="MXN")),
    ("product last4", lambda: product(product_id="PRD-X", product_number_last4="48a1")),
    ("product owner", lambda: product(product_id="PRD-X", customer_id="CLI-NOBODY")),
    ("transaction type", lambda: transaction(transaction_id="T-X", transaction_type="Compra")),
    ("transaction status", lambda: transaction(transaction_id="T-X", transaction_status="Ok")),
    ("transaction amount", lambda: transaction(transaction_id="T-X", amount=Decimal("0"))),
    (
        "missing usd without flag",
        lambda: transaction(transaction_id="T-X", amount_usd=None, amount_usd_source="identity"),
    ),
    (
        "flag missing with usd",
        lambda: transaction(transaction_id="T-X", amount_usd_source="missing"),
    ),
    (
        "fx rate without date",
        lambda: transaction(transaction_id="T-X", amount_usd_source="fx_rate"),
    ),
    (
        "date without fx rate",
        lambda: transaction(transaction_id="T-X", fx_rate_date=date(2026, 6, 16)),
    ),
    ("usd source value", lambda: transaction(transaction_id="T-X", amount_usd_source="guess")),
    ("fraud score", lambda: transaction(transaction_id="T-X", fraud_score=Decimal("100.5"))),
    ("transaction product", lambda: transaction(transaction_id="T-X", product_id="PRD-NOBODY")),
]


@pytest.mark.parametrize(("name", "factory"), CORE_VIOLATIONS, ids=[n for n, _ in CORE_VIOLATIONS])
def test_core_banking_constraints(
    db_session: Session, name: str, factory: Callable[[], Base]
) -> None:
    _base(db_session)
    _fails(db_session, factory())


CASE_VIOLATIONS: list[tuple[str, dict[str, Any]]] = [
    ("reason code", {"reason_code": "RC_OTHER"}),
    ("status", {"status": "Pending"}),
    ("tier", {"tier": "T4"}),
    ("credit flag", {"provisional_credit_flag": "maybe"}),
    ("amount", {"amount": Decimal("-1")}),
    ("unknown transaction", {"transaction_id": "TRX-NOBODY"}),
    ("missing idempotency key", {"idempotency_key": None}),
]


@pytest.mark.parametrize(("name", "changes"), CASE_VIOLATIONS, ids=[n for n, _ in CASE_VIOLATIONS])
def test_case_constraints(db_session: Session, name: str, changes: dict[str, Any]) -> None:
    _base(db_session)
    _fails(db_session, case(**changes))


def test_idempotency_key_is_unique(db_session: Session) -> None:
    _base(db_session)
    db_session.add(case())
    db_session.flush()
    _fails(db_session, case(case_id="CASE-M2"))


@pytest.mark.parametrize(
    "changes",
    [{"queue": "sales"}, {"priority": "urgent"}, {"status": "closed"}, {"handoff_id": "HO-1"}],
)
def test_handoff_constraints(db_session: Session, changes: dict[str, Any]) -> None:
    _base(db_session)
    _fails(db_session, handoff(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"event_type": "debug"},
        {"event_type": "turn_trace", "trace_id": None},
        {"event_type": "security_event", "trace_id": "TR-X"},
        {"payload": None},
    ],
)
def test_audit_constraints(db_session: Session, changes: dict[str, Any]) -> None:
    _fails(db_session, audit(**changes))


def test_trace_id_is_unique(db_session: Session) -> None:
    db_session.add(audit())
    db_session.flush()
    _fails(db_session, audit())


def test_session_activity_cannot_precede_creation(db_session: Session) -> None:
    _base(db_session)
    _fails(db_session, session_row(last_activity_at=datetime(2026, 9, 28, 11, 0, tzinfo=UTC)))


def test_document_hash_is_unique(db_session: Session) -> None:
    db_session.add(customer(document_hash="a" * 64))
    db_session.flush()
    _fails(db_session, customer(customer_id="CLI-M2", document_hash="a" * 64))
