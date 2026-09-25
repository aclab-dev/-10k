"""Tests unitarios — límite de posiciones concurrentes (F17, regla 29 del checklist LIVE)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.core.config import get_config
from backend.risk_engine.checks import CheckOutcome, check_max_open_positions
from backend.risk_engine.engine import validate
from backend.risk_engine.schemas import RiskDecision
from tests.unit.test_risk_engine_validation import _aggregation, _long_decision

_LIMIT = 1


class TestCheckMaxOpenPositions:
    def test_bajo_el_limite_pasa(self) -> None:
        result = check_max_open_positions(_long_decision(), 0, _LIMIT)
        assert result.outcome == CheckOutcome.PASS

    def test_en_el_limite_bloquea(self) -> None:
        result = check_max_open_positions(_long_decision(), _LIMIT, _LIMIT)
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "max_open_positions"

    def test_por_encima_del_limite_bloquea(self) -> None:
        # Estado inconsistente: más posiciones abiertas de las permitidas.
        result = check_max_open_positions(_long_decision(), _LIMIT + 2, _LIMIT)
        assert result.outcome == CheckOutcome.BLOCK

    def test_conteo_no_confiable_bloquea_fail_closed(self) -> None:
        result = check_max_open_positions(_long_decision(), None, _LIMIT)
        assert result.outcome == CheckOutcome.BLOCK
        assert "no confiable" in result.reason

    def test_no_operar_no_evalua_el_limite(self) -> None:
        decision = _long_decision(decision="NO_OPERAR", execute=False)
        result = check_max_open_positions(decision, _LIMIT, _LIMIT)
        assert result.outcome == CheckOutcome.PASS


class TestValidateAplicaElLimite:
    @staticmethod
    def _validate(open_positions_count: int | None) -> RiskDecision:
        decision = _long_decision()
        return validate(
            _aggregation(decision),
            decision,
            Decimal("0"),
            Decimal("0"),
            get_config(),
            funding_rate=0.0001,
            open_positions_count=open_positions_count,
        ).decision

    def test_lee_el_limite_de_la_config(self) -> None:
        limit = get_config().trading.max_open_positions
        assert self._validate(limit - 1) == RiskDecision.APPROVE
        assert self._validate(limit) == RiskDecision.BLOCK
        assert self._validate(limit + 1) == RiskDecision.BLOCK

    def test_conteo_no_confiable_bloquea(self) -> None:
        assert self._validate(None) == RiskDecision.BLOCK

    @pytest.mark.parametrize("count", [0])
    def test_el_check_queda_auditado_en_reasons(self, count: int) -> None:
        decision = _long_decision()
        result = validate(
            _aggregation(decision),
            decision,
            Decimal("0"),
            Decimal("0"),
            get_config(),
            funding_rate=0.0001,
            open_positions_count=count,
        )
        assert "max_open_positions" in result.reasons
