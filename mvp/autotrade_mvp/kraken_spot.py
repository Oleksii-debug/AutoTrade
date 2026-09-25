"""Kraken Spot non-live adapter contract foundation.

The Spot and Derivatives API families are intentionally not merged. This module
only translates already-admitted cash-spot intents. It performs no HTTP request,
holds no credential and grants no financial authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
from uuid import UUID
import re

from .capabilities import CapabilitySnapshot
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


class KrakenSpotAdapterError(ValueError):
    """Raised when a Kraken Spot request cannot be represented safely."""


KRAKEN_SPOT_DOCS = MappingProxyType(
    {
        "api": "https://www.kraken.com/features/trading-api",
        "order_contract": "https://docs.kraken.com/api/docs/websocket-v2/add_order/",
    }
)

_FREE_CLIENT_ID = re.compile(r"^[\x21-\x7e]{1,18}$")
_ORDER_TYPES = frozenset({"MARKET", "LIMIT"})
_SIDES = frozenset({"BUY", "SELL"})
_TIME_IN_FORCE = frozenset({"GTC", "IOC"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KrakenSpotAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise KrakenSpotAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise KrakenSpotAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise KrakenSpotAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise KrakenSpotAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise KrakenSpotAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_spot_client_order_id(value: str) -> str:
    """Validate Kraken Spot cl_ord_id without inventing a provider format.

    Kraken accepts canonical UUID, 32 hexadecimal UUID text, or free-form ASCII
    text up to 18 characters. The dispatcher should therefore use max_length=18
    when supplying its normal prefixed deterministic identifier.
    """

    client_id = _text(value, name="client_order_id")
    try:
        parsed = UUID(client_id)
    except (ValueError, TypeError, AttributeError):
        parsed = None
    if parsed is not None and (str(parsed) == client_id.lower() or parsed.hex == client_id.lower()):
        return client_id
    if re.fullmatch(r"[0-9a-fA-F]{32}", client_id):
        return client_id
    if _FREE_CLIENT_ID.fullmatch(client_id) is None:
        raise KrakenSpotAdapterError(
            "client_order_id must be UUID/32-hex or printable ASCII of at most 18 characters"
        )
    return client_id


@dataclass(frozen=True)
class KrakenSpotOrderIntent:
    instrument_version: str
    pair: str
    side: str
    order_type: str
    volume: Decimal
    price: Decimal | None = None
    time_in_force: str = "GTC"
    post_only: bool = False

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        pair: str,
        side: str,
        order_type: str,
        volume,
        price=None,
        time_in_force: str = "GTC",
        post_only: bool = False,
    ) -> "KrakenSpotOrderIntent":
        side_value = _text(side, name="side").upper()
        order_value = _text(order_type, name="order_type").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if side_value not in _SIDES:
            raise KrakenSpotAdapterError("side must be BUY or SELL")
        if order_value not in _ORDER_TYPES:
            raise KrakenSpotAdapterError("only MARKET and LIMIT are admitted by this Spot foundation")
        if tif not in _TIME_IN_FORCE:
            raise KrakenSpotAdapterError("only GTC and IOC are admitted by this Spot foundation")
        if type(post_only) is not bool:
            raise KrakenSpotAdapterError("post_only must be boolean")
        volume_value = _decimal(volume, name="volume", positive=True)
        price_value = None if price is None else _decimal(price, name="price", positive=True)
        if order_value == "LIMIT" and price_value is None:
            raise KrakenSpotAdapterError("price is required for a limit order")
        if order_value == "MARKET" and price_value is not None:
            raise KrakenSpotAdapterError("price must be omitted for a market order")
        if post_only and order_value != "LIMIT":
            raise KrakenSpotAdapterError("post_only is valid only for limit orders")
        if post_only and tif == "IOC":
            raise KrakenSpotAdapterError("post_only and IOC are mutually exclusive")
        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            pair=_text(pair, name="pair").upper(),
            side=side_value,
            order_type=order_value,
            volume=volume_value,
            price=price_value,
            time_in_force=tif,
            post_only=post_only,
        )


@dataclass(frozen=True)
class KrakenSpotPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def prepare_spot_order_request(
    intent: KrakenSpotOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> KrakenSpotPreparedRequest:
    """Prepare a logical AddOrder request without nonce, signature or deadline.

    Nonce/authentication and any wall-clock deadline belong at the transport
    boundary after GuardedDispatcher's final authority check.
    """

    if not isinstance(intent, KrakenSpotOrderIntent):
        raise TypeError("intent must be KrakenSpotOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_spot_client_order_id(client_order_id)
    if capability.provider_id.upper() != "KRAKEN":
        raise KrakenSpotAdapterError("capability belongs to another provider")
    if capability.instrument_version != intent.instrument_version:
        raise KrakenSpotAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise KrakenSpotAdapterError("exact capability evidence does not admit this order")

    body: dict[str, object] = {
        "pair": intent.pair,
        "type": intent.side.lower(),
        "ordertype": intent.order_type.lower(),
        "volume": _decimal_text(intent.volume),
        "cl_ord_id": client_id,
        "timeinforce": intent.time_in_force.lower(),
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.post_only:
        body["oflags"] = "post"

    return KrakenSpotPreparedRequest(
        endpoint="/0/private/AddOrder",
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(KRAKEN_SPOT_DOCS.values()),
    )


@dataclass(frozen=True)
class KrakenSpotSubmissionAck:
    provider_order_ids: tuple[str, ...]
    description: str | None

    @property
    def proves_fill(self) -> bool:
        return False


def parse_spot_submission_response(payload: Mapping[str, object]) -> KrakenSpotSubmissionAck:
    """Parse AddOrder acknowledgement without converting it into execution."""

    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    errors = payload.get("error", ())
    if isinstance(errors, (str, bytes)) or not isinstance(errors, (list, tuple)):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    nonempty_errors = tuple(str(item) for item in errors if str(item))
    if nonempty_errors:
        raise KrakenSpotAdapterError("provider rejected request: " + "; ".join(nonempty_errors))

    result = payload.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError("successful response must contain a result object")
    txids = result.get("txid")
    if isinstance(txids, (str, bytes)) or not isinstance(txids, (list, tuple)) or not txids:
        raise KrakenSpotAdapterError("successful response must contain one or more transaction ids")
    normalized = tuple(_text(str(value), name="txid") for value in txids)
    if len(set(normalized)) != len(normalized):
        raise KrakenSpotAdapterError("provider transaction ids must be unique")
    descr = result.get("descr")
    description = None
    if isinstance(descr, Mapping) and descr.get("order") is not None:
        description = _text(str(descr["order"]), name="description")
    return KrakenSpotSubmissionAck(provider_order_ids=normalized, description=description)


@dataclass(frozen=True)
class KrakenSpotAbsenceEvidence:
    order_found: bool
    open_orders_complete: bool
    closed_orders_complete: bool
    trades_complete: bool
    ledgers_complete: bool
    consistency_horizon_satisfied: bool

    def __post_init__(self) -> None:
        for field in (
            "order_found",
            "open_orders_complete",
            "closed_orders_complete",
            "trades_complete",
            "ledgers_complete",
            "consistency_horizon_satisfied",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.open_orders_complete
            and self.closed_orders_complete
            and self.trades_complete
            and self.ledgers_complete
            and self.consistency_horizon_satisfied
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


def derivatives_supported_by_this_module() -> bool:
    """Make the separation explicit: Kraken Derivatives needs its own adapter."""

    return False


def _seconds_to_utc(value, *, name: str) -> str:
    seconds = _decimal(value, name=name)
    if seconds < 0:
        raise KrakenSpotAdapterError(f"{name} cannot be negative")
    micros = seconds * Decimal("1000000")
    if micros != micros.to_integral_value():
        raise KrakenSpotAdapterError(f"{name} has precision finer than one microsecond")
    total_micros = int(micros)
    whole_seconds, remainder = divmod(total_micros, 1_000_000)
    instant = datetime.fromtimestamp(whole_seconds, tz=timezone.utc) + timedelta(
        microseconds=remainder
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_trade_history(
    response: Mapping[str, object],
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_provider_order: Mapping[str, str],
    fee_currency_by_pair: Mapping[str, str],
) -> tuple[ProviderFillEvidence, ...]:
    """Map recorded Kraken TradesHistory rows into canonical unique fills.

    Kraken trade rows do not safely imply AutoTrade instrument versions, client
    identities or fee currency. Those mappings must come from separately
    evidenced metadata/order state and are therefore explicit inputs.
    """

    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    raw_errors = response.get("error")
    if isinstance(raw_errors, (str, bytes)) or not isinstance(raw_errors, (list, tuple)):
        raise KrakenSpotAdapterError("Kraken error field must be a sequence")
    if any(str(value) for value in raw_errors):
        raise KrakenSpotAdapterError("Kraken TradesHistory response was not successful")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise KrakenSpotAdapterError("TradesHistory result must be an object")
    trades = result.get("trades")
    if not isinstance(trades, Mapping):
        raise KrakenSpotAdapterError("TradesHistory trades must be an object")
    for name, value in (
        ("instrument_versions", instrument_versions),
        ("client_ids_by_provider_order", client_ids_by_provider_order),
        ("fee_currency_by_pair", fee_currency_by_pair),
    ):
        if not isinstance(value, Mapping):
            raise TypeError(f"{name} must be a mapping")

    fills: list[ProviderFillEvidence] = []
    for trade_id, raw in trades.items():
        execution_id = _text(str(trade_id), name="trade id")
        if not isinstance(raw, Mapping):
            raise KrakenSpotAdapterError(f"trade {execution_id} must be an object")
        pair = _text(str(raw.get("pair", "")), name="pair")
        provider_order_id = _text(str(raw.get("ordertxid", "")), name="ordertxid")
        if pair not in instrument_versions:
            raise KrakenSpotAdapterError(f"unmapped Kraken pair: {pair}")
        if pair not in fee_currency_by_pair:
            raise KrakenSpotAdapterError(
                f"missing evidenced fee currency for Kraken pair: {pair}"
            )
        client_id = client_ids_by_provider_order.get(provider_order_id)
        if client_id is not None:
            client_id = validate_spot_client_order_id(client_id)
        fills.append(
            ProviderFillEvidence.create(
                provider_execution_id=execution_id,
                client_order_id=client_id,
                instrument=_text(instrument_versions[pair], name="instrument_version"),
                quantity=raw.get("vol"),
                price=raw.get("price"),
                fee_amount=raw.get("fee", "0"),
                fee_currency=_text(fee_currency_by_pair[pair], name="fee_currency"),
                trade_time=_seconds_to_utc(raw.get("time"), name="time"),
            )
        )
    return tuple(fills)


def coverage_evidence(
    *,
    surface: str,
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Create canonical coverage without guessing provider absence semantics."""

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise KrakenSpotAdapterError("unsupported Kraken reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise TypeError(f"{name} must be boolean")
    if qualified_exclusion_semantics:
        raise KrakenSpotAdapterError(
            "Kraken Spot foundation cannot self-assert provider exclusion semantics"
        )
    return CoverageSurfaceEvidence(
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=False,
    )
