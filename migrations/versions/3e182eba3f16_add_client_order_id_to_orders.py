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
        sa.Column("client_order_id", sa.String(36), nullable=True),
    )
    op.execute(
        "UPDATE orders SET client_order_id = gen_random_uuid()::text WHERE client_order_id IS NULL"
    )
    op.alter_column("orders", "client_order_id", nullable=False)
    op.create_unique_constraint("uq_orders_client_order_id", "orders", ["client_order_id"])


def downgrade() -> None:
    op.drop_constraint("uq_orders_client_order_id", "orders", type_="unique")
    op.drop_column("orders", "client_order_id")
