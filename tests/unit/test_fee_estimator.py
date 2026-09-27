"""Tests unitarios — estimación de fees pre-trade (`backend/core/fees.py`).

F17, regla 12 de `docs/live_checklist.md`: el Risk Engine proyecta los fees
del round-trip con las tasas reales del adapter antes de operar.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.backtesting.fee_model import FeeModel
from backend.core.fees import FeeEstimate, FeeRates, estimate_fees, estimate_fees_for_decision
from backend.decision_engine.schemas import DecisionType, EntryType
from tests.unit.test_risk_engine_validation import _long_decision

_D = Decimal
_RATES = FeeRates(maker=_D("0.0002"), taker=_D("0.0005"))


def _estimate(**overrides: object) -> FeeEstimate:
    kwargs: dict[str, object] = {
        "side": DecisionType.LONG,
        "entry_type": EntryType.MARKET,
        "notional_usdt": _D("25"),
        "entry_price": _D("95000"),
        "stop_loss": _D("90000"),
        "take_profit": _D("105000"),
        "rates": _RATES,
    }
    kwargs.update(overrides)
    return estimate_fees(**kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FeeRates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [_D("-0.0001"), _D("1"), _D("1.5")])
def test_fee_rates_reject_out_of_range(bad: Decimal) -> None:
    with pytest.raises(ValueError, match="fuera de"):
        FeeRates(maker=bad, taker=_D("0.0005"))
    with pytest.raises(ValueError, match="fuera de"):
        FeeRates(maker=_D("0.0002"), taker=bad)


def test_fee_model_exposes_its_rates() -> None:
    model = FeeModel(taker_rate=_D("0.0004"), maker_rate=_D("0.0001"))
    assert model.rates == FeeRates(maker=_D("0.0001"), taker=_D("0.0004"))


# ---------------------------------------------------------------------------
# estimate_fees
# ---------------------------------------------------------------------------


def test_market_long_pays_taker_on_entry_and_exit() -> None:
    est = _estimate()
    # 25 USDT × 0.05%
    assert est.entry_fee_usdt == _D("0.01250000")
    # cantidad = 25/95000; salida al TP = cantidad × 105000 × 0.05%
    assert est.exit_fee_at_tp_usdt == _D("0.01381579")
    assert est.exit_fee_at_sl_usdt == _D("0.01184211")
    assert est.entry_rate == _D("0.0005")
    assert est.exit_rate == _D("0.0005")


def test_net_rr_matches_formula_and_is_below_gross() -> None:
    est = _estimate()
    qty = _D("25") / _D("95000")
    reward = qty * _D("10000") - _D("25") * _D("0.0005") - qty * _D("105000") * _D("0.0005")
    risk = qty * _D("5000") + _D("25") * _D("0.0005") + qty * _D("90000") * _D("0.0005")
    assert est.gross_risk_reward == _D("2.0000")
    assert est.net_risk_reward == (reward / risk).quantize(_D("0.0001"))
    assert est.net_risk_reward < est.gross_risk_reward


def test_limit_entry_pays_maker_but_exit_stays_taker() -> None:
    est = _estimate(entry_type=EntryType.LIMIT)
    assert est.entry_rate == _D("0.0002")
    assert est.entry_fee_usdt == _D("0.00500000")
    assert est.exit_rate == _D("0.0005")


def test_short_mirrors_distances() -> None:
    est = _estimate(
        side=DecisionType.SHORT,
        stop_loss=_D("100000"),
        take_profit=_D("85000"),
    )
    assert est.gross_risk_reward == _D("2.0000")
    assert est.net_risk_reward < est.gross_risk_reward


def test_net_rr_does_not_depend_on_notional() -> None:
    """Margen y leverage escalan fee, ganancia y pérdida por igual (por eso el gate bloquea)."""
    small = _estimate(notional_usdt=_D("5"))
    large = _estimate(notional_usdt=_D("50"))
    assert small.net_risk_reward == large.net_risk_reward


def test_fees_larger_than_tp_distance_give_negative_rr() -> None:
    # TP a 0.05% de la entrada: el round-trip taker (~0.1%) se lo come entero.
    est = _estimate(stop_loss=_D("94000"), take_profit=_D("95047.5"))
    assert est.net_risk_reward < 0


def test_net_rr_rounds_down_so_the_threshold_never_passes_by_rounding() -> None:
    """Un RR real de 1.49995 no puede quedar como 1.5000 y pasar el gate."""
    # Sin fees el RR neto es el bruto: 1.49995 = 14999.5 / 10000.
    zero = FeeRates(maker=_D("0"), taker=_D("0"))
    est = _estimate(
        entry_price=_D("100000"),
        stop_loss=_D("90000"),
        take_profit=_D("114999.5"),
        rates=zero,
    )
    assert est.net_risk_reward == _D("1.4999")


def test_fees_round_up() -> None:
    """El costo proyectado se redondea en contra: nunca se subestima el fee."""
    # 1 USDT × 0.000000005 = 5e-9: por debajo del paso de 1e-8, pero no es cero.
    est = _estimate(notional_usdt=_D("1"), rates=FeeRates(maker=_D("0"), taker=_D("5E-9")))
    assert est.entry_fee_usdt == _D("0.00000001")


def test_round_trip_at_sl_sums_entry_and_exit() -> None:
    est = _estimate()
    assert est.round_trip_fee_at_sl_usdt == est.entry_fee_usdt + est.exit_fee_at_sl_usdt


def test_audit_reason_carries_fees_and_net_rr() -> None:
    est = _estimate()
    reason = est.as_audit_reason()
    assert str(est.entry_fee_usdt) in reason
    assert str(est.net_risk_reward) in reason
    assert est.method in reason


@pytest.mark.parametrize(
    "overrides",
    [
        {"notional_usdt": _D("0")},
        {"entry_price": _D("0")},
        {"stop_loss": _D("96000")},  # SL del lado de la ganancia en un LONG
        {"take_profit": _D("94000")},  # TP del lado de la pérdida en un LONG
        {"side": DecisionType.NO_OPERAR},
        {"entry_type": EntryType.NO_ENTRY},
    ],
)
def test_invalid_inputs_raise(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _estimate(**overrides)


# ---------------------------------------------------------------------------
# estimate_fees_for_decision
# ---------------------------------------------------------------------------


def test_for_decision_uses_margin_times_leverage() -> None:
    decision = _long_decision(margin_usdt=5.0, leverage=5)
    est = estimate_fees_for_decision(decision, _D("5"), 5, _RATES)
    assert est == _estimate()
