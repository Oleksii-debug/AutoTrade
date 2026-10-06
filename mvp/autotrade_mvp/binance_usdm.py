"""Fail-closed Binance USD-M Futures contract adapter foundation.

This module performs no networking, signing, credential storage or live
qualification. It prepares already-authorized canonical LIMIT/MARKET requests
and maps recorded provider observations into the existing AutoTrade
reconciliation contracts. Order acknowledgement is never execution evidence.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .capabilities import CapabilitySnapshot
from .exact_decimal import ExactDecimalError, parse_bounded_exact_decimal
from .provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    provider_response_observation_require_scope,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


_MAX_UNIX_MILLIS = 253_402_300_799_999
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_PREPARED_REQUEST_TOKEN = object()


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


def _exact_json_object(value: object, *, name: str) -> dict[str, object]:
    """Admit one raw decoded JSON object without caller mapping/key callbacks."""

    if type(value) is not dict:
        raise BinanceUsdmAdapterError(f"{name} must be an exact decoded object")
    for key in value:
        if type(key) is not str:
            raise BinanceUsdmAdapterError(
                f"{name} keys must be exact decoded strings"
            )
    return value


def _exact_decoded_json_tree(value: object, *, name: str) -> object:
    """Reject executable/aliased Python objects before provider evidence hashing."""

    stack = [value]
    seen_containers: set[int] = set()
    while stack:
        current = stack.pop()
        if current is None or type(current) in (str, bool, int, float):
            continue
        if type(current) is dict:
            identity = id(current)
            if identity in seen_containers:
                raise BinanceUsdmAdapterError(
                    f"{name} must be a decoded JSON tree without aliases or cycles"
                )
            seen_containers.add(identity)
            for key, item in current.items():
                if type(key) is not str:
                    raise BinanceUsdmAdapterError(
                        f"{name} keys must be exact decoded strings"
                    )
                stack.append(item)
            continue
        if type(current) is list:
            identity = id(current)
            if identity in seen_containers:
                raise BinanceUsdmAdapterError(
                    f"{name} must be a decoded JSON tree without aliases or cycles"
                )
            seen_containers.add(identity)
            stack.extend(current)
            continue
        raise BinanceUsdmAdapterError(
            f"{name} must contain only exact decoded JSON values"
        )
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str:
        raise BinanceUsdmAdapterError(f"{name} is required")
    stripped = value.strip()
    if not stripped:
        raise BinanceUsdmAdapterError(f"{name} is required")
    return stripped


def _freeze_request_body(value: object, *, name: str) -> Mapping[str, str]:
    """Freeze an internally-shaped provider request without mapping callbacks."""

    if type(value) is not dict:
        raise BinanceUsdmAdapterError(f"{name} must be an exact request mapping")
    frozen: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str or type(item) is not str:
            raise BinanceUsdmAdapterError(
                f"{name} keys and values must be exact strings"
            )
        frozen[key] = item
    return MappingProxyType(frozen)


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
    try:
        result = parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise BinanceUsdmAdapterError(f"{name} must be a bounded exact decimal") from error
    if positive and result <= 0:
        raise BinanceUsdmAdapterError(f"{name} must be positive")
    return result


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value, "f")


def _utc(value: datetime, *, name: str) -> datetime:
    # Keep caller-controlled datetime/tzinfo callbacks outside provider
    # admission.  Exact stdlib datetime + datetime.timezone admits UTC and
    # fixed offsets without invoking polymorphic temporal authority.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise BinanceUsdmAdapterError(
            f"{name} must be an exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _millis(value: object, *, name: str) -> str:
    if type(value) is int:
        raw = value
    elif type(value) is str and value.isdigit():
        # Reject oversized provider text before Python integer materialization.
        # The financial ingress contract owns a finite UTC time domain.
        if len(value) > len(str(_MAX_UNIX_MILLIS)):
            raise BinanceUsdmAdapterError(
                f"{name} exceeds the supported UTC millisecond range"
            )
        raw = int(value)
        if str(raw) != value:
            raise BinanceUsdmAdapterError(
                f"{name} must be a canonical non-negative integer millisecond timestamp"
            )
    else:
        raise BinanceUsdmAdapterError(
            f"{name} must be an integer millisecond timestamp"
        )
    if raw < 0 or raw > _MAX_UNIX_MILLIS:
        raise BinanceUsdmAdapterError(
            f"{name} exceeds the supported UTC millisecond range"
        )
    seconds, remainder = divmod(raw, 1000)
    instant = _UNIX_EPOCH + timedelta(seconds=seconds, milliseconds=remainder)
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
class BinanceUsdmPreparedRequest:
    endpoint: str
    body: Mapping[str, str]
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]
    _preparation_token: InitVar[object | None] = None

    def __post_init__(self, _preparation_token: object | None) -> None:
        if _preparation_token is not _PREPARED_REQUEST_TOKEN:
            raise BinanceUsdmAdapterError(
                "prepared request must come from canonical order preparation"
            )
        object.__setattr__(
            self,
            "body",
            _freeze_request_body(self.body, name="prepared request body"),
        )


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

    if type(intent) is not BinanceUsdmOrderIntent:
        raise TypeError("intent must be exact BinanceUsdmOrderIntent")
    if type(capability) is not CapabilitySnapshot:
        raise TypeError("capability must be exact CapabilitySnapshot")
    canonical_intent = BinanceUsdmOrderIntent.create(
        instrument_version=intent.instrument_version,
        symbol=intent.symbol,
        side=intent.side,
        order_type=intent.order_type,
        quantity=intent.quantity,
        price=intent.price,
        time_in_force=intent.time_in_force,
        position_side=intent.position_side,
        reduce_only=intent.reduce_only,
    )

    point = _utc(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    if capability.provider_id.upper() != "BINANCE":
        raise BinanceUsdmAdapterError("capability belongs to another provider")
    if capability.instrument_version != canonical_intent.instrument_version:
        raise BinanceUsdmAdapterError(
            "capability instrument version does not match intent"
        )
    if not capability.admits(
        at=point,
        order_type=canonical_intent.order_type,
        time_in_force=canonical_intent.time_in_force or "NONE",
        permission_scope="ORDER_WRITE",
    ):
        raise BinanceUsdmAdapterError(
            "exact capability evidence does not admit this order"
        )

    _require_position_mode(capability=capability, intent=canonical_intent)

    body: dict[str, str] = {
        "symbol": canonical_intent.symbol,
        "side": canonical_intent.side,
        "type": canonical_intent.order_type,
        "quantity": _decimal_text(canonical_intent.quantity),
        "newClientOrderId": client_id,
        "newOrderRespType": "ACK",
        "positionSide": canonical_intent.position_side,
    }
    if canonical_intent.price is not None:
        body["price"] = _decimal_text(canonical_intent.price)
    if canonical_intent.time_in_force is not None:
        body["timeInForce"] = canonical_intent.time_in_force
    if canonical_intent.reduce_only:
        body["reduceOnly"] = "true"

    # timestamp, recvWindow, API key and signature belong to the separately
    # qualified transport after GuardedDispatcher's final authority barrier.
    return BinanceUsdmPreparedRequest(
        endpoint=BINANCE_USDM_ENDPOINTS["PLACE_ORDER"],
        body=body,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=BINANCE_USDM_DOCS,
        _preparation_token=_PREPARED_REQUEST_TOKEN,
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
    response = _exact_json_object(response, name="order ACK response")
    _exact_decoded_json_tree(response, name="order ACK response")

    echoed = validate_client_order_id(response.get("clientOrderId"))
    if echoed != cid:
        raise BinanceUsdmAdapterError(
            "Binance USD-M clientOrderId does not match request"
        )

    symbol = _provider_symbol(response.get("symbol"), name="response.symbol")
    order_id = response.get("orderId")
    if type(order_id) is not int or order_id < 0:
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

    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be exact ProviderResponseObservation")
    projection = provider_response_observation_require_scope(
        observation,
        provider_id="BINANCE",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BINANCE_USDM_ENDPOINTS["EXECUTIONS"],
    )
    rows = projection["payload"]
    account_id = projection["account_id"]
    environment = projection["environment"]
    if type(rows) is not tuple:
        raise BinanceUsdmAdapterError("trade rows must be an exact decoded array")
    if type(instrument_versions) is not dict:
        raise BinanceUsdmAdapterError("instrument_versions must be an exact dict")
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
    if type(client_map) is not dict:
        raise BinanceUsdmAdapterError(
            "client_ids_by_order_id must be an exact dict"
        )
    normalized_client_map: dict[int, str] = {}
    seen_client_ids: set[str] = set()
    for raw_order_id, raw_client_id in client_map.items():
        if type(raw_order_id) is not int or raw_order_id < 0:
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
        if type(raw) is not MappingProxyType:
            raise BinanceUsdmAdapterError(
                f"trade row {index} must be an exact decoded object"
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
            type(trade_id) is not int
            or trade_id < 0
            or type(order_id) is not int
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
            evidence_refs=(projection["evidence_ref"],),
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
