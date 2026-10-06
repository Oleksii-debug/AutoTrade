"""Independent conservative oracle for simulated execution results.

The oracle never creates fills.  It verifies an existing simulated result using
only immutable order, liquidity and model inputs, providing a separate check
against optimistic quantity, price, fee and causal-time errors.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from fractions import Fraction

from .exact_decimal import as_fraction, bounded_fraction, exact_multiply, round_fraction_to_quantum

from .execution_realism import (
    ExecutionModel,
    ExecutionRealismError,
    LiquidityObservation,
    SimulatedExecution,
    SimulatedOrder,
    _instant,
)


class ExecutionOracleError(ValueError):
    pass


def _round_down(quantity: Decimal, lot_size: Decimal) -> Decimal:
    return round_fraction_to_quantum(
        as_fraction(quantity),
        lot_size,
        mode="FLOOR",
    )


def assert_conservative_execution(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    result: SimulatedExecution,
) -> None:
    """Reject a simulated result that exceeds independent conservative bounds."""

    if not isinstance(order, SimulatedOrder):
        raise TypeError("order must be SimulatedOrder")
    if not isinstance(observation, LiquidityObservation):
        raise TypeError("observation must be LiquidityObservation")
    if not isinstance(model, ExecutionModel):
        raise TypeError("model must be ExecutionModel")
    if not isinstance(result, SimulatedExecution):
        raise TypeError("result must be SimulatedExecution")
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
            exact_multiply(observation.available_volume, model.max_participation),
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
            if (
                model.price_quantum is None
                or model.price_projection_policy_id is None
                or model.price_projection_policy_version is None
                or model.price_grid_instrument_version is None
            ):
                raise ExecutionOracleError(
                    "MARKET execution requires complete price projection policy evidence"
                )
            if model.price_grid_instrument_version != order.instrument_version:
                raise ExecutionOracleError(
                    "MARKET price grid is not bound to the order instrument_version"
                )
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

            participation = (
                bounded_fraction(
                    as_fraction(independent_capacity)
                    / as_fraction(observation.available_volume)
                )
                if observation.available_volume > 0
                else Fraction(0, 1)
            )
            max_participation = as_fraction(model.max_participation)
            impact_fraction = (
                bounded_fraction(participation / max_participation)
                if max_participation > 0
                else Fraction(0, 1)
            )
            impact_fraction = bounded_fraction(
                min(impact_fraction, Fraction(1, 1))
            )
            impact_bps = bounded_fraction(
                as_fraction(model.impact_bps_at_max_participation)
                * impact_fraction
            )
            additional_spread = (
                model.bar_half_spread_bps
                if model.data_fidelity == "BAR"
                else Decimal("0")
            )
            total_bps = bounded_fraction(
                (
                    as_fraction(additional_spread)
                    + as_fraction(model.slippage_bps)
                    + impact_bps
                ) * as_fraction(model.scenario_cost_multiplier)
            )
            reference_fraction = as_fraction(reference)
            target = bounded_fraction(
                reference_fraction
                + reference_fraction * total_bps / 10000
                if order.side == "BUY"
                else reference_fraction
                - reference_fraction * total_bps / 10000
            )
            if target <= 0:
                raise ExecutionOracleError(
                    "configured adverse costs produce non-positive execution price"
                )
            projected = round_fraction_to_quantum(
                target,
                model.price_quantum,
                mode="CEILING" if order.side == "BUY" else "FLOOR",
            )
            if result.fill_price != projected:
                raise ExecutionOracleError(
                    "market fill price does not match independent price-grid projection"
                )
        else:
            if order.limit_price is None:
                raise ExecutionOracleError("limit execution lacks order limit")
            if order.side == "BUY" and result.fill_price > order.limit_price:
                raise ExecutionOracleError("buy limit filled above limit")
            if order.side == "SELL" and result.fill_price < order.limit_price:
                raise ExecutionOracleError("sell limit filled below limit")

        independent_min_fee = max(
            exact_multiply(
                exact_multiply(result.filled_quantity, result.fill_price),
                model.fee_rate,
            ),
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
