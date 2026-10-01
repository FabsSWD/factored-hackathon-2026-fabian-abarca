"""Builders for Policy Engine tests.

``request()`` builds a request that passes every gate and reaches the COM-03 summary: an
authenticated customer disputes an approved purchase (RC_INCORRECT_AMOUNT, T1) referenced by
ID, with fallback signals so ESC-11 stays quiet. Each test changes only what it is about.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config import PolicyConfig, load_policy_config
from app.contracts import (
    CaseRecord,
    CaseStatus,
    ConversationCounters,
    CustomerRecord,
    ModelSignals,
    ModelSource,
    PolicyDecision,
    PolicyRequest,
    ProductRecord,
    ReasonCode,
    SessionContext,
    Slots,
    Tier,
    TransactionRecord,
    TransactionRef,
)
from app.policy import DeterministicPolicyEngine

CONFIG = load_policy_config()
BUSINESS_DATE = date(2026, 6, 17)
AS_OF = datetime(2026, 6, 18, 6, 0)  # BUSINESS_DATE + 1 day at the 06:00 cutoff
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CUSTOMER_ID = "CUS-1"
TXN_DATE = datetime(2026, 6, 10, 14, 30)

FALLBACK = ModelSignals(source=ModelSource.LLM_FALLBACK, reason_code_probs={}, ambiguity=0.0)


def engine(config: PolicyConfig = CONFIG) -> DeterministicPolicyEngine:
    return DeterministicPolicyEngine(config)


def with_parameters(**values: Any) -> PolicyConfig:
    parameters = CONFIG.parameters.model_copy(update=values)
    return CONFIG.model_copy(update={"parameters": parameters})


def session(age_min: float = 5, idle_min: float = 1) -> SessionContext:
    return SessionContext(
        session_id="SES-1",
        customer_id=CUSTOMER_ID,
        auth_method="test_otp",
        issued_at=NOW - timedelta(minutes=age_min),
        last_activity_at=NOW - timedelta(minutes=idle_min),
    )


def customer(status: str = "Active") -> CustomerRecord:
    return CustomerRecord(customer_id=CUSTOMER_ID, customer_status=status)


def product(
    product_id: str = "PRD-1",
    product_type: str = "Tarjeta Crédito",
    status: str = "Active",
    last4: str = "4821",
) -> ProductRecord:
    return ProductRecord(
        product_id=product_id,
        customer_id=CUSTOMER_ID,
        product_type=product_type,
        product_number_masked=f"****{last4}",
        currency="USD",
        product_status=status,
    )


def txn(
    transaction_id: str = "TXN-1",
    *,
    transaction_type: str = "Purchase",
    status: str = "Approved",
    when: datetime = TXN_DATE,
    amount: str = "50",
    amount_usd: str | None = "50",
    currency: str = "USD",
    merchant: str | None = "Cafe Sintetico",
    fraud_score: float | None = 10.0,
    product_id: str = "PRD-1",
) -> TransactionRecord:
    return TransactionRecord(
        transaction_id=transaction_id,
        customer_id=CUSTOMER_ID,
        product_id=product_id,
        transaction_type=transaction_type,
        transaction_status=status,
        transaction_date=when,
        amount=Decimal(amount),
        currency=currency,
        amount_usd=Decimal(amount_usd) if amount_usd is not None else None,
        merchant_name=merchant,
        fraud_score=fraud_score,
    )


def case(
    case_id: str = "CASE-1",
    *,
    transaction_id: str = "TXN-OLD",
    status: CaseStatus = CaseStatus.OPEN,
    amount_usd: str = "100",
    business_created_at: datetime = AS_OF - timedelta(days=5),
) -> CaseRecord:
    return CaseRecord(
        case_id=case_id,
        customer_id=CUSTOMER_ID,
        transaction_id=transaction_id,
        reason_code=ReasonCode.INCORRECT_AMOUNT,
        status=status,
        tier=Tier.T1,
        amount=Decimal(amount_usd),
        currency="USD",
        amount_usd=Decimal(amount_usd),
        created_at=NOW - timedelta(days=1),
        business_created_at=business_created_at,
    )


DEFAULT_SLOTS: dict[str, Any] = {
    "transaction_ref": TransactionRef(transaction_id="TXN-1"),
    "reason_code": ReasonCode.INCORRECT_AMOUNT,
    "expected_amount": Decimal("40"),
}


def slots(**values: Any) -> Slots:
    return Slots(**{**DEFAULT_SLOTS, **values})


def counters(**values: Any) -> ConversationCounters:
    return ConversationCounters(**values)


def request(**overrides: Any) -> PolicyRequest:
    values: dict[str, Any] = {
        "now": NOW,
        "as_of": AS_OF,
        "conversation_id": "CONV-1",
        "detected_language": "es",
        "session": session(),
        "slots": slots(),
        "signals": FALLBACK,
        "customer": customer(),
        "transaction_candidates": [txn()],
        "products": [product()],
    }
    values.update(overrides)
    return PolicyRequest(**values)


def unauthenticated(**overrides: Any) -> PolicyRequest:
    """A request before GATE-02: no session and no account records."""
    base: dict[str, Any] = {
        "session": None,
        "customer": None,
        "transaction_candidates": [],
        "products": [],
        "slots": Slots(),
    }
    return request(**{**base, **overrides})


def evaluate(req: PolicyRequest, config: PolicyConfig = CONFIG) -> PolicyDecision:
    return engine(config).evaluate(req)


def gate(decision: PolicyDecision, gate_id: str) -> bool | None:
    """The result of a gate, or None if it was not evaluated."""
    return next((g.passed for g in decision.gates_evaluated if g.gate_id == gate_id), None)


def days_before_business_date(days: int, hour: int = 12) -> datetime:
    """A timestamp whose business day is ``days`` calendar days before BUSINESS_DATE."""
    return datetime.combine(BUSINESS_DATE - timedelta(days=days), datetime.min.time()).replace(
        hour=hour
    )
