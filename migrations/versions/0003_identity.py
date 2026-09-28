"""Identity service: revocable sessions, OTP challenges and OTP failures.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("sessions", "ended_at", new_column_name="revoked_at")
    op.create_check_constraint(
        op.f("ck_sessions_revoked_order"),
        "sessions",
        "revoked_at IS NULL OR revoked_at >= created_at",
    )

    op.create_table(
        "otp_challenges",
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("expires_at > issued_at", name=op.f("ck_otp_challenges_expiry_order")),
        sa.PrimaryKeyConstraint("document_hash", name=op.f("pk_otp_challenges")),
    )

    op.create_table(
        "otp_failures",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_otp_failures")),
    )
    op.create_index("ix_otp_failures_document_time", "otp_failures", ["document_hash", "failed_at"])


def downgrade() -> None:
    op.drop_index("ix_otp_failures_document_time", table_name="otp_failures")
    op.drop_table("otp_failures")
    op.drop_table("otp_challenges")
    op.drop_constraint(op.f("ck_sessions_revoked_order"), "sessions", type_="check")
    op.alter_column("sessions", "revoked_at", new_column_name="ended_at")
