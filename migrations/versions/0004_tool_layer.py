"""Tool Layer: case numbers, the mock card-block store, and handoffs before authentication.

- ``case_number_seq`` numbers case references ``DSP-YYYYMMDD-NNNNNN``.
- ``card_blocks`` is the mock card system of ACT-03. Core Banking stays read-only for the
  application: a block is written here, and reads report the card's ``product_status`` as
  ``Blocked`` while a block exists.
- ``handoff_packets.customer_id`` becomes nullable: ACT-05 is always allowed, including an
  escalation before the customer authenticates (ESC-05, ESC-06, ESC-13).

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(sa.schema.CreateSequence(sa.Sequence("case_number_seq")))

    op.create_table(
        "card_blocks",
        sa.Column("product_id", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.String(32), nullable=False),
        sa.Column(
            "blocked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.product_id"],
            name=op.f("fk_card_blocks_product_id_products"),
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.customer_id"],
            name=op.f("fk_card_blocks_customer_id_customers"),
        ),
        sa.PrimaryKeyConstraint("product_id", name=op.f("pk_card_blocks")),
    )
    op.create_index(op.f("ix_card_blocks_customer_id"), "card_blocks", ["customer_id"])

    op.alter_column("handoff_packets", "customer_id", existing_type=sa.String(32), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM handoff_packets WHERE customer_id IS NULL")
    op.alter_column("handoff_packets", "customer_id", existing_type=sa.String(32), nullable=False)
    op.drop_index(op.f("ix_card_blocks_customer_id"), table_name="card_blocks")
    op.drop_table("card_blocks")
    op.execute(sa.schema.DropSequence(sa.Sequence("case_number_seq")))
