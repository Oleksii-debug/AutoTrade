"""Fail-closed Binance USD-M Futures contract adapter foundation.

This module performs no networking, signing, credential storage or live
qualification. It prepares already-authorized canonical LIMIT/MARKET requests
and maps recorded provider observations into the existing AutoTrade
reconciliation contracts. Order acknowledgement is never execution evidence.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .capabilities import CapabilitySnapshot
from .provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


BINANCE_USDM_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "EXCHANGE_INFO": "/fapi/v1/exchangeInfo",
        "MARK_PRICE": "/fapi/v1/premiumIndex",
        "PLACE_ORDER": "/fapi/v1/order",
        "QUERY_ORDER": "/fapi/v1/order",
        "OPEN_ORDERS": "/fapi/v1/openOrders",
        "ORDER_HISTORY": "/fapi/v1/allOrders",
        "EXECUTIONS": "/fapi/v1/userTrades",
        "ACCOUNT": "/fapi/v3/account",
    }
)

BINANCE_USDM_DOCS: tuple[str, ...] = (
    "https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/trade",
    "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Exchange-Information",
    "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Mark-Price",
)

_EXCHANGE_INFO_RULES_TOKEN = object()
_MARK_PRICE_TOKEN = object()
_CLIENT_ID = re.compile(r"^[.A-Za-z0-9_:/-]{1,36}$")
_ALLOWED_TIF = frozenset({"GTC", "IOC", "FOK"})
_ALLOWED_POSITION_SIDES = frozenset({"BOTH", "LONG", "SHORT"})


class BinanceUsdmAdapterError(ProviderCoreError):
    """Raised when recorded Binance USD-M data violates the safe contract."""


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BinanceUsdmAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise BinanceUsdmAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise BinanceUsdmAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise BinanceUsdmAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise BinanceUsdmAdapterError(f"{name} must be positive")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BinanceUsdmAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _millis(value: object, *, name: str) -> str:
    if isinstance(value, bool):
        raise BinanceUsdmAdapterError(f"{name} must be an integer millisecond timestamp")
    if isinstance(value, int):
        raw = value
    elif isinstance(value, str) and value.isdigit():
        raw = int(value)
    else:
        raise BinanceUsdmAdapterError(f"{name} must be an integer millisecond timestamp")
    if raw < 0:
        raise BinanceUsdmAdapterError(f"{name} must be non-negative")
    seconds, remainder = divmod(raw, 1000)
    instant = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(milliseconds=remainder)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        return str(UUID(text))
    except ValueError as error:
        raise BinanceUsdmAdapterError(f"{name} must be a UUID") from error


def _provider_symbol(value: object, *, name: str) -> str:
    symbol = _text(value, name=name)
    if symbol != symbol.upper():
        raise BinanceUsdmAdapterError("Binance USD-M symbol must be uppercase")
    return symbol


def validate_client_order_id(value: object) -> str:
    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise BinanceUsdmAdapterError(
            "client_order_id must be 1-36 Binance-compatible characters"
        )
    return client_id


@dataclass(frozen=True)
class BinanceUsdmOrderIntent:
    instrument_version: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    time_in_force: str | None
    position_side: str
    reduce_only: bool

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        side: str,
        order_type: str,
        quantity,
        price=None,
        time_in_force: str | None = None,
        position_side: str = "BOTH",
        reduce_only: bool = False,
    ) -> "BinanceUsdmOrderIntent":
        instrument = _text(instrument_version, name="instrument_version")
        provider_symbol = _provider_symbol(symbol, name="symbol")

        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise BinanceUsdmAdapterError("side must be BUY or SELL")

        normalized_type = _text(order_type, name="order_type").upper()
        if normalized_type not in {"MARKET", "LIMIT"}:
            raise BinanceUsdmAdapterError(
                "foundation supports only MARKET and LIMIT USD-M orders"
            )

        qty = _decimal(quantity, name="quantity", positive=True)
        px = None if price is None else _decimal(price, name="price", positive=True)

        if normalized_type == "LIMIT":
            if px is None:
                raise BinanceUsdmAdapterError("LIMIT price is required")
            tif = _text(time_in_force or "GTC", name="time_in_force").upper()
            if tif not in _ALLOWED_TIF:
                raise BinanceUsdmAdapterError("unsupported time_in_force")
        else:
            if px is not None:
                raise BinanceUsdmAdapterError("MARKET order must not carry limit price")
            if time_in_force is not None:
                raise BinanceUsdmAdapterError("MARKET order must not carry time_in_force")
            tif = None

        normalized_position_side = _text(position_side, name="position_side").upper()
        if normalized_position_side not in _ALLOWED_POSITION_SIDES:
            raise BinanceUsdmAdapterError("position_side must be BOTH, LONG or SHORT")
        if type(reduce_only) is not bool:
            raise BinanceUsdmAdapterError("reduce_only must be boolean")

        return cls(
            instrument_version=instrument,
            symbol=provider_symbol,
            side=normalized_side,
            order_type=normalized_type,
            quantity=qty,
            price=px,
            time_in_force=tif,
            position_side=normalized_position_side,
            reduce_only=reduce_only,
        )


@dataclass(frozen=True)
class BinanceUsdmMarkPrice:
    """Provider-originated USD-M mark-price evidence for MARKET admission."""

    instrument_version: str
    symbol: str
    price: Decimal
    observed_at: datetime
    source_sha256: str
    _verification_token: InitVar[object | None] = None

    def __post_init__(self, _verification_token: object | None) -> None:
        if _verification_token is not _MARK_PRICE_TOKEN:
            raise BinanceUsdmAdapterError(
                "mark price must come from canonical provider payload parsing"
            )
        instrument = _text(self.instrument_version, name="instrument_version")
        symbol = _provider_symbol(self.symbol, name="mark-price symbol")
        price = _decimal(self.price, name="mark_price", positive=True)
        observed = _utc(self.observed_at, name="mark-price observed_at")
        digest = _text(self.source_sha256, name="mark-price source_sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceUsdmAdapterError(
                "mark-price source_sha256 must be canonical lowercase SHA-256"
            )
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "source_sha256", digest)

    @classmethod
    def from_premium_index(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        payload: Mapping[str, object],
    ) -> "BinanceUsdmMarkPrice":
        if not isinstance(payload, Mapping):
            raise TypeError("mark-price payload must be a mapping")
        requested_symbol = _provider_symbol(symbol, name="mark-price symbol")
        provider_symbol = _provider_symbol(
            payload.get("symbol"),
            name="mark-price payload symbol",
        )
        if provider_symbol != requested_symbol:
            raise BinanceUsdmAdapterError(
                "mark-price payload symbol does not match requested symbol"
            )
        price = _decimal(
            payload.get("markPrice"),
            name="provider mark price",
            positive=True,
        )
        timestamp = _millis(payload.get("time"), name="mark-price time")
        observed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        try:
            canonical = json.dumps(
                {
                    "endpoint": BINANCE_USDM_ENDPOINTS["MARK_PRICE"],
                    "symbol": provider_symbol,
                    "payload": dict(payload),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise BinanceUsdmAdapterError(
                "mark-price payload must contain canonical JSON-compatible values"
            ) from error
        return cls(
            instrument_version=_text(
                instrument_version,
                name="instrument_version",
            ),
            symbol=provider_symbol,
            price=price,
            observed_at=observed,
            source_sha256="sha256:" + sha256(canonical).hexdigest(),
            _verification_token=_MARK_PRICE_TOKEN,
        )


@dataclass(frozen=True)
class BinanceUsdmSymbolRules:
    """Versioned USD-M exchangeInfo filters required before order preparation."""

    instrument_version: str
    symbol: str
    status: str
    contract_type: str
    source_sha256: str
    supported_order_types: frozenset[str]
    supported_time_in_force: frozenset[str]
    lot_min_qty: Decimal
    lot_max_qty: Decimal
    lot_step_size: Decimal
    market_min_qty: Decimal | None
    market_max_qty: Decimal | None
    market_step_size: Decimal | None
    price_min: Decimal
    price_max: Decimal
    tick_size: Decimal
    min_notional: Decimal | None
    _verification_token: InitVar[object | None] = None

    def __post_init__(self, _verification_token: object | None) -> None:
        if _verification_token is not _EXCHANGE_INFO_RULES_TOKEN:
            raise BinanceUsdmAdapterError(
                "exchangeInfo rules must come from canonical provider payload parsing"
            )
        instrument = _text(self.instrument_version, name="instrument_version")
        symbol = _provider_symbol(self.symbol, name="exchangeInfo symbol")
        status = _text(self.status, name="exchangeInfo status").upper()
        contract_type = _text(
            self.contract_type,
            name="exchangeInfo contract_type",
        ).upper()
        digest = _text(self.source_sha256, name="source_sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceUsdmAdapterError(
                "source_sha256 must be canonical lowercase SHA-256"
            )
        if (
            not isinstance(self.supported_order_types, frozenset)
            or not self.supported_order_types
        ):
            raise BinanceUsdmAdapterError(
                "supported_order_types must be a non-empty frozenset"
            )
        order_types = frozenset(
            _text(value, name="supported order type").upper()
            for value in self.supported_order_types
        )
        if (
            not isinstance(self.supported_time_in_force, frozenset)
            or not self.supported_time_in_force
        ):
            raise BinanceUsdmAdapterError(
                "supported_time_in_force must be a non-empty frozenset"
            )
        time_in_force = frozenset(
            _text(value, name="supported time in force").upper()
            for value in self.supported_time_in_force
        )

        lot_min = _decimal(self.lot_min_qty, name="lot_min_qty", positive=True)
        lot_max = _decimal(self.lot_max_qty, name="lot_max_qty", positive=True)
        lot_step = _decimal(self.lot_step_size, name="lot_step_size", positive=True)
        if lot_min > lot_max:
            raise BinanceUsdmAdapterError("LOT_SIZE minQty exceeds maxQty")

        price_min = _decimal(self.price_min, name="price_min")
        price_max = _decimal(self.price_max, name="price_max")
        tick = _decimal(self.tick_size, name="tick_size")
        if (
            price_min < 0
            or price_max < 0
            or tick < 0
            or (price_max and price_min > price_max)
        ):
            raise BinanceUsdmAdapterError("PRICE_FILTER bounds are invalid")

        for name in ("market_min_qty", "market_max_qty", "market_step_size"):
            value = getattr(self, name)
            if value is not None:
                normalized = _decimal(value, name=name, positive=True)
                object.__setattr__(self, name, normalized)
        if (
            self.market_min_qty is not None
            and self.market_max_qty is not None
            and self.market_min_qty > self.market_max_qty
        ):
            raise BinanceUsdmAdapterError(
                "MARKET_LOT_SIZE minQty exceeds maxQty"
            )

        min_notional = self.min_notional
        if min_notional is not None:
            min_notional = _decimal(
                min_notional,
                name="min_notional",
                positive=True,
            )

        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "contract_type", contract_type)
        object.__setattr__(self, "source_sha256", digest)
        object.__setattr__(self, "supported_order_types", order_types)
        object.__setattr__(self, "supported_time_in_force", time_in_force)
        object.__setattr__(self, "lot_min_qty", lot_min)
        object.__setattr__(self, "lot_max_qty", lot_max)
        object.__setattr__(self, "lot_step_size", lot_step)
        object.__setattr__(self, "price_min", price_min)
        object.__setattr__(self, "price_max", price_max)
        object.__setattr__(self, "tick_size", tick)
        object.__setattr__(self, "min_notional", min_notional)

    @classmethod
    def from_exchange_info(
        cls,
        *,
        instrument_version: str,
        symbol_payload: Mapping[str, object],
    ) -> "BinanceUsdmSymbolRules":
        if not isinstance(symbol_payload, Mapping):
            raise TypeError("symbol_payload must be a mapping")
        instrument = _text(instrument_version, name="instrument_version")
        symbol = _provider_symbol(
            symbol_payload.get("symbol"),
            name="exchangeInfo symbol",
        )
        status = _text(
            symbol_payload.get("status"),
            name="exchangeInfo status",
        ).upper()
        contract_type = _text(
            symbol_payload.get("contractType"),
            name="exchangeInfo contractType",
        ).upper()

        raw_order_types = symbol_payload.get("orderTypes")
        if (
            isinstance(raw_order_types, (str, bytes))
            or not isinstance(raw_order_types, list)
            or not raw_order_types
        ):
            raise BinanceUsdmAdapterError(
                "exchangeInfo orderTypes must be a non-empty array"
            )
        order_types = frozenset(
            _text(value, name="exchangeInfo orderType").upper()
            for value in raw_order_types
        )
        if len(order_types) != len(raw_order_types):
            raise BinanceUsdmAdapterError(
                "exchangeInfo orderTypes must be unique"
            )

        raw_tif = symbol_payload.get("timeInForce")
        if (
            isinstance(raw_tif, (str, bytes))
            or not isinstance(raw_tif, list)
            or not raw_tif
        ):
            raise BinanceUsdmAdapterError(
                "exchangeInfo timeInForce must be a non-empty array"
            )
        time_in_force = frozenset(
            _text(value, name="exchangeInfo timeInForce").upper()
            for value in raw_tif
        )
        if len(time_in_force) != len(raw_tif):
            raise BinanceUsdmAdapterError(
                "exchangeInfo timeInForce values must be unique"
            )

        filters = symbol_payload.get("filters")
        if isinstance(filters, (str, bytes)) or not isinstance(filters, list):
            raise BinanceUsdmAdapterError(
                "exchangeInfo filters must be an array"
            )
        by_type: dict[str, Mapping[str, object]] = {}
        for item in filters:
            if not isinstance(item, Mapping):
                raise BinanceUsdmAdapterError(
                    "exchangeInfo filter must be an object"
                )
            kind = _text(item.get("filterType"), name="filterType").upper()
            if kind in by_type:
                raise BinanceUsdmAdapterError(
                    f"duplicate exchangeInfo filter: {kind}"
                )
            by_type[kind] = item
        for required in ("PRICE_FILTER", "LOT_SIZE"):
            if required not in by_type:
                raise BinanceUsdmAdapterError(
                    f"exchangeInfo is missing required {required} filter"
                )

        price_filter = by_type["PRICE_FILTER"]
        price_min = _decimal(price_filter.get("minPrice"), name="minPrice")
        price_max = _decimal(price_filter.get("maxPrice"), name="maxPrice")
        tick_size = _decimal(price_filter.get("tickSize"), name="tickSize")
        if (
            price_min < 0
            or price_max < 0
            or tick_size < 0
            or (price_max and price_min > price_max)
        ):
            raise BinanceUsdmAdapterError(
                "PRICE_FILTER bounds are invalid"
            )

        lot_filter = by_type["LOT_SIZE"]
        lot_min = _decimal(
            lot_filter.get("minQty"),
            name="minQty",
            positive=True,
        )
        lot_max = _decimal(
            lot_filter.get("maxQty"),
            name="maxQty",
            positive=True,
        )
        lot_step = _decimal(
            lot_filter.get("stepSize"),
            name="stepSize",
            positive=True,
        )
        if lot_min > lot_max:
            raise BinanceUsdmAdapterError(
                "LOT_SIZE minQty exceeds maxQty"
            )

        market_min = market_max = market_step = None
        market_filter = by_type.get("MARKET_LOT_SIZE")
        if market_filter is not None:
            raw_min = _decimal(
                market_filter.get("minQty"),
                name="market minQty",
            )
            raw_max = _decimal(
                market_filter.get("maxQty"),
                name="market maxQty",
            )
            raw_step = _decimal(
                market_filter.get("stepSize"),
                name="market stepSize",
            )
            if raw_min < 0 or raw_max < 0 or raw_step < 0:
                raise BinanceUsdmAdapterError(
                    "MARKET_LOT_SIZE values cannot be negative"
                )
            if raw_max and raw_min > raw_max:
                raise BinanceUsdmAdapterError(
                    "MARKET_LOT_SIZE minQty exceeds maxQty"
                )
            market_min = raw_min if raw_min > 0 else None
            market_max = raw_max if raw_max > 0 else None
            market_step = raw_step if raw_step > 0 else None

        min_notional = None
        notional_filter = by_type.get("MIN_NOTIONAL")
        if notional_filter is not None:
            min_notional = _decimal(
                notional_filter.get("notional"),
                name="MIN_NOTIONAL notional",
                positive=True,
            )

        try:
            canonical = json.dumps(
                symbol_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise BinanceUsdmAdapterError(
                "exchangeInfo symbol payload must contain canonical JSON-compatible values"
            ) from error
        return cls(
            instrument_version=instrument,
            symbol=symbol,
            status=status,
            contract_type=contract_type,
            source_sha256="sha256:" + sha256(canonical).hexdigest(),
            supported_order_types=order_types,
            supported_time_in_force=time_in_force,
            lot_min_qty=lot_min,
            lot_max_qty=lot_max,
            lot_step_size=lot_step,
            market_min_qty=market_min,
            market_max_qty=market_max,
            market_step_size=market_step,
            price_min=price_min,
            price_max=price_max,
            tick_size=tick_size,
            min_notional=min_notional,
            _verification_token=_EXCHANGE_INFO_RULES_TOKEN,
        )

    @staticmethod
    def _require_step(
        value: Decimal,
        step: Decimal | None,
        *,
        minimum: Decimal | None,
        name: str,
    ) -> None:
        if step in (None, Decimal("0")):
            return
        origin = Decimal("0") if minimum is None else minimum
        if (value - origin) % step != 0:
            raise BinanceUsdmAdapterError(
                f"{name} does not align with exchangeInfo minimum/step"
            )

    def validate(
        self,
        intent: BinanceUsdmOrderIntent,
        *,
        at: datetime,
        mark_price: BinanceUsdmMarkPrice | None = None,
        maximum_mark_price_age_seconds: int | None = None,
    ) -> BinanceUsdmMarkPrice | None:
        if not isinstance(intent, BinanceUsdmOrderIntent):
            raise TypeError("intent must be BinanceUsdmOrderIntent")
        if self.instrument_version != intent.instrument_version:
            raise BinanceUsdmAdapterError(
                "exchangeInfo rules instrument version does not match intent"
            )
        if self.symbol != intent.symbol:
            raise BinanceUsdmAdapterError(
                "exchangeInfo rules symbol does not match intent"
            )
        if self.status != "TRADING":
            raise BinanceUsdmAdapterError(
                "exchangeInfo symbol is not TRADING"
            )
        if intent.order_type not in self.supported_order_types:
            raise BinanceUsdmAdapterError(
                "exchangeInfo does not support requested order type"
            )
        if (
            intent.time_in_force is not None
            and intent.time_in_force not in self.supported_time_in_force
        ):
            raise BinanceUsdmAdapterError(
                "exchangeInfo does not support requested time_in_force"
            )

        if intent.order_type == "MARKET" and any(
            value is not None
            for value in (
                self.market_min_qty,
                self.market_max_qty,
                self.market_step_size,
            )
        ):
            min_qty = self.market_min_qty
            max_qty = self.market_max_qty
            step = self.market_step_size
        else:
            min_qty = self.lot_min_qty
            max_qty = self.lot_max_qty
            step = self.lot_step_size

        if min_qty is not None and intent.quantity < min_qty:
            raise BinanceUsdmAdapterError(
                "quantity is below exchangeInfo minimum"
            )
        if max_qty is not None and intent.quantity > max_qty:
            raise BinanceUsdmAdapterError(
                "quantity exceeds exchangeInfo maximum"
            )
        self._require_step(
            intent.quantity,
            step,
            minimum=min_qty,
            name="quantity",
        )

        if intent.order_type == "LIMIT":
            if intent.price is None:
                raise BinanceUsdmAdapterError("LIMIT price is required")
            if self.price_min > 0 and intent.price < self.price_min:
                raise BinanceUsdmAdapterError(
                    "price is below exchangeInfo minimum"
                )
            if self.price_max > 0 and intent.price > self.price_max:
                raise BinanceUsdmAdapterError(
                    "price exceeds exchangeInfo maximum"
                )
            self._require_step(
                intent.price,
                self.tick_size,
                minimum=self.price_min,
                name="price",
            )

        if self.min_notional is None or intent.reduce_only:
            return None

        if intent.order_type == "LIMIT":
            if intent.price is None:
                raise BinanceUsdmAdapterError("LIMIT price is required")
            effective_price = intent.price
            selected_mark_price = None
        else:
            if not isinstance(mark_price, BinanceUsdmMarkPrice):
                raise BinanceUsdmAdapterError(
                    "MARKET min-notional admission requires canonical mark-price evidence"
                )
            if (
                isinstance(maximum_mark_price_age_seconds, bool)
                or not isinstance(maximum_mark_price_age_seconds, int)
                or maximum_mark_price_age_seconds < 0
            ):
                raise BinanceUsdmAdapterError(
                    "maximum_mark_price_age_seconds must be a non-negative integer"
                )
            point = _utc(at, name="at")
            if mark_price.instrument_version != intent.instrument_version:
                raise BinanceUsdmAdapterError(
                    "mark-price instrument version does not match intent"
                )
            if mark_price.symbol != intent.symbol:
                raise BinanceUsdmAdapterError(
                    "mark-price symbol does not match intent"
                )
            if mark_price.observed_at > point:
                raise BinanceUsdmAdapterError(
                    "mark-price evidence is from the future"
                )
            if point - mark_price.observed_at > timedelta(
                seconds=maximum_mark_price_age_seconds
            ):
                raise BinanceUsdmAdapterError(
                    "mark-price evidence is stale"
                )
            effective_price = mark_price.price
            selected_mark_price = mark_price

        if intent.quantity * effective_price < self.min_notional:
            raise BinanceUsdmAdapterError(
                "notional is below exchangeInfo minimum"
            )
        return selected_mark_price


@dataclass(frozen=True)
class BinanceUsdmPreparedRequest:
    endpoint: str
    body: Mapping[str, str]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]
    filter_source_sha256: str
    mark_price_source_sha256: str | None = None

    def __post_init__(self) -> None:
        digest = _text(
            self.filter_source_sha256,
            name="filter_source_sha256",
        )
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceUsdmAdapterError(
                "filter_source_sha256 must be canonical lowercase SHA-256"
            )
        mark_digest = self.mark_price_source_sha256
        if mark_digest is not None:
            mark_digest = _text(
                mark_digest,
                name="mark_price_source_sha256",
            )
            if (
                len(mark_digest) != 71
                or not mark_digest.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in mark_digest[7:])
            ):
                raise BinanceUsdmAdapterError(
                    "mark_price_source_sha256 must be canonical lowercase SHA-256"
                )
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))
        object.__setattr__(self, "filter_source_sha256", digest)
        object.__setattr__(self, "mark_price_source_sha256", mark_digest)


def _require_position_mode(
    *,
    capability: CapabilitySnapshot,
    intent: BinanceUsdmOrderIntent,
) -> None:
    mode = _text(capability.position_mode, name="capability.position_mode").upper()
    if mode == "NET":
        if intent.position_side != "BOTH":
            raise BinanceUsdmAdapterError(
                "one-way/NET capability requires position_side BOTH"
            )
        return
    if mode == "HEDGE":
        if intent.position_side not in {"LONG", "SHORT"}:
            raise BinanceUsdmAdapterError(
                "hedge capability requires position_side LONG or SHORT"
            )
        if intent.reduce_only:
            raise BinanceUsdmAdapterError(
                "Binance USD-M reduceOnly cannot be sent in Hedge Mode"
            )
        return
    raise BinanceUsdmAdapterError("unsupported or conflicted position mode")


def prepare_order_request(
    intent: BinanceUsdmOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    symbol_rules: BinanceUsdmSymbolRules,
    at: datetime,
    mark_price: BinanceUsdmMarkPrice | None = None,
    maximum_mark_price_age_seconds: int | None = None,
) -> BinanceUsdmPreparedRequest:
    """Prepare but never sign or send a USD-M order."""

    if not isinstance(intent, BinanceUsdmOrderIntent):
        raise TypeError("intent must be BinanceUsdmOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if not isinstance(symbol_rules, BinanceUsdmSymbolRules):
        raise TypeError("symbol_rules must be BinanceUsdmSymbolRules")

    point = _utc(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "BINANCE":
        raise BinanceUsdmAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise BinanceUsdmAdapterError(
            "capability instrument version does not match intent"
        )
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force or "NONE",
        permission_scope="ORDER_WRITE",
    ):
        raise BinanceUsdmAdapterError(
            "exact capability evidence does not admit this order"
        )

    _require_position_mode(capability=capability, intent=intent)
    selected_mark_price = symbol_rules.validate(
        intent,
        at=point,
        mark_price=mark_price,
        maximum_mark_price_age_seconds=maximum_mark_price_age_seconds,
    )

    body: dict[str, str] = {
        "symbol": intent.symbol,
        "side": intent.side,
        "type": intent.order_type,
        "quantity": _decimal_text(intent.quantity),
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
        "positionSide": intent.position_side,
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.time_in_force is not None:
        body["timeInForce"] = intent.time_in_force
    if intent.reduce_only:
        body["reduceOnly"] = "true"

    # timestamp, recvWindow, API key and signature belong to the separately
    # qualified transport after GuardedDispatcher's final authority barrier.
    return BinanceUsdmPreparedRequest(
        endpoint=BINANCE_USDM_ENDPOINTS["PLACE_ORDER"],
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=BINANCE_USDM_DOCS,
        filter_source_sha256=symbol_rules.source_sha256,
        mark_price_source_sha256=(
            None
            if selected_mark_price is None
            else selected_mark_price.source_sha256
        ),
    )


def _response_evidence(
    response: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, str]:
    try:
        encoded = json.dumps(
            response,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BinanceUsdmAdapterError(
            "response must contain canonical JSON-compatible values"
        ) from error
    digest = sha256(encoded).hexdigest()
    source_uri = "https://fapi.binance.com/fapi/v1/order"
    return {
        "artifact_id": str(
            uuid5(NAMESPACE_URL, f"{source_uri}#sha256:{digest}")
        ),
        "sha256": f"sha256:{digest}",
        "source_uri": source_uri,
        "observed_at": observed_at,
        "rights_id": "provider-observation-binance-usdm",
    }


def parse_order_ack(
    *,
    attempt_id: str,
    client_order_id: str,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Map a recorded create-order response as acknowledgement only."""

    aid = _uuid(attempt_id, name="attempt_id")
    cid = validate_client_order_id(client_order_id)
    if not isinstance(response, Mapping):
        raise BinanceUsdmAdapterError("response must be an object")

    echoed = validate_client_order_id(response.get("clientOrderId"))
    if echoed != cid:
        raise BinanceUsdmAdapterError(
            "Binance USD-M clientOrderId does not match request"
        )

    symbol = _provider_symbol(response.get("symbol"), name="response.symbol")
    order_id = response.get("orderId")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 0:
        raise BinanceUsdmAdapterError(
            "response.orderId must be a non-negative integer"
        )
    when = _millis(response.get("updateTime"), name="response.updateTime")

    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": f"BINANCE-USDM:{symbol}:{order_id}",
        "client_order_id": cid,
        "provider_received_at": when,
        "evidence": [_response_evidence(response, observed_at=when)],
        "retry_disposition": "NEVER",
    }


def parse_account_trades(
    observation: ProviderResponseObservation,
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_order_id: Mapping[int, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map one authenticated exact-byte USD-M user-trade read to fills."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="BINANCE",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BINANCE_USDM_ENDPOINTS["EXECUTIONS"],
    )
    rows = observation.payload
    account_id = observation.account_id
    environment = observation.environment
    if not isinstance(rows, (list, tuple)):
        raise BinanceUsdmAdapterError("trade rows must be an array")
    if not isinstance(instrument_versions, Mapping):
        raise BinanceUsdmAdapterError("instrument_versions must be a mapping")
    normalized_instruments: dict[str, str] = {}
    for raw_symbol, raw_instrument_version in instrument_versions.items():
        provider_symbol = _provider_symbol(
            raw_symbol,
            name="instrument_versions symbol",
        )
        instrument_version = _text(
            raw_instrument_version,
            name="instrument_version",
        )
        if provider_symbol in normalized_instruments:
            raise BinanceUsdmAdapterError(
                "instrument_versions contains duplicate normalized symbols"
            )
        normalized_instruments[provider_symbol] = instrument_version

    client_map = {} if client_ids_by_order_id is None else client_ids_by_order_id
    if not isinstance(client_map, Mapping):
        raise BinanceUsdmAdapterError(
            "client_ids_by_order_id must be a mapping"
        )
    normalized_client_map: dict[int, str] = {}
    seen_client_ids: set[str] = set()
    for raw_order_id, raw_client_id in client_map.items():
        if (
            isinstance(raw_order_id, bool)
            or not isinstance(raw_order_id, int)
            or raw_order_id < 0
        ):
            raise BinanceUsdmAdapterError(
                "client_ids_by_order_id keys must be non-negative integer order ids"
            )
        normalized_client_id = validate_client_order_id(raw_client_id)
        if normalized_client_id in seen_client_ids:
            raise BinanceUsdmAdapterError(
                "client_ids_by_order_id maps one client_order_id to multiple provider order ids"
            )
        normalized_client_map[raw_order_id] = normalized_client_id
        seen_client_ids.add(normalized_client_id)

    by_id: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise BinanceUsdmAdapterError(
                f"trade row {index} must be an object"
            )

        symbol = _provider_symbol(
            raw.get("symbol"),
            name=f"trade[{index}].symbol",
        )
        if symbol not in normalized_instruments:
            raise BinanceUsdmAdapterError(
                f"unmapped Binance USD-M symbol: {symbol}"
            )

        trade_id = raw.get("id")
        order_id = raw.get("orderId")
        if (
            isinstance(trade_id, bool)
            or not isinstance(trade_id, int)
            or trade_id < 0
            or isinstance(order_id, bool)
            or not isinstance(order_id, int)
            or order_id < 0
        ):
            raise BinanceUsdmAdapterError(
                "trade id and orderId must be non-negative integers"
            )

        side = _text(raw.get("side"), name=f"trade[{index}].side").upper()
        if side not in {"BUY", "SELL"}:
            raise BinanceUsdmAdapterError("trade side must be BUY or SELL")
        position_side = _text(
            raw.get("positionSide"), name=f"trade[{index}].positionSide"
        ).upper()
        if position_side not in _ALLOWED_POSITION_SIDES:
            raise BinanceUsdmAdapterError(
                "trade positionSide must be BOTH, LONG or SHORT"
            )

        execution_id = f"BINANCE-USDM:{symbol}:{trade_id}"
        client_id = normalized_client_map.get(order_id)

        fill = ProviderFillEvidence.create(
            provider_id="BINANCE",
            account_id=account_id,
            environment=environment,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=_text(
                normalized_instruments[symbol], name="instrument_version"
            ),
            side=side,
            position_side=position_side,
            quantity=raw.get("qty"),
            price=raw.get("price"),
            fee_amount=raw.get("commission"),
            fee_currency=_text(
                raw.get("commissionAsset"), name="commissionAsset"
            ),
            trade_time=_millis(raw.get("time"), name="trade.time"),
            evidence_refs=(observation.evidence_ref,),
        )
        previous = by_id.get(execution_id)
        if previous is not None and previous != fill:
            raise BinanceUsdmAdapterError(
                "Binance USD-M trade id has conflicting economic content"
            )
        by_id[execution_id] = fill

    return tuple(by_id.values())


def coverage_evidence(
    *,
    account_id: str,
    environment: str,
    surface: str,
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Create fail-closed reconciliation coverage evidence.

    Binance USD-M order history has retention limits, so coverage and provider
    exclusion semantics must be established independently; empty history alone
    is never promoted to PROVEN_ABSENT.
    """

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise BinanceUsdmAdapterError("unsupported reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise BinanceUsdmAdapterError(f"{name} must be boolean")
    if qualified_exclusion_semantics:
        raise BinanceUsdmAdapterError(
            "Binance USD-M foundation cannot self-assert provider exclusion semantics; "
            "exact qualification evidence is required"
        )

    return CoverageSurfaceEvidence(
        provider_id="BINANCE",
        account_id=account_id,
        environment=environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=qualified_exclusion_semantics,
    )
