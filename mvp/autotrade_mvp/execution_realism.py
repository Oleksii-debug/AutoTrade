"""Deterministic execution-realism oracle for causal simulations.

The module is deliberately not an order-management system and never sends an
order.  It turns an already-admitted simulated order plus one future liquidity
observation into a conservative execution result.  All financial inputs are
exact Decimal-compatible values; binary floats are rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from fractions import Fraction
import json
from typing import Literal

from .exact_decimal import (
    ExactDecimalError,
    as_fraction,
    bounded_fraction,
    exact_multiply,
    is_exact_decimal_multiple,
    parse_bounded_exact_decimal,
    round_fraction_to_quantum,
)


class ExecutionRealismError(ValueError):
    pass


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, (bool, float)):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except (TypeError, ValueError) as error:
        raise ExecutionRealismError(f"{name} must be a finite decimal") from error


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
    if type(value) is not str or not value.strip():
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


_PRICE_PROJECTION_POLICY_ID = "ADVERSE_INSTRUMENT_TICK"
_PRICE_PROJECTION_POLICY_VERSION = "1"


@dataclass(frozen=True)
class ExecutionPriceProjectionPolicy:
    """Versioned adverse price-grid projection bound to instrument metadata facts."""

    policy_id: str
    policy_version: str
    instrument_version: str
    price_quantum: Decimal
    instrument_metadata_binding: str

    def __post_init__(self) -> None:
        policy_id = _text(self.policy_id, name="projection policy_id")
        policy_version = _text(self.policy_version, name="projection policy_version")
        if policy_id != _PRICE_PROJECTION_POLICY_ID or policy_version != _PRICE_PROJECTION_POLICY_VERSION:
            raise ExecutionRealismError("unsupported execution price projection policy")
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_version", policy_version)
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="projection instrument_version"),
        )
        object.__setattr__(
            self,
            "price_quantum",
            _positive(self.price_quantum, name="projection price_quantum"),
        )
        object.__setattr__(
            self,
            "instrument_metadata_binding",
            _digest(
                self.instrument_metadata_binding,
                name="projection instrument_metadata_binding",
            ),
        )

    @classmethod
    def from_instrument(cls, instrument) -> "ExecutionPriceProjectionPolicy":
        """Issue projection semantics from one detached canonical InstrumentVersion."""

        from .instruments import InstrumentVersion, _detached_instrument_version

        if type(instrument) is not InstrumentVersion:
            raise TypeError("instrument must be exact InstrumentVersion")
        detached = _detached_instrument_version(instrument)
        return cls(
            policy_id=_PRICE_PROJECTION_POLICY_ID,
            policy_version=_PRICE_PROJECTION_POLICY_VERSION,
            instrument_version=f"{detached.instrument_id}@{detached.version}",
            price_quantum=detached.price_tick,
            instrument_metadata_binding=detached.metadata_evidence_binding(),
        )


def _instrument_price_source_evidence_binding(instrument) -> str:
    """Bind the complete immutable metadata-evidence identity behind one price grid."""

    from .instruments import InstrumentVersion, _detached_instrument_version

    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    detached = _detached_instrument_version(instrument)
    if not detached.metadata_evidence:
        raise ExecutionRealismError(
            "canonical instrument price grid requires metadata evidence"
        )
    payload: list[dict[str, str]] = []
    for evidence in detached.metadata_evidence:
        if any(
            type(key) is not str or type(value) is not str
            for key, value in evidence.items()
        ):
            raise TypeError(
                "instrument metadata evidence must contain exact text identity"
            )
        payload.append({key: evidence[key] for key in sorted(evidence)})
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, init=False)
class ExecutionPriceGrid:
    """Immutable MARKET price-grid authority issued by InstrumentRegistry."""

    instrument_version: str
    price_quantum: Decimal
    projection_policy_id: str
    projection_policy_version: str
    instrument_metadata_binding: str
    source_evidence_binding: str
    _authority_token: object = field(repr=False, compare=False)
    _issued_fingerprint: str = field(repr=False, compare=False)

    def __init__(self, *args, **kwargs) -> None:
        raise TypeError(
            "ExecutionPriceGrid must be issued by the canonical InstrumentRegistry"
        )

    @classmethod
    def from_registry(
        cls,
        registry,
        instrument_version: str,
    ) -> "ExecutionPriceGrid":
        return _issue_execution_price_grid(registry, instrument_version)

    @property
    def fingerprint(self) -> str:
        return _execution_price_grid_fingerprint(self)


def _execution_price_grid_authority_operations():
    authority_token = object()
    public_fields = (
        "instrument_version",
        "price_quantum",
        "projection_policy_id",
        "projection_policy_version",
        "instrument_metadata_binding",
        "source_evidence_binding",
    )
    expected_state_fields = frozenset(
        public_fields + ("_authority_token", "_issued_fingerprint")
    )

    def normalize(
        *,
        instrument_version,
        price_quantum,
        projection_policy_id,
        projection_policy_version,
        instrument_metadata_binding,
        source_evidence_binding,
    ) -> dict[str, object]:
        policy_id = _text(
            projection_policy_id,
            name="price grid projection_policy_id",
        )
        policy_version = _text(
            projection_policy_version,
            name="price grid projection_policy_version",
        )
        if (
            policy_id != _PRICE_PROJECTION_POLICY_ID
            or policy_version != _PRICE_PROJECTION_POLICY_VERSION
        ):
            raise ExecutionRealismError(
                "unsupported execution price projection policy"
            )
        return {
            "instrument_version": _text(
                instrument_version,
                name="price grid instrument_version",
            ),
            "price_quantum": _positive(
                price_quantum,
                name="price grid price_quantum",
            ),
            "projection_policy_id": policy_id,
            "projection_policy_version": policy_version,
            "instrument_metadata_binding": _digest(
                instrument_metadata_binding,
                name="price grid instrument_metadata_binding",
            ),
            "source_evidence_binding": _digest(
                source_evidence_binding,
                name="price grid source_evidence_binding",
            ),
        }

    def fingerprint_values(values: dict[str, object]) -> str:
        payload = {
            "instrument_version": values["instrument_version"],
            "price_quantum": _decimal_text(values["price_quantum"]),
            "projection_policy_id": values["projection_policy_id"],
            "projection_policy_version": values["projection_policy_version"],
            "instrument_metadata_binding": values["instrument_metadata_binding"],
            "source_evidence_binding": values["source_evidence_binding"],
            "rounding": {"BUY": "CEILING", "SELL": "FLOOR"},
        }
        return sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def build(values: dict[str, object]) -> ExecutionPriceGrid:
        normalized = normalize(**values)
        result = object.__new__(ExecutionPriceGrid)
        for name in public_fields:
            object.__setattr__(result, name, normalized[name])
        object.__setattr__(result, "_authority_token", authority_token)
        object.__setattr__(
            result,
            "_issued_fingerprint",
            fingerprint_values(normalized),
        )
        return result

    def issue(registry, instrument_version: str) -> ExecutionPriceGrid:
        from .instruments import InstrumentRegistry, InstrumentVersion

        if type(registry) is not InstrumentRegistry:
            raise TypeError("registry must be exact InstrumentRegistry")
        # Exact registry type alone is insufficient: normal Python instances can
        # shadow methods in __dict__.  Price-grid authority must read through
        # the canonical class method so a caller-attached registry.exact cannot
        # substitute a different tick/instrument at issuance time.
        instrument = InstrumentRegistry.exact(registry, instrument_version)
        if type(instrument) is not InstrumentVersion:
            raise TypeError("registry returned non-canonical InstrumentVersion")
        return build(
            {
                "instrument_version": (
                    f"{instrument.instrument_id}@{instrument.version}"
                ),
                "price_quantum": instrument.price_tick,
                "projection_policy_id": _PRICE_PROJECTION_POLICY_ID,
                "projection_policy_version": _PRICE_PROJECTION_POLICY_VERSION,
                "instrument_metadata_binding": (
                    instrument.metadata_evidence_binding()
                ),
                "source_evidence_binding": (
                    _instrument_price_source_evidence_binding(instrument)
                ),
            }
        )

    def detach(value) -> ExecutionPriceGrid:
        if type(value) is not ExecutionPriceGrid:
            raise TypeError("price_grid must be exact ExecutionPriceGrid")
        state = object.__getattribute__(value, "__dict__")
        if (
            type(state) is not dict
            or any(type(name) is not str for name in state)
            or set(state) != expected_state_fields
        ):
            raise TypeError("price_grid has non-canonical state")
        if state["_authority_token"] is not authority_token:
            raise ExecutionRealismError(
                "price grid was not issued by the canonical InstrumentRegistry"
            )
        values = {name: state[name] for name in public_fields}
        normalized = normalize(**values)
        expected_fingerprint = fingerprint_values(normalized)
        if (
            type(state["_issued_fingerprint"]) is not str
            or state["_issued_fingerprint"] != expected_fingerprint
        ):
            raise ExecutionRealismError(
                "price grid authority content changed after issuance"
            )
        return build(normalized)

    def fingerprint(value) -> str:
        detached = detach(value)
        return object.__getattribute__(detached, "_issued_fingerprint")

    def matches_instrument(value, instrument) -> bool:
        from .instruments import InstrumentVersion, _detached_instrument_version

        detached_grid = detach(value)
        if type(instrument) is not InstrumentVersion:
            raise TypeError("instrument must be exact InstrumentVersion")
        detached_instrument = _detached_instrument_version(instrument)
        expected_version = (
            f"{detached_instrument.instrument_id}@{detached_instrument.version}"
        )
        return (
            detached_grid.instrument_version == expected_version
            and detached_grid.price_quantum == detached_instrument.price_tick
            and detached_grid.instrument_metadata_binding
            == _digest(
                detached_instrument.metadata_evidence_binding(),
                name="instrument metadata binding",
            )
            and detached_grid.source_evidence_binding
            == _instrument_price_source_evidence_binding(detached_instrument)
            and detached_grid.projection_policy_id
            == _PRICE_PROJECTION_POLICY_ID
            and detached_grid.projection_policy_version
            == _PRICE_PROJECTION_POLICY_VERSION
        )

    return issue, detach, fingerprint, matches_instrument


(
    _issue_execution_price_grid,
    _detach_execution_price_grid,
    _execution_price_grid_fingerprint,
    _execution_price_grid_matches_instrument,
) = _execution_price_grid_authority_operations()


def _projection_grid_consistent(
    projection: ExecutionPriceProjectionPolicy,
    grid: ExecutionPriceGrid,
) -> bool:
    return (
        projection.instrument_version == grid.instrument_version
        and projection.price_quantum == grid.price_quantum
        and projection.policy_id == grid.projection_policy_id
        and projection.policy_version == grid.projection_policy_version
        and projection.instrument_metadata_binding
        == grid.instrument_metadata_binding
    )


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
    price_projection: ExecutionPriceProjectionPolicy | None = None
    price_grid: ExecutionPriceGrid | None = None

    def __post_init__(self) -> None:
        if type(self.latency_ms) is not int or self.latency_ms < 0:
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
        if scenario in {"BASE", "ADVERSE"} and multiplier < 1:
            raise ExecutionRealismError(
                f"{scenario} scenario_cost_multiplier cannot be below 1"
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
        if self.price_projection is not None:
            projection = _detached_dataclass_input(
                self.price_projection,
                ExecutionPriceProjectionPolicy,
                name="price_projection",
            )
            object.__setattr__(self, "price_projection", projection)
        if self.price_grid is not None:
            grid = _detach_execution_price_grid(self.price_grid)
            object.__setattr__(self, "price_grid", grid)
        if self.price_projection is not None and self.price_grid is not None:
            if not _projection_grid_consistent(
                self.price_projection,
                self.price_grid,
            ):
                raise ExecutionRealismError(
                    "price projection and canonical price grid authority mismatch"
                )

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
        price_projection: ExecutionPriceProjectionPolicy | None = None,
        price_grid: ExecutionPriceGrid | None = None,
    ) -> "ExecutionModel":
        if type(latency_ms) is not int or latency_ms < 0:
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
        if normalized_scenario in {"BASE", "ADVERSE"} and multiplier < 1:
            raise ExecutionRealismError(
                f"{normalized_scenario} scenario_cost_multiplier cannot be below 1"
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
            price_projection=price_projection,
            price_grid=price_grid,
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
            "price_projection": (
                None
                if self.price_projection is None
                else {
                    "policy_id": self.price_projection.policy_id,
                    "policy_version": self.price_projection.policy_version,
                    "instrument_version": self.price_projection.instrument_version,
                    "price_quantum": _decimal_text(self.price_projection.price_quantum),
                    "instrument_metadata_binding": self.price_projection.instrument_metadata_binding,
                }
            ),
            "price_grid_fingerprint": (
                None
                if self.price_grid is None
                else self.price_grid.fingerprint
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
        if self.already_triggered and order_type != "STOP_LIMIT":
            raise ExecutionRealismError(
                "already_triggered is only valid for STOP_LIMIT orders"
            )
        quantity = _positive(self.quantity, name="quantity")
        lot_size = _positive(self.lot_size, name="lot_size")
        try:
            quantity_is_multiple = is_exact_decimal_multiple(quantity, lot_size)
        except ExactDecimalError as error:
            raise ExecutionRealismError(
                "quantity multiple check exceeds exact arithmetic resource envelope"
            ) from error
        if not quantity_is_multiple:
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
        if already_triggered and normalized_type != "STOP_LIMIT":
            raise ExecutionRealismError(
                "already_triggered is only valid for STOP_LIMIT orders"
            )
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
        try:
            quantity_is_multiple = is_exact_decimal_multiple(
                normalized_quantity,
                normalized_lot_size,
            )
        except ExactDecimalError as error:
            raise ExecutionRealismError(
                "quantity multiple check exceeds exact arithmetic resource envelope"
            ) from error
        if not quantity_is_multiple:
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
    instrument_version: str
    market_time: str
    available_at: str
    available_volume: Decimal
    interval_start: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    bar_high: Decimal | None = None
    bar_low: Decimal | None = None

    def __post_init__(self) -> None:
        instrument_version = _text(
            self.instrument_version, name="instrument_version"
        )
        market = _instant(self.market_time, name="market_time")
        available = _instant(self.available_at, name="available_at")
        if available < market:
            raise ExecutionRealismError("available_at cannot precede market_time")
        volume = _non_negative(self.available_volume, name="available_volume")
        interval_start = (
            None
            if self.interval_start is None
            else _instant(self.interval_start, name="interval_start")
        )
        if interval_start is not None and interval_start >= market:
            raise ExecutionRealismError(
                "interval_start must be strictly before market_time"
            )
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
        object.__setattr__(self, "instrument_version", instrument_version)
        object.__setattr__(self, "market_time", _utc(market))
        object.__setattr__(self, "available_at", _utc(available))
        object.__setattr__(self, "available_volume", volume)
        object.__setattr__(
            self,
            "interval_start",
            None if interval_start is None else _utc(interval_start),
        )
        object.__setattr__(self, "bid", bid)
        object.__setattr__(self, "ask", ask)
        object.__setattr__(self, "bar_high", high)
        object.__setattr__(self, "bar_low", low)

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        market_time: str,
        available_at: str,
        available_volume,
        interval_start=None,
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
        normalized_interval_start = (
            None
            if interval_start is None
            else _instant(interval_start, name="interval_start")
        )
        if normalized_interval_start is not None and normalized_interval_start >= market:
            raise ExecutionRealismError(
                "interval_start must be strictly before market_time"
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
            instrument_version=_text(
                instrument_version, name="instrument_version"
            ),
            market_time=_utc(market),
            available_at=_utc(available),
            available_volume=_non_negative(
                available_volume,
                name="available_volume",
            ),
            interval_start=(
                None
                if normalized_interval_start is None
                else _utc(normalized_interval_start)
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


def _detached_dataclass_input(value, expected_type, *, name: str):
    """Take one held canonical snapshot of a caller-owned execution DTO."""

    if type(value) is not expected_type:
        raise TypeError(f"{name} must be exact {expected_type.__name__}")
    state = object.__getattribute__(value, "__dict__")
    if type(state) is not dict:
        raise TypeError(f"{name} must expose canonical dataclass state")
    snapshot = dict(state)
    state_keys = tuple(snapshot)
    if any(type(field_name) is not str for field_name in state_keys):
        raise TypeError(f"{name} has non-canonical state field names")
    expected_fields = tuple(field.name for field in fields(expected_type))
    if set(state_keys) != set(expected_fields):
        raise TypeError(f"{name} has unexpected state fields")
    return expected_type(
        **{field_name: snapshot[field_name] for field_name in expected_fields}
    )


def _exact_product(*values: Decimal, name: str) -> Decimal:
    try:
        return exact_multiply(*values)
    except ExactDecimalError as error:
        raise ExecutionRealismError(
            f"{name} exceeds exact arithmetic resource envelope"
        ) from error


def _bounded_rational(value: Fraction, *, name: str) -> Fraction:
    try:
        return bounded_fraction(value)
    except (ExactDecimalError, TypeError) as error:
        raise ExecutionRealismError(
            f"{name} exceeds exact rational resource envelope"
        ) from error


def _require_market_projection_authority(
    order: SimulatedOrder,
    model: ExecutionModel,
) -> ExecutionPriceProjectionPolicy:
    projection = model.price_projection
    if projection is None:
        raise ExecutionRealismError(
            "MARKET execution requires authoritative price projection policy"
        )
    grid = model.price_grid
    if grid is None:
        raise ExecutionRealismError(
            "MARKET execution requires canonical InstrumentRegistry price grid"
        )
    grid = _detach_execution_price_grid(grid)
    if not _projection_grid_consistent(projection, grid):
        raise ExecutionRealismError(
            "price projection and canonical price grid authority mismatch"
        )
    if (
        projection.instrument_version != order.instrument_version
        or grid.instrument_version != order.instrument_version
    ):
        raise ExecutionRealismError(
            "price projection instrument_version must match order instrument_version"
        )
    return projection


def _market_projected_price(
    *,
    order: SimulatedOrder,
    observation: LiquidityObservation,
    model: ExecutionModel,
    capacity: Decimal,
    base_price: Decimal,
    additional_spread_bps: Decimal,
) -> Decimal:
    projection = _require_market_projection_authority(order, model)
    try:
        reference_is_on_grid = is_exact_decimal_multiple(
            base_price,
            projection.price_quantum,
        )
    except ExactDecimalError as error:
        raise ExecutionRealismError(
            "market reference price grid check exceeds exact arithmetic resource envelope"
        ) from error
    if not reference_is_on_grid:
        raise ExecutionRealismError(
            "market reference price is not aligned to authoritative price quantum"
        )

    available = as_fraction(observation.available_volume)
    participation = (
        _bounded_rational(as_fraction(capacity) / available, name="market participation")
        if available > 0
        else Fraction(0, 1)
    )
    maximum_participation = as_fraction(model.max_participation)
    impact_fraction = (
        _bounded_rational(
            participation / maximum_participation,
            name="market impact fraction",
        )
        if maximum_participation > 0
        else Fraction(0, 1)
    )
    impact_fraction = min(impact_fraction, Fraction(1, 1))
    impact_bps = _bounded_rational(
        as_fraction(model.impact_bps_at_max_participation) * impact_fraction,
        name="market impact bps",
    )
    total_bps = _bounded_rational(
        _bounded_rational(
            as_fraction(additional_spread_bps)
            + as_fraction(model.slippage_bps)
            + impact_bps,
            name="market total bps before scenario",
        )
        * as_fraction(model.scenario_cost_multiplier),
        name="market total bps",
    )
    price_delta = _bounded_rational(
        as_fraction(base_price) * total_bps / Fraction(10000, 1),
        name="market price delta",
    )
    unrounded = _bounded_rational(
        as_fraction(base_price) + price_delta
        if order.side == "BUY"
        else as_fraction(base_price) - price_delta,
        name="market projected price",
    )
    try:
        fill_price = round_fraction_to_quantum(
            unrounded,
            projection.price_quantum,
            mode="CEILING" if order.side == "BUY" else "FLOOR",
        )
    except ExactDecimalError as error:
        raise ExecutionRealismError(
            "market price projection exceeds exact arithmetic resource envelope"
        ) from error
    if fill_price <= 0:
        raise ExecutionRealismError(
            "configured adverse costs produce non-positive execution price"
        )
    return fill_price


def _round_down(quantity: Decimal, lot_size: Decimal) -> Decimal:
    try:
        return round_fraction_to_quantum(
            as_fraction(quantity),
            lot_size,
            mode="FLOOR",
        )
    except ExactDecimalError as error:
        raise ExecutionRealismError(
            "quantity rounding exceeds exact arithmetic resource envelope"
        ) from error


def _capacity_quantity(
    *,
    order_quantity: Decimal,
    observation: LiquidityObservation,
    model: ExecutionModel,
    lot_size: Decimal,
) -> Decimal:
    participating_volume = _exact_product(
        observation.available_volume,
        model.max_participation,
        name="participation capacity",
    )
    raw = min(order_quantity, participating_volume)
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

    order = _detached_dataclass_input(order, SimulatedOrder, name="order")
    observation = _detached_dataclass_input(
        observation,
        LiquidityObservation,
        name="observation",
    )
    model = _detached_dataclass_input(model, ExecutionModel, name="model")
    if observation.instrument_version != order.instrument_version:
        raise ExecutionRealismError(
            "liquidity instrument_version must exactly match order instrument_version"
        )
    if order.order_type == "MARKET":
        _require_market_projection_authority(order, model)

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

    if model.data_fidelity == "BAR":
        if observation.interval_start is None:
            raise ExecutionRealismError(
                "BAR fidelity requires interval_start for causal volume"
            )
        interval_start = _instant(
            observation.interval_start,
            name="interval_start",
        )
        if interval_start < arrival:
            return SimulatedExecution(
                status="AMBIGUOUS_NO_FILL",
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
                reason=(
                    "BAR volume includes liquidity from before venue arrival; "
                    "wait for a fully future interval"
                ),
                warnings=tuple(warnings),
            )

    capacity = _capacity_quantity(
        order_quantity=order.quantity,
        observation=observation,
        model=model,
        lot_size=order.lot_size,
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
        if capacity <= 0:
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
                reason=(
                    "stop triggered but qualified participation capacity "
                    "is below one lot"
                ),
                warnings=tuple(warnings),
            )
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

    if capacity <= 0:
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
            reason="qualified participation capacity is below one lot",
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
        fill_price = _market_projected_price(
            order=order,
            observation=observation,
            model=model,
            capacity=capacity,
            base_price=base_price,
            additional_spread_bps=additional_spread_bps,
        )

    notional = _exact_product(
        capacity,
        fill_price,
        name="execution notional",
    )
    proportional_fee = _exact_product(
        notional,
        model.fee_rate,
        name="execution fee",
    )
    fee = max(proportional_fee, model.minimum_fee)
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
