"""Independent conservative oracle for simulated execution results.

The oracle never creates fills.  It verifies an existing simulated result using
only immutable order, liquidity and model inputs, providing a separate check
against optimistic quantity, price, fee and causal-time errors.
"""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal
from fractions import Fraction

from .exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_multiply,
    is_exact_decimal_multiple,
    round_fraction_to_quantum,
)
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


def _oracle_exact_product(*values: Decimal, name: str) -> Decimal:
    try:
        return exact_multiply(*values)
    except ExactDecimalError as error:
        raise ExecutionOracleError(
            f"{name} exceeds exact arithmetic resource envelope"
        ) from error


def _oracle_bounded_rational(value: Fraction, *, name: str) -> Fraction:
    try:
        return bounded_fraction(value)
    except (ExactDecimalError, TypeError) as error:
        raise ExecutionOracleError(
            f"{name} exceeds exact rational resource envelope"
        ) from error


def _oracle_market_price_bound(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    capacity: Decimal,
    base_price: Decimal,
    additional_spread_bps: Decimal,
) -> Decimal:
    projection = model.price_projection
    if projection is None:
        raise ExecutionOracleError(
            "MARKET execution requires authoritative price projection policy"
        )
    if projection.instrument_version != order.instrument_version:
        raise ExecutionOracleError(
            "price projection instrument_version must match order instrument_version"
        )
    try:
        reference_is_on_grid = is_exact_decimal_multiple(
            base_price,
            projection.price_quantum,
        )
    except ExactDecimalError as error:
        raise ExecutionOracleError(
            "independent market reference price grid check exceeds exact arithmetic resource envelope"
        ) from error
    if not reference_is_on_grid:
        raise ExecutionOracleError(
            "independent market reference price is not aligned to authoritative price quantum"
        )

    available = as_fraction(observation.available_volume)
    participation = (
        _oracle_bounded_rational(
            as_fraction(capacity) / available,
            name="independent market participation",
        )
        if available > 0
        else Fraction(0, 1)
    )
    maximum_participation = as_fraction(model.max_participation)
    impact_fraction = (
        _oracle_bounded_rational(
            participation / maximum_participation,
            name="independent market impact fraction",
        )
        if maximum_participation > 0
        else Fraction(0, 1)
    )
    impact_fraction = min(impact_fraction, Fraction(1, 1))
    impact_bps = _oracle_bounded_rational(
        as_fraction(model.impact_bps_at_max_participation) * impact_fraction,
        name="independent market impact bps",
    )
    total_bps = _oracle_bounded_rational(
        _oracle_bounded_rational(
            as_fraction(additional_spread_bps)
            + as_fraction(model.slippage_bps)
            + impact_bps,
            name="independent market total bps before scenario",
        )
        * as_fraction(model.scenario_cost_multiplier),
        name="independent market total bps",
    )
    price_delta = _oracle_bounded_rational(
        as_fraction(base_price) * total_bps / Fraction(10000, 1),
        name="independent market price delta",
    )
    unrounded = _oracle_bounded_rational(
        as_fraction(base_price) + price_delta
        if order.side == "BUY"
        else as_fraction(base_price) - price_delta,
        name="independent market projected price",
    )
    try:
        return round_fraction_to_quantum(
            unrounded,
            projection.price_quantum,
            mode="CEILING" if order.side == "BUY" else "FLOOR",
        )
    except ExactDecimalError as error:
        raise ExecutionOracleError(
            "market price projection exceeds exact arithmetic resource envelope"
        ) from error


def _oracle_limit_touched(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> bool:
    """Independently prove that the frozen limit was executable."""

    if order.limit_price is None:
        raise ExecutionOracleError("limit execution lacks order limit")
    if model.data_fidelity == "BAR":
        if observation.bar_low is None or observation.bar_high is None:
            raise ExecutionOracleError("BAR limit fill lacks price bounds")
        return (
            observation.bar_low <= order.limit_price
            if order.side == "BUY"
            else observation.bar_high >= order.limit_price
        )
    if observation.bid is None or observation.ask is None:
        raise ExecutionOracleError("limit fill lacks bid/ask evidence")
    return (
        observation.ask <= order.limit_price
        if order.side == "BUY"
        else observation.bid >= order.limit_price
    )


def _oracle_stop_touched(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> bool:
    """Independently prove a STOP_LIMIT trigger from the frozen observation."""

    if order.stop_price is None:
        raise ExecutionOracleError("stop-limit execution lacks stop price")
    if model.data_fidelity == "BAR":
        if observation.bar_low is None or observation.bar_high is None:
            raise ExecutionOracleError("BAR stop trigger lacks price bounds")
        return (
            observation.bar_high >= order.stop_price
            if order.side == "BUY"
            else observation.bar_low <= order.stop_price
        )
    if observation.bid is None or observation.ask is None:
        raise ExecutionOracleError("stop trigger lacks bid/ask evidence")
    return (
        observation.ask >= order.stop_price
        if order.side == "BUY"
        else observation.bid <= order.stop_price
    )


def _round_down(quantity: Decimal, lot_size: Decimal) -> Decimal:
    try:
        return round_fraction_to_quantum(
            as_fraction(quantity),
            lot_size,
            mode="FLOOR",
        )
    except ExactDecimalError as error:
        raise ExecutionOracleError(
            "quantity rounding exceeds exact arithmetic resource envelope"
        ) from error


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
    if order.order_type == "MARKET":
        projection = model.price_projection
        if projection is None:
            raise ExecutionOracleError(
                "MARKET execution requires authoritative price projection policy"
            )
        if projection.instrument_version != order.instrument_version:
            raise ExecutionOracleError(
                "price projection instrument_version must match order instrument_version"
            )
    if result.model_fingerprint != model.fingerprint:
        raise ExecutionOracleError("result model fingerprint mismatch")
    if result.data_fidelity != model.data_fidelity or result.scenario != model.scenario:
        raise ExecutionOracleError("result model dimensions mismatch")

    zero = Decimal("0")
    if result.filled_quantity < zero:
        raise ExecutionOracleError("filled quantity cannot be negative")
    if result.filled_quantity > order.quantity:
        raise ExecutionOracleError("filled quantity exceeds order quantity")
    try:
        result_is_lot_multiple = is_exact_decimal_multiple(
            result.filled_quantity,
            order.lot_size,
        )
    except ExactDecimalError as error:
        raise ExecutionOracleError(
            "filled quantity lot check exceeds exact arithmetic resource envelope"
        ) from error
    if not result_is_lot_multiple:
        raise ExecutionOracleError("filled quantity violates lot size")

    independent_participating_volume = _oracle_exact_product(
        observation.available_volume,
        model.max_participation,
        name="independent participation capacity",
    )
    independent_capacity = _round_down(
        min(order.quantity, independent_participating_volume),
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

    if market_time <= arrival:
        if result.status != "WAITING_FOR_LATENCY":
            raise ExecutionOracleError(
                "pre-arrival result must remain WAITING_FOR_LATENCY"
            )
    elif result.status == "WAITING_FOR_LATENCY":
        raise ExecutionOracleError(
            "post-arrival result cannot claim WAITING_FOR_LATENCY"
        )

    if result.status == "AMBIGUOUS_NO_FILL" and model.data_fidelity != "BAR":
        raise ExecutionOracleError(
            "AMBIGUOUS_NO_FILL requires BAR causal ambiguity"
        )

    if model.data_fidelity == "BAR" and market_time > arrival:
        if observation.interval_start is None:
            raise ExecutionOracleError(
                "post-arrival BAR result requires interval_start for causal classification"
            )
        interval_start_for_status = _instant(
            observation.interval_start,
            name="interval_start",
        )
        interval_overlaps_arrival = interval_start_for_status < arrival
        same_bar_stop_limit_ambiguity = False
        if (
            not interval_overlaps_arrival
            and order.order_type == "STOP_LIMIT"
            and not order.already_triggered
            and independent_capacity > zero
        ):
            same_bar_stop_limit_ambiguity = (
                _oracle_stop_touched(
                    order=order,
                    observation=observation,
                    model=model,
                )
                and _oracle_limit_touched(
                    order=order,
                    observation=observation,
                    model=model,
                )
            )
        expected_bar_ambiguity = (
            interval_overlaps_arrival or same_bar_stop_limit_ambiguity
        )
        if expected_bar_ambiguity and result.status != "AMBIGUOUS_NO_FILL":
            raise ExecutionOracleError(
                "BAR causal ambiguity must remain AMBIGUOUS_NO_FILL"
            )
        if not expected_bar_ambiguity and result.status == "AMBIGUOUS_NO_FILL":
            raise ExecutionOracleError(
                "AMBIGUOUS_NO_FILL lacks BAR causal ambiguity"
            )

    if (
        order.order_type == "STOP_LIMIT"
        and not order.already_triggered
        and market_time > arrival
    ):
        causal_stop_evidence = True
        if model.data_fidelity == "BAR":
            if observation.interval_start is None:
                causal_stop_evidence = False
            else:
                causal_stop_evidence = (
                    _instant(
                        observation.interval_start,
                        name="interval_start",
                    )
                    >= arrival
                )
        if (
            causal_stop_evidence
            and _oracle_stop_touched(
                order=order,
                observation=observation,
                model=model,
            )
            and not result.triggered
        ):
            raise ExecutionOracleError(
                "observed stop trigger cannot be omitted from result state"
            )

    if order.already_triggered and not result.triggered:
        raise ExecutionOracleError(
            "triggered state cannot regress after prior STOP_LIMIT trigger"
        )

    if result.triggered and not order.already_triggered:
        if order.order_type != "STOP_LIMIT":
            raise ExecutionOracleError(
                "non-stop order cannot mint triggered state"
            )
        if market_time <= arrival:
            raise ExecutionOracleError(
                "triggered state uses same or earlier liquidity"
            )
        if model.data_fidelity == "BAR":
            if observation.interval_start is None:
                raise ExecutionOracleError(
                    "BAR trigger requires interval_start for causal evidence"
                )
            trigger_interval_start = _instant(
                observation.interval_start,
                name="interval_start",
            )
            if trigger_interval_start < arrival:
                raise ExecutionOracleError(
                    "triggered state uses BAR evidence from before venue arrival"
                )
        if not _oracle_stop_touched(
            order=order,
            observation=observation,
            model=model,
        ):
            raise ExecutionOracleError(
                "triggered state lacks independently observed stop evidence"
            )

    if result.filled_quantity > zero:
        expected_fill_status = (
            "FILLED" if result.filled_quantity == order.quantity else "PARTIAL"
        )
        if result.status != expected_fill_status:
            raise ExecutionOracleError(
                "positive fill status must match exact execution completeness"
            )
    elif result.status in {"FILLED", "PARTIAL"}:
        raise ExecutionOracleError("zero fill cannot claim FILLED or PARTIAL status")

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
            expected_market_price = _oracle_market_price_bound(
                order=order,
                observation=observation,
                model=model,
                capacity=independent_capacity,
                base_price=reference,
                additional_spread_bps=(
                    model.bar_half_spread_bps
                    if model.data_fidelity == "BAR"
                    else Decimal("0")
                ),
            )
            if result.fill_price != expected_market_price:
                raise ExecutionOracleError(
                    "market fill_price must equal independently projected adverse tick bound"
                )
        else:
            if order.limit_price is None:
                raise ExecutionOracleError("limit execution lacks order limit")
            if order.order_type == "STOP_LIMIT" and not order.already_triggered:
                raise ExecutionOracleError(
                    "stop-limit fill requires a previously triggered order"
                )
            if not _oracle_limit_touched(
                order=order,
                observation=observation,
                model=model,
            ):
                raise ExecutionOracleError(
                    "limit fill lacks independently executable price evidence"
                )
            if result.fill_price != order.limit_price:
                raise ExecutionOracleError(
                    "limit fill_price must equal the frozen order limit"
                )

        independent_notional = _oracle_exact_product(
            result.filled_quantity,
            result.fill_price,
            name="independent execution notional",
        )
        independent_proportional_fee = _oracle_exact_product(
            independent_notional,
            model.fee_rate,
            name="independent execution fee",
        )
        independent_min_fee = max(
            independent_proportional_fee,
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
