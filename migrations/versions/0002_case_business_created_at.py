"""Cases record the business clock at creation (business_created_at) for ESC-02.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28

The column is NOT NULL without a default: no case exists before this revision. Evaluation
scenarios seed earlier cases with an explicit business date.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cases", sa.Column("business_created_at", sa.DateTime(), nullable=False))
    op.create_index(
        "ix_cases_customer_business_created", "cases", ["customer_id", "business_created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_cases_customer_business_created", table_name="cases")
    op.drop_column("cases", "business_created_at")
