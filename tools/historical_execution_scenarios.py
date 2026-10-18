"""Causal OHLCV execution scenarios for provider-free historical research.

This module is an adapter, not a broker, OMS, ledger, or fill authority.  It
turns one already-completed next bar into the canonical execution-realism
oracle's BAR observation.  The caller must still settle accepted simulated
fills through the existing OMS/risk/ledger path.
"""
from __future__ import annotations

from datetime import datetime, timezone

from mvp.autotrade_mvp.exact_decimal import parse_bounded_exact_decimal
from mvp.autotrade_mvp.execution_realism import (
    ExecutionModel,
    ExecutionPriceProjectionPolicy,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    simulate_execution,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion


def _instant(value: str, *, name: str) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ExecutionRealismError(f"{name} must be an exact UTC instant ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ExecutionRealismError(f"{name} must be an ISO-8601 UTC instant") from exc
    if parsed.tzinfo is None:
        raise ExecutionRealismError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _price(value, *, name: str):
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use exact decimal input")
    try:
        parsed = parse_bounded_exact_decimal(value)
    except (TypeError, ValueError) as exc:
        raise ExecutionRealismError(f"{name} must be a finite exact decimal") from exc
    if parsed <= 0:
        raise ExecutionRealismError(f"{name} must be positive")
    return parsed


def conservative_bar_model(
    *,
    model_version: str,
    calibration_sha256: str,
    instrument: InstrumentVersion,
    fee_rate,
    maximum_participation,
    slippage_bps,
    half_spread_bps,
    latency_ms: int = 0,
) -> ExecutionModel:
    """Build the explicit adverse BAR scenario consumed by the canonical oracle."""

    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    projection = ExecutionPriceProjectionPolicy.from_instrument(instrument)
    return ExecutionModel.create(
        model_version=model_version,
        calibration_sha256=calibration_sha256,
        data_fidelity="BAR",
        scenario="ADVERSE",
        latency_ms=latency_ms,
        fee_rate=fee_rate,
        minimum_fee="0",
        max_participation=maximum_participation,
        slippage_bps=slippage_bps,
        impact_bps_at_max_participation="0",
        bar_half_spread_bps=half_spread_bps,
        scenario_cost_multiplier="1",
        price_projection=projection,
    )


def evaluate_completed_next_bar(
    order: SimulatedOrder,
    model: ExecutionModel,
    *,
    as_of: str,
    interval_start: str,
    interval_end: str,
    available_at: str,
    open_price,
    high_price,
    low_price,
    close_price,
    base_volume,
) -> SimulatedExecution:
    """Evaluate one immediate next bar without exposing future/incomplete data.

    OHLCV volume is only a conservative capacity input.  It is not order-book
    liquidity or proof that a fill occurred.  The canonical oracle applies
    adverse high/low execution prices, configured half-spread/slippage, exact
    fees and participation caps.
    """

    if type(order) is not SimulatedOrder:
        raise TypeError("order must be exact SimulatedOrder")
    if type(model) is not ExecutionModel:
        raise TypeError("model must be exact ExecutionModel")
    if model.data_fidelity != "BAR" or model.scenario != "ADVERSE":
        raise ExecutionRealismError(
            "historical next-bar execution requires the adverse BAR scenario"
        )

    submitted = _instant(order.submitted_at, name="order submitted_at")
    start = _instant(interval_start, name="interval_start")
    end = _instant(interval_end, name="interval_end")
    published = _instant(available_at, name="available_at")
    cut = _instant(as_of, name="as_of")
    if start != submitted:
        raise ExecutionRealismError(
            "historical execution bar must immediately follow the decision cut"
        )
    if end <= start:
        raise ExecutionRealismError("historical execution bar interval is invalid")
    if published < end:
        raise ExecutionRealismError("historical bar cannot be available before it completes")
    if cut < published:
        raise ExecutionRealismError("future historical bar is unavailable at the requested cut")
    if cut != published:
        raise ExecutionRealismError(
            "stale historical execution bar is not the next newly available bar"
        )

    opening = _price(open_price, name="open_price")
    high = _price(high_price, name="high_price")
    low = _price(low_price, name="low_price")
    closing = _price(close_price, name="close_price")
    if low > min(opening, closing) or high < max(opening, closing) or high < low:
        raise ExecutionRealismError("historical execution bar has invalid OHLC ordering")
    if isinstance(base_volume, (bool, float)):
        raise TypeError("base_volume must use exact decimal input")
    try:
        volume = parse_bounded_exact_decimal(base_volume)
    except (TypeError, ValueError) as exc:
        raise ExecutionRealismError("base_volume must be a finite exact decimal") from exc
    if volume < 0:
        raise ExecutionRealismError("base_volume must be non-negative")

    observation = LiquidityObservation.create(
        instrument_version=order.instrument_version,
        market_time=interval_end,
        available_at=available_at,
        available_volume=volume,
        interval_start=interval_start,
        bar_high=high,
        bar_low=low,
    )
    return simulate_execution(order, observation, model)
