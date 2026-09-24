"""WhiteBIT non-live adapter contract foundation.

This module deliberately does not perform network requests or hold credentials.
It translates already-admitted AutoTrade intents into provider-shaped request
payloads and normalizes provider evidence. Live authority remains with the
guarded dispatcher and exact capability evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
import re

from .capabilities import CapabilitySnapshot
from .reconciliation import ProviderFillEvidence


class WhiteBitAdapterError(ValueError):
    """Raised when WhiteBIT-specific input cannot be represented safely."""


WHITEBIT_OFFICIAL_DOCS = MappingProxyType(
    {
        "api": "https://docs.whitebit.com/api-reference/overview",
        "order_types": "https://docs.whitebit.com/concepts/order-types",
        "client_order_id": "https://docs.whitebit.com/guides/client-order-id",
    }
)

_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PRODUCTS = frozenset({"SPOT", "COLLATERAL", "FUTURES"})
_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"})
_SIDES = frozenset({"BUY", "SELL"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WhiteBitAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise WhiteBitAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise WhiteBitAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise WhiteBitAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise WhiteBitAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WhiteBitAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_client_order_id(value: str) -> str:
    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise WhiteBitAdapterError(
            "client_order_id must be 1-64 characters using only letters, numbers, dash, dot or underscore"
        )
    return client_id


@dataclass(frozen=True)
class WhiteBitOrderIntent:
    instrument_version: str
    product_family: str
    market: str
    side: str
    order_type: str
    amount: Decimal
    price: Decimal | None = None
    activation_price: Decimal | None = None
    time_in_force: str = "GTC"
    post_only: bool = False
    reduce_only: bool = False
    position_side: str | None = None

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        product_family: str,
        market: str,
        side: str,
        order_type: str,
        amount,
        price=None,
        activation_price=None,
        time_in_force: str = "GTC",
        post_only: bool = False,
        reduce_only: bool = False,
        position_side: str | None = None,
    ) -> "WhiteBitOrderIntent":
        product = _text(product_family, name="product_family").upper()
        order = _text(order_type, name="order_type").upper()
        side_value = _text(side, name="side").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if product not in _PRODUCTS:
            raise WhiteBitAdapterError("unsupported WhiteBIT product family")
        if order not in _ORDER_TYPES:
            raise WhiteBitAdapterError("unsupported WhiteBIT order type")
        if side_value not in _SIDES:
            raise WhiteBitAdapterError("side must be BUY or SELL")
        if tif not in {"GTC", "IOC"}:
            raise WhiteBitAdapterError("time_in_force must be GTC or IOC")
        if type(post_only) is not bool or type(reduce_only) is not bool:
            raise WhiteBitAdapterError("post_only and reduce_only must be boolean")
        amount_value = _decimal(amount, name="amount", positive=True)
        price_value = None if price is None else _decimal(price, name="price", positive=True)
        activation_value = (
            None
            if activation_price is None
            else _decimal(activation_price, name="activation_price", positive=True)
        )
        if order in {"LIMIT", "STOP_LIMIT"} and price_value is None:
            raise WhiteBitAdapterError("price is required for limit orders")
        if order in {"MARKET", "STOP_MARKET"} and price_value is not None:
            raise WhiteBitAdapterError("price is not valid for market orders")
        if order.startswith("STOP_") and activation_value is None:
            raise WhiteBitAdapterError("activation_price is required for stop orders")
        if not order.startswith("STOP_") and activation_value is not None:
            raise WhiteBitAdapterError("activation_price is only valid for stop orders")
        if reduce_only and product == "SPOT":
            raise WhiteBitAdapterError("reduce_only is not available for spot orders")
        if tif == "IOC" and not (product == "SPOT" and order == "LIMIT"):
            raise WhiteBitAdapterError("IOC is admitted only for spot limit orders in this adapter foundation")
        if post_only and order not in {"LIMIT", "STOP_LIMIT"}:
            raise WhiteBitAdapterError("post_only is valid only for limit-style orders")
        if post_only and tif == "IOC":
            raise WhiteBitAdapterError("post_only and IOC are mutually exclusive")
        normalized_position_side = None
        if position_side is not None:
            normalized_position_side = _text(position_side, name="position_side").upper()
            if normalized_position_side not in {"LONG", "SHORT"}:
                raise WhiteBitAdapterError("position_side must be LONG or SHORT")
            if product == "SPOT":
                raise WhiteBitAdapterError("position_side is not valid for spot")
        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            product_family=product,
            market=_text(market, name="market").upper(),
            side=side_value,
            order_type=order,
            amount=amount_value,
            price=price_value,
            activation_price=activation_value,
            time_in_force=tif,
            post_only=post_only,
            reduce_only=reduce_only,
            position_side=normalized_position_side,
        )


@dataclass(frozen=True)
class WhiteBitPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


_SPOT_ENDPOINTS = {
    "MARKET": "/api/v4/order/market",
    "LIMIT": "/api/v4/order/new",
    "STOP_MARKET": "/api/v4/order/stop_market",
    "STOP_LIMIT": "/api/v4/order/stop_limit",
}
_COLLATERAL_ENDPOINTS = {
    "MARKET": "/api/v4/order/collateral/market",
    "LIMIT": "/api/v4/order/collateral/limit",
    "STOP_MARKET": "/api/v4/order/collateral/trigger-market",
    "STOP_LIMIT": "/api/v4/order/collateral/stop-limit",
}


def prepare_order_request(
    intent: WhiteBitOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> WhiteBitPreparedRequest:
    """Translate an admitted intent without sending it.

    `request` and `nonce` are intentionally not added here. A transport wrapper
    must add provider authentication fields after the guarded dispatcher's final
    send barrier, so a stale signed payload cannot become a second send authority.
    """

    if not isinstance(intent, WhiteBitOrderIntent):
        raise TypeError("intent must be WhiteBitOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "WHITEBIT":
        raise WhiteBitAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise WhiteBitAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise WhiteBitAdapterError("exact capability evidence does not admit this order")

    endpoints = _SPOT_ENDPOINTS if intent.product_family == "SPOT" else _COLLATERAL_ENDPOINTS
    body: dict[str, object] = {
        "market": intent.market,
        "side": intent.side.lower(),
        "amount": _decimal_text(intent.amount),
        "clientOrderId": client_id,
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.activation_price is not None:
        body["activation_price"] = _decimal_text(intent.activation_price)
    if intent.order_type in {"LIMIT", "STOP_LIMIT"}:
        body["postOnly"] = intent.post_only
    if intent.time_in_force == "IOC":
        body["ioc"] = True
    if intent.product_family != "SPOT":
        body["reduceOnly"] = intent.reduce_only
        if intent.position_side is not None:
            body["positionSide"] = intent.position_side

    return WhiteBitPreparedRequest(
        endpoint=endpoints[intent.order_type],
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(WHITEBIT_OFFICIAL_DOCS.values()),
    )


@dataclass(frozen=True)
class WhiteBitOrderSnapshot:
    provider_order_id: str
    client_order_id: str | None
    market: str
    provider_status: str
    normalized_status: str
    provider_amount: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal | None
    deal_money: Decimal | None
    average_fill_price: Decimal | None
    terminal_remainder_cancelled: bool
    reduce_only: bool | None


_PROVIDER_STATUSES = frozenset(
    {
        "NEW",
        "FILLED",
        "PARTIALLY_FILLED",
        "CANCELED",
        "CANCELED_TAKER_BAND",
        "AUTO_CANCELED_REDUCE_ONLY",
        "AUTO_CANCELED_LIQUIDATION",
        "CANCELED_STP",
    }
)


def parse_order_snapshot(payload: Mapping[str, object]) -> WhiteBitOrderSnapshot:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    provider_order_id = _text(str(payload.get("orderId", "")), name="orderId")
    raw_client_order_id = payload.get("clientOrderId")
    client_order_id = (
        None
        if raw_client_order_id in {None, ""}
        else validate_client_order_id(str(raw_client_order_id))
    )
    market = _text(str(payload.get("market", "")), name="market").upper()
    status = _text(str(payload.get("status", "")), name="status").upper()
    if status not in _PROVIDER_STATUSES:
        raise WhiteBitAdapterError(f"unsupported provider order status: {status}")

    amount = _decimal(payload.get("amount"), name="amount", positive=True)
    filled = _decimal(payload.get("dealStock"), name="dealStock")
    if filled < 0 or filled > amount:
        raise WhiteBitAdapterError("filled quantity must be between zero and provider amount")
    left_value = payload.get("left")
    remaining = None if left_value is None else _decimal(left_value, name="left")
    if remaining is not None and (remaining < 0 or remaining > amount):
        raise WhiteBitAdapterError("remaining quantity must be between zero and provider amount")
    deal_money_value = payload.get("dealMoney")
    deal_money = None if deal_money_value is None else _decimal(deal_money_value, name="dealMoney")
    if deal_money is not None and deal_money < 0:
        raise WhiteBitAdapterError("dealMoney cannot be negative")
    average = None
    if filled > 0 and deal_money is not None:
        average = deal_money / filled

    if status == "FILLED":
        normalized = "FILLED"
        cancelled_remainder = False
    elif status == "PARTIALLY_FILLED":
        normalized = "PARTIALLY_FILLED"
        cancelled_remainder = False
    elif status == "NEW":
        normalized = "WORKING"
        cancelled_remainder = False
    else:
        normalized = "PARTIALLY_FILLED_CANCELLED" if filled > 0 else "CANCELLED"
        cancelled_remainder = True

    reduce_only_value = payload.get("reduceOnly")
    if reduce_only_value is not None and type(reduce_only_value) is not bool:
        raise WhiteBitAdapterError("reduceOnly must be boolean when present")

    return WhiteBitOrderSnapshot(
        provider_order_id=provider_order_id,
        client_order_id=client_order_id,
        market=market,
        provider_status=status,
        normalized_status=normalized,
        provider_amount=amount,
        filled_quantity=filled,
        remaining_quantity=remaining,
        deal_money=deal_money,
        average_fill_price=average,
        terminal_remainder_cancelled=cancelled_remainder,
        reduce_only=reduce_only_value,
    )


@dataclass(frozen=True)
class WhiteBitLookupRequest:
    surface: str
    endpoint: str
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def order_lookup_requests(*, market: str, client_order_id: str) -> tuple[WhiteBitLookupRequest, ...]:
    """Return exact-ID order lookup requests; these alone never prove absence."""

    market_value = _text(market, name="market").upper()
    client_id = validate_client_order_id(client_order_id)
    body = {"market": market_value, "clientOrderId": client_id}
    return (
        WhiteBitLookupRequest("OPEN_ORDERS", "/api/v4/orders", body),
        WhiteBitLookupRequest("ORDER_HISTORY", "/api/v4/trade-account/order/history", body),
    )


@dataclass(frozen=True)
class WhiteBitAbsenceEvidence:
    order_found: bool
    open_orders_complete: bool
    order_history_complete: bool
    executions_complete: bool
    activities_complete: bool
    consistency_horizon_satisfied: bool

    def __post_init__(self) -> None:
        for field in (
            "order_found",
            "open_orders_complete",
            "order_history_complete",
            "executions_complete",
            "activities_complete",
            "consistency_horizon_satisfied",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.open_orders_complete
            and self.order_history_complete
            and self.executions_complete
            and self.activities_complete
            and self.consistency_horizon_satisfied
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


def _unix_instant(value, *, name: str) -> str:
    """Convert an exact Unix timestamp to UTC without binary-float rounding."""
    instant = _decimal(value, name=name)
    if instant < 0:
        raise WhiteBitAdapterError(f"{name} cannot be negative")
    whole = int(instant)
    fractional = instant - Decimal(whole)
    microseconds = fractional * Decimal("1000000")
    if microseconds != microseconds.to_integral_value():
        raise WhiteBitAdapterError(f"{name} exceeds microsecond precision")
    parsed = datetime.fromtimestamp(whole, tz=timezone.utc).replace(
        microsecond=int(microseconds)
    )
    return parsed.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WhiteBitExecutionDeal:
    provider_execution_id: str
    provider_order_id: str
    client_order_id: str | None
    market: str
    side: str
    role: str
    quantity: Decimal
    price: Decimal
    deal_value: Decimal
    fee_amount: Decimal
    fee_currency: str
    trade_time: str

    def to_reconciliation_fill(self) -> ProviderFillEvidence:
        return ProviderFillEvidence.create(
            provider_execution_id=self.provider_execution_id,
            client_order_id=self.client_order_id,
            instrument=self.market,
            quantity=self.quantity,
            price=self.price,
            fee_amount=self.fee_amount,
            fee_currency=self.fee_currency,
            trade_time=self.trade_time,
        )


def parse_execution_deal(
    payload: Mapping[str, object],
    *,
    market: str,
) -> WhiteBitExecutionDeal:
    """Normalize one provider deal; the deal id is the unique fill identity."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    required = (
        "id",
        "orderId",
        "time",
        "side",
        "role",
        "amount",
        "price",
        "deal",
        "fee",
        "feeAsset",
    )
    missing = [name for name in required if name not in payload]
    if missing:
        raise WhiteBitAdapterError(
            "execution deal missing required fields: " + ", ".join(missing)
        )

    side = _text(str(payload["side"]), name="side").upper()
    if side not in _SIDES:
        raise WhiteBitAdapterError("execution side must be BUY or SELL")

    raw_role = payload["role"]
    if raw_role == 1 or raw_role == "1":
        role = "MAKER"
    elif raw_role == 2 or raw_role == "2":
        role = "TAKER"
    else:
        raise WhiteBitAdapterError("execution role must be 1 (maker) or 2 (taker)")

    quantity = _decimal(payload["amount"], name="amount", positive=True)
    price = _decimal(payload["price"], name="price", positive=True)
    deal_value = _decimal(payload["deal"], name="deal", positive=True)
    if deal_value != quantity * price:
        raise WhiteBitAdapterError(
            "execution deal value must equal exact amount multiplied by price"
        )
    fee = _decimal(payload["fee"], name="fee")
    if fee < 0:
        raise WhiteBitAdapterError("execution fee cannot be negative")

    raw_client_id = payload.get("clientOrderId")
    client_order_id = (
        None
        if raw_client_id in {None, ""}
        else validate_client_order_id(str(raw_client_id))
    )

    return WhiteBitExecutionDeal(
        provider_execution_id=_text(
            str(payload["id"]),
            name="execution id",
        ),
        provider_order_id=_text(
            str(payload["orderId"]),
            name="order id",
        ),
        client_order_id=client_order_id,
        market=_text(market, name="market").upper(),
        side=side,
        role=role,
        quantity=quantity,
        price=price,
        deal_value=deal_value,
        fee_amount=fee,
        fee_currency=_text(str(payload["feeAsset"]), name="feeAsset").upper(),
        trade_time=_unix_instant(payload["time"], name="time"),
    )


def parse_execution_history(
    records: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
    *,
    market: str,
) -> tuple[WhiteBitExecutionDeal, ...]:
    """Deduplicate exact repeated deal observations and reject contradictions."""
    if not isinstance(records, (list, tuple)):
        raise TypeError("records must be a list or tuple")
    by_id: dict[str, WhiteBitExecutionDeal] = {}
    for payload in records:
        deal = parse_execution_deal(payload, market=market)
        existing = by_id.get(deal.provider_execution_id)
        if existing is not None:
            if existing != deal:
                raise WhiteBitAdapterError(
                    "provider execution id has conflicting observations"
                )
            continue
        by_id[deal.provider_execution_id] = deal
    return tuple(by_id[key] for key in sorted(by_id))


@dataclass(frozen=True)
class WhiteBitPageEvidence:
    offset: int
    limit: int
    record_count: int

    def __post_init__(self) -> None:
        for name in ("offset", "limit", "record_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise WhiteBitAdapterError(f"{name} must be an integer")
        if self.offset < 0:
            raise WhiteBitAdapterError("offset cannot be negative")
        if self.limit < 1:
            raise WhiteBitAdapterError("limit must be positive")
        if self.record_count < 0 or self.record_count > self.limit:
            raise WhiteBitAdapterError(
                "record_count must be between zero and the page limit"
            )

    @property
    def proves_last_page(self) -> bool:
        return self.record_count < self.limit


class WhiteBitPaginationCoverage:
    """Contiguous offset pagination proof for one provider surface."""

    def __init__(self, *, maximum_limit: int, initial_offset: int = 0) -> None:
        if (
            not isinstance(maximum_limit, int)
            or isinstance(maximum_limit, bool)
            or maximum_limit < 1
        ):
            raise WhiteBitAdapterError("maximum_limit must be positive")
        if (
            not isinstance(initial_offset, int)
            or isinstance(initial_offset, bool)
            or initial_offset < 0
        ):
            raise WhiteBitAdapterError("initial_offset must be non-negative")
        self.maximum_limit = maximum_limit
        self.initial_offset = initial_offset
        self._pages: list[WhiteBitPageEvidence] = []

    def add_page(self, page: WhiteBitPageEvidence) -> None:
        if not isinstance(page, WhiteBitPageEvidence):
            raise TypeError("page must be WhiteBitPageEvidence")
        if page.limit > self.maximum_limit:
            raise WhiteBitAdapterError(
                f"page limit cannot exceed {self.maximum_limit}"
            )
        if self.complete:
            raise WhiteBitAdapterError("pagination coverage is already complete")
        expected = (
            self.initial_offset
            if not self._pages
            else self._pages[-1].offset + self._pages[-1].limit
        )
        if page.offset != expected:
            raise WhiteBitAdapterError(
                f"pagination gap: expected offset {expected}"
            )
        self._pages.append(page)

    @property
    def complete(self) -> bool:
        return bool(self._pages and self._pages[-1].proves_last_page)

    @property
    def next_offset(self) -> int:
        if not self._pages:
            return self.initial_offset
        return self._pages[-1].offset + self._pages[-1].limit

    @property
    def pages(self) -> tuple[WhiteBitPageEvidence, ...]:
        return tuple(self._pages)


def order_history_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(maximum_limit=500)


def execution_history_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(maximum_limit=500)


def open_order_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(maximum_limit=100)


def _history_window(*, start_unix: int, end_unix: int) -> tuple[int, int]:
    for value, name in ((start_unix, "start_unix"), (end_unix, "end_unix")):
        if not isinstance(value, int) or isinstance(value, bool):
            raise WhiteBitAdapterError(f"{name} must be an integer")
        if value < 0:
            raise WhiteBitAdapterError(f"{name} cannot be negative")
    if end_unix < start_unix:
        raise WhiteBitAdapterError("end_unix must not precede start_unix")
    if end_unix - start_unix > 31 * 24 * 60 * 60:
        raise WhiteBitAdapterError(
            "WhiteBIT history request window cannot exceed 31 days"
        )
    return start_unix, end_unix


def paged_order_history_request(
    *,
    start_unix: int,
    end_unix: int,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    _history_window(start_unix=start_unix, end_unix=end_unix)
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 500:
        raise WhiteBitAdapterError("order-history limit cannot exceed 500")
    body: dict[str, object] = {
        "startDate": start_unix,
        "endDate": end_unix,
        "offset": offset,
        "limit": limit,
    }
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "ORDER_HISTORY",
        "/api/v4/trade-account/order/history",
        body,
    )


def paged_execution_history_request(
    *,
    start_unix: int,
    end_unix: int,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    _history_window(start_unix=start_unix, end_unix=end_unix)
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 500:
        raise WhiteBitAdapterError("execution-history limit cannot exceed 500")
    body: dict[str, object] = {
        "startDate": start_unix,
        "endDate": end_unix,
        "offset": offset,
        "limit": limit,
    }
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "EXECUTIONS",
        "/api/v4/trade-account/executed-history",
        body,
    )


def paged_open_orders_request(
    *,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 100:
        raise WhiteBitAdapterError("open-orders limit cannot exceed 100")
    body: dict[str, object] = {"offset": offset, "limit": limit}
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "OPEN_ORDERS",
        "/api/v4/orders",
        body,
    )
