"""Tests unitarios para check_anti_martingala y check_anti_averaging (F9 [74])."""

from decimal import Decimal

import pytest

from backend.risk_engine.checks import (
    CheckOutcome,
    check_anti_averaging,
    check_anti_martingala,
)

# ---------------------------------------------------------------------------
# check_anti_martingala
# ---------------------------------------------------------------------------


class TestCheckAntiMartingala:
    """Verifica la detección del patrón martingala (doblar tras pérdida)."""

    # --- Casos que NO deben bloquear ---

    def test_no_history_passes(self) -> None:
        """Sin historial de trades no se puede detectar el patrón → no bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("5.0"),
            last_trade_pnl_usdt=None,
            last_trade_margin_usdt=None,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_no_pnl_history_passes(self) -> None:
        """PnL desconocido (None) aunque haya margen previo → no bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("5.0"),
            last_trade_pnl_usdt=None,
            last_trade_margin_usdt=Decimal("3.0"),
        )
        assert result.outcome == CheckOutcome.PASS

    def test_no_margin_history_passes(self) -> None:
        """Margen previo desconocido (None) aunque haya PnL → no bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("5.0"),
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_margin_usdt=None,
        )
        assert result.outcome == CheckOutcome.PASS

    def test_last_trade_profitable_passes(self) -> None:
        """Último trade fue ganador → cualquier margen es aceptable."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("9.0"),
            last_trade_pnl_usdt=Decimal("3.50"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.PASS

    def test_last_trade_breakeven_passes(self) -> None:
        """Último trade en breakeven (PnL=0) → no hay pérdida, no bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("9.0"),
            last_trade_pnl_usdt=Decimal("0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.PASS

    def test_same_margin_after_loss_passes(self) -> None:
        """Mismo margen tras pérdida → no es martingala (no aumentó)."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("5.0"),
            last_trade_pnl_usdt=Decimal("-1.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.PASS

    def test_smaller_margin_after_loss_passes(self) -> None:
        """Margen menor tras pérdida → comportamiento correcto, no bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("3.0"),
            last_trade_pnl_usdt=Decimal("-2.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.PASS

    # --- Casos que SÍ deben bloquear ---

    def test_blocks_martingala_increase(self) -> None:
        """Margen mayor tras pérdida → martingala detectada, debe bloquear."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("8.0"),
            last_trade_pnl_usdt=Decimal("-3.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_martingala"
        assert "8.0" in result.reason
        assert "5.0" in result.reason
        assert "-3.0" in result.reason

    def test_blocks_any_increase_after_any_loss(self) -> None:
        """Cualquier aumento de margen tras cualquier pérdida es bloqueado."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("5.01"),
            last_trade_pnl_usdt=Decimal("-0.01"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_martingala"

    def test_blocks_doubling_down(self) -> None:
        """Caso clásico de doblar la apuesta tras pérdida."""
        result = check_anti_martingala(
            proposed_margin_usdt=Decimal("10.0"),
            last_trade_pnl_usdt=Decimal("-5.0"),
            last_trade_margin_usdt=Decimal("5.0"),
        )
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_martingala"

    # --- Validaciones de tipos / errores ---

    def test_raises_on_non_decimal_proposed_margin(self) -> None:
        with pytest.raises(ValueError, match="proposed_margin_usdt debe ser Decimal"):
            check_anti_martingala(
                proposed_margin_usdt=5.0,  # type: ignore[arg-type]
                last_trade_pnl_usdt=Decimal("-1.0"),
                last_trade_margin_usdt=Decimal("3.0"),
            )

    def test_raises_on_zero_proposed_margin(self) -> None:
        with pytest.raises(ValueError, match="proposed_margin_usdt debe ser positivo"):
            check_anti_martingala(
                proposed_margin_usdt=Decimal("0"),
                last_trade_pnl_usdt=None,
                last_trade_margin_usdt=None,
            )

    def test_raises_on_negative_proposed_margin(self) -> None:
        with pytest.raises(ValueError, match="proposed_margin_usdt debe ser positivo"):
            check_anti_martingala(
                proposed_margin_usdt=Decimal("-1.0"),
                last_trade_pnl_usdt=None,
                last_trade_margin_usdt=None,
            )

    def test_raises_on_non_decimal_pnl(self) -> None:
        with pytest.raises(ValueError, match="last_trade_pnl_usdt debe ser Decimal o None"):
            check_anti_martingala(
                proposed_margin_usdt=Decimal("5.0"),
                last_trade_pnl_usdt=-3.0,  # type: ignore[arg-type]
                last_trade_margin_usdt=Decimal("3.0"),
            )

    def test_raises_on_non_decimal_last_margin(self) -> None:
        with pytest.raises(ValueError, match="last_trade_margin_usdt debe ser Decimal o None"):
            check_anti_martingala(
                proposed_margin_usdt=Decimal("5.0"),
                last_trade_pnl_usdt=Decimal("-1.0"),
                last_trade_margin_usdt=3.0,  # type: ignore[arg-type]
            )

    def test_raises_on_zero_last_margin(self) -> None:
        with pytest.raises(ValueError, match="last_trade_margin_usdt debe ser positivo"):
            check_anti_martingala(
                proposed_margin_usdt=Decimal("5.0"),
                last_trade_pnl_usdt=Decimal("-1.0"),
                last_trade_margin_usdt=Decimal("0"),
            )


# ---------------------------------------------------------------------------
# check_anti_averaging
# ---------------------------------------------------------------------------


class TestCheckAntiAveraging:
    """Verifica el bloqueo del promediado de pérdidas (averaging down)."""

    # --- Casos que NO deben bloquear ---

    def test_no_open_position_passes(self) -> None:
        """Sin posición abierta no hay nada que promediar."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=None)
        assert result.outcome == CheckOutcome.PASS

    def test_position_in_profit_passes(self) -> None:
        """Posición abierta en ganancia → no bloquear."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=Decimal("2.50"))
        assert result.outcome == CheckOutcome.PASS

    def test_position_at_breakeven_passes(self) -> None:
        """Posición en breakeven (PnL=0) → no hay pérdida, no bloquear."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=Decimal("0"))
        assert result.outcome == CheckOutcome.PASS

    # --- Casos que SÍ deben bloquear ---

    def test_blocks_averaging_on_losing_position(self) -> None:
        """Posición abierta con PnL negativo → promediado detectado, bloquear."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=Decimal("-3.75"))
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_averaging"
        assert "-3.75" in result.reason

    def test_blocks_on_small_loss(self) -> None:
        """Cualquier pérdida, por pequeña que sea, activa el bloqueo."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=Decimal("-0.01"))
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_averaging"

    def test_blocks_on_large_loss(self) -> None:
        """Pérdida grande también es bloqueada."""
        result = check_anti_averaging(open_position_unrealized_pnl_usdt=Decimal("-9.99"))
        assert result.outcome == CheckOutcome.BLOCK
        assert result.rule == "anti_averaging"

    # --- Validaciones de tipos / errores ---

    def test_raises_on_non_decimal(self) -> None:
        with pytest.raises(
            ValueError, match="open_position_unrealized_pnl_usdt debe ser Decimal o None"
        ):
            check_anti_averaging(open_position_unrealized_pnl_usdt=-3.75)  # type: ignore[arg-type]

    def test_raises_on_int(self) -> None:
        with pytest.raises(
            ValueError, match="open_position_unrealized_pnl_usdt debe ser Decimal o None"
        ):
            check_anti_averaging(open_position_unrealized_pnl_usdt=-1)  # type: ignore[arg-type]
