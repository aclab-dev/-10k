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
    # Backfill antes del índice: un ambiente viejo puede tener mas de un
    # RUNNING acumulado (huerfanos de crashes que nunca vieron un restart
    # posterior que los cerrara via close_orphan_running()). Sin esto,
    # create_index falla con UniqueViolation contra datos preexistentes.
    # Mismo criterio que close_orphan_running(): se conserva el mas nuevo por
    # started_at, el resto pasa a CRASHED (no STOPPED, no hubo shutdown limpio).
    op.execute(
        """
        UPDATE bot_runs
        SET status = 'CRASHED',
            ended_at = now(),
            notes = CASE
                WHEN notes IS NULL THEN 'Cerrado por la migracion d92a4c17e8f3: RUNNING duplicado preexistente al agregar uq_bot_runs_single_running.'
                ELSE notes || E'\\nCerrado por la migracion d92a4c17e8f3: RUNNING duplicado preexistente al agregar uq_bot_runs_single_running.'
            END
        WHERE status = 'RUNNING'
          AND id NOT IN (
              SELECT id FROM bot_runs WHERE status = 'RUNNING' ORDER BY started_at DESC LIMIT 1
          )
        """
    )
    op.create_index(
        "uq_bot_runs_single_running",
        "bot_runs",
        ["status"],
        unique=True,
        postgresql_where=text("status = 'RUNNING'"),
    )


def downgrade() -> None:
    op.drop_index("uq_bot_runs_single_running", table_name="bot_runs")
