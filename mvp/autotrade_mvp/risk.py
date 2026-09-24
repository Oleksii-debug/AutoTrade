"""Fail-closed independent risk foundation for simulated AutoTrade admission."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping


def _decimal(value: Decimal | str | int, *, name: str, non_negative: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    if non_negative and result < 0:
        raise ValueError(f"{name} must be non-negative")
    return result


def _aware(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


@dataclass(frozen=True)
class RiskLimits:
    max_order_notional: Decimal
    max_position_quantity: Decimal
    max_gross_exposure: Decimal
    max_stress_loss: Decimal
    stress_fraction: Decimal
    max_market_age: timedelta


@dataclass(frozen=True)
class RiskCheck:
    rule_id: str
    status: str
    measured: str
    limit: str
    reason_code: str | None = None


@dataclass(frozen=True)
class RiskDecision:
    verdict: str
    checks: tuple[RiskCheck, ...]
    reservation_delta: Mapping[str, Decimal]
    reason_codes: tuple[str, ...]


def build_limits(
    *,
    max_order_notional: Decimal | str | int,
    max_position_quantity: Decimal | str | int,
    max_gross_exposure: Decimal | str | int,
    max_stress_loss: Decimal | str | int,
    stress_fraction: Decimal | str | int,
    max_market_age: timedelta,
) -> RiskLimits:
    if not isinstance(max_market_age, timedelta) or max_market_age <= timedelta(0):
        raise ValueError("max_market_age must be positive")
    stress = _decimal(stress_fraction, name="stress_fraction", non_negative=True)
    if stress > 1:
        raise ValueError("stress_fraction must not exceed 1")
    values = {
        "max_order_notional": _decimal(max_order_notional, name="max_order_notional", non_negative=True),
        "max_position_quantity": _decimal(max_position_quantity, name="max_position_quantity", non_negative=True),
        "max_gross_exposure": _decimal(max_gross_exposure, name="max_gross_exposure", non_negative=True),
        "max_stress_loss": _decimal(max_stress_loss, name="max_stress_loss", non_negative=True),
    }
    if any(value == 0 for value in values.values()):
        raise ValueError("risk limits must be positive")
    return RiskLimits(
        **values,
        stress_fraction=stress,
        max_market_age=max_market_age,
    )


def evaluate_order(
    *,
    limits: RiskLimits,
    now: datetime,
    market_observed_at: datetime,
    capability_status: str,
    capability_expires_at: datetime,
    side: str,
    quantity: Decimal | str | int,
    price: Decimal | str | int,
    contract_multiplier: Decimal | str | int,
    current_position: Decimal | str | int,
    available_cash: Decimal | str | int,
    already_reserved_cash: Decimal | str | int,
    settlement_currency: str,
) -> RiskDecision:
    instant = _aware(now, name="now")
    observed = _aware(market_observed_at, name="market_observed_at")
    expires = _aware(capability_expires_at, name="capability_expires_at")
    normalized_side = side.upper() if isinstance(side, str) else ""
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not settlement_currency or not settlement_currency.strip():
        raise ValueError("settlement_currency is required")

    qty = _decimal(quantity, name="quantity")
    unit_price = _decimal(price, name="price")
    multiplier = _decimal(contract_multiplier, name="contract_multiplier")
    position = _decimal(current_position, name="current_position")
    cash = _decimal(available_cash, name="available_cash", non_negative=True)
    reserved = _decimal(already_reserved_cash, name="already_reserved_cash", non_negative=True)
    if qty <= 0 or unit_price <= 0 or multiplier <= 0:
        raise ValueError("quantity, price and contract_multiplier must be positive")

    signed_qty = qty if normalized_side == "BUY" else -qty
    order_notional = qty * unit_price * multiplier
    projected_position = position + signed_qty
    projected_gross = abs(projected_position) * unit_price * multiplier
    stress_loss = projected_gross * limits.stress_fraction
    required_cash = order_notional if normalized_side == "BUY" else Decimal("0")
    free_cash = cash - reserved

    checks: list[RiskCheck] = []

    capability_ok = capability_status == "VERIFIED" and instant < expires
    checks.append(RiskCheck(
        "CAPABILITY.CURRENT",
        "PASS" if capability_ok else "FAIL",
        f"{capability_status}@{expires.isoformat()}",
        f"VERIFIED until after {instant.isoformat()}",
        None if capability_ok else ("AUTH.EXPIRED" if instant >= expires else "CAPABILITY.UNKNOWN"),
    ))

    age = instant - observed
    freshness_ok = timedelta(0) <= age <= limits.max_market_age
    checks.append(RiskCheck(
        "DATA.FRESHNESS",
        "PASS" if freshness_ok else "FAIL",
        str(age),
        str(limits.max_market_age),
        None if freshness_ok else "DATA.STALE",
    ))

    notional_ok = order_notional <= limits.max_order_notional
    checks.append(RiskCheck(
        "RISK.ORDER_NOTIONAL",
        "PASS" if notional_ok else "FAIL",
        str(order_notional),
        str(limits.max_order_notional),
        None if notional_ok else "RISK.LIMIT_BREACH",
    ))

    position_ok = abs(projected_position) <= limits.max_position_quantity
    checks.append(RiskCheck(
        "RISK.POSITION_QUANTITY",
        "PASS" if position_ok else "FAIL",
        str(abs(projected_position)),
        str(limits.max_position_quantity),
        None if position_ok else "RISK.LIMIT_BREACH",
    ))

    gross_ok = projected_gross <= limits.max_gross_exposure
    checks.append(RiskCheck(
        "RISK.GROSS_EXPOSURE",
        "PASS" if gross_ok else "FAIL",
        str(projected_gross),
        str(limits.max_gross_exposure),
        None if gross_ok else "RISK.LIMIT_BREACH",
    ))

    stress_ok = stress_loss <= limits.max_stress_loss
    checks.append(RiskCheck(
        "RISK.STRESS_LOSS",
        "PASS" if stress_ok else "FAIL",
        str(stress_loss),
        str(limits.max_stress_loss),
        None if stress_ok else "RISK.LIMIT_BREACH",
    ))

    cash_ok = required_cash <= free_cash
    checks.append(RiskCheck(
        "RISK.AVAILABLE_CASH",
        "PASS" if cash_ok else "FAIL",
        str(required_cash),
        str(max(free_cash, Decimal("0"))),
        None if cash_ok else "RISK.INSUFFICIENT_AVAILABLE",
    ))

    reasons = tuple(
        dict.fromkeys(check.reason_code for check in checks if check.reason_code is not None)
    )
    verdict = "ALLOW" if not reasons else "REJECT"
    reservation_delta = (
        {f"CASH:{settlement_currency.strip()}": required_cash}
        if verdict == "ALLOW" and required_cash > 0
        else {}
    )
    return RiskDecision(
        verdict=verdict,
        checks=tuple(checks),
        reservation_delta=reservation_delta,
        reason_codes=reasons,
    )
