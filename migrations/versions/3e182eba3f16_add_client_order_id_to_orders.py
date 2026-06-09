"""add_client_order_id_to_orders

Revision ID: 3e182eba3f16
Revises: c1edf83a521c
Create Date: 2026-06-09

Agrega orders.client_order_id como clave de idempotencia (F10-05).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3e182eba3f16"
down_revision: str | None = "c1edf83a521c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "orders",
        sa.Column("client_order_id", sa.String(36), nullable=False, server_default=""),
    )
    op.create_unique_constraint("uq_orders_client_order_id", "orders", ["client_order_id"])
    op.create_index("ix_orders_client_order_id", "orders", ["client_order_id"], unique=True)
    # Remove server_default after backfill — column is NOT NULL going forward
    op.alter_column("orders", "client_order_id", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_orders_client_order_id", table_name="orders")
    op.drop_constraint("uq_orders_client_order_id", "orders", type_="unique")
    op.drop_column("orders", "client_order_id")
