"""Audit access events: every read of the audit API is recorded (``event_type = 'audit_access'``).

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = op.f("ck_audit_logs_event_type")


def upgrade() -> None:
    op.drop_constraint(CHECK, "audit_logs", type_="check")
    op.create_check_constraint(
        CHECK, "audit_logs", "event_type IN ('audit_access', 'security_event', 'turn_trace')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM audit_logs WHERE event_type = 'audit_access'")
    op.drop_constraint(CHECK, "audit_logs", type_="check")
    op.create_check_constraint(
        CHECK, "audit_logs", "event_type IN ('security_event', 'turn_trace')"
    )
