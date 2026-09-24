"""Immutable instrument-version registry for the network-free AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Iterable, Mapping
from uuid import UUID


class InstrumentRegistryError(ValueError):
    """Base error for invalid instrument metadata or lookups."""


class InstrumentConflict(InstrumentRegistryError):
    """Raised when immutable identities or symbol intervals conflict."""


class InstrumentNotFound(InstrumentRegistryError):
    """Raised when no instrument version is valid for the requested instant."""


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InstrumentRegistryError(f"{field} is required")
    return value.strip()


def _decimal(value: Decimal | str | int, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise InstrumentRegistryError(f"{field} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise InstrumentRegistryError(f"{field} must be a finite decimal") from error
    if not result.is_finite():
        raise InstrumentRegistryError(f"{field} must be a finite decimal")
    if positive and result <= 0:
        raise InstrumentRegistryError(f"{field} must be positive")
    return result


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InstrumentRegistryError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


@dataclass(frozen=True)
class OffsetTransition:
    effective_from: datetime
    utc_offset_minutes: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "effective_from", _utc(self.effective_from, "effective_from"))
        if isinstance(self.utc_offset_minutes, bool) or not isinstance(self.utc_offset_minutes, int):
            raise InstrumentRegistryError("utc_offset_minutes must be an integer")
        if not -14 * 60 <= self.utc_offset_minutes <= 14 * 60:
            raise InstrumentRegistryError("utc_offset_minutes is outside supported bounds")


@dataclass(frozen=True)
class WeeklySession:
    weekday: int
    open_minute: int
    close_minute: int

    def __post_init__(self) -> None:
        if isinstance(self.weekday, bool) or self.weekday not in range(7):
            raise InstrumentRegistryError("weekday must be between 0 and 6")
        if not 0 <= self.open_minute < 24 * 60:
            raise InstrumentRegistryError("open_minute is outside the day")
        if not 0 < self.close_minute <= 24 * 60:
            raise InstrumentRegistryError("close_minute is outside the day")
        if self.open_minute >= self.close_minute:
            raise InstrumentRegistryError("overnight or empty sessions require an explicit split")


@dataclass(frozen=True)
class TradingCalendar:
    calendar_id: str
    timezone_id: str
    sessions: tuple[WeeklySession, ...] = ()
    transitions: tuple[OffsetTransition, ...] = ()
    continuous: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "calendar_id", _text(self.calendar_id, "calendar_id"))
        object.__setattr__(self, "timezone_id", _text(self.timezone_id, "timezone_id"))
        object.__setattr__(self, "sessions", tuple(self.sessions))
        ordered = tuple(sorted(tuple(self.transitions), key=lambda item: item.effective_from))
        if len({item.effective_from for item in ordered}) != len(ordered):
            raise InstrumentRegistryError("calendar transition instants must be unique")
        object.__setattr__(self, "transitions", ordered)
        if not self.continuous and not self.sessions:
            raise InstrumentRegistryError("non-continuous calendar requires sessions")

    @classmethod
    def continuous_24_7(cls, calendar_id: str = "CONTINUOUS_24_7") -> "TradingCalendar":
        return cls(
            calendar_id=calendar_id,
            timezone_id="UTC",
            continuous=True,
            transitions=(OffsetTransition(datetime(1970, 1, 1, tzinfo=timezone.utc), 0),),
        )

    def _offset_minutes(self, instant: datetime) -> int:
        point = _utc(instant, "instant")
        applicable = [item for item in self.transitions if item.effective_from <= point]
        if not applicable:
            raise InstrumentRegistryError("calendar has no offset evidence for requested instant")
        return applicable[-1].utc_offset_minutes

    def is_open(self, instant: datetime) -> bool:
        point = _utc(instant, "instant")
        if self.continuous:
            return True
        local = point + timedelta(minutes=self._offset_minutes(point))
        minute = local.hour * 60 + local.minute
        return any(
            session.weekday == local.weekday()
            and session.open_minute <= minute < session.close_minute
            for session in self.sessions
        )


@dataclass(frozen=True)
class DeliverableLeg:
    asset_id: str
    quantity: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _text(self.asset_id, "asset_id"))
        object.__setattr__(self, "quantity", _decimal(self.quantity, "quantity", positive=True))


@dataclass(frozen=True)
class InstrumentVersion:
    instrument_id: str
    version: int
    provider_id: str
    venue_id: str
    provider_symbol: str
    asset_class: str
    base_currency: str
    quote_currency: str
    settlement_currency: str
    quantity_unit: str
    contract_multiplier: Decimal
    price_tick: Decimal
    quantity_step: Decimal
    minimum_quantity: Decimal
    calendar_id: str
    timezone_id: str
    effective_from: datetime
    status: str = "ACTIVE"
    effective_to: datetime | None = None
    minimum_notional_amount: Decimal | None = None
    minimum_notional_currency: str | None = None
    maximum_quantity: Decimal | None = None
    price_band_low: Decimal | None = None
    price_band_high: Decimal | None = None
    payoff: str | None = None
    underlying_id: str | None = None
    expiry: datetime | None = None
    last_trade_at: datetime | None = None
    delivery_cutoff: datetime | None = None
    settlement_method: str | None = None
    funding_schedule: str | None = None
    strike: Decimal | None = None
    option_right: str | None = None
    exercise_style: str | None = None
    deliverable: tuple[DeliverableLeg, ...] = ()
    margin_model_id: str | None = None
    metadata_evidence: tuple[Mapping[str, object], ...] = ()

    def __post_init__(self) -> None:
        try:
            UUID(self.instrument_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise InstrumentRegistryError("instrument_id must be a UUID") from error
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise InstrumentRegistryError("version must be a positive integer")

        for field in (
            "provider_id",
            "venue_id",
            "provider_symbol",
            "base_currency",
            "quote_currency",
            "settlement_currency",
            "quantity_unit",
            "calendar_id",
            "timezone_id",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))

        if self.asset_class not in {
            "CASH_EQUITY",
            "FUND",
            "FX",
            "CRYPTO_SPOT",
            "FUTURE",
            "PERPETUAL",
            "OPTION",
        }:
            raise InstrumentRegistryError("asset_class is unsupported")
        if self.status not in {"ACTIVE", "INACTIVE", "DELISTED", "EXPIRED"}:
            raise InstrumentRegistryError("status is unsupported")

        for field in ("contract_multiplier", "price_tick", "quantity_step", "minimum_quantity"):
            object.__setattr__(self, field, _decimal(getattr(self, field), field, positive=True))

        if self.maximum_quantity is not None:
            maximum = _decimal(self.maximum_quantity, "maximum_quantity", positive=True)
            if maximum < self.minimum_quantity:
                raise InstrumentRegistryError("maximum_quantity is below minimum_quantity")
            object.__setattr__(self, "maximum_quantity", maximum)

        if self.minimum_notional_amount is not None:
            amount = _decimal(self.minimum_notional_amount, "minimum_notional_amount", positive=True)
            object.__setattr__(self, "minimum_notional_amount", amount)
            if self.minimum_notional_currency is None:
                raise InstrumentRegistryError(
                    "minimum_notional_currency is required with minimum_notional_amount"
                )
        if self.minimum_notional_currency is not None:
            object.__setattr__(
                self,
                "minimum_notional_currency",
                _text(self.minimum_notional_currency, "minimum_notional_currency"),
            )
            if self.minimum_notional_amount is None:
                raise InstrumentRegistryError(
                    "minimum_notional_amount is required with minimum_notional_currency"
                )

        if (self.price_band_low is None) != (self.price_band_high is None):
            raise InstrumentRegistryError("price bands require both low and high")
        if self.price_band_low is not None:
            low = _decimal(self.price_band_low, "price_band_low", positive=True)
            high = _decimal(self.price_band_high, "price_band_high", positive=True)
            if low >= high:
                raise InstrumentRegistryError("price_band_low must be below price_band_high")
            object.__setattr__(self, "price_band_low", low)
            object.__setattr__(self, "price_band_high", high)

        start = _utc(self.effective_from, "effective_from")
        object.__setattr__(self, "effective_from", start)
        for field in ("effective_to", "expiry", "last_trade_at", "delivery_cutoff"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _utc(value, field))
        if self.effective_to is not None and self.effective_to <= start:
            raise InstrumentRegistryError("effective_to must be after effective_from")

        object.__setattr__(self, "deliverable", tuple(self.deliverable))
        frozen_evidence = tuple(MappingProxyType(dict(item)) for item in self.metadata_evidence)
        object.__setattr__(self, "metadata_evidence", frozen_evidence)

        derivative = self.asset_class in {"FUTURE", "PERPETUAL", "OPTION"}
        if derivative:
            if self.payoff not in {"LINEAR", "INVERSE", "OPTION"}:
                raise InstrumentRegistryError("derivative payoff is required")
            if self.underlying_id is None:
                raise InstrumentRegistryError("derivative underlying_id is required")
            if self.settlement_method is None:
                raise InstrumentRegistryError("derivative settlement_method is required")
            if self.margin_model_id is None:
                raise InstrumentRegistryError("derivative margin_model_id is required")
            object.__setattr__(self, "underlying_id", _text(self.underlying_id, "underlying_id"))
            object.__setattr__(
                self, "settlement_method", _text(self.settlement_method, "settlement_method")
            )
            object.__setattr__(
                self, "margin_model_id", _text(self.margin_model_id, "margin_model_id")
            )
        elif any(
            value is not None
            for value in (
                self.payoff,
                self.underlying_id,
                self.expiry,
                self.last_trade_at,
                self.delivery_cutoff,
                self.settlement_method,
                self.funding_schedule,
                self.strike,
                self.option_right,
                self.exercise_style,
                self.margin_model_id,
            )
        ) or self.deliverable:
            raise InstrumentRegistryError("derivative fields are not valid for this asset class")

        if self.asset_class in {"FUTURE", "OPTION"} and self.expiry is None:
            raise InstrumentRegistryError("dated derivative requires expiry")
        if self.asset_class == "PERPETUAL":
            if self.expiry is not None:
                raise InstrumentRegistryError("perpetual must not invent an expiry")
            if self.funding_schedule is None:
                raise InstrumentRegistryError("perpetual funding_schedule is required")
            object.__setattr__(
                self, "funding_schedule", _text(self.funding_schedule, "funding_schedule")
            )
            if self.payoff == "OPTION":
                raise InstrumentRegistryError("perpetual payoff cannot be OPTION")
        elif self.funding_schedule is not None:
            raise InstrumentRegistryError("funding_schedule is only valid for perpetuals")

        if self.asset_class == "OPTION":
            if self.payoff != "OPTION":
                raise InstrumentRegistryError("option payoff must be OPTION")
            if self.strike is None:
                raise InstrumentRegistryError("option strike is required")
            object.__setattr__(self, "strike", _decimal(self.strike, "strike", positive=True))
            if self.option_right not in {"CALL", "PUT"}:
                raise InstrumentRegistryError("option_right must be CALL or PUT")
            if self.exercise_style is None:
                raise InstrumentRegistryError("exercise_style is required")
            object.__setattr__(
                self, "exercise_style", _text(self.exercise_style, "exercise_style")
            )
            if not self.deliverable:
                raise InstrumentRegistryError("option deliverable is required")
        elif self.strike is not None or self.option_right is not None or self.exercise_style is not None:
            raise InstrumentRegistryError("option-only fields are not valid for this asset class")

    def contains(self, instant: datetime, implicit_end: datetime | None = None) -> bool:
        point = _utc(instant, "instant")
        end = self.effective_to
        if implicit_end is not None and (end is None or implicit_end < end):
            end = implicit_end
        return self.effective_from <= point and (end is None or point < end)

    def validate_price(self, price: Decimal | str | int) -> Decimal:
        value = _decimal(price, "price", positive=True)
        if value % self.price_tick != 0:
            raise InstrumentRegistryError("price is not aligned to price_tick")
        if self.price_band_low is not None and value < self.price_band_low:
            raise InstrumentRegistryError("price is below price_band_low")
        if self.price_band_high is not None and value > self.price_band_high:
            raise InstrumentRegistryError("price is above price_band_high")
        return value

    def validate_quantity(self, quantity: Decimal | str | int) -> Decimal:
        value = _decimal(quantity, "quantity", positive=True)
        if value < self.minimum_quantity:
            raise InstrumentRegistryError("quantity is below minimum_quantity")
        if self.maximum_quantity is not None and value > self.maximum_quantity:
            raise InstrumentRegistryError("quantity is above maximum_quantity")
        if value % self.quantity_step != 0:
            raise InstrumentRegistryError("quantity is not aligned to quantity_step")
        return value

    def to_contract_dict(self) -> dict:
        payload = {
            "instrument_id": self.instrument_id,
            "version": str(self.version),
            "provider_id": self.provider_id,
            "venue_id": self.venue_id,
            "provider_symbol": self.provider_symbol,
            "asset_class": self.asset_class,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "settlement_currency": self.settlement_currency,
            "quantity_unit": self.quantity_unit,
            "contract_multiplier": _decimal_text(self.contract_multiplier),
            "price_tick": _decimal_text(self.price_tick),
            "quantity_step": _decimal_text(self.quantity_step),
            "minimum_quantity": _decimal_text(self.minimum_quantity),
            "calendar_id": self.calendar_id,
            "timezone_id": self.timezone_id,
            "effective_from": _utc_text(self.effective_from),
            "status": self.status,
            "metadata_evidence": [dict(item) for item in self.metadata_evidence],
        }
        optional = {
            "effective_to": _utc_text(self.effective_to) if self.effective_to else None,
            "maximum_quantity": (
                _decimal_text(self.maximum_quantity) if self.maximum_quantity is not None else None
            ),
            "payoff": self.payoff,
            "underlying_id": self.underlying_id,
            "expiry": _utc_text(self.expiry) if self.expiry else None,
            "last_trade_at": _utc_text(self.last_trade_at) if self.last_trade_at else None,
            "delivery_cutoff": _utc_text(self.delivery_cutoff) if self.delivery_cutoff else None,
            "settlement_method": self.settlement_method,
            "funding_schedule": self.funding_schedule,
            "strike": _decimal_text(self.strike) if self.strike is not None else None,
            "option_right": self.option_right,
            "exercise_style": self.exercise_style,
            "margin_model_id": self.margin_model_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.minimum_notional_amount is not None:
            payload["minimum_notional"] = {
                "amount": _decimal_text(self.minimum_notional_amount),
                "currency": self.minimum_notional_currency,
            }
        if self.price_band_low is not None:
            payload["price_bands"] = {
                "low": _decimal_text(self.price_band_low),
                "high": _decimal_text(self.price_band_high),
            }
        if self.deliverable:
            payload["deliverable"] = [
                {"asset_id": leg.asset_id, "quantity": _decimal_text(leg.quantity)}
                for leg in self.deliverable
            ]
        return payload


class InstrumentRegistry:
    """Append-only registry whose history remains stable across metadata changes."""

    def __init__(
        self,
        calendars: Iterable[TradingCalendar] = (),
        versions: Iterable[InstrumentVersion] = (),
    ) -> None:
        self._calendars: dict[str, TradingCalendar] = {
            "CONTINUOUS_24_7": TradingCalendar.continuous_24_7()
        }
        self._versions: dict[str, list[InstrumentVersion]] = {}
        for calendar in calendars:
            self.add_calendar(calendar)
        for version in versions:
            self.add(version)

    def add_calendar(self, calendar: TradingCalendar) -> None:
        existing = self._calendars.get(calendar.calendar_id)
        if existing is not None and existing != calendar:
            raise InstrumentConflict("calendar_id is immutable")
        self._calendars[calendar.calendar_id] = calendar

    def _candidate_state(self, candidate: InstrumentVersion) -> dict[str, list[InstrumentVersion]]:
        state = {key: list(value) for key, value in self._versions.items()}
        state.setdefault(candidate.instrument_id, []).append(candidate)
        state[candidate.instrument_id].sort(key=lambda item: item.version)
        return state

    @staticmethod
    def _intervals(state: dict[str, list[InstrumentVersion]]):
        for instrument_id, versions in state.items():
            ordered = sorted(versions, key=lambda item: item.version)
            for index, version in enumerate(ordered):
                implicit_end = (
                    ordered[index + 1].effective_from if index + 1 < len(ordered) else None
                )
                end = version.effective_to
                if implicit_end is not None and (end is None or implicit_end < end):
                    end = implicit_end
                yield instrument_id, version, version.effective_from, end

    @staticmethod
    def _overlap(
        left_start: datetime,
        left_end: datetime | None,
        right_start: datetime,
        right_end: datetime | None,
    ) -> bool:
        left_limit = left_end or datetime.max.replace(tzinfo=timezone.utc)
        right_limit = right_end or datetime.max.replace(tzinfo=timezone.utc)
        return left_start < right_limit and right_start < left_limit

    def add(self, version: InstrumentVersion) -> None:
        calendar = self._calendars.get(version.calendar_id)
        if calendar is None:
            raise InstrumentRegistryError("calendar_id is unknown")
        if calendar.timezone_id != version.timezone_id:
            raise InstrumentRegistryError("instrument timezone_id conflicts with its calendar")

        existing = self._versions.get(version.instrument_id, [])
        if existing:
            expected = existing[-1].version + 1
            if version.version != expected:
                raise InstrumentConflict(f"next version must be {expected}")
            if version.effective_from <= existing[-1].effective_from:
                raise InstrumentConflict("new version must start after the previous version")
        elif version.version != 1:
            raise InstrumentConflict("first instrument version must be 1")

        state = self._candidate_state(version)
        intervals = list(self._intervals(state))
        for index, (left_id, left, left_start, left_end) in enumerate(intervals):
            for right_id, right, right_start, right_end in intervals[index + 1 :]:
                if left_id == right_id:
                    continue
                if (
                    left.provider_id == right.provider_id
                    and left.venue_id == right.venue_id
                    and left.provider_symbol == right.provider_symbol
                    and self._overlap(left_start, left_end, right_start, right_end)
                ):
                    raise InstrumentConflict(
                        "provider symbol is assigned to different instruments in overlapping intervals"
                    )
        self._versions = state

    def versions(self, instrument_id: str) -> tuple[InstrumentVersion, ...]:
        return tuple(self._versions.get(instrument_id, ()))

    def at(self, instrument_id: str, instant: datetime) -> InstrumentVersion:
        versions = self._versions.get(instrument_id)
        if not versions:
            raise InstrumentNotFound("instrument_id is unknown")
        for index in range(len(versions) - 1, -1, -1):
            version = versions[index]
            implicit_end = (
                versions[index + 1].effective_from if index + 1 < len(versions) else None
            )
            if version.contains(instant, implicit_end):
                return version
        raise InstrumentNotFound("no instrument version is effective at requested instant")

    def resolve(
        self,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        instant: datetime,
    ) -> InstrumentVersion:
        provider = _text(provider_id, "provider_id")
        venue = _text(venue_id, "venue_id")
        symbol = _text(provider_symbol, "provider_symbol")
        matches = []
        for instrument_id in self._versions:
            try:
                version = self.at(instrument_id, instant)
            except InstrumentNotFound:
                continue
            if (
                version.provider_id == provider
                and version.venue_id == venue
                and version.provider_symbol == symbol
            ):
                matches.append(version)
        if not matches:
            raise InstrumentNotFound("provider symbol is not effective at requested instant")
        if len(matches) > 1:
            raise InstrumentConflict("provider symbol resolution is ambiguous")
        return matches[0]

    def require_tradable(self, instrument_id: str, instant: datetime) -> InstrumentVersion:
        version = self.at(instrument_id, instant)
        if version.status != "ACTIVE":
            raise InstrumentRegistryError(f"instrument status is {version.status}")
        calendar = self._calendars[version.calendar_id]
        if not calendar.is_open(instant):
            raise InstrumentRegistryError("instrument calendar is closed")
        point = _utc(instant, "instant")
        if version.last_trade_at is not None and point > version.last_trade_at:
            raise InstrumentRegistryError("instrument is past last_trade_at")
        if version.expiry is not None and point >= version.expiry:
            raise InstrumentRegistryError("instrument is expired")
        return version
