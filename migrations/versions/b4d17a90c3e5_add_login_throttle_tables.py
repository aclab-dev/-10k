"""add_login_throttle_tables

Revision ID: b4d17a90c3e5
Revises: 2ea8562d3fe7
Create Date: 2026-08-12

Crea login_attempts y login_lockouts para el rate limiting y el lockout de
brute-force del login del dashboard (F15).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b4d17a90c3e5"
down_revision: str | None = "2ea8562d3fe7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("identifier", sa.String(255), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Índice compuesto en el orden en que se filtra: scope + identifier exactos y
    # rango sobre timestamp. Sirve tanto al conteo de la ventana como a la purga.
    op.create_index(
        "ix_login_attempts_scope_identifier_timestamp",
        "login_attempts",
        ["scope", "identifier", "timestamp"],
    )
    # La purga de intentos vencidos filtra solo por timestamp: el compuesto no
    # le sirve porque timestamp es su última columna.
    op.create_index("ix_login_attempts_timestamp", "login_attempts", ["timestamp"])

    op.create_table(
        "login_lockouts",
        sa.Column("id", postgresql.UUID(as_uuid=False), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("identifier", sa.String(255), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lockout_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "identifier", name="uq_login_lockouts_scope_identifier"),
    )


def downgrade() -> None:
    op.drop_table("login_lockouts")
    op.drop_index("ix_login_attempts_timestamp", table_name="login_attempts")
    op.drop_index("ix_login_attempts_scope_identifier_timestamp", table_name="login_attempts")
    op.drop_table("login_attempts")
