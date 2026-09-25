"""Deterministic, evidence-bound market-data normalization for the AutoTrade MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from .instruments import (
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentRegistryError,
    InstrumentVersion,
)


KINDS = {
    "TRADE",
    "QUOTE",
    "BOOK_SNAPSHOT",
    "BOOK_DELTA",
    "BAR",
    "FUNDING",
    "MARK",
    "INDEX",
    "STATUS",
}


class MarketDataError(ValueError):
    """Raised when raw market data cannot be normalized safely."""


class SequenceConflict(MarketDataError):
    """Raised when one sequence identity is reused with changed content."""


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MarketDataError(f"{field} is required")
    return value.strip()


def _instant(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise MarketDataError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sequence(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketDataError(f"{field} must be a non-negative integer")
    return value


def _decimal(value: Decimal | str | int, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MarketDataError(f"{field} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise MarketDataError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise MarketDataError(f"{field} must be a finite decimal")
    if positive and result <= 0:
        raise MarketDataError(f"{field} must be positive")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise MarketDataError("raw_evidence_ref is required")
    return MappingProxyType(dict(value))


@dataclass(frozen=True)
class RawMarketUpdate:
    provider_id: str
    venue_id: str
    provider_symbol: str
    kind: str
    source_event_at: datetime
    available_at: datetime
    ingested_at: datetime
    availability_basis: str
    revision: int
    payload: Mapping[str, Any]
    raw_evidence_ref: Mapping[str, object]
    source_sequence: int | None = None
    sequence_stream: str | None = None

    def __post_init__(self) -> None:
        for field in ("provider_id", "venue_id", "provider_symbol", "availability_basis"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        kind = _text(self.kind, "kind").upper()
        if kind not in KINDS:
            raise MarketDataError("kind is unsupported")
        object.__setattr__(self, "kind", kind)
        source = _instant(self.source_event_at, "source_event_at")
        available = _instant(self.available_at, "available_at")
        ingested = _instant(self.ingested_at, "ingested_at")
        if source > available:
            raise MarketDataError("source_event_at must not be after available_at")
        if available > ingested:
            raise MarketDataError("available_at must not be after ingested_at")
        object.__setattr__(self, "source_event_at", source)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "ingested_at", ingested)
        revision = _sequence(self.revision, "revision")
        if revision is None:
            raise MarketDataError("revision is required")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(
            self, "source_sequence", _sequence(self.source_sequence, "source_sequence")
        )
        if not isinstance(self.payload, Mapping):
            raise MarketDataError("payload must be an object")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        object.__setattr__(self, "raw_evidence_ref", _evidence(self.raw_evidence_ref))
        if self.sequence_stream is not None:
            object.__setattr__(
                self,
                "sequence_stream",
                _text(self.sequence_stream, "sequence_stream"),
            )


@dataclass(frozen=True)
class NormalizedMarketEvent:
    event_id: str
    instrument_version: str
    kind: str
    source_event_at: datetime
    available_at: datetime
    availability_basis: str
    ingested_at: datetime
    revision: int
    payload_json: str
    quality_flags: tuple[str, ...]
    raw_evidence_ref: Mapping[str, object]
    source_sequence: int | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def to_contract_dict(self) -> dict[str, Any]:
        result = {
            "event_id": self.event_id,
            "instrument_version": self.instrument_version,
            "kind": self.kind,
            "source_event_at": _utc_text(self.source_event_at),
            "available_at": _utc_text(self.available_at),
            "availability_basis": self.availability_basis,
            "ingested_at": _utc_text(self.ingested_at),
            "revision": str(self.revision),
            "payload": self.payload,
            "quality_flags": list(self.quality_flags),
            "raw_evidence_ref": dict(self.raw_evidence_ref),
        }
        if self.source_sequence is not None:
            result["source_sequence"] = str(self.source_sequence)
        return result


class MarketNormalizer:
    """Normalize provider-shaped events without inventing missing market facts."""

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        max_available_age: timedelta = timedelta(seconds=5),
    ) -> None:
        if not isinstance(registry, InstrumentRegistry):
            raise TypeError("registry must be InstrumentRegistry")
        if not isinstance(max_available_age, timedelta) or max_available_age <= timedelta(0):
            raise MarketDataError("max_available_age must be positive")
        self._registry = registry
        self._max_available_age = max_available_age
        self._last_sequence: dict[tuple[str, str, str, str], int] = {}
        self._seen_sequence: dict[tuple[str, str, str, str, int], tuple[str, str]] = {}
        self._unverified_book_streams: set[tuple[str, str, str, str]] = set()

    @staticmethod
    def _instrument_version_id(instrument: InstrumentVersion) -> str:
        return f"{instrument.instrument_id}:{instrument.version}"

    @staticmethod
    def _price(instrument: InstrumentVersion, value: Any, field: str) -> str:
        try:
            return _decimal_text(instrument.validate_price(value))
        except InstrumentRegistryError as error:
            raise MarketDataError(f"{field}: {error}") from error

    @staticmethod
    def _quantity(
        instrument: InstrumentVersion,
        value: Any,
        field: str,
        *,
        allow_zero: bool = False,
    ) -> str:
        exact = _decimal(value, field)
        if allow_zero and exact == 0:
            return "0"
        try:
            return _decimal_text(instrument.validate_quantity(exact))
        except InstrumentRegistryError as error:
            raise MarketDataError(f"{field}: {error}") from error

    def _levels(
        self,
        instrument: InstrumentVersion,
        values: Any,
        field: str,
        *,
        allow_zero: bool,
    ) -> list[dict[str, str]]:
        if not isinstance(values, (list, tuple)):
            raise MarketDataError(f"{field} must be a list")
        result: list[dict[str, str]] = []
        seen_prices: set[str] = set()
        for index, level in enumerate(values):
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                raise MarketDataError(f"{field}[{index}] must contain price and quantity")
            price = self._price(instrument, level[0], f"{field}[{index}].price")
            quantity = self._quantity(
                instrument,
                level[1],
                f"{field}[{index}].quantity",
                allow_zero=allow_zero,
            )
            if price in seen_prices:
                raise MarketDataError(f"{field} contains duplicate price levels")
            seen_prices.add(price)
            result.append({"price": price, "quantity": quantity})
        return result

    def _normalize_payload(
        self,
        instrument: InstrumentVersion,
        kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = dict(payload)
        if kind == "TRADE":
            result = {
                "price": self._price(instrument, raw.get("price"), "price"),
                "quantity": self._quantity(instrument, raw.get("quantity"), "quantity"),
            }
            side = raw.get("side")
            if side is not None:
                normalized_side = _text(side, "side").upper()
                if normalized_side not in {"BUY", "SELL", "UNKNOWN"}:
                    raise MarketDataError("trade side is unsupported")
                result["side"] = normalized_side
            return result

        if kind == "QUOTE":
            bid = self._price(instrument, raw.get("bid_price"), "bid_price")
            ask = self._price(instrument, raw.get("ask_price"), "ask_price")
            if Decimal(bid) > Decimal(ask):
                raise MarketDataError("quote is crossed")
            return {
                "bid_price": bid,
                "bid_quantity": self._quantity(
                    instrument, raw.get("bid_quantity"), "bid_quantity"
                ),
                "ask_price": ask,
                "ask_quantity": self._quantity(
                    instrument, raw.get("ask_quantity"), "ask_quantity"
                ),
            }

        if kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}:
            allow_zero = kind == "BOOK_DELTA"
            bids = self._levels(
                instrument,
                raw.get("bids"),
                "bids",
                allow_zero=allow_zero,
            )
            asks = self._levels(
                instrument,
                raw.get("asks"),
                "asks",
                allow_zero=allow_zero,
            )
            if kind == "BOOK_SNAPSHOT" and bids and asks:
                best_bid = max(Decimal(level["price"]) for level in bids)
                best_ask = min(Decimal(level["price"]) for level in asks)
                if best_bid > best_ask:
                    raise MarketDataError("book snapshot is crossed")
            return {"bids": bids, "asks": asks}

        if kind == "BAR":
            prices = {
                name: self._price(instrument, raw.get(name), name)
                for name in ("open", "high", "low", "close")
            }
            opened = Decimal(prices["open"])
            high = Decimal(prices["high"])
            low = Decimal(prices["low"])
            closed = Decimal(prices["close"])
            if high < max(opened, low, closed) or low > min(opened, high, closed):
                raise MarketDataError("bar OHLC bounds are inconsistent")
            volume_raw = raw.get("volume")
            volume = _decimal(volume_raw, "volume")
            if volume < 0:
                raise MarketDataError("volume must be non-negative")
            if volume == 0:
                volume_text = "0"
            else:
                volume_text = self._quantity(instrument, volume, "volume")
            return {**prices, "volume": volume_text}

        if kind == "FUNDING":
            rate = _decimal(raw.get("rate"), "rate")
            result: dict[str, Any] = {"rate": _decimal_text(rate)}
            if raw.get("next_funding_at") is not None:
                value = raw["next_funding_at"]
                if isinstance(value, datetime):
                    result["next_funding_at"] = _utc_text(_instant(value, "next_funding_at"))
                elif isinstance(value, str) and value.endswith("Z"):
                    result["next_funding_at"] = value
                else:
                    raise MarketDataError("next_funding_at must be an UTC instant")
            return result

        if kind in {"MARK", "INDEX"}:
            return {"price": self._price(instrument, raw.get("price"), "price")}

        if kind == "STATUS":
            return {"status": _text(raw.get("status"), "status").upper()}

        raise MarketDataError("kind is unsupported")

    def normalize(self, update: RawMarketUpdate) -> NormalizedMarketEvent:
        try:
            instrument = self._registry.resolve(
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                update.source_event_at,
            )
        except (InstrumentRegistryError, InstrumentNotFound) as error:
            raise MarketDataError("market update cannot be resolved to an instrument version") from error

        normalized_payload = self._normalize_payload(instrument, update.kind, update.payload)
        payload_json = _canonical(normalized_payload)
        payload_digest = sha256(payload_json.encode("utf-8")).hexdigest()
        flags: set[str] = set()

        if update.ingested_at - update.available_at > self._max_available_age:
            flags.add("STALE")
        try:
            self._registry.require_tradable(instrument.instrument_id, update.source_event_at)
        except InstrumentRegistryError:
            flags.add("NOT_TRADABLE_AT_EVENT_TIME")

        stream = update.sequence_stream or update.kind
        stream_key = (
            update.provider_id,
            update.venue_id,
            update.provider_symbol,
            stream,
        )
        sequence_identity = (
            *stream_key,
            update.source_sequence,
        ) if update.source_sequence is not None else None

        new_sequence = False
        if update.source_sequence is not None:
            existing = self._seen_sequence.get(sequence_identity)
            if existing is not None:
                existing_digest, _ = existing
                if existing_digest != payload_digest:
                    raise SequenceConflict(
                        "source sequence was reused with different normalized content"
                    )
                flags.add("DUPLICATE")
            else:
                new_sequence = True
                last = self._last_sequence.get(stream_key)
                if last is not None:
                    if update.source_sequence > last + 1:
                        flags.add("SEQUENCE_GAP")
                    elif update.source_sequence < last:
                        flags.add("OUT_OF_ORDER")
                self._last_sequence[stream_key] = (
                    update.source_sequence
                    if last is None
                    else max(last, update.source_sequence)
                )

        if update.kind == "BOOK_DELTA" and (
            "SEQUENCE_GAP" in flags or "OUT_OF_ORDER" in flags
        ):
            self._unverified_book_streams.add(stream_key)

        if (
            update.kind == "BOOK_SNAPSHOT"
            and update.source_sequence is not None
            and "DUPLICATE" not in flags
            and "OUT_OF_ORDER" not in flags
        ):
            self._unverified_book_streams.discard(stream_key)

        if update.kind == "BOOK_DELTA" and stream_key in self._unverified_book_streams:
            flags.add("UNVERIFIED_BOOK_STATE")

        identity_material = "|".join(
            [
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                stream,
                str(update.source_sequence) if update.source_sequence is not None else "-",
                str(update.revision),
                payload_digest,
                _utc_text(update.source_event_at),
                _utc_text(update.available_at),
                _utc_text(update.ingested_at),
                ",".join(sorted(flags)),
                _canonical(dict(update.raw_evidence_ref)),
            ]
        )
        event_id = str(uuid5(NAMESPACE_URL, identity_material))
        if update.source_sequence is not None and new_sequence:
            self._seen_sequence[sequence_identity] = (payload_digest, event_id)

        return NormalizedMarketEvent(
            event_id=event_id,
            instrument_version=self._instrument_version_id(instrument),
            kind=update.kind,
            source_event_at=update.source_event_at,
            available_at=update.available_at,
            availability_basis=update.availability_basis,
            ingested_at=update.ingested_at,
            source_sequence=update.source_sequence,
            revision=update.revision,
            payload_json=payload_json,
            quality_flags=tuple(sorted(flags)),
            raw_evidence_ref=update.raw_evidence_ref,
        )
