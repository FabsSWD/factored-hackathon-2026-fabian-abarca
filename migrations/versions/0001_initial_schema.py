"""Initial schema: Core Banking, Cases, handoff packets, sessions and audit logs.

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _lineage() -> list[sa.Column[Any]]:
    return [
        sa.Column("source_file", sa.String(512), nullable=False),
        sa.Column("ingested_at", sa.DateTime(), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("document_type", sa.String(32), nullable=True),
        sa.Column("document_hash", sa.String(64), nullable=True),
        sa.Column("customer_status", sa.String(16), nullable=False),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("registration_date", sa.DateTime(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        sa.Column("gender", sa.String(16), nullable=True),
        sa.Column("age_band", sa.String(8), nullable=True),
        sa.Column("detected_accent", sa.String(32), nullable=True),
        sa.Column("segment", sa.String(32), nullable=True),
        sa.Column("credit_score", sa.Numeric(6, 1), nullable=True),
        sa.Column("estimated_monthly_income", sa.Numeric(18, 2), nullable=True),
        sa.Column("occupation", sa.String(64), nullable=True),
        sa.Column("marital_status", sa.String(32), nullable=True),
        sa.Column("education_level", sa.String(32), nullable=True),
        *_lineage(),
        sa.CheckConstraint(
            "customer_status IN ('Active', 'Closed', 'Inactive', 'Suspended')",
            name=op.f("ck_customers_status"),
        ),
        sa.PrimaryKeyConstraint("customer_id", name=op.f("pk_customers")),
        sa.UniqueConstraint("document_hash", name=op.f("uq_customers_document_hash")),
    )

    op.create_table(
        "products",
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("product_type", sa.String(32), nullable=False),
        sa.Column("product_number_last4", sa.String(4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("product_status", sa.String(16), nullable=False),
        sa.Column("opening_date", sa.Date(), nullable=True),
        sa.Column("expiration_date", sa.Date(), nullable=True),
        sa.Column("last_updated", sa.DateTime(), nullable=True),
        *_lineage(),
        sa.CheckConstraint(
            "product_type IN ('Cuenta Ahorro', 'Cuenta Corriente', 'Inversión', "
            "'Préstamo Hipotecario', 'Préstamo Personal', 'Seguro', 'Tarjeta Crédito', "
            "'Tarjeta Débito')",
            name=op.f("ck_products_type"),
        ),
        sa.CheckConstraint(
            "product_status IN ('Active', 'Blocked', 'Closed', 'Suspended')",
            name=op.f("ck_products_status"),
        ),
        sa.CheckConstraint("currency IN ('ARS', 'COP', 'USD')", name=op.f("ck_products_currency")),
        sa.CheckConstraint("product_number_last4 ~ '^[0-9]{4}$'", name=op.f("ck_products_last4")),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_products_customer_id_customers"),
        ),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_products")),
    )
    op.create_index(op.f("ix_products_customer_id"), "products", ["customer_id"])

    op.create_table(
        "transactions",
        sa.Column("transaction_id", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("transaction_date", sa.DateTime(), nullable=False),
        sa.Column("process_date", sa.Date(), nullable=False),
        sa.Column("transaction_type", sa.String(16), nullable=False),
        sa.Column("transaction_category", sa.String(32), nullable=True),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_usd", sa.Numeric(18, 2), nullable=True),
        sa.Column("amount_usd_source", sa.String(8), nullable=False),
        sa.Column("fx_rate_date", sa.Date(), nullable=True),
        sa.Column("channel", sa.String(16), nullable=True),
        sa.Column("merchant_name", sa.String(128), nullable=True),
        sa.Column("merchant_category", sa.String(64), nullable=True),
        sa.Column("transaction_status", sa.String(16), nullable=False),
        sa.Column("response_code", sa.String(4), nullable=True),
        sa.Column("fraud_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("is_fraud", sa.Boolean(), nullable=True),
        *_lineage(),
        sa.CheckConstraint(
            "transaction_type IN ('Adjustment', 'Deposit', 'Payment', 'Purchase', "
            "'Transfer', 'Withdrawal')",
            name=op.f("ck_transactions_type"),
        ),
        sa.CheckConstraint(
            "transaction_status IN ('Approved', 'Declined', 'Pending', 'Reversed')",
            name=op.f("ck_transactions_status"),
        ),
        sa.CheckConstraint(
            "currency IN ('ARS', 'COP', 'USD')", name=op.f("ck_transactions_currency")
        ),
        sa.CheckConstraint(
            "amount_usd_source IN ('fx_rate', 'identity', 'missing', 'source')",
            name=op.f("ck_transactions_amount_usd_source"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transactions_amount_positive")),
        sa.CheckConstraint(
            "amount_usd IS NULL OR amount_usd >= 0",
            name=op.f("ck_transactions_amount_usd_non_negative"),
        ),
        sa.CheckConstraint(
            "(amount_usd IS NULL) = (amount_usd_source = 'missing')",
            name=op.f("ck_transactions_amount_usd_missing"),
        ),
        sa.CheckConstraint(
            "(fx_rate_date IS NOT NULL) = (amount_usd_source = 'fx_rate')",
            name=op.f("ck_transactions_fx_rate_date"),
        ),
        sa.CheckConstraint(
            "fraud_score IS NULL OR fraud_score BETWEEN 0 AND 100",
            name=op.f("ck_transactions_fraud_score"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_transactions_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            name=op.f("fk_transactions_product_id_products"),
        ),
        sa.PrimaryKeyConstraint("transaction_id", name=op.f("pk_transactions")),
    )
    op.create_index(
        "ix_transactions_customer_date", "transactions", ["customer_id", "transaction_date"]
    )
    op.create_index(op.f("ix_transactions_product_id"), "transactions", ["product_id"])

    op.create_table(
        "cases",
        sa.Column("case_id", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("transaction_id", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("tier", sa.String(2), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount_usd", sa.Numeric(18, 2), nullable=False),
        sa.Column("provisional_credit_flag", sa.String(16), nullable=True),
        sa.Column("idempotency_key", sa.String(80), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason_code IN ('RC_DUPLICATE', 'RC_FEE', 'RC_INCORRECT_AMOUNT', "
            "'RC_NOT_RECEIVED', 'RC_UNRECOGNIZED')",
            name=op.f("ck_cases_reason_code"),
        ),
        sa.CheckConstraint(
            "status IN ('Closed', 'Draft', 'Escalated', 'In Process', 'Open', 'Rejected', "
            "'Resolved')",
            name=op.f("ck_cases_status"),
        ),
        sa.CheckConstraint("tier IN ('T1', 'T2', 'T3')", name=op.f("ck_cases_tier")),
        sa.CheckConstraint(
            "provisional_credit_flag IS NULL OR provisional_credit_flag IN "
            "('eligible', 'requires_review')",
            name=op.f("ck_cases_provisional_credit_flag"),
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_cases_amount_positive")),
        sa.CheckConstraint("amount_usd >= 0", name=op.f("ck_cases_amount_usd_non_negative")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.customer_id"], name=op.f("fk_cases_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transaction_id"],
            name=op.f("fk_cases_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("case_id", name=op.f("pk_cases")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_cases_idempotency_key")),
    )
    op.create_index(op.f("ix_cases_customer_id"), "cases", ["customer_id"])
    op.create_index(op.f("ix_cases_transaction_id"), "cases", ["transaction_id"])

    op.create_table(
        "handoff_packets",
        sa.Column("handoff_id", sa.String(18), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("transaction_id", sa.String(32), nullable=True),
        sa.Column("queue", sa.String(16), nullable=False),
        sa.Column("priority", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column("policy_version", sa.String(16), nullable=False),
        sa.Column("packet", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "queue IN ('disputes', 'fraud', 'security_review')",
            name=op.f("ck_handoff_packets_queue"),
        ),
        sa.CheckConstraint(
            "priority IN ('high', 'normal')", name=op.f("ck_handoff_packets_priority")
        ),
        sa.CheckConstraint(
            "status IN ('acknowledged', 'pending')", name=op.f("ck_handoff_packets_status")
        ),
        sa.CheckConstraint(
            "handoff_id ~ '^HO-[0-9]{8}-[0-9]{6}$'", name=op.f("ck_handoff_packets_handoff_id")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_handoff_packets_customer_id_customers"),
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.transaction_id"],
            name=op.f("fk_handoff_packets_transaction_id_transactions"),
        ),
        sa.PrimaryKeyConstraint("handoff_id", name=op.f("pk_handoff_packets")),
    )
    op.create_index(op.f("ix_handoff_packets_customer_id"), "handoff_packets", ["customer_id"])

    op.create_table(
        "sessions",
        sa.Column("session_id", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column("auth_method", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "last_activity_at >= created_at", name=op.f("ck_sessions_activity_order")
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_sessions_customer_id_customers"),
        ),
        sa.PrimaryKeyConstraint("session_id", name=op.f("pk_sessions")),
    )
    op.create_index(op.f("ix_sessions_customer_id"), "sessions", ["customer_id"])

    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("conversation_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('security_event', 'turn_trace')",
            name=op.f("ck_audit_logs_event_type"),
        ),
        sa.CheckConstraint(
            "(event_type = 'turn_trace') = (trace_id IS NOT NULL)",
            name=op.f("ck_audit_logs_trace_id_for_traces"),
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.session_id"],
            name=op.f("fk_audit_logs_session_id_sessions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
        sa.UniqueConstraint("trace_id", name=op.f("uq_audit_logs_trace_id")),
    )
    op.create_index(op.f("ix_audit_logs_event_type"), "audit_logs", ["event_type"])
    op.create_index(op.f("ix_audit_logs_conversation_id"), "audit_logs", ["conversation_id"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("sessions")
    op.drop_table("handoff_packets")
    op.drop_table("cases")
    op.drop_table("transactions")
    op.drop_table("products")
    op.drop_table("customers")
