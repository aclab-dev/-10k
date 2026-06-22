"""add_strategy_setups_table

Revision ID: a7f3c9d1e2b4
Revises: 3e182eba3f16
Create Date: 2026-06-20

Crea la tabla strategy_setups para el registro de estrategias y setups con
versión, parámetros y metadatos (F12).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a7f3c9d1e2b4"
down_revision: str | None = "3e182eba3f16"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_setups",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("slug", sa.String(96), nullable=False),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("parameters", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("metadata", postgresql.JSON(astext_type=sa.Text()), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("registered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_strategy_setups_name"),
    )
    op.create_index("ix_strategy_setups_slug", "strategy_setups", ["slug"])
    op.create_index("ix_strategy_setups_is_active", "strategy_setups", ["is_active"])


def downgrade() -> None:
    op.drop_index("ix_strategy_setups_is_active", table_name="strategy_setups")
    op.drop_index("ix_strategy_setups_slug", table_name="strategy_setups")
    op.drop_table("strategy_setups")
