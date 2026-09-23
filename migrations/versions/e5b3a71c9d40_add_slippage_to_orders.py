"""add_slippage_to_orders

Revision ID: e5b3a71c9d40
Revises: d92a4c17e8f3
Create Date: 2026-09-21

Agrega orders.slippage_usdt (slippage real post-fill, devuelto por el adapter) y
orders.estimated_slippage_usdt (estimación pre-trade del Risk Engine) para poder
comparar estimado vs. real (F17 [162], regla no negociable 13).

Nullable ambas: las órdenes previas a esta migración no tienen el dato y no es
reconstruible a posteriori (el fill_price guardado ya incluye el slippage, pero
no se conservó el precio de referencia contra el que medirlo). Un backfill en 0
las haría indistinguibles de un fill sin slippage, que es exactamente el sesgo
que esta tarjeta viene a corregir.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5b3a71c9d40"
down_revision: str | None = "d92a4c17e8f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("slippage_usdt", sa.Numeric(20, 8), nullable=True))
    op.add_column(
        "orders", sa.Column("estimated_slippage_usdt", sa.Numeric(20, 8), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("orders", "estimated_slippage_usdt")
    op.drop_column("orders", "slippage_usdt")
