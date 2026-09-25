"""Fail-closed Binance Spot contract adapter foundation.

This module performs no networking, signing, credential storage or live
qualification. It only translates already-authorized canonical values and
recorded provider responses. A successful order ACK is never treated as a fill.
"""

from __future__ import annotations

from dataclasses import dataclass
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
class BinanceSpotPreparedRequest:
    endpoint: str
    body: Mapping[str, str]
    capability_snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def prepare_order_request(
    intent: BinanceSpotOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> BinanceSpotPreparedRequest:
    """Prepare but never sign/send a Spot order.

    Quantity is always base-asset quantity. Reverse MARKET quoteOrderQty is
    deliberately excluded from this foundation because its economic unit differs.
    """

    if not isinstance(intent, BinanceSpotOrderIntent):
        raise TypeError("intent must be BinanceSpotOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
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
    client_map = client_ids_by_order_id or {}
    if not isinstance(client_map, Mapping):
        raise BinanceSpotAdapterError("client_ids_by_order_id must be a mapping")

    by_id: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise BinanceSpotAdapterError(f"trade row {index} must be an object")
        symbol = _text(raw.get("symbol"), name=f"trade[{index}].symbol")
        if symbol not in instrument_versions:
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
        client_id = client_map.get(order_id)
        if client_id is not None:
            client_id = validate_client_order_id(client_id)

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
            instrument=_text(instrument_versions[symbol], name="instrument_version"),
            side="BUY" if is_buyer else "SELL",
            position_side="BOTH",
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
