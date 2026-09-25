"""Fail-closed Binance USD-M Futures contract adapter foundation.

This module performs no networking, signing, credential storage or live
qualification. It prepares already-authorized canonical LIMIT/MARKET requests
and maps recorded provider observations into the existing AutoTrade
reconciliation contracts. Order acknowledgement is never execution evidence.
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
    BoundReconciliationResponse,
    ProviderCoreError,
    require_reconciliation_response,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


BINANCE_USDM_ENDPOINTS: Mapping[str, str] = MappingProxyType(
    {
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
)

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
        provider_symbol = _text(symbol, name="symbol")
        if provider_symbol != provider_symbol.upper():
            raise BinanceUsdmAdapterError("Binance USD-M symbol must be uppercase")

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
class BinanceUsdmPreparedRequest:
    endpoint: str
    body: Mapping[str, str]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


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
    at: datetime,
) -> BinanceUsdmPreparedRequest:
    """Prepare but never sign or send a USD-M order."""

    if not isinstance(intent, BinanceUsdmOrderIntent):
        raise TypeError("intent must be BinanceUsdmOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")

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

    symbol = _text(response.get("symbol"), name="response.symbol")
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
    evidence: BoundReconciliationResponse,
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_order_id: Mapping[int, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map one provenance-bound USD-M user-trade response to fill evidence."""

    rows, account_id, environment = require_reconciliation_response(
        evidence,
        provider_id="BINANCE",
        surface="EXECUTIONS",
        endpoint=BINANCE_USDM_ENDPOINTS["EXECUTIONS"],
    )
    if not isinstance(rows, list):
        raise BinanceUsdmAdapterError("trade rows must be an array")
    if not isinstance(instrument_versions, Mapping):
        raise BinanceUsdmAdapterError("instrument_versions must be a mapping")
    client_map = client_ids_by_order_id or {}
    if not isinstance(client_map, Mapping):
        raise BinanceUsdmAdapterError(
            "client_ids_by_order_id must be a mapping"
        )

    by_id: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(rows):
        if not isinstance(raw, Mapping):
            raise BinanceUsdmAdapterError(
                f"trade row {index} must be an object"
            )

        symbol = _text(raw.get("symbol"), name=f"trade[{index}].symbol")
        if symbol not in instrument_versions:
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

        position_side = _text(
            raw.get("positionSide"), name=f"trade[{index}].positionSide"
        ).upper()
        if position_side not in _ALLOWED_POSITION_SIDES:
            raise BinanceUsdmAdapterError(
                "trade positionSide must be BOTH, LONG or SHORT"
            )

        execution_id = f"BINANCE-USDM:{symbol}:{trade_id}"
        client_id = client_map.get(order_id)
        if client_id is not None:
            client_id = validate_client_order_id(client_id)

        fill = ProviderFillEvidence.create(
            provider_id="BINANCE",
            account_id=account_id,
            environment=environment,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=_text(
                instrument_versions[symbol], name="instrument_version"
            ),
            quantity=raw.get("qty"),
            price=raw.get("price"),
            fee_amount=raw.get("commission"),
            fee_currency=_text(
                raw.get("commissionAsset"), name="commissionAsset"
            ),
            trade_time=_millis(raw.get("time"), name="trade.time"),
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
