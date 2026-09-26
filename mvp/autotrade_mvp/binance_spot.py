"""Fail-closed Binance Spot contract adapter foundation.

This module performs no networking, signing, credential storage or live
qualification. It only translates already-authorized canonical values and
recorded provider responses. A successful order ACK is never treated as a fill.
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


BINANCE_SPOT_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
        "EXCHANGE_INFO": "/api/v3/exchangeInfo",
        "PLACE_ORDER": "/api/v3/order",
        "OPEN_ORDERS": "/api/v3/openOrders",
        "ORDER_HISTORY": "/api/v3/allOrders",
        "EXECUTIONS": "/api/v3/myTrades",
        "ACCOUNT": "/api/v3/account",
    }
)

_CLIENT_ID = re.compile(r"^[A-Za-z0-9_.:/-]{1,36}$")
_EXCHANGE_INFO_RULES_TOKEN = object()
_REFERENCE_PRICE_TOKEN = object()
_ALLOWED_TIF = frozenset({"GTC", "IOC", "FOK"})


class BinanceSpotAdapterError(ProviderCoreError):
    pass


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BinanceSpotAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
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


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise BinanceSpotAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _millis(value: object, *, name: str) -> str:
    if isinstance(value, bool):
        raise BinanceSpotAdapterError(f"{name} must be an integer millisecond timestamp")
    try:
        raw = int(value)
    except (TypeError, ValueError) as error:
        raise BinanceSpotAdapterError(f"{name} must be an integer millisecond timestamp") from error
    if raw < 0 or str(raw) != str(value).strip():
        raise BinanceSpotAdapterError(f"{name} must be a non-negative integer millisecond timestamp")
    seconds, remainder = divmod(raw, 1000)
    instant = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(milliseconds=remainder)
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _nonnegative_int(value: object, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BinanceSpotAdapterError(f"{name} must be a non-negative integer")
    return value


def validate_client_order_id(value: object) -> str:
    client_id = _text(value, name="client_order_id")
    if not _CLIENT_ID.fullmatch(client_id):
        raise BinanceSpotAdapterError(
            "client_order_id must be 1-36 Binance-compatible characters"
        )
    return client_id


@dataclass(frozen=True)
class BinanceSpotOrderIntent:
    instrument_version: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    time_in_force: str | None

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
    ) -> "BinanceSpotOrderIntent":
        instrument = _text(instrument_version, name="instrument_version")
        provider_symbol = _text(symbol, name="symbol")
        if provider_symbol != provider_symbol.upper():
            raise BinanceSpotAdapterError("Binance Spot symbol must be uppercase")
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise BinanceSpotAdapterError("side must be BUY or SELL")
        normalized_type = _text(order_type, name="order_type").upper()
        if normalized_type not in {"MARKET", "LIMIT"}:
            raise BinanceSpotAdapterError(
                "foundation supports only MARKET and LIMIT Spot orders"
            )
        qty = _decimal(quantity, name="quantity", positive=True)
        px = None if price is None else _decimal(price, name="price", positive=True)

        if normalized_type == "LIMIT":
            if px is None:
                raise BinanceSpotAdapterError("LIMIT price is required")
            tif = _text(time_in_force or "GTC", name="time_in_force").upper()
            if tif not in _ALLOWED_TIF:
                raise BinanceSpotAdapterError("unsupported time_in_force")
        else:
            if px is not None:
                raise BinanceSpotAdapterError("MARKET order must not carry limit price")
            if time_in_force is not None:
                raise BinanceSpotAdapterError("MARKET order must not carry time_in_force")
            tif = None

        return cls(
            instrument_version=instrument,
            symbol=provider_symbol,
            side=normalized_side,
            order_type=normalized_type,
            quantity=qty,
            price=px,
            time_in_force=tif,
        )


@dataclass(frozen=True)
class BinanceSpotReferencePrice:
    """Provider-originated Spot price evidence with explicit window semantics."""

    instrument_version: str
    symbol: str
    price: Decimal | None
    observed_at: datetime
    source_sha256: str
    reference_kind: str
    averaging_window_minutes: int | None
    _verification_token: InitVar[object | None] = None

    def __post_init__(self, _verification_token: object | None) -> None:
        if _verification_token is not _REFERENCE_PRICE_TOKEN:
            raise BinanceSpotAdapterError(
                "reference price must come from canonical provider payload parsing"
            )
        instrument = _text(self.instrument_version, name="instrument_version")
        symbol = _text(self.symbol, name="symbol")
        if symbol != symbol.upper():
            raise BinanceSpotAdapterError("reference-price symbol must be uppercase")
        price = (
            None
            if self.price is None
            else _decimal(self.price, name="reference_price", positive=True)
        )
        observed_at = _utc(self.observed_at, name="reference_price observed_at")
        digest = _text(self.source_sha256, name="reference_price source_sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceSpotAdapterError(
                "reference_price source_sha256 must be canonical lowercase SHA-256"
            )
        kind = _text(self.reference_kind, name="reference_kind").upper()
        if kind not in {"REFERENCE", "AVERAGE", "LAST"}:
            raise BinanceSpotAdapterError(
                "reference_kind must be REFERENCE, AVERAGE or LAST"
            )
        if kind == "REFERENCE":
            if self.averaging_window_minutes is not None:
                raise BinanceSpotAdapterError(
                    "REFERENCE price observation must not carry an averaging window"
                )
            window = None
        else:
            if price is None:
                raise BinanceSpotAdapterError(
                    f"{kind} reference price requires a provider price"
                )
            window = _nonnegative_int(
                self.averaging_window_minutes,
                name="averaging_window_minutes",
            )
            if kind == "AVERAGE" and window == 0:
                raise BinanceSpotAdapterError(
                    "AVERAGE reference price requires a positive averaging window"
                )
            if kind == "LAST" and window != 0:
                raise BinanceSpotAdapterError(
                    "LAST reference price requires zero averaging window"
                )
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "observed_at", observed_at)
        object.__setattr__(self, "source_sha256", digest)
        object.__setattr__(self, "reference_kind", kind)
        object.__setattr__(self, "averaging_window_minutes", window)

    @staticmethod
    def _source_digest(
        *,
        symbol: str,
        endpoint: str,
        payload: Mapping[str, object],
    ) -> str:
        canonical = json.dumps(
            {
                "endpoint": endpoint,
                "symbol": symbol,
                "payload": dict(payload),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + sha256(canonical).hexdigest()

    @classmethod
    def from_reference_price_payload(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        payload: Mapping[str, object],
    ) -> "BinanceSpotReferencePrice":
        """Parse GET /api/v3/referencePrice without inventing null fallback."""

        if not isinstance(payload, Mapping):
            raise TypeError("reference-price payload must be a mapping")
        if set(payload) != {"symbol", "referencePrice", "timestamp"}:
            raise BinanceSpotAdapterError(
                "reference-price payload fields are not canonical"
            )
        requested_symbol = _text(symbol, name="reference-price symbol")
        if requested_symbol != requested_symbol.upper():
            raise BinanceSpotAdapterError(
                "reference-price symbol must be uppercase"
            )
        provider_symbol = _text(
            payload.get("symbol"),
            name="reference-price payload symbol",
        )
        if provider_symbol != requested_symbol:
            raise BinanceSpotAdapterError(
                "reference-price payload symbol does not match requested symbol"
            )
        raw_price = payload.get("referencePrice")
        price = (
            None
            if raw_price is None
            else _decimal(
                raw_price,
                name="provider reference price",
                positive=True,
            )
        )
        timestamp_text = _millis(
            payload.get("timestamp"),
            name="reference-price timestamp",
        )
        observed_at = datetime.fromisoformat(
            timestamp_text.replace("Z", "+00:00")
        )
        return cls(
            instrument_version=_text(
                instrument_version,
                name="instrument_version",
            ),
            symbol=provider_symbol,
            price=price,
            observed_at=observed_at,
            source_sha256=cls._source_digest(
                symbol=provider_symbol,
                endpoint="/api/v3/referencePrice",
                payload=payload,
            ),
            reference_kind="REFERENCE",
            averaging_window_minutes=None,
            _verification_token=_REFERENCE_PRICE_TOKEN,
        )

    @classmethod
    def from_average_price_payload(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        payload: Mapping[str, object],
    ) -> "BinanceSpotReferencePrice":
        """Parse the documented GET /api/v3/avgPrice response."""

        if not isinstance(payload, Mapping):
            raise TypeError("average-price payload must be a mapping")
        provider_symbol = _text(symbol, name="average-price symbol")
        if provider_symbol != provider_symbol.upper():
            raise BinanceSpotAdapterError("average-price symbol must be uppercase")
        window = _nonnegative_int(payload.get("mins"), name="average-price mins")
        if window == 0:
            raise BinanceSpotAdapterError(
                "average-price endpoint must report a positive averaging window"
            )
        price = _decimal(payload.get("price"), name="average price", positive=True)
        timestamp_text = _millis(
            payload.get("closeTime"),
            name="average-price closeTime",
        )
        observed_at = datetime.fromisoformat(
            timestamp_text.replace("Z", "+00:00")
        )
        return cls(
            instrument_version=_text(
                instrument_version,
                name="instrument_version",
            ),
            symbol=provider_symbol,
            price=price,
            observed_at=observed_at,
            source_sha256=cls._source_digest(
                symbol=provider_symbol,
                endpoint="/api/v3/avgPrice",
                payload=payload,
            ),
            reference_kind="AVERAGE",
            averaging_window_minutes=window,
            _verification_token=_REFERENCE_PRICE_TOKEN,
        )

    @classmethod
    def from_last_trade_payload(
        cls,
        *,
        instrument_version: str,
        symbol: str,
        payload: Mapping[str, object],
    ) -> "BinanceSpotReferencePrice":
        """Parse one documented recent-trade row as last-price evidence."""

        if not isinstance(payload, Mapping):
            raise TypeError("last-trade payload must be a mapping")
        provider_symbol = _text(symbol, name="last-price symbol")
        if provider_symbol != provider_symbol.upper():
            raise BinanceSpotAdapterError("last-price symbol must be uppercase")
        price = _decimal(payload.get("price"), name="last trade price", positive=True)
        timestamp_text = _millis(
            payload.get("time"),
            name="last-trade time",
        )
        observed_at = datetime.fromisoformat(
            timestamp_text.replace("Z", "+00:00")
        )
        return cls(
            instrument_version=_text(
                instrument_version,
                name="instrument_version",
            ),
            symbol=provider_symbol,
            price=price,
            observed_at=observed_at,
            source_sha256=cls._source_digest(
                symbol=provider_symbol,
                endpoint="/api/v3/trades",
                payload=payload,
            ),
            reference_kind="LAST",
            averaging_window_minutes=0,
            _verification_token=_REFERENCE_PRICE_TOKEN,
        )

@dataclass(frozen=True)
class BinanceSpotSymbolRules:
    """Versioned exchangeInfo symbol filters required before order preparation."""

    instrument_version: str
    symbol: str
    source_sha256: str
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
    max_notional: Decimal | None
    min_notional_applies_to_market: bool
    max_notional_applies_to_market: bool
    notional_avg_price_mins: int | None
    _verification_token: InitVar[object | None] = None

    def __post_init__(self, _verification_token: object | None) -> None:
        if _verification_token is not _EXCHANGE_INFO_RULES_TOKEN:
            raise BinanceSpotAdapterError(
                "exchangeInfo rules must come from canonical provider payload parsing"
            )
        instrument = _text(self.instrument_version, name="instrument_version")
        symbol = _text(self.symbol, name="symbol")
        digest = _text(self.source_sha256, name="source_sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceSpotAdapterError(
                "source_sha256 must be canonical lowercase SHA-256"
            )
        lot_min = _decimal(self.lot_min_qty, name="lot_min_qty", positive=True)
        lot_max = _decimal(self.lot_max_qty, name="lot_max_qty", positive=True)
        lot_step = _decimal(self.lot_step_size, name="lot_step_size", positive=True)
        price_min = _decimal(self.price_min, name="price_min")
        price_max = _decimal(self.price_max, name="price_max")
        tick = _decimal(self.tick_size, name="tick_size")
        if tick < 0:
            raise BinanceSpotAdapterError("tick_size cannot be negative")
        if lot_min > lot_max or price_min < 0 or price_max < 0:
            raise BinanceSpotAdapterError("exchangeInfo rule bounds are invalid")
        for name in ("min_notional_applies_to_market", "max_notional_applies_to_market"):
            if type(getattr(self, name)) is not bool:
                raise BinanceSpotAdapterError(f"{name} must be boolean")
        notional_window = self.notional_avg_price_mins
        if notional_window is not None:
            notional_window = _nonnegative_int(
                notional_window,
                name="notional_avg_price_mins",
            )
        if (
            self.min_notional_applies_to_market
            or self.max_notional_applies_to_market
        ) and notional_window is None:
            raise BinanceSpotAdapterError(
                "market notional filters require avgPriceMins"
            )
        object.__setattr__(
            self,
            "notional_avg_price_mins",
            notional_window,
        )
        for name in ("market_min_qty", "market_max_qty", "market_step_size"):
            value = getattr(self, name)
            if value is not None:
                normalized = _decimal(value, name=name, positive=True)
                object.__setattr__(self, name, normalized)
        for name in ("min_notional", "max_notional"):
            value = getattr(self, name)
            if value is not None:
                normalized = _decimal(value, name=name, positive=True)
                object.__setattr__(self, name, normalized)
        object.__setattr__(self, "instrument_version", instrument)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "source_sha256", digest)
        object.__setattr__(self, "lot_min_qty", lot_min)
        object.__setattr__(self, "lot_max_qty", lot_max)
        object.__setattr__(self, "lot_step_size", lot_step)
        object.__setattr__(self, "price_min", price_min)
        object.__setattr__(self, "price_max", price_max)
        object.__setattr__(self, "tick_size", tick)

    @classmethod
    def from_exchange_info(
        cls,
        *,
        instrument_version: str,
        symbol_payload: Mapping[str, object],
    ) -> "BinanceSpotSymbolRules":
        if not isinstance(symbol_payload, Mapping):
            raise TypeError("symbol_payload must be a mapping")
        instrument = _text(instrument_version, name="instrument_version")
        symbol = _text(symbol_payload.get("symbol"), name="symbol")
        if symbol != symbol.upper():
            raise BinanceSpotAdapterError("exchangeInfo symbol must be uppercase")
        filters = symbol_payload.get("filters")
        if isinstance(filters, (str, bytes)) or not isinstance(filters, list):
            raise BinanceSpotAdapterError("exchangeInfo filters must be an array")
        by_type: dict[str, Mapping[str, object]] = {}
        for item in filters:
            if not isinstance(item, Mapping):
                raise BinanceSpotAdapterError("exchangeInfo filter must be an object")
            kind = _text(item.get("filterType"), name="filterType")
            if kind in by_type:
                raise BinanceSpotAdapterError(f"duplicate exchangeInfo filter: {kind}")
            by_type[kind] = item
        for required in ("PRICE_FILTER", "LOT_SIZE"):
            if required not in by_type:
                raise BinanceSpotAdapterError(
                    f"exchangeInfo is missing required {required} filter"
                )

        price_filter = by_type["PRICE_FILTER"]
        lot_filter = by_type["LOT_SIZE"]
        price_min = _decimal(price_filter.get("minPrice"), name="minPrice")
        price_max = _decimal(price_filter.get("maxPrice"), name="maxPrice")
        tick_size = _decimal(price_filter.get("tickSize"), name="tickSize")
        if tick_size < 0:
            raise BinanceSpotAdapterError("tickSize cannot be negative")
        lot_min = _decimal(lot_filter.get("minQty"), name="minQty", positive=True)
        lot_max = _decimal(lot_filter.get("maxQty"), name="maxQty", positive=True)
        lot_step = _decimal(lot_filter.get("stepSize"), name="stepSize", positive=True)
        if lot_min > lot_max:
            raise BinanceSpotAdapterError("LOT_SIZE minQty exceeds maxQty")
        if price_min < 0 or price_max < 0 or (price_max and price_min > price_max):
            raise BinanceSpotAdapterError("PRICE_FILTER bounds are invalid")

        market_min = market_max = market_step = None
        market_filter = by_type.get("MARKET_LOT_SIZE")
        if market_filter is not None:
            raw_min = _decimal(market_filter.get("minQty"), name="market minQty")
            raw_max = _decimal(market_filter.get("maxQty"), name="market maxQty")
            raw_step = _decimal(market_filter.get("stepSize"), name="market stepSize")
            if raw_min < 0 or raw_max < 0 or raw_step < 0:
                raise BinanceSpotAdapterError("MARKET_LOT_SIZE values cannot be negative")
            if raw_max and raw_min > raw_max:
                raise BinanceSpotAdapterError("MARKET_LOT_SIZE minQty exceeds maxQty")
            market_min = raw_min if raw_min > 0 else None
            market_max = raw_max if raw_max > 0 else None
            market_step = raw_step if raw_step > 0 else None

        min_notional = max_notional = None
        min_market = max_market = False
        notional_window = None
        if "NOTIONAL" in by_type:
            item = by_type["NOTIONAL"]
            min_notional = _decimal(
                item.get("minNotional"),
                name="minNotional",
                positive=True,
            )
            raw_max = _decimal(item.get("maxNotional"), name="maxNotional")
            if raw_max < 0:
                raise BinanceSpotAdapterError("maxNotional cannot be negative")
            max_notional = raw_max if raw_max > 0 else None
            min_market = item.get("applyMinToMarket", False)
            max_market = item.get("applyMaxToMarket", False)
            if type(min_market) is not bool or type(max_market) is not bool:
                raise BinanceSpotAdapterError(
                    "NOTIONAL market-application flags must be boolean"
                )
            notional_window = _nonnegative_int(
                item.get("avgPriceMins"),
                name="NOTIONAL avgPriceMins",
            )
        if "MIN_NOTIONAL" in by_type:
            item = by_type["MIN_NOTIONAL"]
            legacy_min = _decimal(
                item.get("minNotional"),
                name="minNotional",
                positive=True,
            )
            legacy_market = item.get("applyToMarket", False)
            if type(legacy_market) is not bool:
                raise BinanceSpotAdapterError(
                    "MIN_NOTIONAL applyToMarket must be boolean"
                )
            legacy_window = _nonnegative_int(
                item.get("avgPriceMins"),
                name="MIN_NOTIONAL avgPriceMins",
            )
            if min_notional is None:
                min_notional = legacy_min
                min_market = legacy_market
                notional_window = legacy_window
            elif (
                min_notional != legacy_min
                or min_market != legacy_market
                or notional_window != legacy_window
            ):
                raise BinanceSpotAdapterError(
                    "coexisting NOTIONAL and MIN_NOTIONAL constraints are inconsistent"
                )

        canonical = json.dumps(
            symbol_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return cls(
            instrument_version=instrument,
            symbol=symbol,
            source_sha256="sha256:" + sha256(canonical).hexdigest(),
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
            max_notional=max_notional,
            min_notional_applies_to_market=min_market,
            max_notional_applies_to_market=max_market,
            notional_avg_price_mins=notional_window,
            _verification_token=_EXCHANGE_INFO_RULES_TOKEN,
        )

    @staticmethod
    def _require_step(value: Decimal, step: Decimal, *, name: str) -> None:
        if step == 0:
            return
        if value % step != 0:
            raise BinanceSpotAdapterError(
                f"{name} is not an exact multiple of exchangeInfo step"
            )

    def validate(
        self,
        intent: BinanceSpotOrderIntent,
        *,
        at: datetime,
        reference_price_observation: BinanceSpotReferencePrice | None = None,
        market_reference: BinanceSpotReferencePrice | None = None,
        maximum_market_reference_age_seconds: int | None = None,
    ) -> BinanceSpotReferencePrice | None:
        if self.instrument_version != intent.instrument_version:
            raise BinanceSpotAdapterError(
                "exchangeInfo rules instrument version does not match intent"
            )
        if self.symbol != intent.symbol:
            raise BinanceSpotAdapterError("exchangeInfo rules symbol does not match intent")

        if intent.order_type == "MARKET" and any(
            item is not None
            for item in (self.market_min_qty, self.market_max_qty, self.market_step_size)
        ):
            min_qty = self.market_min_qty
            max_qty = self.market_max_qty
            step = self.market_step_size
        else:
            min_qty = self.lot_min_qty
            max_qty = self.lot_max_qty
            step = self.lot_step_size
        if min_qty is not None and intent.quantity < min_qty:
            raise BinanceSpotAdapterError("quantity is below exchangeInfo minimum")
        if max_qty is not None and intent.quantity > max_qty:
            raise BinanceSpotAdapterError("quantity exceeds exchangeInfo maximum")
        if step is not None:
            self._require_step(intent.quantity, step, name="quantity")

        effective_price = intent.price
        selected_market_reference: BinanceSpotReferencePrice | None = None
        if intent.order_type == "LIMIT":
            if effective_price is None:
                raise BinanceSpotAdapterError("LIMIT price is required")
            if self.price_min > 0 and effective_price < self.price_min:
                raise BinanceSpotAdapterError("price is below exchangeInfo minimum")
            if self.price_max > 0 and effective_price > self.price_max:
                raise BinanceSpotAdapterError("price exceeds exchangeInfo maximum")
            self._require_step(effective_price, self.tick_size, name="price")
        elif (
            self.min_notional_applies_to_market
            or self.max_notional_applies_to_market
        ):
            if not isinstance(
                reference_price_observation,
                BinanceSpotReferencePrice,
            ):
                raise BinanceSpotAdapterError(
                    "market notional filter requires provider "
                    "reference-price observation evidence"
                )
            if (
                isinstance(maximum_market_reference_age_seconds, bool)
                or not isinstance(maximum_market_reference_age_seconds, int)
                or maximum_market_reference_age_seconds < 0
            ):
                raise BinanceSpotAdapterError(
                    "maximum_market_reference_age_seconds must be a non-negative integer"
                )
            point = _utc(at, name="at")

            def require_bound_reference(
                reference: object,
                *,
                role: str,
            ) -> BinanceSpotReferencePrice:
                if not isinstance(reference, BinanceSpotReferencePrice):
                    raise BinanceSpotAdapterError(
                        f"{role} requires canonical provider reference-price evidence"
                    )
                if reference.instrument_version != intent.instrument_version:
                    raise BinanceSpotAdapterError(
                        f"{role} instrument version does not match intent"
                    )
                if reference.symbol != intent.symbol:
                    raise BinanceSpotAdapterError(
                        f"{role} symbol does not match intent"
                    )
                if reference.observed_at > point:
                    raise BinanceSpotAdapterError(
                        f"{role} evidence is from the future"
                    )
                if point - reference.observed_at > timedelta(
                    seconds=maximum_market_reference_age_seconds
                ):
                    raise BinanceSpotAdapterError(f"{role} evidence is stale")
                return reference

            provider_reference = require_bound_reference(
                reference_price_observation,
                role="provider reference-price observation",
            )
            if provider_reference.reference_kind != "REFERENCE":
                raise BinanceSpotAdapterError(
                    "provider reference-price observation must come from "
                    "/api/v3/referencePrice"
                )

            if provider_reference.price is not None:
                selected_market_reference = provider_reference
                effective_price = provider_reference.price
            else:
                fallback = require_bound_reference(
                    market_reference,
                    role="null reference-price fallback",
                )
                required_window = self.notional_avg_price_mins
                if required_window is None:
                    raise BinanceSpotAdapterError(
                        "market notional filter is missing avgPriceMins"
                    )
                if fallback.averaging_window_minutes != required_window:
                    raise BinanceSpotAdapterError(
                        "reference-price averaging window does not match "
                        "exchangeInfo avgPriceMins"
                    )
                expected_kind = (
                    "LAST" if required_window == 0 else "AVERAGE"
                )
                if fallback.reference_kind != expected_kind:
                    raise BinanceSpotAdapterError(
                        "reference-price semantics do not match "
                        "exchangeInfo avgPriceMins"
                    )
                if fallback.price is None:
                    raise BinanceSpotAdapterError(
                        "null reference-price fallback lacks a provider price"
                    )
                selected_market_reference = fallback
                effective_price = fallback.price

        if effective_price is not None:
            notional = intent.quantity * effective_price
            if self.min_notional is not None and (
                intent.order_type == "LIMIT" or self.min_notional_applies_to_market
            ) and notional < self.min_notional:
                raise BinanceSpotAdapterError("notional is below exchangeInfo minimum")
            if self.max_notional is not None and (
                intent.order_type == "LIMIT" or self.max_notional_applies_to_market
            ) and notional > self.max_notional:
                raise BinanceSpotAdapterError("notional exceeds exchangeInfo maximum")

        return selected_market_reference


@dataclass(frozen=True)
class BinanceSpotPreparedRequest:
    endpoint: str
    body: Mapping[str, str]
    capability_snapshot_id: str
    filter_source_sha256: str
    reference_price_observation_source_sha256: str | None = None
    market_reference_source_sha256: str | None = None
    market_reference_kind: str | None = None
    market_reference_window_minutes: int | None = None

    def __post_init__(self) -> None:
        digest = _text(self.filter_source_sha256, name="filter_source_sha256")
        if (
            len(digest) != 71
            or not digest.startswith("sha256:")
            or any(ch not in "0123456789abcdef" for ch in digest[7:])
        ):
            raise BinanceSpotAdapterError(
                "filter_source_sha256 must be canonical lowercase SHA-256"
            )
        observation_digest = self.reference_price_observation_source_sha256
        if observation_digest is not None:
            observation_digest = _text(
                observation_digest,
                name="reference_price_observation_source_sha256",
            )
            if (
                len(observation_digest) != 71
                or not observation_digest.startswith("sha256:")
                or any(
                    ch not in "0123456789abcdef"
                    for ch in observation_digest[7:]
                )
            ):
                raise BinanceSpotAdapterError(
                    "reference_price_observation_source_sha256 must be "
                    "canonical lowercase SHA-256"
                )
        reference_digest = self.market_reference_source_sha256
        if reference_digest is not None:
            reference_digest = _text(
                reference_digest,
                name="market_reference_source_sha256",
            )
            if (
                len(reference_digest) != 71
                or not reference_digest.startswith("sha256:")
                or any(ch not in "0123456789abcdef" for ch in reference_digest[7:])
            ):
                raise BinanceSpotAdapterError(
                    "market_reference_source_sha256 must be canonical lowercase SHA-256"
                )
        reference_kind = self.market_reference_kind
        reference_window = self.market_reference_window_minutes
        if reference_digest is None:
            if reference_kind is not None or reference_window is not None:
                raise BinanceSpotAdapterError(
                    "market reference metadata requires source evidence"
                )
        else:
            reference_kind = _text(
                reference_kind,
                name="market_reference_kind",
            ).upper()
            if reference_kind not in {"REFERENCE", "AVERAGE", "LAST"}:
                raise BinanceSpotAdapterError(
                    "market_reference_kind must be REFERENCE, AVERAGE or LAST"
                )
            if reference_kind == "REFERENCE":
                if reference_window is not None:
                    raise BinanceSpotAdapterError(
                        "REFERENCE market price must not carry an averaging window"
                    )
            else:
                reference_window = _nonnegative_int(
                    reference_window,
                    name="market_reference_window_minutes",
                )
                if (
                    (reference_kind == "AVERAGE" and reference_window == 0)
                    or (reference_kind == "LAST" and reference_window != 0)
                ):
                    raise BinanceSpotAdapterError(
                        "market reference kind/window combination is invalid"
                    )
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))
        object.__setattr__(self, "filter_source_sha256", digest)
        object.__setattr__(
            self,
            "reference_price_observation_source_sha256",
            observation_digest,
        )
        object.__setattr__(
            self,
            "market_reference_source_sha256",
            reference_digest,
        )
        object.__setattr__(self, "market_reference_kind", reference_kind)
        object.__setattr__(
            self,
            "market_reference_window_minutes",
            reference_window,
        )


def prepare_order_request(
    intent: BinanceSpotOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    symbol_rules: BinanceSpotSymbolRules,
    at: datetime,
    reference_price_observation: BinanceSpotReferencePrice | None = None,
    market_reference: BinanceSpotReferencePrice | None = None,
    maximum_market_reference_age_seconds: int | None = None,
) -> BinanceSpotPreparedRequest:
    """Prepare but never sign/send a Spot order.

    Quantity is always base-asset quantity. Reverse MARKET quoteOrderQty is
    deliberately excluded from this foundation because its economic unit differs.
    """

    if not isinstance(intent, BinanceSpotOrderIntent):
        raise TypeError("intent must be BinanceSpotOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if not isinstance(symbol_rules, BinanceSpotSymbolRules):
        raise TypeError("symbol_rules must be BinanceSpotSymbolRules")
    point = _utc(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "BINANCE":
        raise BinanceSpotAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise BinanceSpotAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force or "NONE",
        permission_scope="ORDER_WRITE",
    ):
        raise BinanceSpotAdapterError("exact capability evidence does not admit this order")

    selected_market_reference = symbol_rules.validate(
        intent,
        at=point,
        reference_price_observation=reference_price_observation,
        market_reference=market_reference,
        maximum_market_reference_age_seconds=maximum_market_reference_age_seconds,
    )

    body: dict[str, str] = {
        "symbol": intent.symbol,
        "side": intent.side,
        "type": intent.order_type,
        "quantity": _decimal_text(intent.quantity),
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.time_in_force is not None:
        body["timeInForce"] = intent.time_in_force

    # timestamp, recvWindow, API key and signature belong to the qualified
    # transport wrapper after the dispatcher's final authority barrier.
    return BinanceSpotPreparedRequest(
        endpoint=BINANCE_SPOT_ENDPOINTS["PLACE_ORDER"],
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        filter_source_sha256=symbol_rules.source_sha256,
        reference_price_observation_source_sha256=(
            None
            if reference_price_observation is None
            else reference_price_observation.source_sha256
        ),
        market_reference_source_sha256=(
            None
            if selected_market_reference is None
            else selected_market_reference.source_sha256
        ),
        market_reference_kind=(
            None
            if selected_market_reference is None
            else selected_market_reference.reference_kind
        ),
        market_reference_window_minutes=(
            None
            if selected_market_reference is None
            else selected_market_reference.averaging_window_minutes
        ),
    )


def _response_evidence(
    response: Mapping[str, Any],
    *,
    observed_at: str,
) -> dict[str, str]:
    encoded = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = sha256(encoded).hexdigest()
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"https://api.binance.com/api/v3/order#sha256:{digest}",
            )
        ),
        "sha256": f"sha256:{digest}",
        "source_uri": "https://api.binance.com/api/v3/order",
        "observed_at": observed_at,
        "rights_id": "provider-observation-binance",
    }


def parse_order_ack(
    *,
    attempt_id: str,
    client_order_id: str,
    response: Mapping[str, Any],
) -> dict[str, Any]:
    """Map a recorded ACK response without inventing execution."""

    try:
        aid = str(UUID(_text(attempt_id, name="attempt_id")))
    except ValueError as error:
        raise BinanceSpotAdapterError("attempt_id must be a UUID") from error
    cid = validate_client_order_id(client_order_id)
    if not isinstance(response, Mapping):
        raise BinanceSpotAdapterError("response must be an object")

    echoed = validate_client_order_id(response.get("clientOrderId"))
    if echoed != cid:
        raise BinanceSpotAdapterError("Binance clientOrderId does not match request")
    symbol = _text(response.get("symbol"), name="response.symbol")
    if symbol != symbol.upper():
        raise BinanceSpotAdapterError(
            "response.symbol must be canonical uppercase"
        )
    order_id = response.get("orderId")
    if isinstance(order_id, bool) or not isinstance(order_id, int) or order_id < 0:
        raise BinanceSpotAdapterError("response.orderId must be a non-negative integer")
    when = _millis(response.get("transactTime"), name="response.transactTime")

    # ACK is intentionally not parsed for fills or economic state. Those arrive
    # through execution/account evidence and reconciliation.
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": f"{symbol}:{order_id}",
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
    """Map one authenticated exact-byte account-trade read to unique fills."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    try:
        observation.require_scope(
            provider_id="BINANCE",
            surface=Surface.ACTIVITIES,
            endpoint=BINANCE_SPOT_ENDPOINTS["EXECUTIONS"],
        )
    except ProviderCoreError as error:
        raise BinanceSpotAdapterError("trade observation scope mismatch") from error
    rows = observation.payload
    account_id = observation.account_id
    environment = observation.environment
    if not isinstance(rows, (list, tuple)):
        raise BinanceSpotAdapterError("trade rows must be an array")
    if not isinstance(instrument_versions, Mapping):
        raise BinanceSpotAdapterError("instrument_versions must be a mapping")
    normalized_instruments: dict[str, str] = {}
    for raw_symbol, raw_instrument_version in instrument_versions.items():
        provider_symbol = _text(
            raw_symbol,
            name="instrument_versions symbol",
        )
        if provider_symbol != provider_symbol.upper():
            raise BinanceSpotAdapterError(
                "instrument_versions symbols must be canonical uppercase"
            )
        instrument_version = _text(
            raw_instrument_version,
            name="instrument_version",
        )
        if provider_symbol in normalized_instruments:
            raise BinanceSpotAdapterError(
                "instrument_versions contains duplicate normalized symbols"
            )
        normalized_instruments[provider_symbol] = instrument_version

    client_map = {} if client_ids_by_order_id is None else client_ids_by_order_id
    if not isinstance(client_map, Mapping):
        raise BinanceSpotAdapterError("client_ids_by_order_id must be a mapping")
    normalized_client_map: dict[int, str] = {}
    for raw_order_id, raw_client_id in client_map.items():
        if (
            isinstance(raw_order_id, bool)
            or not isinstance(raw_order_id, int)
            or raw_order_id < 0
        ):
            raise BinanceSpotAdapterError(
                "client_ids_by_order_id keys must be non-negative integer order ids"
            )
        normalized_client_id = validate_client_order_id(raw_client_id)
        if normalized_client_id in normalized_client_map.values():
            raise BinanceSpotAdapterError(
                "client_ids_by_order_id maps one client_order_id to multiple provider order ids"
            )
        normalized_client_map[raw_order_id] = normalized_client_id

    by_id: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise BinanceSpotAdapterError(f"trade row {index} must be an object")
        symbol = _text(raw.get("symbol"), name=f"trade[{index}].symbol")
        if symbol not in normalized_instruments:
            raise BinanceSpotAdapterError(f"unmapped Binance Spot symbol: {symbol}")
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
            raise BinanceSpotAdapterError("trade id and orderId must be non-negative integers")
        execution_id = f"BINANCE-SPOT:{symbol}:{trade_id}"
        client_id = normalized_client_map.get(order_id)

        is_buyer = raw.get("isBuyer")
        if type(is_buyer) is not bool:
            raise BinanceSpotAdapterError(
                "trade isBuyer must be provider-evidenced boolean"
            )
        fill = ProviderFillEvidence.create(
            provider_id="BINANCE",
            account_id=account_id,
            environment=environment,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=normalized_instruments[symbol],
            side="BUY" if is_buyer else "SELL",
            quantity=raw.get("qty"),
            price=raw.get("price"),
            fee_amount=raw.get("commission", "0"),
            fee_currency=_text(raw.get("commissionAsset"), name="commissionAsset"),
            trade_time=_millis(raw.get("time"), name="trade.time"),
            evidence_refs=(observation.evidence_ref,),
        )
        previous = by_id.get(execution_id)
        if previous is not None and previous != fill:
            raise BinanceSpotAdapterError(
                "Binance Spot trade id has conflicting economic content"
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
    normalized = _text(surface, name="surface").upper()
    if normalized not in {"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"}:
        raise BinanceSpotAdapterError("unsupported reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be boolean")
    if qualified_exclusion_semantics:
        raise BinanceSpotAdapterError(
            "Binance Spot foundation cannot self-assert provider exclusion semantics; "
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
