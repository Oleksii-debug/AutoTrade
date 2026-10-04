"""Independent conservative oracle for simulated execution results.

The oracle never creates fills.  It verifies an existing simulated result using
only immutable order, liquidity and model inputs, providing a separate check
against optimistic quantity, price, fee and causal-time errors.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal, ROUND_DOWN

from .execution_realism import (
    ExecutionModel,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    _decimal,
    _detached_dataclass_input,
    _instant,
)


class ExecutionOracleError(ValueError):
    pass


def _round_down(quantity: Decimal, lot_size: Decimal) -> Decimal:
    lots = (quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
    return lots * lot_size


def _detached_execution_result(result: SimulatedExecution) -> SimulatedExecution:
    """Validate one held result snapshot without trusting caller object identity."""

    detached = _detached_dataclass_input(
        result,
        SimulatedExecution,
        name="result",
    )
    for field_name in (
        "status",
        "arrival_at",
        "model_fingerprint",
        "scenario",
        "data_fidelity",
        "reason",
        "evidence_available_at",
    ):
        if type(getattr(detached, field_name)) is not str:
            raise TypeError(f"result {field_name} must be exact text")
    if detached.trade_time is not None and type(detached.trade_time) is not str:
        raise TypeError("result trade_time must be exact text or None")
    if type(detached.triggered) is not bool:
        raise TypeError("result triggered must be exact boolean")
    if type(detached.warnings) is not tuple or any(
        type(warning) is not str for warning in detached.warnings
    ):
        raise TypeError("result warnings must be an exact tuple of text")
    for field_name in ("filled_quantity", "fee"):
        if type(getattr(detached, field_name)) is not Decimal:
            raise TypeError(f"result {field_name} must be exact Decimal")
    if detached.fill_price is not None and type(detached.fill_price) is not Decimal:
        raise TypeError("result fill_price must be exact Decimal or None")

    try:
        filled_quantity = _decimal(
            detached.filled_quantity,
            name="result.filled_quantity",
        )
        fee = _decimal(detached.fee, name="result.fee")
        fill_price = (
            None
            if detached.fill_price is None
            else _decimal(detached.fill_price, name="result.fill_price")
        )
    except ExecutionRealismError as error:
        raise ExecutionOracleError(
            "result financial scalars must be canonical finite decimals"
        ) from error

    if detached.status not in {
        "FILLED",
        "PARTIAL",
        "NO_FILL",
        "WAITING_FOR_LATENCY",
        "AMBIGUOUS_NO_FILL",
    }:
        raise ExecutionOracleError("unsupported result status")

    return SimulatedExecution(
        status=detached.status,
        filled_quantity=filled_quantity,
        fill_price=fill_price,
        fee=fee,
        arrival_at=detached.arrival_at,
        trade_time=detached.trade_time,
        evidence_available_at=detached.evidence_available_at,
        triggered=detached.triggered,
        model_fingerprint=detached.model_fingerprint,
        scenario=detached.scenario,
        data_fidelity=detached.data_fidelity,
        reason=detached.reason,
        warnings=tuple(detached.warnings),
    )


def assert_conservative_execution(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    result: SimulatedExecution,
) -> None:
    """Reject a simulated result that exceeds independent conservative bounds."""

    order = _detached_dataclass_input(order, SimulatedOrder, name="order")
    observation = _detached_dataclass_input(
        observation,
        LiquidityObservation,
        name="observation",
    )
    model = _detached_dataclass_input(model, ExecutionModel, name="model")
    result = _detached_execution_result(result)
    if observation.instrument_version != order.instrument_version:
        raise ExecutionOracleError("instrument identity mismatch")
    if result.model_fingerprint != model.fingerprint:
        raise ExecutionOracleError("result model fingerprint mismatch")
    if result.data_fidelity != model.data_fidelity or result.scenario != model.scenario:
        raise ExecutionOracleError("result model dimensions mismatch")

    zero = Decimal("0")
    if result.filled_quantity < zero:
        raise ExecutionOracleError("filled quantity cannot be negative")
    if result.filled_quantity > order.quantity:
        raise ExecutionOracleError("filled quantity exceeds order quantity")
    if result.filled_quantity % order.lot_size != 0:
        raise ExecutionOracleError("filled quantity violates lot size")

    independent_capacity = _round_down(
        min(
            order.quantity,
            observation.available_volume * model.max_participation,
        ),
        order.lot_size,
    )
    if result.filled_quantity > independent_capacity:
        raise ExecutionOracleError(
            "filled quantity exceeds independently qualified participation capacity"
        )

    submitted = _instant(order.submitted_at, name="submitted_at")
    arrival = submitted + timedelta(milliseconds=model.latency_ms)
    market_time = _instant(observation.market_time, name="market_time")
    canonical_arrival = arrival.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if result.arrival_at != canonical_arrival:
        raise ExecutionOracleError("result arrival_at must equal independently derived arrival")

    if result.filled_quantity > zero:
        if market_time <= arrival:
            raise ExecutionOracleError("fill uses same or earlier liquidity")
        if model.data_fidelity == "BAR":
            if observation.interval_start is None:
                raise ExecutionOracleError(
                    "BAR fill requires interval_start for causal volume"
                )
            interval_start = _instant(
                observation.interval_start,
                name="interval_start",
            )
            if interval_start < arrival:
                raise ExecutionOracleError(
                    "BAR fill uses interval volume from before venue arrival"
                )
        if result.trade_time != observation.market_time:
            raise ExecutionOracleError("fill trade_time must equal source market_time")
        if result.fill_price is None or result.fill_price <= zero:
            raise ExecutionOracleError("positive fill requires positive fill_price")
        if result.fee < zero:
            raise ExecutionOracleError("fee cannot be negative")

        if order.order_type == "MARKET":
            if model.data_fidelity == "BAR":
                if observation.bar_high is None or observation.bar_low is None:
                    raise ExecutionOracleError("BAR market fill lacks price bounds")
                reference = (
                    observation.bar_high
                    if order.side == "BUY"
                    else observation.bar_low
                )
            else:
                if observation.bid is None or observation.ask is None:
                    raise ExecutionOracleError("market fill lacks bid/ask evidence")
                reference = (
                    observation.ask
                    if order.side == "BUY"
                    else observation.bid
                )
            if order.side == "BUY" and result.fill_price < reference:
                raise ExecutionOracleError(
                    "market buy result is more favorable than executable reference"
                )
            if order.side == "SELL" and result.fill_price > reference:
                raise ExecutionOracleError(
                    "market sell result is more favorable than executable reference"
                )
        else:
            if order.limit_price is None:
                raise ExecutionOracleError("limit execution lacks order limit")
            if order.side == "BUY" and result.fill_price > order.limit_price:
                raise ExecutionOracleError("buy limit filled above limit")
            if order.side == "SELL" and result.fill_price < order.limit_price:
                raise ExecutionOracleError("sell limit filled below limit")

        independent_min_fee = max(
            result.filled_quantity * result.fill_price * model.fee_rate,
            model.minimum_fee,
        )
        if result.fee < independent_min_fee:
            raise ExecutionOracleError("fee is below independently required minimum")
    else:
        if result.fill_price is not None:
            raise ExecutionOracleError("zero fill cannot carry fill_price")
        if result.fee != zero:
            raise ExecutionOracleError("zero fill cannot carry fee")
        if result.trade_time is not None:
            raise ExecutionOracleError("zero fill cannot claim trade_time")

    if result.evidence_available_at != observation.available_at:
        raise ExecutionOracleError(
            "result must retain exact evidence availability time"
        )
