"""Binance Spot non-live adapter contract foundation.

This module intentionally covers Spot only. Margin, USD-M, COIN-M and Options
remain separate product families. It prepares provider-shaped requests from
already-admitted intents but never signs, timestamps or sends them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
import re

from .capabilities import CapabilitySnapshot


class BinanceSpotAdapterError(ValueError):
    """Raised when a Binance Spot request cannot be represented safely."""


BINANCE_SPOT_DOCS = MappingProxyType(
    {
        "trade": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints",
    }
)

_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_ORDER_TYPES = frozenset({"MARKET", "LIMIT"})
_SIDES = frozenset({"BUY", "SELL"})
_TIFS = frozenset({"GTC", "IOC", "FOK"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BinanceSpotAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise BinanceSpotAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BinanceSpotAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise BinanceSpotAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise BinanceSpotAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BinanceSpotAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_client_order_id(value: str) -> str:
    """Use a conservative subset compatible with dispatcher-generated IDs."""

    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise BinanceSpotAdapterError(
            "client_order_id must be 1-32 letters, digits, dash or underscore"
        )
    return client_id


@dataclass(frozen=True)
class BinanceSpotOrderIntent:
    instrument_version: str
    symbol: str
    side: str
    order_type: str
    amount: Decimal
    market_amount_unit: str = "BASE"
    price: Decimal | None = None
    time_in_force: str | None = None

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        side: str,
        order_type: str,
        amount,
        market_amount_unit: str = "BASE",
        price=None,
        time_in_force: str | None = None,
    ) -> "BinanceSpotOrderIntent":
        side_value = _text(side, name="side").upper()
        order_value = _text(order_type, name="order_type").upper()
        unit = _text(market_amount_unit, name="market_amount_unit").upper()
        if side_value not in _SIDES:
            raise BinanceSpotAdapterError("side must be BUY or SELL")
        if order_value not in _ORDER_TYPES:
            raise BinanceSpotAdapterError("only MARKET and LIMIT are admitted by this Spot foundation")
        if unit not in {"BASE", "QUOTE"}:
            raise BinanceSpotAdapterError("market_amount_unit must be BASE or QUOTE")
        amount_value = _decimal(amount, name="amount", positive=True)
        price_value = None if price is None else _decimal(price, name="price", positive=True)

        if order_value == "LIMIT":
            if unit != "BASE":
                raise BinanceSpotAdapterError("limit order quantity must use BASE units")
            if price_value is None:
                raise BinanceSpotAdapterError("price is required for a limit order")
            if time_in_force is None:
                raise BinanceSpotAdapterError("time_in_force is required for a limit order")
            tif = _text(time_in_force, name="time_in_force").upper()
            if tif not in _TIFS:
                raise BinanceSpotAdapterError("unsupported time_in_force")
        else:
            if price_value is not None:
                raise BinanceSpotAdapterError("price must be omitted for a market order")
            if time_in_force is not None:
                raise BinanceSpotAdapterError("time_in_force must be omitted for a market order")
            tif = None

        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            symbol=_text(symbol, name="symbol").upper(),
            side=side_value,
            order_type=order_value,
            amount=amount_value,
            market_amount_unit=unit,
            price=price_value,
            time_in_force=tif,
        )


@dataclass(frozen=True)
class BinanceSpotPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def prepare_spot_order_request(
    intent: BinanceSpotOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> BinanceSpotPreparedRequest:
    """Prepare POST /api/v3/order without timestamp/signature.

    The request explicitly asks for ACK so an accepted submission is not
    confused with fill evidence. Fills are consumed from execution/account
    evidence and reconciliation.
    """

    if not isinstance(intent, BinanceSpotOrderIntent):
        raise TypeError("intent must be BinanceSpotOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "BINANCE":
        raise BinanceSpotAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise BinanceSpotAdapterError("capability instrument version does not match intent")

    capability_tif = intent.time_in_force or "GTC"
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=capability_tif,
        permission_scope="ORDER_WRITE",
    ):
        raise BinanceSpotAdapterError("exact capability evidence does not admit this order")

    body: dict[str, object] = {
        "symbol": intent.symbol,
        "side": intent.side,
        "type": intent.order_type,
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
    }
    if intent.order_type == "LIMIT":
        body["quantity"] = _decimal_text(intent.amount)
        body["price"] = _decimal_text(intent.price)
        body["timeInForce"] = intent.time_in_force
    elif intent.market_amount_unit == "BASE":
        body["quantity"] = _decimal_text(intent.amount)
    else:
        body["quoteOrderQty"] = _decimal_text(intent.amount)

    return BinanceSpotPreparedRequest(
        endpoint="/api/v3/order",
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(BINANCE_SPOT_DOCS.values()),
    )


@dataclass(frozen=True)
class BinanceSpotSubmissionAck:
    symbol: str
    provider_order_id: str
    client_order_id: str
    transaction_time_ms: int | None

    @property
    def proves_fill(self) -> bool:
        return False


def parse_ack_response(payload: Mapping[str, object]) -> BinanceSpotSubmissionAck:
    """Parse the deliberately requested ACK response only."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    symbol = _text(str(payload.get("symbol", "")), name="symbol").upper()
    order_id = _text(str(payload.get("orderId", "")), name="orderId")
    client_id = validate_client_order_id(str(payload.get("clientOrderId", "")))
    transaction = payload.get("transactTime")
    if transaction is not None and (not isinstance(transaction, int) or isinstance(transaction, bool) or transaction < 0):
        raise BinanceSpotAdapterError("transactTime must be a non-negative integer when present")
    return BinanceSpotSubmissionAck(
        symbol=symbol,
        provider_order_id=order_id,
        client_order_id=client_id,
        transaction_time_ms=transaction,
    )


@dataclass(frozen=True)
class BinanceSpotFillEvidence:
    trade_id: str
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str

    @classmethod
    def create(
        cls,
        *,
        trade_id,
        price,
        quantity,
        commission,
        commission_asset: str,
    ) -> "BinanceSpotFillEvidence":
        return cls(
            trade_id=_text(str(trade_id), name="trade_id"),
            price=_decimal(price, name="price", positive=True),
            quantity=_decimal(quantity, name="quantity", positive=True),
            commission=_decimal(commission, name="commission"),
            commission_asset=_text(commission_asset, name="commission_asset").upper(),
        )


@dataclass(frozen=True)
class BinanceSpotAbsenceEvidence:
    query_order_complete: bool
    open_orders_complete: bool
    all_orders_complete: bool
    account_trades_complete: bool
    consistency_horizon_satisfied: bool
    order_found: bool

    def __post_init__(self) -> None:
        for field in (
            "query_order_complete",
            "open_orders_complete",
            "all_orders_complete",
            "account_trades_complete",
            "consistency_horizon_satisfied",
            "order_found",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.query_order_complete
            and self.open_orders_complete
            and self.all_orders_complete
            and self.account_trades_complete
            and self.consistency_horizon_satisfied
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


def derivatives_supported_by_this_module() -> bool:
    return False
