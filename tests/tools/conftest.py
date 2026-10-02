from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import load_policy_config
from app.contracts import (
    AuthStatus,
    EscalationReason,
    Evidence,
    EvidenceKind,
    HandoffAuth,
    HandoffPacket,
    Language,
    Priority,
    Queue,
    SessionContext,
)
from app.storage.models import AuditLog, Product, SessionRow, Transaction
from app.tools import DatabaseToolLayer, ToolConfig
from tests.conftest import Pipeline
from tests.fixtures.core_banking import CUSTOMER, OTHER_CUSTOMER

AS_OF = datetime(2026, 6, 18, 6, 0)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
PARAMETERS = load_policy_config().parameters


@pytest.fixture
def session_factory(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def make_session(db: Session, customer_id: str, session_id: str) -> SessionContext:
    db.add(
        SessionRow(
            session_id=session_id,
            customer_id=customer_id,
            auth_method="test_otp",
            created_at=NOW,
            last_activity_at=NOW,
        )
    )
    db.flush()
    return SessionContext(
        session_id=session_id,
        customer_id=customer_id,
        auth_method="test_otp",
        issued_at=NOW,
        last_activity_at=NOW,
    )


ToolFactory = Callable[..., DatabaseToolLayer]


@pytest.fixture
def make_tools(session_factory: sessionmaker[Session], db_session: Session) -> ToolFactory:
    sessions: dict[str, SessionContext] = {}

    def build(customer_id: str | None = CUSTOMER, **config: Any) -> DatabaseToolLayer:
        session = None
        if customer_id is not None:
            if customer_id not in sessions:
                sessions[customer_id] = make_session(
                    db_session, customer_id, f"SES-{len(sessions) + 1}"
                )
            session = sessions[customer_id]
        values: dict[str, Any] = {
            "as_of": AS_OF,
            "parameters": PARAMETERS,
            "retry_wait_seconds": 0,
        }
        values.update(config)
        return DatabaseToolLayer(
            session_factory,
            session,
            ToolConfig(**values),
            conversation_id="CONV-1",
            clock=lambda: NOW,
        )

    return build


@pytest.fixture
def tools(make_tools: ToolFactory) -> DatabaseToolLayer:
    return make_tools()


@pytest.fixture
def other_tools(make_tools: ToolFactory) -> DatabaseToolLayer:
    return make_tools(OTHER_CUSTOMER)


def add_transaction(db: Session, transaction_id: str, at: datetime, **values: Any) -> None:
    """A Core Banking row seeded by the test (the application itself never writes these)."""
    row: dict[str, Any] = {
        "transaction_id": transaction_id,
        "customer_id": CUSTOMER,
        "product_id": "PRD-CARDALPHA001",
        "transaction_date": at,
        "process_date": at.date(),
        "transaction_type": "Purchase",
        "amount": Decimal("500.00"),
        "currency": "USD",
        "amount_usd": Decimal("500.00"),
        "amount_usd_source": "identity",
        "merchant_name": "Tienda Prueba",
        "transaction_status": "Approved",
        "fraud_score": Decimal("10.00"),
        "is_fraud": False,
        "source_file": "test",
        "ingested_at": datetime(2026, 9, 28),
    }
    row.update(values)
    db.add(Transaction(**row))
    db.flush()


def add_product(db: Session, product_id: str, product_type: str, status: str) -> None:
    db.add(
        Product(
            product_id=product_id,
            customer_id=CUSTOMER,
            product_type=product_type,
            product_number_last4="9999",
            currency="USD",
            product_status=status,
            opening_date=date(2024, 1, 1),
            source_file="test",
            ingested_at=datetime(2026, 9, 28),
        )
    )
    db.flush()


def security_events(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(AuditLog).where(AuditLog.event_type == "security_event").order_by(AuditLog.id)
    )
    return [row.payload for row in rows]


def count(db: Session, model: type, **filters: Any) -> int:
    query = select(func.count()).select_from(model)
    for name, value in filters.items():
        query = query.where(getattr(model, name) == value)
    return db.scalar(query) or 0


def packet(handoff_id: str = "HO-20261001-000001", **overrides: Any) -> HandoffPacket:
    values: dict[str, Any] = {
        "handoff_id": handoff_id,
        "created_at": NOW,
        "business_date": date(2026, 6, 17),
        "language": Language.ES,
        "queue": Queue.DISPUTES,
        "priority": Priority.NORMAL,
        "customer_ref": "CUS-pseudonym-1",
        "auth": HandoffAuth(status=AuthStatus.AUTHENTICATED, method="test_otp", session_age_min=3),
        "request_summary": "Customer asks for a human agent.",
        "triggered_rules": ["ESC-05"],
        "escalation_reasons": [
            EscalationReason(
                rule_id="ESC-05",
                description="The customer asked for a human agent.",
                evidence=[
                    Evidence(
                        kind=EvidenceKind.FLAG,
                        name="human_requested",
                        value="true",
                        origin="customer statement (rule detector and LLM extraction)",
                    )
                ],
            )
        ],
        "transcript_ref": "CONV-1",
        "policy_version": "0.4.9",
    }
    values.update(overrides)
    return HandoffPacket(**values)
