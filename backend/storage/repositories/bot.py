"""Repositorios para el agregado Bot: BotRun, BotState, AccountState."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select

from backend.storage.models import AccountState, BotRun, BotState
from backend.storage.repositories.base import BaseRepository


class BotRunRepository(BaseRepository[BotRun]):
    model = BotRun

    def get_active(self) -> BotRun | None:
        """Devuelve el BotRun RUNNING mas reciente, si existe.

        El invariante "a lo sumo un RUNNING" lo sostiene la DB: el indice unico
        parcial uq_bot_runs_single_running (migracion d92a4c17e8f3, F16 [114])
        rechaza cualquier segundo insert con status='RUNNING' mientras el
        primero siga en ese estado, asi que en operacion normal nunca hay mas
        de una fila para desempatar aca.

        El order_by no es redundante igual: es defensa ante datos de antes de
        esa migracion (un ambiente viejo con RUNNING duplicados que la
        migracion no haya limpiado) y deja a get_active() con el mismo
        criterio de desempate que get_most_recent() — gana el mas nuevo — en
        vez de dejarlo en manos del planner de Postgres.
        """
        stmt = (
            select(BotRun)
            .where(BotRun.status == "RUNNING")
            .order_by(BotRun.started_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def get_most_recent(self) -> BotRun | None:
        """Devuelve el BotRun mas nuevo (cualquier status), o None si no hay ninguno.

        Usado al arrancar un worker nuevo para decidir si hay que arrastrar el
        ultimo estado conocido (ver Orchestrator._resolve_carried_over_state).
        """
        stmt = select(BotRun).order_by(BotRun.started_at.desc()).limit(1)
        return self._session.scalars(stmt).first()

    def close_orphan_running(self, *, reason: str) -> list[BotRun]:
        """Cierra como CRASHED los BotRun que quedaron colgados en RUNNING.

        Orchestrator.close() marca STOPPED el run al hacer shutdown, pero corre
        en el finally de run(): un SIGKILL (OOM, `docker kill`, corte de luz) se
        lo saltea y deja la fila en RUNNING para siempre. El arranque siguiente
        agrega otra, y ahi el invariante "a lo sumo un RUNNING" deja de valer.

        Se cierran al arrancar en vez de solo desempatar en la lectura para que
        el invariante se sostenga en la DB y no solo en get_active(); ver
        docs/decisions/F15-03-bot-run-active-resolution.md.

        CRASHED y no STOPPED: STOPPED afirma que hubo shutdown limpio, y estas
        corridas son justamente las que no lo tuvieron. Mezclarlas borraria la
        unica evidencia de que el proceso murio mal.

        `ended_at` queda en el momento en que se detecta el huerfano, no en el
        de la muerte real (que nadie registro). El motivo se anota en `notes`.

        Devuelve los BotRun cerrados (vacio si no habia ninguno), para que el
        caller pueda loguearlos. No commitea: sigue la convencion de
        BaseRepository y queda a cargo del caller.
        """
        stmt = select(BotRun).where(BotRun.status == "RUNNING").order_by(BotRun.started_at.asc())
        orphans = list(self._session.scalars(stmt))
        for orphan in orphans:
            orphan.notes = reason if orphan.notes is None else f"{orphan.notes}\n{reason}"
            self.close(orphan, status="CRASHED")
        return orphans

    def close(self, bot_run: BotRun, status: str = "STOPPED") -> BotRun:
        bot_run.status = status
        bot_run.ended_at = datetime.now(UTC)
        return self.save(bot_run)


class BotStateRepository(BaseRepository[BotState]):
    model = BotState

    def get_latest(self, bot_run_id: str) -> BotState | None:
        stmt = (
            select(BotState)
            .where(BotState.bot_run_id == bot_run_id)
            .order_by(BotState.created_at.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()


class AccountStateRepository(BaseRepository[AccountState]):
    model = AccountState

    def get_latest(self, bot_run_id: str) -> AccountState | None:
        stmt = (
            select(AccountState)
            .where(AccountState.bot_run_id == bot_run_id)
            .order_by(AccountState.timestamp.desc())
            .limit(1)
        )
        return self._session.scalars(stmt).first()

    def list_history(
        self,
        bot_run_id: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[AccountState], int]:
        """Serie temporal de AccountState (balance/equity/PnL/drawdown) para el dashboard.

        Devuelve (página ordenada ASC por timestamp, total de registros que matchean el filtro).
        """
        filters = [AccountState.bot_run_id == bot_run_id]
        if since is not None:
            filters.append(AccountState.timestamp >= since)
        if until is not None:
            filters.append(AccountState.timestamp <= until)

        total = self._session.scalar(select(func.count()).select_from(AccountState).where(*filters))
        stmt = (
            select(AccountState)
            .where(*filters)
            .order_by(AccountState.timestamp.asc())
            .limit(limit)
            .offset(offset)
        )
        items = list(self._session.scalars(stmt))
        return items, int(total) if total is not None else 0
