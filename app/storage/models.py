"""SQLAlchemy models for the three stores of the architecture (§3): Core Banking, Cases, Audit.

Core Banking tables (customers, products, transactions) are loaded offline and only read by
the application. They keep only the columns the system uses plus the DATA-02 attributes needed
for fairness reporting: names, contact data, addresses, full product numbers and dates of birth
are never stored (minimization, dispute policy §12).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    Numeric,
    Sequence,
    String,
    Table,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.contracts import (
    CaseStatus,
    Priority,
    ProvisionalCreditFlag,
    Queue,
    ReasonCode,
    Tier,
)
from app.storage.data_contract import (
    CURRENCIES,
    CUSTOMER_STATUSES,
    PRODUCT_STATUSES,
    PRODUCT_TYPES,
    TRANSACTION_STATUSES,
    TRANSACTION_TYPES,
    AmountUsdSource,
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(18, 2)


def _in(column: str, values: Any) -> str:
    listed = ", ".join(f"'{value}'" for value in sorted(str(v) for v in values))
    return f"{column} IN ({listed})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class LineageMixin:
    """DATA-04 and data lineage: the raw file each row came from and when it was loaded."""

    source_file: Mapped[str] = mapped_column(String(512))
    ingested_at: Mapped[datetime] = mapped_column(DateTime)


# ---------------------------------------------------------------------------
# Core Banking (read-only for the application)
# ---------------------------------------------------------------------------


class Customer(LineageMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (CheckConstraint(_in("customer_status", CUSTOMER_STATUSES), name="status"),)

    customer_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    document_type: Mapped[str | None] = mapped_column(String(32))
    # HMAC-SHA256 of document_number with DOCUMENT_HASH_KEY; the number itself is not stored.
    document_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    customer_status: Mapped[str] = mapped_column(String(16))
    country: Mapped[str | None] = mapped_column(String(64))
    registration_date: Mapped[datetime | None] = mapped_column(DateTime)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime)
    # DATA-02 attributes: fairness reporting only, never decision inputs.
    gender: Mapped[str | None] = mapped_column(String(16))
    age_band: Mapped[str | None] = mapped_column(String(8))
    detected_accent: Mapped[str | None] = mapped_column(String(32))
    segment: Mapped[str | None] = mapped_column(String(32))
    credit_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    estimated_monthly_income: Mapped[Decimal | None] = mapped_column(Money)
    occupation: Mapped[str | None] = mapped_column(String(64))
    marital_status: Mapped[str | None] = mapped_column(String(32))
    education_level: Mapped[str | None] = mapped_column(String(32))


class Product(LineageMixin, Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint(_in("product_type", PRODUCT_TYPES), name="type"),
        CheckConstraint(_in("product_status", PRODUCT_STATUSES), name="status"),
        CheckConstraint(_in("currency", CURRENCIES), name="currency"),
        CheckConstraint("product_number_last4 ~ '^[0-9]{4}$'", name="last4"),
    )

    product_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    product_type: Mapped[str] = mapped_column(String(32))
    # COM-06: only the last four digits are kept.
    product_number_last4: Mapped[str] = mapped_column(String(4))
    currency: Mapped[str] = mapped_column(String(3))
    product_status: Mapped[str] = mapped_column(String(16))
    opening_date: Mapped[date | None] = mapped_column(Date)
    expiration_date: Mapped[date | None] = mapped_column(Date)
    last_updated: Mapped[datetime | None] = mapped_column(DateTime)


class Transaction(LineageMixin, Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint(_in("transaction_type", TRANSACTION_TYPES), name="type"),
        CheckConstraint(_in("transaction_status", TRANSACTION_STATUSES), name="status"),
        CheckConstraint(_in("currency", CURRENCIES), name="currency"),
        CheckConstraint(_in("amount_usd_source", list(AmountUsdSource)), name="amount_usd_source"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("amount_usd IS NULL OR amount_usd >= 0", name="amount_usd_non_negative"),
        CheckConstraint(
            "(amount_usd IS NULL) = (amount_usd_source = 'missing')", name="amount_usd_missing"
        ),
        CheckConstraint(
            "(fx_rate_date IS NOT NULL) = (amount_usd_source = 'fx_rate')", name="fx_rate_date"
        ),
        CheckConstraint("fraud_score IS NULL OR fraud_score BETWEEN 0 AND 100", name="fraud_score"),
        Index("ix_transactions_customer_date", "customer_id", "transaction_date"),
    )

    transaction_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"))
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), index=True)
    # Naive local time of the dataset (policy §15, "Business date").
    transaction_date: Mapped[datetime] = mapped_column(DateTime)
    process_date: Mapped[date] = mapped_column(Date)
    transaction_type: Mapped[str] = mapped_column(String(16))
    transaction_category: Mapped[str | None] = mapped_column(String(32))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    amount_usd: Mapped[Decimal | None] = mapped_column(Money)
    amount_usd_source: Mapped[str] = mapped_column(String(8))
    fx_rate_date: Mapped[date | None] = mapped_column(Date)
    channel: Mapped[str | None] = mapped_column(String(16))
    merchant_name: Mapped[str | None] = mapped_column(String(128))
    merchant_category: Mapped[str | None] = mapped_column(String(64))
    transaction_status: Mapped[str] = mapped_column(String(16))
    response_code: Mapped[str | None] = mapped_column(String(4))
    fraud_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # Bank label assigned afterwards; MUST NOT be used by any rule (policy §7). Evaluation only.
    is_fraud: Mapped[bool | None] = mapped_column(Boolean)


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        CheckConstraint(_in("reason_code", list(ReasonCode)), name="reason_code"),
        CheckConstraint(_in("status", list(CaseStatus)), name="status"),
        CheckConstraint(_in("tier", list(Tier)), name="tier"),
        CheckConstraint(
            "provisional_credit_flag IS NULL OR "
            + _in("provisional_credit_flag", list(ProvisionalCreditFlag)),
            name="provisional_credit_flag",
        ),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("amount_usd >= 0", name="amount_usd_non_negative"),
        Index("ix_cases_customer_business_created", "customer_id", "business_created_at"),
    )

    case_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    transaction_id: Mapped[str] = mapped_column(
        ForeignKey("transactions.transaction_id"), index=True
    )
    reason_code: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    tier: Mapped[str] = mapped_column(String(2))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    amount_usd: Mapped[Decimal] = mapped_column(Money)
    provisional_credit_flag: Mapped[str | None] = mapped_column(String(16))
    # ACT-02: transaction_id + reason_code, so a retry can never create a second case.
    idempotency_key: Mapped[str] = mapped_column(String(80), unique=True)
    # Real time, for audit. Rules use business_created_at (policy §15, "Business date").
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # The business clock (as_of) when the case was created; ESC-02 counts cases with it.
    business_created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


CASE_NUMBER_SEQ = Sequence("case_number_seq", metadata=Base.metadata)
"""Numbers case references DSP-YYYYMMDD-NNNNNN (ACT-02)."""
HANDOFF_NUMBER_SEQ = Sequence("handoff_number_seq", metadata=Base.metadata)
"""Numbers handoff IDs HO-YYYYMMDD-NNNNNN (policy §13)."""


class CardBlock(Base):
    """The mock card system of ACT-03. Core Banking stays read-only for the application, so a
    block is recorded here and reads report the card as ``Blocked`` while it exists. Unblocking
    is prohibited (ACT-06): nothing in the application deletes these rows."""

    __tablename__ = "card_blocks"

    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    blocked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class HandoffPacketRow(Base):
    __tablename__ = "handoff_packets"
    __table_args__ = (
        CheckConstraint(_in("queue", list(Queue)), name="queue"),
        CheckConstraint(_in("priority", list(Priority)), name="priority"),
        CheckConstraint(_in("status", ("acknowledged", "pending")), name="status"),
        CheckConstraint("handoff_id ~ '^HO-[0-9]{8}-[0-9]{6,}$'", name="handoff_id"),
    )

    handoff_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # None for an escalation before authentication (ACT-05 is always allowed).
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    transaction_id: Mapped[str | None] = mapped_column(ForeignKey("transactions.transaction_id"))
    queue: Mapped[str] = mapped_column(String(16))
    priority: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), server_default="pending")
    policy_version: Mapped[str] = mapped_column(String(16))
    # The full §13 packet, validated by app.contracts.HandoffPacket before it is written.
    packet: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


class SessionRow(Base):
    """Identity Service sessions (GATE-02). Times are real time, never the business clock."""

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("last_activity_at >= created_at", name="activity_order"),
        CheckConstraint("revoked_at IS NULL OR revoked_at >= created_at", name="revoked_order"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.customer_id"), index=True)
    auth_method: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OtpChallenge(Base):
    """One pending OTP per document, keyed by the document's HMAC (never the number itself).

    A challenge is stored for unknown documents too, so /auth/login behaves identically
    whether or not the document exists.
    """

    __tablename__ = "otp_challenges"
    __table_args__ = (CheckConstraint("expires_at > issued_at", name="expiry_order"),)

    document_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class OtpFailure(Base):
    """Failed OTP verifications, counted per document within OTP_FAILURE_WINDOW_MIN."""

    __tablename__ = "otp_failures"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_hash: Mapped[str] = mapped_column(String(64))
    failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_otp_failures_document_time", "document_hash", "failed_at"),)


class AuditLog(Base):
    """Turn traces (architecture §9), security events (GATE-04, ESC-13, failed OTPs), and
    reads of the audit API (audit_access)."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint(
            _in("event_type", ("audit_access", "security_event", "turn_trace")), name="event_type"
        ),
        CheckConstraint(
            "(event_type = 'turn_trace') = (trace_id IS NOT NULL)", name="trace_id_for_traces"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(16), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


CORE_BANKING_TABLES: tuple[Table, ...] = tuple(
    Base.metadata.tables[name] for name in ("customers", "products", "transactions")
)
"""Load order respects foreign keys."""
