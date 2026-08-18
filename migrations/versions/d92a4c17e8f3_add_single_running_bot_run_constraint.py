"""add_single_running_bot_run_constraint

Revision ID: d92a4c17e8f3
Revises: b4d17a90c3e5
Create Date: 2026-08-18

Idempotencia de ciclo (F16 [114]): "a lo sumo un BotRun RUNNING" era hasta ahora
solo una convención de aplicación (BotRunRepository.get_active()/
close_orphan_running()), sin nada que lo garantice en DB ante dos arranques
concurrentes del worker (ej. ventana de un deploy con rolling restart). Un
índice único parcial lo convierte en invariante real: Postgres rechaza el
segundo INSERT con status='RUNNING' mientras el primero siga en ese estado.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "d92a4c17e8f3"
down_revision: str | None = "b4d17a90c3e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_bot_runs_single_running",
        "bot_runs",
        ["status"],
        unique=True,
        postgresql_where=text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_bot_runs_single_running", table_name="bot_runs")
