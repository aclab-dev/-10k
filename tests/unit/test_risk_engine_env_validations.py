"""Tests unitarios para check_leverage_cap — validaciones por entorno (F9 [71])."""

import pytest

from backend.core.config import Environment
from backend.risk_engine.checks import check_leverage_cap

# ---------------------------------------------------------------------------
# PAPER — cap 10x
# ---------------------------------------------------------------------------


class TestPaperLeverageCap:
    def test_at_cap_passes(self) -> None:
        assert check_leverage_cap(10, Environment.PAPER) is None

    def test_below_cap_passes(self) -> None:
        assert check_leverage_cap(1, Environment.PAPER) is None
        assert check_leverage_cap(5, Environment.PAPER) is None

    def test_above_cap_returns_reasons(self) -> None:
        result = check_leverage_cap(11, Environment.PAPER)
        assert result is not None
        assert "leverage_cap_paper" in result
        assert "11x" in result["leverage_cap_paper"]
        assert "10x" in result["leverage_cap_paper"]

    def test_reason_mentions_environment(self) -> None:
        result = check_leverage_cap(11, Environment.PAPER)
        assert result is not None
        assert "PAPER" in result["leverage_cap_paper"]


# ---------------------------------------------------------------------------
# TESTNET — cap 5x
# ---------------------------------------------------------------------------


class TestTestnetLeverageCap:
    def test_at_cap_passes(self) -> None:
        assert check_leverage_cap(5, Environment.TESTNET) is None

    def test_below_cap_passes(self) -> None:
        assert check_leverage_cap(1, Environment.TESTNET) is None

    def test_above_cap_returns_reasons(self) -> None:
        result = check_leverage_cap(6, Environment.TESTNET)
        assert result is not None
        assert "leverage_cap_testnet" in result
        assert "6x" in result["leverage_cap_testnet"]
        assert "5x" in result["leverage_cap_testnet"]

    def test_paper_cap_does_not_pass_testnet(self) -> None:
        # 10x pasa PAPER pero no TESTNET
        result = check_leverage_cap(10, Environment.TESTNET)
        assert result is not None
        assert "leverage_cap_testnet" in result


# ---------------------------------------------------------------------------
# LIVE inicial — cap 3x
# ---------------------------------------------------------------------------


class TestLiveInitialLeverageCap:
    def test_at_cap_passes(self) -> None:
        assert check_leverage_cap(3, Environment.LIVE, is_live_initial=True) is None

    def test_below_cap_passes(self) -> None:
        assert check_leverage_cap(1, Environment.LIVE, is_live_initial=True) is None

    def test_above_cap_returns_reasons(self) -> None:
        result = check_leverage_cap(4, Environment.LIVE, is_live_initial=True)
        assert result is not None
        assert "leverage_cap_live_initial" in result
        assert "4x" in result["leverage_cap_live_initial"]
        assert "3x" in result["leverage_cap_live_initial"]

    def test_reason_mentions_initial_phase(self) -> None:
        result = check_leverage_cap(4, Environment.LIVE, is_live_initial=True)
        assert result is not None
        assert "inicial" in result["leverage_cap_live_initial"]

    def test_default_applies_most_restrictive_cap(self) -> None:
        # El default (is_live_initial=True) debe aplicar el cap más bajo.
        # 4x viola LIVE inicial (3x) pero no LIVE absoluto (5x).
        # Si el default cambia a False, este test falla y lo hace visible.
        result = check_leverage_cap(4, Environment.LIVE)
        assert result is not None
        assert "leverage_cap_live_initial" in result

    def test_live_initial_cap_is_below_absolute(self) -> None:
        # Invariante de seguridad: cap inicial SIEMPRE < cap absoluto en LIVE.
        # Si esto falla, revisar _LEVERAGE_CAP_LIVE_INITIAL/_LEVERAGE_CAP_LIVE_ABSOLUTE
        # antes de cambiar el test — el fallo indica un cambio de regla de negocio.
        assert check_leverage_cap(3, Environment.LIVE, is_live_initial=True) is None
        assert check_leverage_cap(3, Environment.LIVE, is_live_initial=False) is None
        assert check_leverage_cap(4, Environment.LIVE, is_live_initial=True) is not None
        assert check_leverage_cap(4, Environment.LIVE, is_live_initial=False) is None


# ---------------------------------------------------------------------------
# LIVE absoluto — cap 5x
# ---------------------------------------------------------------------------


class TestLiveAbsoluteLeverageCap:
    def test_at_cap_passes(self) -> None:
        assert check_leverage_cap(5, Environment.LIVE, is_live_initial=False) is None

    def test_below_cap_passes(self) -> None:
        assert check_leverage_cap(3, Environment.LIVE, is_live_initial=False) is None

    def test_above_cap_returns_reasons(self) -> None:
        result = check_leverage_cap(6, Environment.LIVE, is_live_initial=False)
        assert result is not None
        assert "leverage_cap_live_absolute" in result
        assert "6x" in result["leverage_cap_live_absolute"]
        assert "5x" in result["leverage_cap_live_absolute"]

    def test_initial_cap_enforced_within_absolute(self) -> None:
        # 4x viola el cap inicial (3x) pero NO el absoluto (5x)
        result = check_leverage_cap(4, Environment.LIVE, is_live_initial=False)
        assert result is None


# ---------------------------------------------------------------------------
# Leverage inválido (≤ 0 o tipo incorrecto)
# ---------------------------------------------------------------------------


class TestInvalidLeverage:
    def test_zero_leverage_raises(self) -> None:
        with pytest.raises(ValueError, match="entero positivo"):
            check_leverage_cap(0, Environment.PAPER)

    def test_negative_leverage_raises(self) -> None:
        with pytest.raises(ValueError, match="entero positivo"):
            check_leverage_cap(-5, Environment.PAPER)

    def test_negative_leverage_raises_in_live(self) -> None:
        with pytest.raises(ValueError, match="entero positivo"):
            check_leverage_cap(-1, Environment.LIVE)

    def test_float_leverage_raises(self) -> None:
        with pytest.raises(ValueError, match="entero"):
            check_leverage_cap(2.5, Environment.PAPER)  # type: ignore[arg-type]

    def test_bool_leverage_raises(self) -> None:
        # bool es subclase de int en Python; debe rechazarse explícitamente.
        with pytest.raises(ValueError, match="entero"):
            check_leverage_cap(True, Environment.PAPER)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Entorno no reconocido
# ---------------------------------------------------------------------------


class TestUnrecognizedEnvironment:
    def test_invalid_string_raises(self) -> None:
        with pytest.raises(ValueError, match="no reconocido"):
            check_leverage_cap(5, "STAGING")  # type: ignore[arg-type]

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="no reconocido"):
            check_leverage_cap(5, "")  # type: ignore[arg-type]

    def test_none_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="no reconocido"):
            check_leverage_cap(5, None)  # type: ignore[arg-type]

    def test_numeric_string_raises(self) -> None:
        with pytest.raises(ValueError, match="no reconocido"):
            check_leverage_cap(5, "123")  # type: ignore[arg-type]

    def test_live_initial_key_is_not_a_valid_environment(self) -> None:
        # "LIVE_INITIAL" es una key interna de _LEVERAGE_CAP_*, no un valor de Environment.
        with pytest.raises(ValueError, match="no reconocido"):
            check_leverage_cap(3, "LIVE_INITIAL")  # type: ignore[arg-type]

    def test_is_live_initial_false_in_paper_raises(self) -> None:
        # is_live_initial solo aplica en LIVE; pasarlo como False en otro entorno es caller error.
        with pytest.raises(ValueError, match="solo aplica en Environment.LIVE"):
            check_leverage_cap(5, Environment.PAPER, is_live_initial=False)

    def test_is_live_initial_false_in_testnet_raises(self) -> None:
        with pytest.raises(ValueError, match="solo aplica en Environment.LIVE"):
            check_leverage_cap(5, Environment.TESTNET, is_live_initial=False)


# ---------------------------------------------------------------------------
# Conversión desde string válido
# ---------------------------------------------------------------------------


class TestStringEnvironmentInput:
    def test_paper_string_accepted(self) -> None:
        assert check_leverage_cap(10, "PAPER") is None  # type: ignore[arg-type]

    def test_testnet_string_accepted(self) -> None:
        assert check_leverage_cap(5, "TESTNET") is None  # type: ignore[arg-type]

    def test_live_string_accepted(self) -> None:
        assert check_leverage_cap(3, "LIVE") is None  # type: ignore[arg-type]

    def test_lowercase_string_accepted(self) -> None:
        assert check_leverage_cap(3, "paper") is None  # type: ignore[arg-type]
