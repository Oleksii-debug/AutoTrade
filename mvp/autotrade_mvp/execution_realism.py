"""Deterministic execution-realism oracle for causal simulations.

The module is deliberately not an order-management system and never sends an
order.  It turns an already-admitted simulated order plus one future liquidity
observation into a conservative execution result.  All financial inputs are
exact Decimal-compatible values; binary floats are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from hashlib import sha256
import json
from typing import Literal


class ExecutionRealismError(ValueError):
    pass


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ExecutionRealismError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ExecutionRealismError(f"{name} must be a finite decimal")
    return result


def _non_negative(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0:
        raise ExecutionRealismError(f"{name} must be non-negative")
    return result


def _positive(value, *, name: str) -> Decimal:
    result = _decimal(value, name=name)
    if result <= 0:
        raise ExecutionRealismError(f"{name} must be positive")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExecutionRealismError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    if not text.endswith("Z"):
        raise ExecutionRealismError(f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise ExecutionRealismError(f"{name} must be an ISO-8601 instant") from error
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _digest(value: str, *, name: str) -> str:
    text = _text(value, name=name).lower()
    if text.startswith("sha256:"):
        text = text[7:]
    if len(text) != 64:
        raise ExecutionRealismError(f"{name} must be a SHA-256 digest")
    try:
        int(text, 16)
    except ValueError as error:
        raise ExecutionRealismError(f"{name} must be hexadecimal") from error
    return text


@dataclass(frozen=True)
class ExecutionModel:
    model_version: str
    calibration_sha256: str
    data_fidelity: Literal["BAR", "TOP_OF_BOOK", "BOOK"]
    scenario: Literal["OPTIMISTIC", "BASE", "ADVERSE"]
    latency_ms: int
    fee_rate: Decimal
    minimum_fee: Decimal
    max_participation: Decimal
    slippage_bps: Decimal
    impact_bps_at_max_participation: Decimal
    bar_half_spread_bps: Decimal
    scenario_cost_multiplier: Decimal

    def __post_init__(self) -> None:
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, int)
            or self.latency_ms < 0
        ):
            raise ExecutionRealismError("latency_ms must be a non-negative integer")
        fidelity = _text(self.data_fidelity, name="data_fidelity").upper()
        if fidelity not in {"BAR", "TOP_OF_BOOK", "BOOK"}:
            raise ExecutionRealismError("unsupported data_fidelity")
        scenario = _text(self.scenario, name="scenario").upper()
        if scenario not in {"OPTIMISTIC", "BASE", "ADVERSE"}:
            raise ExecutionRealismError("unsupported execution scenario")
        participation = _non_negative(
            self.max_participation, name="max_participation"
        )
        if participation > 1:
            raise ExecutionRealismError("max_participation cannot exceed 1")
        multiplier = _positive(
            self.scenario_cost_multiplier, name="scenario_cost_multiplier"
        )
        if scenario == "ADVERSE" and multiplier < 1:
            raise ExecutionRealismError(
                "ADVERSE scenario_cost_multiplier cannot be below 1"
            )
        object.__setattr__(self, "model_version", _text(self.model_version, name="model_version"))
        object.__setattr__(
            self,
            "calibration_sha256",
            _digest(self.calibration_sha256, name="calibration_sha256"),
        )
        object.__setattr__(self, "data_fidelity", fidelity)
        object.__setattr__(self, "scenario", scenario)
        object.__setattr__(self, "fee_rate", _non_negative(self.fee_rate, name="fee_rate"))
        object.__setattr__(
            self, "minimum_fee", _non_negative(self.minimum_fee, name="minimum_fee")
        )
        object.__setattr__(self, "max_participation", participation)
        object.__setattr__(
            self, "slippage_bps", _non_negative(self.slippage_bps, name="slippage_bps")
        )
        object.__setattr__(
            self,
            "impact_bps_at_max_participation",
            _non_negative(
                self.impact_bps_at_max_participation,
                name="impact_bps_at_max_participation",
            ),
        )
        object.__setattr__(
            self,
            "bar_half_spread_bps",
            _non_negative(self.bar_half_spread_bps, name="bar_half_spread_bps"),
        )
        object.__setattr__(self, "scenario_cost_multiplier", multiplier)

    @classmethod
    def create(
        cls,
        *,
        model_version: str,
        calibration_sha256: str,
        data_fidelity: str,
        scenario: str,
        latency_ms: int,
        fee_rate,
        minimum_fee,
        max_participation,
        slippage_bps,
        impact_bps_at_max_participation,
        bar_half_spread_bps=0,
        scenario_cost_multiplier=1,
    ) -> "ExecutionModel":
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, int) or latency_ms < 0:
            raise ExecutionRealismError("latency_ms must be a non-negative integer")
        fidelity = _text(data_fidelity, name="data_fidelity").upper()
        if fidelity not in {"BAR", "TOP_OF_BOOK", "BOOK"}:
            raise ExecutionRealismError("unsupported data_fidelity")
        normalized_scenario = _text(scenario, name="scenario").upper()
        if normalized_scenario not in {"OPTIMISTIC", "BASE", "ADVERSE"}:
            raise ExecutionRealismError("unsupported execution scenario")
        participation = _non_negative(max_participation, name="max_participation")
        if participation > 1:
            raise ExecutionRealismError("max_participation cannot exceed 1")
        multiplier = _positive(
            scenario_cost_multiplier,
            name="scenario_cost_multiplier",
        )
        if normalized_scenario == "ADVERSE" and multiplier < 1:
            raise ExecutionRealismError(
                "ADVERSE scenario_cost_multiplier cannot be below 1"
            )
        return cls(
            model_version=_text(model_version, name="model_version"),
            calibration_sha256=_digest(
                calibration_sha256,
                name="calibration_sha256",
            ),
            data_fidelity=fidelity,
            scenario=normalized_scenario,
            latency_ms=latency_ms,
            fee_rate=_non_negative(fee_rate, name="fee_rate"),
            minimum_fee=_non_negative(minimum_fee, name="minimum_fee"),
            max_participation=participation,
            slippage_bps=_non_negative(slippage_bps, name="slippage_bps"),
            impact_bps_at_max_participation=_non_negative(
                impact_bps_at_max_participation,
                name="impact_bps_at_max_participation",
            ),
            bar_half_spread_bps=_non_negative(
                bar_half_spread_bps,
                name="bar_half_spread_bps",
            ),
            scenario_cost_multiplier=multiplier,
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "model_version": self.model_version,
            "calibration_sha256": self.calibration_sha256,
            "data_fidelity": self.data_fidelity,
            "scenario": self.scenario,
            "latency_ms": self.latency_ms,
            "fee_rate": _decimal_text(self.fee_rate),
            "minimum_fee": _decimal_text(self.minimum_fee),
            "max_participation": _decimal_text(self.max_participation),
            "slippage_bps": _decimal_text(self.slippage_bps),
            "impact_bps_at_max_participation": _decimal_text(
                self.impact_bps_at_max_participation
            ),
            "bar_half_spread_bps": _decimal_text(self.bar_half_spread_bps),
            "scenario_cost_multiplier": _decimal_text(
                self.scenario_cost_multiplier
            ),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SimulatedOrder:
    order_id: str
    instrument_version: str
    side: Literal["BUY", "SELL"]
    order_type: Literal["MARKET", "LIMIT", "STOP_LIMIT"]
    quantity: Decimal
    submitted_at: str
    lot_size: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    already_triggered: bool = False

    def __post_init__(self) -> None:
        side = _text(self.side, name="side").upper()
        if side not in {"BUY", "SELL"}:
            raise ExecutionRealismError("side must be BUY or SELL")
        order_type = _text(self.order_type, name="order_type").upper()
        if order_type not in {"MARKET", "LIMIT", "STOP_LIMIT"}:
            raise ExecutionRealismError("unsupported order_type")
        if type(self.already_triggered) is not bool:
            raise TypeError("already_triggered must be boolean")
        quantity = _positive(self.quantity, name="quantity")
        lot_size = _positive(self.lot_size, name="lot_size")
        if quantity % lot_size != 0:
            raise ExecutionRealismError("quantity must be an exact multiple of lot_size")
        limit = None if self.limit_price is None else _positive(
            self.limit_price, name="limit_price"
        )
        stop = None if self.stop_price is None else _positive(
            self.stop_price, name="stop_price"
        )
        if order_type == "MARKET" and (limit is not None or stop is not None):
            raise ExecutionRealismError("MARKET order cannot carry limit/stop price")
        if order_type == "LIMIT" and (limit is None or stop is not None):
            raise ExecutionRealismError("LIMIT order requires only limit_price")
        if order_type == "STOP_LIMIT" and (limit is None or stop is None):
            raise ExecutionRealismError(
                "STOP_LIMIT order requires stop_price and limit_price"
            )
        submitted = _instant(self.submitted_at, name="submitted_at")
        object.__setattr__(self, "order_id", _text(self.order_id, name="order_id"))
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "order_type", order_type)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "submitted_at", _utc(submitted))
        object.__setattr__(self, "lot_size", lot_size)
        object.__setattr__(self, "limit_price", limit)
        object.__setattr__(self, "stop_price", stop)

    @classmethod
    def create(
        cls,
        *,
        order_id: str,
        instrument_version: str,
        side: str,
        order_type: str,
        quantity,
        submitted_at: str,
        lot_size,
        limit_price=None,
        stop_price=None,
        already_triggered: bool = False,
    ) -> "SimulatedOrder":
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise ExecutionRealismError("side must be BUY or SELL")
        normalized_type = _text(order_type, name="order_type").upper()
        if normalized_type not in {"MARKET", "LIMIT", "STOP_LIMIT"}:
            raise ExecutionRealismError("unsupported order_type")
        if type(already_triggered) is not bool:
            raise TypeError("already_triggered must be boolean")
        limit = (
            _positive(limit_price, name="limit_price")
            if limit_price is not None
            else None
        )
        stop = (
            _positive(stop_price, name="stop_price")
            if stop_price is not None
            else None
        )
        if normalized_type == "MARKET" and (limit is not None or stop is not None):
            raise ExecutionRealismError("MARKET order cannot carry limit/stop price")
        if normalized_type == "LIMIT" and (limit is None or stop is not None):
            raise ExecutionRealismError("LIMIT order requires only limit_price")
        if normalized_type == "STOP_LIMIT" and (limit is None or stop is None):
            raise ExecutionRealismError(
                "STOP_LIMIT order requires stop_price and limit_price"
            )
        submitted = _instant(submitted_at, name="submitted_at")
        normalized_quantity = _positive(quantity, name="quantity")
        normalized_lot_size = _positive(lot_size, name="lot_size")
        if normalized_quantity % normalized_lot_size != 0:
            raise ExecutionRealismError("quantity must be an exact multiple of lot_size")
        return cls(
            order_id=_text(order_id, name="order_id"),
            instrument_version=_text(
                instrument_version,
                name="instrument_version",
            ),
            side=normalized_side,
            order_type=normalized_type,
            quantity=normalized_quantity,
            submitted_at=_utc(submitted),
            lot_size=normalized_lot_size,
            limit_price=limit,
            stop_price=stop,
            already_triggered=already_triggered,
        )


@dataclass(frozen=True)
class LiquidityObservation:
    market_time: str
    available_at: str
    available_volume: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    bar_high: Decimal | None = None
    bar_low: Decimal | None = None

    def __post_init__(self) -> None:
        market = _instant(self.market_time, name="market_time")
        available = _instant(self.available_at, name="available_at")
        if available < market:
            raise ExecutionRealismError("available_at cannot precede market_time")
        volume = _non_negative(self.available_volume, name="available_volume")
        bid = None if self.bid is None else _positive(self.bid, name="bid")
        ask = None if self.ask is None else _positive(self.ask, name="ask")
        if bid is not None and ask is not None and ask < bid:
            raise ExecutionRealismError("ask cannot be below bid")
        high = None if self.bar_high is None else _positive(
            self.bar_high, name="bar_high"
        )
        low = None if self.bar_low is None else _positive(
            self.bar_low, name="bar_low"
        )
        if high is not None and low is not None and high < low:
            raise ExecutionRealismError("bar_high cannot be below bar_low")
        object.__setattr__(self, "market_time", _utc(market))
        object.__setattr__(self, "available_at", _utc(available))
        object.__setattr__(self, "available_volume", volume)
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "bar_high", high)
        object.__setattr__(self, "bar_low", low)

    @classmethod
    def create(
        cls,
        *,
        market_time: str,
        available_at: str,
        available_volume,
        bid=None,
        ask=None,
        bar_high=None,
        bar_low=None,
    ) -> "LiquidityObservation":
        market = _instant(market_time, name="market_time")
        available = _instant(available_at, name="available_at")
        if available < market:
            raise ExecutionRealismError(
                "available_at cannot precede market_time"
            )
        normalized_bid = _positive(bid, name="bid") if bid is not None else None
        normalized_ask = _positive(ask, name="ask") if ask is not None else None
        if (
            normalized_bid is not None
            and normalized_ask is not None
            and normalized_ask < normalized_bid
        ):
            raise ExecutionRealismError("ask cannot be below bid")
        high = (
            _positive(bar_high, name="bar_high")
            if bar_high is not None
            else None
        )
        low = (
            _positive(bar_low, name="bar_low")
            if bar_low is not None
            else None
        )
        if high is not None and low is not None and high < low:
            raise ExecutionRealismError("bar_high cannot be below bar_low")
        return cls(
            market_time=_utc(market),
            available_at=_utc(available),
            available_volume=_non_negative(
                available_volume,
                name="available_volume",
            ),
            bid=normalized_bid,
            ask=normalized_ask,
            bar_high=high,
            bar_low=low,
        )


@dataclass(frozen=True)
class SimulatedExecution:
    status: Literal[
        "FILLED",
        "PARTIAL",
        "NO_FILL",
        "WAITING_FOR_LATENCY",
        "AMBIGUOUS_NO_FILL",
    ]
    filled_quantity: Decimal
    fill_price: Decimal | None
    fee: Decimal
    arrival_at: str
    trade_time: str | None
    evidence_available_at: str
    triggered: bool
    model_fingerprint: str
    scenario: str
    data_fidelity: str
    reason: str
    warnings: tuple[str, ...]

    @property
    def optimistic_only(self) -> bool:
        return self.scenario == "OPTIMISTIC"


def _round_down(quantity: Decimal, lot_size: Decimal) -> Decimal:
    lots = (quantity / lot_size).to_integral_value(rounding=ROUND_DOWN)
    return lots * lot_size


def _capacity_quantity(
    *,
    order_quantity: Decimal,
    observation: LiquidityObservation,
    model: ExecutionModel,
    lot_size: Decimal,
) -> Decimal:
    raw = min(
        order_quantity,
        observation.available_volume * model.max_participation,
    )
    return _round_down(raw, lot_size)


def _limit_touched(
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> bool:
    assert order.limit_price is not None
    if model.data_fidelity == "BAR":
        if observation.bar_low is None or observation.bar_high is None:
            raise ExecutionRealismError(
                "BAR fidelity requires bar_low and bar_high"
            )
        if order.side == "BUY":
            return observation.bar_low <= order.limit_price
        return observation.bar_high >= order.limit_price

    if observation.bid is None or observation.ask is None:
        raise ExecutionRealismError(
            "TOP_OF_BOOK/BOOK fidelity requires bid and ask"
        )
    if order.side == "BUY":
        return observation.ask <= order.limit_price
    return observation.bid >= order.limit_price


def _stop_touched(
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> bool:
    assert order.stop_price is not None
    if model.data_fidelity == "BAR":
        if observation.bar_low is None or observation.bar_high is None:
            raise ExecutionRealismError(
                "BAR fidelity requires bar_low and bar_high"
            )
        if order.side == "BUY":
            return observation.bar_high >= order.stop_price
        return observation.bar_low <= order.stop_price

    if observation.bid is None or observation.ask is None:
        raise ExecutionRealismError(
            "TOP_OF_BOOK/BOOK fidelity requires bid and ask"
        )
    if order.side == "BUY":
        return observation.ask >= order.stop_price
    return observation.bid <= order.stop_price


def _market_reference(
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> tuple[Decimal, Decimal]:
    """Return adverse executable reference and participation-independent spread bps."""

    if model.data_fidelity == "BAR":
        if observation.bar_high is None or observation.bar_low is None:
            raise ExecutionRealismError(
                "BAR fidelity requires bar_low and bar_high"
            )
        base = observation.bar_high if order.side == "BUY" else observation.bar_low
        return base, model.bar_half_spread_bps

    if observation.bid is None or observation.ask is None:
        raise ExecutionRealismError(
            "TOP_OF_BOOK/BOOK fidelity requires bid and ask"
        )
    base = observation.ask if order.side == "BUY" else observation.bid
    return base, Decimal("0")


def simulate_execution(
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
) -> SimulatedExecution:
    """Evaluate exactly one future liquidity observation.

    The observation's market time must be strictly after the order reaches the
    venue. This prevents an order decided from one event from filling against
    that same or earlier liquidity by default.
    """

    if not isinstance(order, SimulatedOrder):
        raise TypeError("order must be SimulatedOrder")
    if not isinstance(observation, LiquidityObservation):
        raise TypeError("observation must be LiquidityObservation")
    if not isinstance(model, ExecutionModel):
        raise TypeError("model must be ExecutionModel")

    submitted = _instant(order.submitted_at, name="submitted_at")
    arrival = submitted + timedelta(milliseconds=model.latency_ms)
    market_time = _instant(observation.market_time, name="market_time")
    arrival_text = _utc(arrival)

    warnings: list[str] = []
    if model.data_fidelity == "BAR":
        warnings.append(
            "BAR fidelity cannot establish intrabar queue or event ordering"
        )
    elif model.data_fidelity == "TOP_OF_BOOK":
        warnings.append(
            "TOP_OF_BOOK fidelity cannot establish queue priority"
        )
    if model.scenario == "OPTIMISTIC":
        warnings.append(
            "OPTIMISTIC execution evidence is not sufficient for promotion"
        )

    if market_time <= arrival:
        return SimulatedExecution(
            status="WAITING_FOR_LATENCY",
            filled_quantity=Decimal("0"),
            fill_price=None,
            fee=Decimal("0"),
            arrival_at=arrival_text,
            trade_time=None,
            evidence_available_at=observation.available_at,
            triggered=order.already_triggered,
            model_fingerprint=model.fingerprint,
            scenario=model.scenario,
            data_fidelity=model.data_fidelity,
            reason="liquidity is not strictly later than venue arrival",
            warnings=tuple(warnings),
        )

    capacity = _capacity_quantity(
        order_quantity=order.quantity,
        observation=observation,
        model=model,
        lot_size=order.lot_size,
    )
    if capacity <= 0:
        return SimulatedExecution(
            status="NO_FILL",
            filled_quantity=Decimal("0"),
            fill_price=None,
            fee=Decimal("0"),
            arrival_at=arrival_text,
            trade_time=None,
            evidence_available_at=observation.available_at,
            triggered=order.already_triggered,
            model_fingerprint=model.fingerprint,
            scenario=model.scenario,
            data_fidelity=model.data_fidelity,
            reason="qualified participation capacity is below one lot",
            warnings=tuple(warnings),
        )

    triggered = order.already_triggered
    if order.order_type == "STOP_LIMIT" and not triggered:
        stop_touched = _stop_touched(order, observation, model)
        if not stop_touched:
            return SimulatedExecution(
                status="NO_FILL",
                filled_quantity=Decimal("0"),
                fill_price=None,
                fee=Decimal("0"),
                arrival_at=arrival_text,
                trade_time=None,
                evidence_available_at=observation.available_at,
                triggered=False,
                model_fingerprint=model.fingerprint,
                scenario=model.scenario,
                data_fidelity=model.data_fidelity,
                reason="stop trigger was not reached",
                warnings=tuple(warnings),
            )
        triggered = True
        if model.data_fidelity == "BAR" and _limit_touched(order, observation, model):
            # With OHLC only, seeing both trigger and limit prices inside one
            # candle does not prove that executable limit liquidity occurred
            # after the trigger. Do not choose the favorable chronology.
            return SimulatedExecution(
                status="AMBIGUOUS_NO_FILL",
                filled_quantity=Decimal("0"),
                fill_price=None,
                fee=Decimal("0"),
                arrival_at=arrival_text,
                trade_time=None,
                evidence_available_at=observation.available_at,
                triggered=True,
                model_fingerprint=model.fingerprint,
                scenario=model.scenario,
                data_fidelity=model.data_fidelity,
                reason=(
                    "BAR data cannot prove stop-before-limit intrabar ordering; "
                    "wait for later liquidity"
                ),
                warnings=tuple(warnings),
            )

        # A stop-limit becomes executable only after the trigger event.  Even
        # with top-of-book or book data, reusing the same observation as both
        # trigger and post-trigger liquidity would manufacture favorable event
        # ordering. Carry triggered state into the next observation instead.
        return SimulatedExecution(
            status="NO_FILL",
            filled_quantity=Decimal("0"),
            fill_price=None,
            fee=Decimal("0"),
            arrival_at=arrival_text,
            trade_time=None,
            evidence_available_at=observation.available_at,
            triggered=True,
            model_fingerprint=model.fingerprint,
            scenario=model.scenario,
            data_fidelity=model.data_fidelity,
            reason="stop triggered; wait for later liquidity before limit execution",
            warnings=tuple(warnings),
        )

    if order.order_type in {"LIMIT", "STOP_LIMIT"}:
        if not _limit_touched(order, observation, model):
            return SimulatedExecution(
                status="NO_FILL",
                filled_quantity=Decimal("0"),
                fill_price=None,
                fee=Decimal("0"),
                arrival_at=arrival_text,
                trade_time=None,
                evidence_available_at=observation.available_at,
                triggered=triggered,
                model_fingerprint=model.fingerprint,
                scenario=model.scenario,
                data_fidelity=model.data_fidelity,
                reason="limit price was not executable",
                warnings=tuple(warnings),
            )
        assert order.limit_price is not None
        fill_price = order.limit_price
    else:
        base_price, additional_spread_bps = _market_reference(
            order,
            observation,
            model,
        )
        participation = (
            capacity / observation.available_volume
            if observation.available_volume > 0
            else Decimal("0")
        )
        impact_fraction = (
            participation / model.max_participation
            if model.max_participation > 0
            else Decimal("0")
        )
        impact_bps = (
            model.impact_bps_at_max_participation
            * min(impact_fraction, Decimal("1"))
        )
        total_bps = (
            additional_spread_bps + model.slippage_bps + impact_bps
        ) * model.scenario_cost_multiplier
        price_delta = base_price * total_bps / Decimal("10000")
        fill_price = (
            base_price + price_delta
            if order.side == "BUY"
            else base_price - price_delta
        )
        if fill_price <= 0:
            raise ExecutionRealismError(
                "configured adverse costs produce non-positive execution price"
            )

    notional = capacity * fill_price
    fee = max(notional * model.fee_rate, model.minimum_fee)
    status = "FILLED" if capacity == order.quantity else "PARTIAL"
    return SimulatedExecution(
        status=status,
        filled_quantity=capacity,
        fill_price=fill_price,
        fee=fee,
        arrival_at=arrival_text,
        trade_time=observation.market_time,
        evidence_available_at=observation.available_at,
        triggered=triggered,
        model_fingerprint=model.fingerprint,
        scenario=model.scenario,
        data_fidelity=model.data_fidelity,
        reason=(
            "full quantity executed under bounded conservative model"
            if status == "FILLED"
            else "execution capped by qualified participation and available volume"
        ),
        warnings=tuple(warnings),
    )
