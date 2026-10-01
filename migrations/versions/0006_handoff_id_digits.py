"""Handoff IDs keep growing past six digits: ``^HO-\d{8}-\d{6,}$``, up to 32 characters.

The number is zero-padded to six digits and never wrapped, so IDs stay unique (primary key).

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHECK = op.f("ck_handoff_packets_handoff_id")


def upgrade() -> None:
    op.drop_constraint(CHECK, "handoff_packets", type_="check")
    op.alter_column(
        "handoff_packets", "handoff_id", existing_type=sa.String(18), type_=sa.String(32)
    )
    op.create_check_constraint(CHECK, "handoff_packets", "handoff_id ~ '^HO-[0-9]{8}-[0-9]{6,}$'")


def downgrade() -> None:
    op.drop_constraint(CHECK, "handoff_packets", type_="check")
    op.alter_column(
        "handoff_packets", "handoff_id", existing_type=sa.String(32), type_=sa.String(18)
    )
    op.create_check_constraint(CHECK, "handoff_packets", "handoff_id ~ '^HO-[0-9]{8}-[0-9]{6}$'")
