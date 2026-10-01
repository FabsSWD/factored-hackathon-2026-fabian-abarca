"""Handoff Builder: ``handoff_number_seq`` numbers handoff IDs ``HO-YYYYMMDD-NNNNNN``.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("handoff_number_seq")))


def downgrade() -> None:
    op.execute(sa.schema.DropSequence(sa.Sequence("handoff_number_seq")))
