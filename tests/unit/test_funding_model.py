"""Tests unitarios — compute_funding_payment."""

from __future__ import annotations

from decimal import Decimal

from backend.core.funding import compute_funding_payment
from backend.exchange_adapters.schemas import OrderSide


def test_long_positive_rate_pays() -> None:
    """Long con rate positivo paga funding (retorna positivo)."""
    payment = compute_funding_payment(
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("0.0001"),
    )
    assert payment == Decimal("5")  # 1 * 50000 * 0.0001


def test_long_negative_rate_receives() -> None:
    """Long con rate negativo recibe funding (retorna negativo)."""
    payment = compute_funding_payment(
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("-0.0001"),
    )
    assert payment == Decimal("-5")


def test_short_positive_rate_receives() -> None:
    """Short con rate positivo recibe funding (retorna negativo)."""
    payment = compute_funding_payment(
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("0.0001"),
    )
    assert payment == Decimal("-5")


def test_short_negative_rate_pays() -> None:
    """Short con rate negativo paga funding (retorna positivo)."""
    payment = compute_funding_payment(
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        mark_price=Decimal("50000"),
        funding_rate=Decimal("-0.0001"),
    )
    assert payment == Decimal("5")


def test_zero_rate_no_payment() -> None:
    payment = compute_funding_payment(
        side=OrderSide.BUY,
        quantity=Decimal("2"),
        mark_price=Decimal("30000"),
        funding_rate=Decimal("0"),
    )
    assert payment == Decimal("0")


def test_payment_scales_with_quantity() -> None:
    p1 = compute_funding_payment(OrderSide.BUY, Decimal("1"), Decimal("10000"), Decimal("0.0001"))
    p2 = compute_funding_payment(OrderSide.BUY, Decimal("3"), Decimal("10000"), Decimal("0.0001"))
    assert p2 == p1 * 3
