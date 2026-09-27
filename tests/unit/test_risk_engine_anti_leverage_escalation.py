"""Tests unitarios para check_anti_leverage_escalation (F17, regla 28)."""

from decimal import Decimal

import pytest

from backend.risk_engine.checks import CheckOutcome, check_anti_leverage_escalation


class TestCheckAntiLeverageEscalation:
    """Verifica el bloqueo de subir el leverage tras un trade perdedor."""

    # --- Casos que NO deben bloquear ---

    def test_no_history_passes(self) -> None:
        """Sin trade previo no hay pérdida que recuperar → no bloquear."""
        result = check_anti_leverage_escalation(
            proposed_leverage=5,
            last_trade_pnl_usdt=None,
            last_trade_leverage=None,
        )
        assert result.outcome == CheckOutcome.PASS
        assert result.rule == "anti_leverage_escalation"

    def test_no_pnl_history_passes(self) -> None:
        """PnL desconocido aunque haya leverage previo → no bloquear."""
        result = check_anti_leverage_escalation(
            proposed_leverage=5,
            last_trade_pnl_usdt=None,
            last_trade_leverage=3,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_no_leverage_history_passes(self) -> None:
        """Leverage previo desconocido aunque haya PnL → no bloquear."""
        result = check_anti_leverage_escalation(
            proposed_leverage=5,
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_leverage=None,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_same_leverage_after_loss_passes(self) -> None:
        """Mantener el leverage tras una pérdida no es escalar."""
        result = check_anti_leverage_escalation(
            proposed_leverage=5,
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_leverage=5,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_lower_leverage_after_loss_passes(self) -> None:
        """Bajar el leverage tras una pérdida es lo prudente → no bloquear."""
        result = check_anti_leverage_escalation(
            proposed_leverage=3,
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_leverage=5,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_higher_leverage_after_win_passes(self) -> None:
        """Subir el leverage tras una ganancia no es recuperar pérdidas."""
        result = check_anti_leverage_escalation(
            proposed_leverage=8,
            last_trade_pnl_usdt=Decimal("3.50"),
            last_trade_leverage=5,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_higher_leverage_after_breakeven_passes(self) -> None:
        """PnL=0 no es pérdida → no bloquear."""
        result = check_anti_leverage_escalation(
            proposed_leverage=8,
            last_trade_pnl_usdt=Decimal("0"),
            last_trade_leverage=5,
        )
        assert result.outcome == CheckOutcome.PASS

    # --- Casos que SÍ deben bloquear ---

    def test_blocks_higher_leverage_after_loss(self) -> None:
        """Subir el leverage tras una pérdida → escalada detectada."""
        result = check_anti_leverage_escalation(
            proposed_leverage=6,
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_leverage=5,
        )
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_leverage_escalation"
        assert "-2.0" in result.reason
        assert "5x" in result.reason
        assert "6x" in result.reason

    def test_blocks_on_small_loss(self) -> None:
        """Cualquier pérdida, por pequeña que sea, activa el bloqueo."""
        result = check_anti_leverage_escalation(
            proposed_leverage=4,
            last_trade_pnl_usdt=Decimal("-0.01"),
            last_trade_leverage=3,
        )
        assert result.outcome == CheckOutcome.BLOCK

    # --- Validaciones de tipos / errores ---

    def test_raises_on_non_int_proposed_leverage(self) -> None:
        with pytest.raises(ValueError, match="proposed_leverage debe ser int"):
            check_anti_leverage_escalation(
                proposed_leverage=5.0,  # type: ignore[arg-type]
                last_trade_pnl_usdt=None,
                last_trade_leverage=None,
            )

    def test_raises_on_bool_proposed_leverage(self) -> None:
        with pytest.raises(ValueError, match="proposed_leverage debe ser int"):
            check_anti_leverage_escalation(
                proposed_leverage=True,
                last_trade_pnl_usdt=None,
                last_trade_leverage=None,
            )

    def test_raises_on_non_positive_proposed_leverage(self) -> None:
        with pytest.raises(ValueError, match="proposed_leverage debe ser positivo"):
            check_anti_leverage_escalation(
                proposed_leverage=0,
                last_trade_pnl_usdt=None,
                last_trade_leverage=None,
            )

    def test_raises_on_non_decimal_pnl(self) -> None:
        with pytest.raises(ValueError, match="last_trade_pnl_usdt debe ser Decimal o None"):
            check_anti_leverage_escalation(
                proposed_leverage=5,
                last_trade_pnl_usdt=-2.0,  # type: ignore[arg-type]
                last_trade_leverage=5,
            )

    def test_raises_on_non_int_last_leverage(self) -> None:
        with pytest.raises(ValueError, match="last_trade_leverage debe ser int o None"):
            check_anti_leverage_escalation(
                proposed_leverage=5,
                last_trade_pnl_usdt=Decimal("-2.0"),
                last_trade_leverage=5.0,  # type: ignore[arg-type]
            )

    def test_raises_on_non_positive_last_leverage(self) -> None:
        with pytest.raises(ValueError, match="last_trade_leverage debe ser positivo"):
            check_anti_leverage_escalation(
                proposed_leverage=5,
                last_trade_pnl_usdt=Decimal("-2.0"),
                last_trade_leverage=0,
            )
