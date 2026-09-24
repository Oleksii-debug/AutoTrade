"""Alpaca non-live order adapter foundation.

The Trading API shares an order endpoint across several security types, but
their order constraints are not interchangeable. This module encodes a narrow,
fail-closed subset for equities, crypto and single-leg options without making
network requests or granting financial authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
import re

from .capabilities import CapabilitySnapshot


class AlpacaAdapterError(ValueError):
    """Raised when an Alpaca order cannot be represented safely."""


ALPACA_DOCS = MappingProxyType(
    {
        "orders": "https://docs.alpaca.markets/us/reference/postorder",
        "options": "https://docs.alpaca.markets/us/docs/options-trading",
        "paper": "https://docs.alpaca.markets/us/docs/paper-trading",
        "activities": "https://docs.alpaca.markets/eu/docs/activities",
    }
)

_ASSET_CLASSES = frozenset({"EQUITY", "CRYPTO", "OPTION"})
_SIDES = frozenset({"BUY", "SELL"})
_CLIENT_ID = re.compile(r"^[\x21-\x7e]{1,128}$")
_ORDER_TYPES = {
    "EQUITY": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
    "CRYPTO": frozenset({"MARKET", "LIMIT", "STOP_LIMIT"}),
    "OPTION": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
}
_TIFS = {
    "EQUITY": frozenset({"DAY", "GTC"}),
    "CRYPTO": frozenset({"GTC", "IOC"}),
    "OPTION": frozenset({"DAY", "GTC"}),
}


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlpacaAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise AlpacaAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AlpacaAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise AlpacaAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise AlpacaAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_client_order_id(value: str) -> str:
    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise AlpacaAdapterError("client_order_id must be printable ASCII of at most 128 characters")
    return client_id


@dataclass(frozen=True)
class AlpacaOrderIntent:
    instrument_version: str
    asset_class: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    extended_hours: bool = False
    position_intent: str | None = None

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        asset_class: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str,
        quantity=None,
        notional=None,
        limit_price=None,
        stop_price=None,
        extended_hours: bool = False,
        position_intent: str | None = None,
    ) -> "AlpacaOrderIntent":
        asset = _text(asset_class, name="asset_class").upper()
        side_value = _text(side, name="side").upper()
        order = _text(order_type, name="order_type").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if asset not in _ASSET_CLASSES:
            raise AlpacaAdapterError("unsupported asset_class")
        if side_value not in _SIDES:
            raise AlpacaAdapterError("side must be BUY or SELL")
        if order not in _ORDER_TYPES[asset]:
            raise AlpacaAdapterError(f"{order} is not admitted for {asset}")
        if tif not in _TIFS[asset]:
            raise AlpacaAdapterError(f"{tif} is not admitted for {asset}")
        if type(extended_hours) is not bool:
            raise AlpacaAdapterError("extended_hours must be boolean")

        qty = None if quantity is None else _decimal(quantity, name="quantity", positive=True)
        notion = None if notional is None else _decimal(notional, name="notional", positive=True)
        if (qty is None) == (notion is None):
            raise AlpacaAdapterError("exactly one of quantity or notional is required")
        if notion is not None:
            if asset == "OPTION":
                raise AlpacaAdapterError("options cannot use notional sizing")
            if order not in {"MARKET", "LIMIT"} or tif != "DAY":
                raise AlpacaAdapterError("notional sizing is admitted only for DAY market/limit orders")
        if asset == "OPTION" and qty != qty.to_integral_value():
            raise AlpacaAdapterError("option quantity must be a whole number of contracts")

        limit = None if limit_price is None else _decimal(limit_price, name="limit_price", positive=True)
        stop = None if stop_price is None else _decimal(stop_price, name="stop_price", positive=True)
        if order in {"LIMIT", "STOP_LIMIT"} and limit is None:
            raise AlpacaAdapterError("limit_price is required for limit-style orders")
        if order not in {"LIMIT", "STOP_LIMIT"} and limit is not None:
            raise AlpacaAdapterError("limit_price is not valid for this order type")
        if order in {"STOP", "STOP_LIMIT"} and stop is None:
            raise AlpacaAdapterError("stop_price is required for stop orders")
        if order not in {"STOP", "STOP_LIMIT"} and stop is not None:
            raise AlpacaAdapterError("stop_price is not valid for this order type")

        if extended_hours:
            if asset != "EQUITY" or order != "LIMIT" or tif != "DAY":
                raise AlpacaAdapterError(
                    "extended_hours is conservatively admitted only for DAY equity limit orders"
                )

        normalized_position_intent = None
        if position_intent is not None:
            normalized_position_intent = _text(position_intent, name="position_intent").lower()
            if normalized_position_intent not in {
                "buy_to_open",
                "buy_to_close",
                "sell_to_open",
                "sell_to_close",
            }:
                raise AlpacaAdapterError("unsupported position_intent")
            if asset != "OPTION":
                raise AlpacaAdapterError("position_intent is admitted only for options in this foundation")

        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            asset_class=asset,
            symbol=_text(symbol, name="symbol").upper(),
            side=side_value,
            order_type=order,
            time_in_force=tif,
            quantity=qty,
            notional=notion,
            limit_price=limit,
            stop_price=stop,
            extended_hours=extended_hours,
            position_intent=normalized_position_intent,
        )


@dataclass(frozen=True)
class AlpacaPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def prepare_order_request(
    intent: AlpacaOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> AlpacaPreparedRequest:
    if not isinstance(intent, AlpacaOrderIntent):
        raise TypeError("intent must be AlpacaOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "ALPACA":
        raise AlpacaAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise AlpacaAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise AlpacaAdapterError("exact capability evidence does not admit this order")

    body: dict[str, object] = {
        "symbol": intent.symbol,
        "side": intent.side.lower(),
        "type": intent.order_type.lower(),
        "time_in_force": intent.time_in_force.lower(),
        "client_order_id": client_id,
        "extended_hours": intent.extended_hours,
    }
    if intent.quantity is not None:
        body["qty"] = _decimal_text(intent.quantity)
    else:
        body["notional"] = _decimal_text(intent.notional)
    if intent.limit_price is not None:
        body["limit_price"] = _decimal_text(intent.limit_price)
    if intent.stop_price is not None:
        body["stop_price"] = _decimal_text(intent.stop_price)
    if intent.position_intent is not None:
        body["position_intent"] = intent.position_intent

    return AlpacaPreparedRequest(
        endpoint="/v2/orders",
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(ALPACA_DOCS.values()),
    )


@dataclass(frozen=True)
class AlpacaOrderObservation:
    provider_order_id: str
    client_order_id: str
    symbol: str
    provider_status: str
    filled_quantity: Decimal
    average_fill_price: Decimal | None

    @property
    def proves_economic_fill(self) -> bool:
        """Order state alone is not the economic fill identity authority."""

        return False


def parse_order_observation(payload: Mapping[str, object]) -> AlpacaOrderObservation:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    provider_order_id = _text(str(payload.get("id", "")), name="id")
    client_id = validate_client_order_id(str(payload.get("client_order_id", "")))
    symbol = _text(str(payload.get("symbol", "")), name="symbol").upper()
    status = _text(str(payload.get("status", "")), name="status").lower()
    filled = _decimal(payload.get("filled_qty", "0"), name="filled_qty")
    if filled < 0:
        raise AlpacaAdapterError("filled_qty cannot be negative")
    average_value = payload.get("filled_avg_price")
    average = None
    if average_value not in (None, ""):
        average = _decimal(average_value, name="filled_avg_price", positive=True)
        if filled == 0:
            raise AlpacaAdapterError("filled_avg_price cannot exist when filled_qty is zero")
    return AlpacaOrderObservation(
        provider_order_id=provider_order_id,
        client_order_id=client_id,
        symbol=symbol,
        provider_status=status,
        filled_quantity=filled,
        average_fill_price=average,
    )


@dataclass(frozen=True)
class AlpacaAbsenceEvidence:
    by_client_order_id_complete: bool
    orders_history_complete: bool
    trade_events_complete: bool
    activities_complete: bool
    consistency_horizon_satisfied: bool
    order_found: bool

    def __post_init__(self) -> None:
        for field in (
            "by_client_order_id_complete",
            "orders_history_complete",
            "trade_events_complete",
            "activities_complete",
            "consistency_horizon_satisfied",
            "order_found",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.by_client_order_id_complete
            and self.orders_history_complete
            and self.trade_events_complete
            and self.activities_complete
            and self.consistency_horizon_satisfied
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


def paper_evidence_proves_live_execution_realism() -> bool:
    """Paper omits important live effects and cannot prove live execution realism."""

    return False
