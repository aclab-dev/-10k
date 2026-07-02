"""add_metrics_to_backtest_results

Revision ID: f8a2b1c3d4e5
Revises: a7f3c9d1e2b4
Create Date: 2026-06-30

Agrega sortino_ratio, expectancy_usdt y profit_factor a backtest_results (F12).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2ea8562d3fe7"
down_revision: str | None = "a7f3c9d1e2b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("backtest_results", sa.Column("sortino_ratio", sa.Float(), nullable=True))
    op.add_column("backtest_results", sa.Column("expectancy_usdt", sa.Numeric(20, 8), nullable=True))
    op.add_column("backtest_results", sa.Column("profit_factor", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("backtest_results", "profit_factor")
    op.drop_column("backtest_results", "expectancy_usdt")
    op.drop_column("backtest_results", "sortino_ratio")
