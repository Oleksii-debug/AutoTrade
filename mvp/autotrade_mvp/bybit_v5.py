"""Pure Bybit V5 contract adapter foundation.

This module deliberately performs no networking, stores no credentials and
cannot grant trading authority. It translates already-authorized canonical
values and recorded Bybit responses into AutoTrade provider/reconciliation
contracts. Live, demo and test environments remain unqualified until exact
adapter evidence satisfies provider_core.QualificationEvidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .capabilities import CapabilityError, CapabilitySnapshot
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderCoreError,
    ProviderResponseObservation,
    ProviderSubmissionObservation,
    Surface,
    prepare_authenticated_read_query,
)
from .reconciliation import (
    CoverageSurfaceEvidence,
    ProviderFillEvidence,
    ProviderWorkingOrderEvidence,
)


BYBIT_DOCUMENTED_ENDPOINTS: Mapping[str, str] = {
    "SERVER_TIME": "/v5/market/time",
    "INSTRUMENTS": "/v5/market/instruments-info",
    "PLACE_ORDER": "/v5/order/create",
    "OPEN_ORDERS": "/v5/order/realtime",
    "ORDER_HISTORY": "/v5/order/history",
    "EXECUTIONS": "/v5/execution/list",
    "POSITIONS": "/v5/position/list",
    "WALLET": "/v5/account/wallet-balance",
    "ACTIVITIES": "/v5/account/transaction-log",
}

_CATEGORY_BY_FAMILY = {
    "SPOT": "spot",
    "MARGIN": "spot",
    "LINEAR_DERIVATIVES": "linear",
    "INVERSE_DERIVATIVES": "inverse",
    "OPTIONS": "option",
}

_TIME_IN_FORCE = {
    "GTC": "GTC",
    "IOC": "IOC",
    "FOK": "FOK",
    "POST_ONLY": "PostOnly",
}

_AMBIGUOUS_RESPONSE_CODES = frozenset({429, 10000, 10014, 10016})
_CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,36}$")
_REST_BASE_BY_ENVIRONMENT: Mapping[str, str] = {
    "MAINNET": "https://api.bybit.com",
    "TESTNET": "https://api-testnet.bybit.com",
    "DEMO": "https://api-demo.bybit.com",
}
_RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT: Mapping[str, str] = {
    "MAINNET": "LIVE",
    "TESTNET": "PAPER",
    "DEMO": "PAPER",
}
_BYBIT_PREPARED_SUBMISSION_TOKEN = object()


_DERIVATIVE_ORDER_SCOPE_BY_FAMILY: Mapping[str, str] = {
    "LINEAR_DERIVATIVES": "BYBIT.LINEAR.ORDER.WRITE",
    "INVERSE_DERIVATIVES": "BYBIT.INVERSE.ORDER.WRITE",
}
_CAPABILITY_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT: Mapping[str, str] = {
    "MAINNET": "LIVE",
    "TESTNET": "PAPER",
    "DEMO": "PAPER",
}
_ORDER_READ_ENDPOINT_BY_SURFACE: Mapping[str, str] = MappingProxyType(
    {
        "OPEN_ORDERS": BYBIT_DOCUMENTED_ENDPOINTS["OPEN_ORDERS"],
        "ORDER_HISTORY": BYBIT_DOCUMENTED_ENDPOINTS["ORDER_HISTORY"],
    }
)
_BYBIT_ORDER_CATEGORIES = frozenset({"spot", "linear", "inverse", "option"})
_BYBIT_OPEN_ORDER_STATUSES = frozenset({"New", "PartiallyFilled", "Untriggered"})
_BYBIT_CLOSED_ORDER_STATUSES = frozenset(
    {
        "Rejected",
        "PartiallyFilledCanceled",
        "Filled",
        "Cancelled",
        "Triggered",
        "Deactivated",
    }
)
_BYBIT_KNOWN_ORDER_STATUSES = (
    _BYBIT_OPEN_ORDER_STATUSES | _BYBIT_CLOSED_ORDER_STATUSES
)
_MAX_ORDER_HISTORY_WINDOW_MS = 7 * 24 * 60 * 60 * 1000


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be a UUID") from error
    return text


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderCoreError(f"{name} must be an object")
    return value


def _integer(value: object, *, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ProviderCoreError(f"{name} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value and value.lstrip("-").isdigit():
        result = int(value)
    else:
        raise ProviderCoreError(f"{name} must be an integer")
    if minimum is not None and result < minimum:
        raise ProviderCoreError(f"{name} must be at least {minimum}")
    return result


def _decimal(value: object, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProviderCoreError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderCoreError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ProviderCoreError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise ProviderCoreError(f"{name} must be positive")
    return result


def _decimal_text(value: object, *, name: str, positive: bool = False) -> str:
    number = _decimal(value, name=name, positive=positive)
    if number == 0:
        return "0"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _utc_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ProviderCoreError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _millis_to_utc(value: object, *, name: str) -> str:
    milliseconds = _integer(value, name=name, minimum=0)
    seconds, remainder = divmod(milliseconds, 1000)
    instant = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        milliseconds=remainder
    )
    return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _client_order_id(value: object) -> str:
    client_id = _text(value, name="client_order_id")
    if not _CLIENT_ID.fullmatch(client_id):
        raise ProviderCoreError(
            "client_order_id must be 1-36 letters, numbers, dashes or underscores"
        )
    return client_id


def _opaque_cursor(value: object, *, name: str = "cursor") -> str:
    if not isinstance(value, str) or not value:
        raise ProviderCoreError(f"{name} must be a non-empty string")
    if value != value.strip():
        raise ProviderCoreError(f"{name} must not contain surrounding whitespace")
    return value


def _response_evidence(
    endpoint: str,
    response: Mapping[str, Any],
    *,
    observed_at: str,
    environment: str,
) -> dict[str, str]:
    normalized_environment = _text(environment, name="environment").upper()
    try:
        rest_base = _REST_BASE_BY_ENVIRONMENT[normalized_environment]
    except KeyError as error:
        raise ProviderCoreError(
            "Bybit evidence environment must be MAINNET, TESTNET or DEMO"
        ) from error
    source_uri = f"{rest_base}{endpoint}"
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
                f"{source_uri}#sha256:{digest}",
            )
        ),
        "sha256": f"sha256:{digest}",
        # Environment is bound by the environment-specific provider endpoint.
        # EvidenceRef itself remains the canonical common contract.
        "source_uri": source_uri,
        "observed_at": observed_at,
        "rights_id": "provider-observation-bybit",
    }


def validate_auth_timestamp(
    *,
    request_timestamp_ms: object,
    server_time_ms: object,
    recv_window_ms: object = 5000,
) -> None:
    """Enforce Bybit's documented authenticated-request time window locally."""

    request = _integer(
        request_timestamp_ms, name="request_timestamp_ms", minimum=0
    )
    server = _integer(server_time_ms, name="server_time_ms", minimum=0)
    window = _integer(recv_window_ms, name="recv_window_ms", minimum=1)
    if not (server - window <= request < server + 1000):
        raise ProviderCoreError("Bybit request timestamp is outside authentication window")


def server_time_from_response(response: Mapping[str, Any]) -> str:
    envelope = _mapping(response, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit server-time response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    nanoseconds = _integer(result.get("timeNano"), name="timeNano", minimum=0)
    seconds, nanos = divmod(nanoseconds, 1_000_000_000)
    instant = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
        microseconds=nanos // 1000
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _position_idx_from_capability(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    account_id: str,
    instrument_version: str,
    provider_environment: str,
    product_family: str,
    side: str,
    order_type: str,
    time_in_force: str,
    reduce_only: bool,
    position_side: str | None,
) -> int:
    """Project verified account-mode authority into Bybit positionIdx.

    The capability is the authority. A caller cannot select a syntactically
    valid index independently of the verified account/category mode.
    """

    if not isinstance(capability, CapabilitySnapshot):
        raise ProviderCoreError(
            "Bybit derivative orders require a canonical CapabilitySnapshot"
        )
    if capability.provider_id.upper() != "BYBIT":
        raise ProviderCoreError("capability belongs to another provider")

    account = _text(account_id, name="account_id")
    instrument = _text(instrument_version, name="instrument_version")
    if capability.account_id != account:
        raise ProviderCoreError("capability account does not match target account")
    if capability.instrument_version != instrument:
        raise ProviderCoreError(
            "capability instrument version does not match target instrument"
        )
    provider_env = _text(
        provider_environment,
        name="provider_environment",
    ).upper()
    try:
        expected_capability_environment = (
            _CAPABILITY_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_env]
        )
    except KeyError as error:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        ) from error
    if capability.environment != expected_capability_environment:
        raise ProviderCoreError(
            "capability environment does not match target Bybit environment"
        )

    family = _text(product_family, name="product_family").upper()
    try:
        permission_scope = _DERIVATIVE_ORDER_SCOPE_BY_FAMILY[family]
    except KeyError as error:
        raise ProviderCoreError(
            "position-mode capability is only valid for Bybit derivatives"
        ) from error

    normalized_side = _text(side, name="side").upper()
    normalized_type = _text(order_type, name="order_type").upper()
    tif = _text(time_in_force, name="time_in_force").upper()
    try:
        admitted = capability.admits(
            at=at,
            order_type=normalized_type,
            time_in_force=tif,
            permission_scope=permission_scope,
        )
    except CapabilityError as error:
        raise ProviderCoreError("capability timestamp or shape is invalid") from error
    if not admitted:
        raise ProviderCoreError(
            "Bybit derivative action is not admitted by current verified capability"
        )

    if type(reduce_only) is not bool:
        raise ProviderCoreError("reduce_only must be boolean")
    mode = capability.position_mode.strip().upper()
    if mode == "ONE_WAY":
        if position_side is not None:
            normalized_position_side = _text(position_side, name="position_side").upper()
            if normalized_position_side != "NET":
                raise ProviderCoreError("ONE_WAY mode accepts only NET position_side")
        return 0
    if mode == "HEDGE":
        if position_side is None:
            raise ProviderCoreError("HEDGE mode requires explicit target position_side")
        normalized_position_side = _text(position_side, name="position_side").upper()
        if normalized_position_side not in {"LONG", "SHORT"}:
            raise ProviderCoreError("HEDGE position_side must be LONG or SHORT")
        expected_order_side = (
            "SELL" if reduce_only and normalized_position_side == "LONG"
            else "BUY" if reduce_only
            else "BUY" if normalized_position_side == "LONG"
            else "SELL"
        )
        if normalized_side != expected_order_side:
            raise ProviderCoreError("order side is inconsistent with target hedge position leg")
        return 1 if normalized_position_side == "LONG" else 2
    raise ProviderCoreError(
        "Bybit position_mode must be explicitly ONE_WAY or HEDGE"
    )


def build_order_payload(
    *,
    product_family: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: object,
    client_order_id: str,
    time_in_force: str,
    price: object | None = None,
    reduce_only: bool = False,
    position_side: str | None = None,
    position_idx: int | None = None,
    capability: CapabilitySnapshot | None = None,
    capability_at: datetime | None = None,
    account_id: str | None = None,
    instrument_version: str | None = None,
    provider_environment: str | None = None,
) -> dict[str, Any]:
    """Translate a bounded canonical order into a Bybit V5 request payload.

    Spot and margin market quantity is explicitly expressed in base-coin units
    so provider defaults cannot silently reinterpret a BUY quantity as quote
    notional.
    """

    family = _text(product_family, name="product_family").upper()
    try:
        category = _CATEGORY_BY_FAMILY[family]
    except KeyError as error:
        raise ProviderCoreError("unsupported Bybit product family") from error

    provider_symbol = _text(symbol, name="symbol")
    if provider_symbol != provider_symbol.upper():
        raise ProviderCoreError("Bybit symbol must be uppercase")

    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")

    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in {"MARKET", "LIMIT"}:
        raise ProviderCoreError("Bybit foundation supports MARKET or LIMIT only")

    tif = _text(time_in_force, name="time_in_force").upper()
    if tif not in _TIME_IN_FORCE:
        raise ProviderCoreError("unsupported time_in_force")
    if normalized_type == "MARKET" and tif != "IOC":
        raise ProviderCoreError("Bybit market orders require canonical IOC semantics")

    if type(reduce_only) is not bool:
        raise ProviderCoreError("reduce_only must be boolean")
    if family in {"SPOT", "MARGIN"} and reduce_only:
        raise ProviderCoreError("spot/margin reduce_only is not qualified by this adapter")

    derivative_family = family in {
        "LINEAR_DERIVATIVES",
        "INVERSE_DERIVATIVES",
    }
    effective_position_idx: int | None = None
    if derivative_family:
        if (
            capability_at is None
            or account_id is None
            or instrument_version is None
            or provider_environment is None
        ):
            raise ProviderCoreError(
                "Bybit derivative orders require verified "
                "account/category/environment capability context"
            )
        effective_position_idx = _position_idx_from_capability(
            capability=capability,
            at=capability_at,
            account_id=account_id,
            instrument_version=instrument_version,
            provider_environment=provider_environment,
            product_family=family,
            side=normalized_side,
            order_type=normalized_type,
            time_in_force=tif,
            reduce_only=reduce_only,
            position_side=position_side,
        )
        if position_idx is not None:
            if type(position_idx) is not int or position_idx not in {0, 1, 2}:
                raise ProviderCoreError("position_idx must be 0, 1 or 2")
            if position_idx != effective_position_idx:
                raise ProviderCoreError(
                    "position_idx conflicts with verified account position mode"
                )
    elif position_idx is not None:
        raise ProviderCoreError("position_idx is only supported for derivative orders")

    payload: dict[str, Any] = {
        "category": category,
        "symbol": provider_symbol,
        "side": "Buy" if normalized_side == "BUY" else "Sell",
        "orderType": "Market" if normalized_type == "MARKET" else "Limit",
        "qty": _decimal_text(quantity, name="quantity", positive=True),
        "timeInForce": _TIME_IN_FORCE[tif],
        "orderLinkId": _client_order_id(client_order_id),
    }

    if normalized_type == "LIMIT":
        if price is None:
            raise ProviderCoreError("limit price is required")
        payload["price"] = _decimal_text(price, name="price", positive=True)
    elif price is not None:
        raise ProviderCoreError("market order must not carry an ignored limit price")

    if family == "SPOT":
        payload["isLeverage"] = 0
        if normalized_type == "MARKET":
            payload["marketUnit"] = "baseCoin"
    elif family == "MARGIN":
        payload["isLeverage"] = 1
        if normalized_type == "MARKET":
            payload["marketUnit"] = "baseCoin"
    else:
        payload["reduceOnly"] = reduce_only
        payload["positionIdx"] = effective_position_idx

    return payload




@dataclass(frozen=True)
class BybitPreparedSubmission:
    """Capability-bound Bybit order body for the guarded dispatcher."""

    endpoint: str
    body: Mapping[str, Any]
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    entity_id: str
    instrument_version: str
    body_sha256: str = field(init=False)
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _BYBIT_PREPARED_SUBMISSION_TOKEN:
            raise ProviderCoreError(
                "BybitPreparedSubmission must come from canonical capability preparation"
            )
        if self.endpoint != BYBIT_DOCUMENTED_ENDPOINTS["PLACE_ORDER"]:
            raise ProviderCoreError("Bybit prepared endpoint mismatch")
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be a mapping")
        rendered = json.dumps(
            dict(self.body),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        body = json.loads(rendered)
        body["orderLinkId"] = _client_order_id(body.get("orderLinkId"))
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        runtime_environment = _text(self.environment, name="environment").upper()
        if (
            _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
            != runtime_environment
        ):
            raise ProviderCoreError(
                "Bybit provider environment does not match canonical runtime environment"
            )
        object.__setattr__(self, "body", MappingProxyType(body))
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(self, "environment", runtime_environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _text(self.capability_snapshot_id, name="capability_snapshot_id"),
        )
        object.__setattr__(
            self,
            "entity_id",
            _text(self.entity_id, name="entity_id"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(
            self,
            "body_sha256",
            "sha256:" + sha256(rendered.encode("utf-8")).hexdigest(),
        )

    @property
    def capability_snapshot_ids(self) -> tuple[str, ...]:
        return (self.capability_snapshot_id,)

    @property
    def instrument_versions(self) -> tuple[str, ...]:
        return (self.instrument_version,)


def prepare_order_submission(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    provider_environment: str,
    product_family: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: object,
    client_order_id: str,
    time_in_force: str,
    price: object | None = None,
    reduce_only: bool = False,
    position_side: str | None = None,
    position_idx: int | None = None,
) -> BybitPreparedSubmission:
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if capability.provider_id.upper() != "BYBIT":
        raise ProviderCoreError("capability belongs to another provider")
    provider_env = _text(
        provider_environment,
        name="provider_environment",
    ).upper()
    if provider_env not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        )
    runtime_env = _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_env]
    if capability.environment.upper() != runtime_env:
        raise ProviderCoreError(
            "capability environment does not match Bybit provider environment"
        )
    point = (
        at.astimezone(timezone.utc)
        if isinstance(at, datetime) and at.tzinfo is not None
        else None
    )
    if point is None:
        raise ProviderCoreError("at must be timezone-aware")
    normalized_type = _text(order_type, name="order_type").upper()
    normalized_tif = _text(time_in_force, name="time_in_force").upper()
    if not capability.admits(
        at=point,
        order_type=normalized_type,
        time_in_force=normalized_tif,
        permission_scope="ORDER_WRITE",
    ):
        raise ProviderCoreError(
            "exact capability evidence does not admit this Bybit order"
        )
    body = build_order_payload(
        product_family=product_family,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        client_order_id=client_order_id,
        time_in_force=time_in_force,
        price=price,
        reduce_only=reduce_only,
        position_side=position_side,
        position_idx=position_idx,
        capability=capability,
        capability_at=point,
        account_id=capability.account_id,
        instrument_version=capability.instrument_version,
        provider_environment=provider_env,
    )
    return BybitPreparedSubmission(
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["PLACE_ORDER"],
        body=body,
        account_id=capability.account_id,
        environment=runtime_env,
        provider_environment=provider_env,
        capability_snapshot_id=capability.snapshot_id,
        entity_id=capability.entity_id,
        instrument_version=capability.instrument_version,
        _factory_token=_BYBIT_PREPARED_SUBMISSION_TOKEN,
    )


def guarded_order_projection(
    prepared_request: BybitPreparedSubmission,
) -> Mapping[str, object]:
    """Project canonical Bybit preparation into the shared guarded transport seam."""

    if not isinstance(prepared_request, BybitPreparedSubmission):
        raise TypeError("prepared_request must be BybitPreparedSubmission")
    return MappingProxyType(
        {
            "endpoint": prepared_request.endpoint,
            "body": dict(prepared_request.body),
            "account_id": prepared_request.account_id,
            "environment": prepared_request.environment,
            "provider_environment": prepared_request.provider_environment,
            "capability_snapshot_id": prepared_request.capability_snapshot_id,
            "entity_id": prepared_request.entity_id,
            "capability_snapshot_ids": list(
                prepared_request.capability_snapshot_ids
            ),
            "instrument_versions": list(prepared_request.instrument_versions),
            "body_sha256": prepared_request.body_sha256,
        }
    )


def _submission_evidence(
    observation: ProviderSubmissionObservation,
    *,
    prepared_request: BybitPreparedSubmission,
) -> dict[str, str]:
    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    observation.require_scope(
        provider_id="BYBIT",
        endpoint=prepared_request.endpoint,
        prepared_request_sha256=prepared_request.body_sha256,
        capability_snapshot_ids=prepared_request.capability_snapshot_ids,
        instrument_versions=prepared_request.instrument_versions,
        account_id=prepared_request.account_id,
        environment=prepared_request.environment,
        client_order_id=_client_order_id(
            prepared_request.body.get("orderLinkId")
        ),
    )
    source_uri = (
        _REST_BASE_BY_ENVIRONMENT[prepared_request.provider_environment]
        + prepared_request.endpoint
    )
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"{source_uri}#{observation.evidence_ref}",
            )
        ),
        "sha256": observation.response_sha256,
        "source_uri": source_uri,
        "observed_at": observation.observed_at,
        "rights_id": "provider-observation-bybit",
    }

def parse_submission_response(
    *,
    attempt_id: str,
    prepared_request: BybitPreparedSubmission,
    observation: ProviderSubmissionObservation | None = None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map one exact durable Bybit create-order response to SubmissionResult."""

    aid = _uuid_text(attempt_id, name="attempt_id")
    if not isinstance(prepared_request, BybitPreparedSubmission):
        raise TypeError("prepared_request must be BybitPreparedSubmission")
    cid = _client_order_id(prepared_request.body.get("orderLinkId"))
    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if observation is not None:
            raise ProviderCoreError(
                "ambiguous transport cannot also claim an authoritative response"
            )
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "BYBIT_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }
    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    if observation.response_binding.attempt_id != aid:
        raise ProviderCoreError("Bybit submission observation attempt_id mismatch")
    evidence = [
        _submission_evidence(
            observation,
            prepared_request=prepared_request,
        )
    ]
    envelope = _mapping(observation.payload, name="response")
    code = _integer(envelope.get("retCode"), name="retCode")
    provider_received_at = (
        _millis_to_utc(envelope.get("time"), name="response.time")
        if envelope.get("time") is not None
        else None
    )

    if code == 0:
        result = _mapping(envelope.get("result"), name="result")
        provider_order_id = _text(result.get("orderId"), name="result.orderId")
        echoed_client_id = _text(
            result.get("orderLinkId"), name="result.orderLinkId"
        )
        if echoed_client_id != cid:
            raise ProviderCoreError(
                "Bybit orderLinkId response does not match request"
            )
        return {
            "attempt_id": aid,
            "outcome": "ACKNOWLEDGED",
            "provider_order_id": provider_order_id,
            "client_order_id": cid,
            **(
                {"provider_received_at": provider_received_at}
                if provider_received_at is not None
                else {}
            ),
            "evidence": evidence,
            "retry_disposition": "NEVER",
        }

    outcome = "UNKNOWN" if code in _AMBIGUOUS_RESPONSE_CODES else "REJECTED"
    return {
        "attempt_id": aid,
        "outcome": outcome,
        "client_order_id": cid,
        **(
            {"provider_received_at": provider_received_at}
            if provider_received_at is not None
            else {}
        ),
        "reason_code": f"BYBIT_{code}",
        "evidence": evidence,
        "retry_disposition": (
            "RECONCILE_FIRST" if outcome == "UNKNOWN" else "NEVER"
        ),
    }


@dataclass(frozen=True)
class BybitOrderSnapshot:
    """One exact Bybit order-state observation; never fill evidence by itself."""

    account_id: str
    environment: str
    provider_environment: str
    category: str
    provider_order_id: str
    client_order_id: str | None
    symbol: str
    order_status: str
    remaining_quantity: Decimal
    created_at: str
    updated_at: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)

        category = _text(self.category, name="category").lower()
        if category not in _BYBIT_ORDER_CATEGORIES:
            raise ProviderCoreError("unsupported Bybit order category")
        object.__setattr__(self, "category", category)
        object.__setattr__(
            self,
            "provider_order_id",
            _text(self.provider_order_id, name="provider_order_id"),
        )
        if self.client_order_id is not None:
            object.__setattr__(
                self,
                "client_order_id",
                _client_order_id(self.client_order_id),
            )
        object.__setattr__(self, "symbol", _text(self.symbol, name="symbol"))
        status = _text(self.order_status, name="order_status")
        if status not in _BYBIT_KNOWN_ORDER_STATUSES:
            raise ProviderCoreError("unsupported Bybit order status")
        object.__setattr__(self, "order_status", status)
        remaining = _decimal(
            self.remaining_quantity,
            name="remaining_quantity",
        )
        if remaining < 0:
            raise ProviderCoreError("remaining_quantity must be non-negative")
        if status in _BYBIT_OPEN_ORDER_STATUSES and remaining <= 0:
            raise ProviderCoreError(
                "Bybit open order must have positive remaining quantity"
            )
        object.__setattr__(self, "remaining_quantity", remaining)
        created_at = _utc_text(self.created_at, name="created_at")
        updated_at = _utc_text(self.updated_at, name="updated_at")
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        if updated < created:
            raise ProviderCoreError("Bybit order updated_at precedes created_at")
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")


@dataclass(frozen=True)
class BybitOrderPage:
    """Exact cursor page for one authenticated order-reconciliation read."""

    account_id: str
    environment: str
    provider_environment: str
    surface: str
    category: str
    orders: tuple[BybitOrderSnapshot, ...]
    next_cursor: str | None
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)

        surface = _text(self.surface, name="surface").upper()
        if surface not in _ORDER_READ_ENDPOINT_BY_SURFACE:
            raise ProviderCoreError("unsupported Bybit order reconciliation surface")
        object.__setattr__(self, "surface", surface)

        category = _text(self.category, name="category").lower()
        if category not in _BYBIT_ORDER_CATEGORIES:
            raise ProviderCoreError("unsupported Bybit order category")
        object.__setattr__(self, "category", category)

        if not isinstance(self.orders, tuple):
            raise TypeError("orders must be an immutable tuple")
        identities: set[str] = set()
        for order in self.orders:
            if not isinstance(order, BybitOrderSnapshot):
                raise TypeError("orders must contain BybitOrderSnapshot values")
            if (
                order.account_id != self.account_id
                or order.environment != self.environment
                or order.provider_environment != self.provider_environment
                or order.category != self.category
            ):
                raise ProviderCoreError("Bybit order page contains cross-scope order evidence")
            if order.provider_order_id in identities:
                raise ProviderCoreError("Bybit order page contains duplicate order identity")
            identities.add(order.provider_order_id)

        if self.next_cursor is not None:
            object.__setattr__(
                self,
                "next_cursor",
                _opaque_cursor(self.next_cursor, name="next_cursor"),
            )
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")

    @property
    def pagination_complete(self) -> bool:
        return self.next_cursor is None


def prepare_order_read_query(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    surface: str,
    category: str,
    client_order_id: str | None = None,
    symbol: str | None = None,
    cursor: str | None = None,
    limit: object = 50,
    start_time_ms: object | None = None,
    end_time_ms: object | None = None,
) -> AuthenticatedReadQueryBinding:
    """Prepare one capability-bound Bybit order reconciliation page request.

    The function performs no network I/O. History windows are bounded to the
    provider's documented seven-day maximum and pagination cursors stay opaque.
    """

    normalized_surface = _text(surface, name="surface").upper()
    try:
        endpoint = _ORDER_READ_ENDPOINT_BY_SURFACE[normalized_surface]
    except KeyError as error:
        raise ProviderCoreError("unsupported Bybit order reconciliation surface") from error

    normalized_category = _text(category, name="category").lower()
    if normalized_category not in _BYBIT_ORDER_CATEGORIES:
        raise ProviderCoreError("unsupported Bybit order category")

    normalized_limit = _integer(limit, name="limit", minimum=1)
    if normalized_limit > 50:
        raise ProviderCoreError("Bybit order page limit cannot exceed 50")

    query: dict[str, str] = {
        "category": normalized_category,
        "limit": str(normalized_limit),
    }
    if client_order_id is not None:
        query["orderLinkId"] = _client_order_id(client_order_id)
    if symbol is not None:
        provider_symbol = _text(symbol, name="symbol")
        if provider_symbol != provider_symbol.upper():
            raise ProviderCoreError("Bybit symbol must be uppercase")
        query["symbol"] = provider_symbol
    if cursor is not None:
        query["cursor"] = _opaque_cursor(cursor)

    if normalized_surface == "OPEN_ORDERS":
        # Make the requested provider state explicit rather than depending on
        # a remote default. Bybit ignores openOnly for direct order identities.
        query["openOnly"] = "0"
        if normalized_category == "linear" and symbol is None:
            raise ProviderCoreError(
                "Bybit linear realtime order query requires symbol"
            )
        if start_time_ms is not None or end_time_ms is not None:
            raise ProviderCoreError(
                "Bybit realtime order query does not accept history time windows"
            )
    else:
        start = (
            None
            if start_time_ms is None
            else _integer(start_time_ms, name="start_time_ms", minimum=0)
        )
        end = (
            None
            if end_time_ms is None
            else _integer(end_time_ms, name="end_time_ms", minimum=0)
        )
        if start is not None and end is not None:
            if end < start:
                raise ProviderCoreError("Bybit order history end precedes start")
            if end - start > _MAX_ORDER_HISTORY_WINDOW_MS:
                raise ProviderCoreError(
                    "Bybit order history window cannot exceed seven days"
                )
        if start is not None:
            query["startTime"] = str(start)
        if end is not None:
            query["endTime"] = str(end)

    return prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
        query=query,
        at=at,
        permission_scope="ORDER.READ",
    )


def parse_order_page(observation: ProviderResponseObservation) -> BybitOrderPage:
    """Parse one exact Bybit realtime/history page without inventing fill state."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")

    endpoint = observation.query_binding.endpoint
    surface = next(
        (
            name
            for name, documented_endpoint in _ORDER_READ_ENDPOINT_BY_SURFACE.items()
            if documented_endpoint == endpoint
        ),
        None,
    )
    if surface is None:
        raise ProviderCoreError("unsupported exact Bybit order reconciliation endpoint")

    observation.require_scope(
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=endpoint,
    )
    provider_environment = _text(
        observation.provider_environment,
        name="provider_environment",
    ).upper()
    if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        )
    if (
        _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
        != observation.environment
    ):
        raise ProviderCoreError(
            "Bybit provider environment does not match runtime environment"
        )

    envelope = _mapping(observation.payload, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit order response was not successful")
    result = _mapping(envelope.get("result"), name="result")

    query_category = observation.query_binding.query.get("category")
    if query_category is None:
        raise ProviderCoreError("Bybit order query is missing category provenance")
    query_category = _text(query_category, name="query category").lower()
    if query_category not in _BYBIT_ORDER_CATEGORIES:
        raise ProviderCoreError("unsupported Bybit order query category")
    response_category = _text(result.get("category"), name="result.category").lower()
    if response_category != query_category:
        raise ProviderCoreError("Bybit order response category does not match query")

    rows = result.get("list")
    if not isinstance(rows, (list, tuple)):
        raise ProviderCoreError("result.list must be an array")

    raw_cursor = result.get("nextPageCursor")
    next_cursor = (
        None
        if raw_cursor in (None, "")
        else _opaque_cursor(raw_cursor, name="nextPageCursor")
    )
    expected_client_id = observation.query_binding.query.get("orderLinkId")

    by_order: dict[str, BybitOrderSnapshot] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, name=f"result.list[{index}]")
        provider_order_id = _text(row.get("orderId"), name="orderId")
        raw_link = row.get("orderLinkId")
        client_order_id = (
            None if raw_link in (None, "") else _client_order_id(raw_link)
        )
        if (
            expected_client_id is not None
            and client_order_id != expected_client_id
        ):
            raise ProviderCoreError(
                "Bybit orderLinkId response does not match exact reconciliation query"
            )

        created_ms = _integer(row.get("createdTime"), name="createdTime", minimum=0)
        updated_ms = _integer(row.get("updatedTime"), name="updatedTime", minimum=0)
        if updated_ms < created_ms:
            raise ProviderCoreError("Bybit order updatedTime precedes createdTime")

        snapshot = BybitOrderSnapshot(
            account_id=observation.account_id,
            environment=observation.environment,
            provider_environment=provider_environment,
            category=query_category,
            provider_order_id=provider_order_id,
            client_order_id=client_order_id,
            symbol=_text(row.get("symbol"), name="symbol"),
            order_status=_text(row.get("orderStatus"), name="orderStatus"),
            remaining_quantity=_decimal(
                row.get("leavesQty"),
                name="leavesQty",
            ),
            created_at=_millis_to_utc(created_ms, name="createdTime"),
            updated_at=_millis_to_utc(updated_ms, name="updatedTime"),
            evidence_ref=observation.evidence_ref,
        )
        previous = by_order.get(provider_order_id)
        if previous is not None and previous != snapshot:
            raise ProviderCoreError(
                "Bybit order id appears with conflicting state content"
            )
        by_order[provider_order_id] = snapshot

    return BybitOrderPage(
        account_id=observation.account_id,
        environment=observation.environment,
        provider_environment=provider_environment,
        surface=surface,
        category=query_category,
        orders=tuple(by_order.values()),
        next_cursor=next_cursor,
        evidence_ref=observation.evidence_ref,
    )


def working_orders_from_page(
    page: BybitOrderPage,
    *,
    instrument_versions: Mapping[str, str],
) -> tuple[ProviderWorkingOrderEvidence, ...]:
    """Project documented open Bybit states into canonical working-order evidence."""

    if not isinstance(page, BybitOrderPage):
        raise TypeError("page must be BybitOrderPage")
    if page.surface != "OPEN_ORDERS":
        raise ProviderCoreError(
            "Bybit working-order evidence requires OPEN_ORDERS page provenance"
        )
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")

    working: list[ProviderWorkingOrderEvidence] = []
    for order in page.orders:
        if order.order_status in _BYBIT_CLOSED_ORDER_STATUSES:
            continue
        if order.order_status not in _BYBIT_OPEN_ORDER_STATUSES:
            raise ProviderCoreError("unsupported Bybit working order status")
        try:
            instrument = instrument_versions[order.symbol]
        except KeyError as error:
            raise ProviderCoreError(
                f"unmapped Bybit instrument symbol: {order.symbol}"
            ) from error
        working.append(
            ProviderWorkingOrderEvidence.create(
                provider_id="BYBIT",
                account_id=order.account_id,
                environment=order.environment,
                provider_environment=order.provider_environment,
                provider_order_id=order.provider_order_id,
                client_order_id=order.client_order_id,
                instrument=_text(instrument, name="instrument_version"),
                remaining_quantity=order.remaining_quantity,
            )
        )
    return tuple(working)


def prepare_next_order_read_query(
    *,
    observation: ProviderResponseObservation,
    capability: CapabilitySnapshot,
    at: datetime,
) -> AuthenticatedReadQueryBinding | None:
    """Continue an exact Bybit order cursor chain without re-authoring its scope."""

    page = parse_order_page(observation)
    if page.next_cursor is None:
        return None
    binding = observation.query_binding
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if (
        capability.snapshot_id != binding.capability_snapshot_id
        or capability.provider_id.upper() != binding.provider_id
        or capability.account_id != binding.account_id
        or capability.entity_id != binding.entity_id
        or capability.environment != binding.environment
        or capability.instrument_version != binding.instrument_version
    ):
        raise ProviderCoreError(
            "Bybit pagination must retain the exact verified capability scope"
        )

    query = dict(binding.query)
    query["cursor"] = page.next_cursor
    return prepare_authenticated_read_query(
        capability=capability,
        surface=binding.surface,
        endpoint=binding.endpoint,
        query=query,
        at=at,
        permission_scope=binding.permission_scope,
    )


def order_history_coverage_from_pages(
    observations: tuple[ProviderResponseObservation, ...],
    *,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Derive bounded history coverage only from a contiguous exact cursor chain.

    This helper intentionally requires explicit startTime and endTime. Provider
    default windows are not converted into absence coverage because doing so
    would require additional qualified clock/retention semantics.
    """

    if not isinstance(observations, tuple) or not observations:
        raise ProviderCoreError(
            "Bybit order history coverage requires a non-empty immutable page tuple"
        )
    for name, value in (
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise ProviderCoreError(f"{name} must be boolean")

    pages = tuple(parse_order_page(observation) for observation in observations)
    if any(page.surface != "ORDER_HISTORY" for page in pages):
        raise ProviderCoreError(
            "Bybit order history coverage accepts ORDER_HISTORY pages only"
        )

    first_observation = observations[0]
    first_binding = first_observation.query_binding
    if "cursor" in first_binding.query:
        raise ProviderCoreError(
            "Bybit order history coverage must begin at the first page"
        )
    raw_start = first_binding.query.get("startTime")
    raw_end = first_binding.query.get("endTime")
    if raw_start is None or raw_end is None:
        raise ProviderCoreError(
            "Bybit order history coverage requires explicit startTime and endTime"
        )
    start_ms = _integer(raw_start, name="startTime", minimum=0)
    end_ms = _integer(raw_end, name="endTime", minimum=0)
    if end_ms < start_ms:
        raise ProviderCoreError("Bybit order history end precedes start")
    if end_ms - start_ms > _MAX_ORDER_HISTORY_WINDOW_MS:
        raise ProviderCoreError(
            "Bybit order history window cannot exceed seven days"
        )

    base_query = {
        key: value
        for key, value in first_binding.query.items()
        if key != "cursor"
    }
    first_page = pages[0]
    scope = (
        first_page.account_id,
        first_page.environment,
        first_page.provider_environment,
        first_page.category,
    )
    evidence_refs: set[str] = set()
    for index, (observation, page) in enumerate(zip(observations, pages)):
        if (
            page.account_id,
            page.environment,
            page.provider_environment,
            page.category,
        ) != scope:
            raise ProviderCoreError(
                "Bybit order history pagination crossed provider/account/category scope"
            )
        current_base = {
            key: value
            for key, value in observation.query_binding.query.items()
            if key != "cursor"
        }
        if current_base != base_query:
            raise ProviderCoreError(
                "Bybit order history pagination changed the base query"
            )
        expected_cursor = None if index == 0 else pages[index - 1].next_cursor
        actual_cursor = observation.query_binding.query.get("cursor")
        if actual_cursor != expected_cursor:
            raise ProviderCoreError(
                "Bybit order history pagination cursor chain is not contiguous"
            )
        if observation.evidence_ref in evidence_refs:
            raise ProviderCoreError(
                "Bybit order history pagination repeats exact page evidence"
            )
        evidence_refs.add(observation.evidence_ref)
        if index < len(pages) - 1 and page.next_cursor is None:
            raise ProviderCoreError(
                "Bybit order history pagination continues after a terminal page"
            )

    if pages[-1].next_cursor is not None:
        raise ProviderCoreError(
            "Bybit order history pagination is incomplete"
        )

    return coverage_evidence(
        account_id=first_page.account_id,
        environment=first_page.environment,
        provider_environment=first_page.provider_environment,
        surface="ORDER_HISTORY",
        coverage_start=_millis_to_utc(start_ms, name="startTime"),
        coverage_end=_millis_to_utc(end_ms, name="endTime"),
        pagination_complete=True,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        qualified_exclusion_semantics=qualified_exclusion_semantics,
    )


@dataclass(frozen=True)
class BybitExecutionPage:
    """Economically complete fills from one exact execution-history cursor page."""

    account_id: str
    environment: str
    provider_environment: str
    category: str
    fills: tuple[ProviderFillEvidence, ...]
    next_cursor: str | None
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)

        category = _text(self.category, name="category").lower()
        if category not in _BYBIT_ORDER_CATEGORIES:
            raise ProviderCoreError("unsupported Bybit execution category")
        object.__setattr__(self, "category", category)

        if not isinstance(self.fills, tuple):
            raise TypeError("fills must be an immutable tuple")
        execution_ids: set[str] = set()
        for fill in self.fills:
            if not isinstance(fill, ProviderFillEvidence):
                raise TypeError("fills must contain ProviderFillEvidence values")
            if (
                fill.provider_id != "BYBIT"
                or fill.account_id != self.account_id
                or fill.environment != self.environment
                or fill.provider_environment != self.provider_environment
            ):
                raise ProviderCoreError(
                    "Bybit execution page contains cross-scope fill evidence"
                )
            if fill.evidence_refs != (self.evidence_ref,):
                raise ProviderCoreError(
                    "Bybit execution page fill evidence does not match page provenance"
                )
            if fill.provider_execution_id in execution_ids:
                raise ProviderCoreError(
                    "Bybit execution page contains duplicate execution identity"
                )
            execution_ids.add(fill.provider_execution_id)

        if self.next_cursor is not None:
            object.__setattr__(
                self,
                "next_cursor",
                _opaque_cursor(self.next_cursor, name="next_cursor"),
            )
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")

    @property
    def pagination_complete(self) -> bool:
        return self.next_cursor is None


def prepare_execution_read_query(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    category: str,
    client_order_id: str | None = None,
    symbol: str | None = None,
    cursor: str | None = None,
    limit: object = 100,
    start_time_ms: object | None = None,
    end_time_ms: object | None = None,
) -> AuthenticatedReadQueryBinding:
    """Prepare one exact Bybit execution-history query without network I/O."""

    normalized_category = _text(category, name="category").lower()
    if normalized_category not in _BYBIT_ORDER_CATEGORIES:
        raise ProviderCoreError("unsupported Bybit execution category")
    normalized_limit = _integer(limit, name="limit", minimum=1)
    if normalized_limit > 100:
        raise ProviderCoreError("Bybit execution page limit cannot exceed 100")

    query: dict[str, str] = {
        "category": normalized_category,
        "limit": str(normalized_limit),
    }
    if client_order_id is not None:
        query["orderLinkId"] = _client_order_id(client_order_id)
    if symbol is not None:
        provider_symbol = _text(symbol, name="symbol")
        if provider_symbol != provider_symbol.upper():
            raise ProviderCoreError("Bybit symbol must be uppercase")
        query["symbol"] = provider_symbol
    if cursor is not None:
        query["cursor"] = _opaque_cursor(cursor)

    start = (
        None
        if start_time_ms is None
        else _integer(start_time_ms, name="start_time_ms", minimum=0)
    )
    end = (
        None
        if end_time_ms is None
        else _integer(end_time_ms, name="end_time_ms", minimum=0)
    )
    if start is not None and end is not None:
        if end < start:
            raise ProviderCoreError("Bybit execution history end precedes start")
        if end - start > _MAX_ORDER_HISTORY_WINDOW_MS:
            raise ProviderCoreError(
                "Bybit execution history window cannot exceed seven days"
            )
    if start is not None:
        query["startTime"] = str(start)
    if end is not None:
        query["endTime"] = str(end)

    return prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["EXECUTIONS"],
        query=query,
        at=at,
        permission_scope="ORDER.READ",
    )


def parse_execution_page(
    observation: ProviderResponseObservation,
    *,
    provider_environment: str,
    instrument_versions: Mapping[str, str],
    qualified_fee_currencies: Mapping[str, str] | None = None,
) -> BybitExecutionPage:
    """Parse one execution cursor page with exact query/category provenance."""

    fills = parse_executions(
        observation,
        provider_environment=provider_environment,
        instrument_versions=instrument_versions,
        qualified_fee_currencies=qualified_fee_currencies,
    )
    result = _mapping(
        _mapping(observation.payload, name="response").get("result"),
        name="result",
    )
    query_category = observation.query_binding.query.get("category")
    if query_category is None:
        raise ProviderCoreError("Bybit execution query is missing category provenance")
    query_category = _text(query_category, name="query category").lower()
    if query_category not in _BYBIT_ORDER_CATEGORIES:
        raise ProviderCoreError("unsupported Bybit execution query category")
    response_category = _text(result.get("category"), name="result.category").lower()
    if response_category != query_category:
        raise ProviderCoreError(
            "Bybit execution response category does not match query"
        )

    expected_client_id = observation.query_binding.query.get("orderLinkId")
    if expected_client_id is not None and any(
        fill.client_order_id != expected_client_id for fill in fills
    ):
        raise ProviderCoreError(
            "Bybit execution orderLinkId response does not match exact query"
        )

    raw_cursor = result.get("nextPageCursor")
    next_cursor = (
        None
        if raw_cursor in (None, "")
        else _opaque_cursor(raw_cursor, name="nextPageCursor")
    )
    return BybitExecutionPage(
        account_id=observation.account_id,
        environment=observation.environment,
        provider_environment=provider_environment,
        category=query_category,
        fills=fills,
        next_cursor=next_cursor,
        evidence_ref=observation.evidence_ref,
    )


def prepare_next_execution_read_query(
    *,
    observation: ProviderResponseObservation,
    capability: CapabilitySnapshot,
    provider_environment: str,
    instrument_versions: Mapping[str, str],
    at: datetime,
    qualified_fee_currencies: Mapping[str, str] | None = None,
) -> AuthenticatedReadQueryBinding | None:
    """Continue an exact execution cursor chain under the same capability."""

    page = parse_execution_page(
        observation,
        provider_environment=provider_environment,
        instrument_versions=instrument_versions,
        qualified_fee_currencies=qualified_fee_currencies,
    )
    if page.next_cursor is None:
        return None
    binding = observation.query_binding
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if (
        capability.snapshot_id != binding.capability_snapshot_id
        or capability.provider_id.upper() != binding.provider_id
        or capability.account_id != binding.account_id
        or capability.entity_id != binding.entity_id
        or capability.environment != binding.environment
        or capability.instrument_version != binding.instrument_version
    ):
        raise ProviderCoreError(
            "Bybit execution pagination must retain the exact verified capability scope"
        )
    query = dict(binding.query)
    query["cursor"] = page.next_cursor
    return prepare_authenticated_read_query(
        capability=capability,
        surface=binding.surface,
        endpoint=binding.endpoint,
        query=query,
        at=at,
        permission_scope=binding.permission_scope,
    )


def execution_history_coverage_from_pages(
    observations: tuple[ProviderResponseObservation, ...],
    *,
    provider_environment: str,
    instrument_versions: Mapping[str, str],
    consistency_horizon_satisfied: bool,
    qualified_fee_currencies: Mapping[str, str] | None = None,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Derive execution coverage only from a complete exact cursor chain."""

    if not isinstance(observations, tuple) or not observations:
        raise ProviderCoreError(
            "Bybit execution coverage requires a non-empty immutable page tuple"
        )
    for name, value in (
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise ProviderCoreError(f"{name} must be boolean")

    pages = tuple(
        parse_execution_page(
            observation,
            provider_environment=provider_environment,
            instrument_versions=instrument_versions,
            qualified_fee_currencies=qualified_fee_currencies,
        )
        for observation in observations
    )
    first_observation = observations[0]
    first_binding = first_observation.query_binding
    if "cursor" in first_binding.query:
        raise ProviderCoreError(
            "Bybit execution coverage must begin at the first page"
        )
    raw_start = first_binding.query.get("startTime")
    raw_end = first_binding.query.get("endTime")
    if raw_start is None or raw_end is None:
        raise ProviderCoreError(
            "Bybit execution coverage requires explicit startTime and endTime"
        )
    start_ms = _integer(raw_start, name="startTime", minimum=0)
    end_ms = _integer(raw_end, name="endTime", minimum=0)
    if end_ms < start_ms:
        raise ProviderCoreError("Bybit execution history end precedes start")
    if end_ms - start_ms > _MAX_ORDER_HISTORY_WINDOW_MS:
        raise ProviderCoreError(
            "Bybit execution history window cannot exceed seven days"
        )

    base_query = {
        key: value
        for key, value in first_binding.query.items()
        if key != "cursor"
    }
    first_page = pages[0]
    scope = (
        first_page.account_id,
        first_page.environment,
        first_page.provider_environment,
        first_page.category,
    )
    evidence_refs: set[str] = set()
    for index, (observation, page) in enumerate(zip(observations, pages)):
        if (
            page.account_id,
            page.environment,
            page.provider_environment,
            page.category,
        ) != scope:
            raise ProviderCoreError(
                "Bybit execution pagination crossed provider/account/category scope"
            )
        current_base = {
            key: value
            for key, value in observation.query_binding.query.items()
            if key != "cursor"
        }
        if current_base != base_query:
            raise ProviderCoreError(
                "Bybit execution pagination changed the base query"
            )
        expected_cursor = None if index == 0 else pages[index - 1].next_cursor
        actual_cursor = observation.query_binding.query.get("cursor")
        if actual_cursor != expected_cursor:
            raise ProviderCoreError(
                "Bybit execution pagination cursor chain is not contiguous"
            )
        if observation.evidence_ref in evidence_refs:
            raise ProviderCoreError(
                "Bybit execution pagination repeats exact page evidence"
            )
        evidence_refs.add(observation.evidence_ref)
        if index < len(pages) - 1 and page.next_cursor is None:
            raise ProviderCoreError(
                "Bybit execution pagination continues after a terminal page"
            )
    if pages[-1].next_cursor is not None:
        raise ProviderCoreError("Bybit execution pagination is incomplete")

    return coverage_evidence(
        account_id=first_page.account_id,
        environment=first_page.environment,
        provider_environment=first_page.provider_environment,
        surface="EXECUTIONS",
        coverage_start=_millis_to_utc(start_ms, name="startTime"),
        coverage_end=_millis_to_utc(end_ms, name="endTime"),
        pagination_complete=True,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        qualified_exclusion_semantics=qualified_exclusion_semantics,
    )


def parse_executions(
    observation: ProviderResponseObservation,
    *,
    provider_environment: str | None = None,
    instrument_versions: Mapping[str, str],
    qualified_fee_currencies: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map one authenticated, exact-byte Bybit execution read into fills."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    if provider_environment is None:
        raise ProviderCoreError(
            "Bybit execution parsing requires expected provider_environment"
        )
    provider_environment_scope = _text(
        provider_environment,
        name="provider_environment",
    ).upper()
    if provider_environment_scope not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        )
    observation.require_scope(
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["EXECUTIONS"],
        provider_environment=provider_environment_scope,
    )
    response = observation.payload
    account_id = observation.account_id
    environment = observation.environment
    envelope = _mapping(response, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit execution response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    rows = result.get("list")
    if not isinstance(rows, (list, tuple)):
        raise ProviderCoreError("result.list must be an array")
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")
    if qualified_fee_currencies is not None and not isinstance(
        qualified_fee_currencies, Mapping
    ):
        raise ProviderCoreError("qualified_fee_currencies must be a mapping")

    by_execution: dict[str, ProviderFillEvidence] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, name=f"result.list[{index}]")
        execution_id = _text(row.get("execId"), name="execId")
        symbol = _text(row.get("symbol"), name="symbol")
        try:
            instrument = instrument_versions[symbol]
        except KeyError as error:
            raise ProviderCoreError(
                f"unmapped Bybit instrument symbol: {symbol}"
            ) from error
        instrument = _text(instrument, name="instrument_version")

        link = row.get("orderLinkId")
        client_id = None
        if link not in (None, ""):
            client_id = _client_order_id(link)

        extra_fees = row.get("extraFees")
        if extra_fees not in (None, "", [], {}, ()):
            raise ProviderCoreError(
                "Bybit execution has extraFees that are not yet represented "
                "in canonical fill economics"
            )

        provider_fee_currency = row.get("feeCurrency")
        if isinstance(provider_fee_currency, str) and provider_fee_currency.strip():
            fee_currency = provider_fee_currency.strip()
        else:
            if qualified_fee_currencies is None:
                raise ProviderCoreError(
                    "Bybit execution fee currency is unresolved; qualified "
                    "fee-currency evidence is required"
                )
            try:
                fee_currency = qualified_fee_currencies[instrument]
            except KeyError as error:
                raise ProviderCoreError(
                    "Bybit execution fee currency is unresolved for instrument"
                ) from error
            fee_currency = _text(
                fee_currency,
                name="qualified fee currency",
            )

        side = _text(row.get("side"), name="side").upper()
        if side not in {"BUY", "SELL"}:
            raise ProviderCoreError("execution side must be BUY or SELL")
        fill = ProviderFillEvidence.create(
            provider_id="BYBIT",
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment_scope,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=instrument,
            side=side,
            quantity=row.get("execQty"),
            price=row.get("execPrice"),
            fee_amount=row.get("execFee"),
            fee_currency=fee_currency,
            trade_time=_millis_to_utc(row.get("execTime"), name="execTime"),
            evidence_refs=(observation.evidence_ref,),
        )
        previous = by_execution.get(execution_id)
        if previous is not None and previous != fill:
            raise ProviderCoreError(
                "Bybit execution id appears with conflicting economic content"
            )
        by_execution[execution_id] = fill

    return tuple(by_execution.values())


@dataclass(frozen=True)
class BybitWalletCoinObservation:
    """One UTA wallet coin observation with liabilities preserved separately."""

    coin: str
    wallet_balance: Decimal
    equity: Decimal
    borrow_amount: Decimal
    spot_borrow: Decimal
    locked: Decimal
    unrealised_pnl: Decimal

    def __post_init__(self) -> None:
        coin = _text(self.coin, name="coin").upper()
        if coin != self.coin:
            object.__setattr__(self, "coin", coin)
        for name in (
            "wallet_balance",
            "equity",
            "borrow_amount",
            "spot_borrow",
            "locked",
            "unrealised_pnl",
        ):
            object.__setattr__(
                self,
                name,
                _decimal(getattr(self, name), name=name),
            )
        if self.borrow_amount < 0 or self.spot_borrow < 0 or self.locked < 0:
            raise ProviderCoreError(
                "Bybit wallet liabilities and locked balance must be non-negative"
            )
        if self.spot_borrow > self.borrow_amount:
            raise ProviderCoreError(
                "Bybit spotBorrow cannot exceed total borrowAmount"
            )


@dataclass(frozen=True)
class BybitWalletSnapshot:
    account_id: str
    environment: str
    provider_environment: str
    account_type: str
    coins: tuple[BybitWalletCoinObservation, ...]
    observed_at: str
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        if _text(self.account_type, name="account_type").upper() != "UNIFIED":
            raise ProviderCoreError("Bybit wallet snapshot requires UNIFIED account type")
        object.__setattr__(self, "account_type", "UNIFIED")
        if not isinstance(self.coins, tuple):
            raise TypeError("coins must be an immutable tuple")
        seen: set[str] = set()
        for coin in self.coins:
            if not isinstance(coin, BybitWalletCoinObservation):
                raise TypeError("coins must contain BybitWalletCoinObservation values")
            if coin.coin in seen:
                raise ProviderCoreError(
                    "Bybit wallet snapshot contains duplicate normalized coin"
                )
            seen.add(coin.coin)
        object.__setattr__(
            self,
            "observed_at",
            _utc_text(self.observed_at, name="observed_at"),
        )
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")


def prepare_wallet_read_query(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    coins: tuple[str, ...] = (),
) -> AuthenticatedReadQueryBinding:
    """Prepare a UNIFIED wallet read without inventing account equivalence."""

    if not isinstance(coins, tuple):
        raise TypeError("coins must be an immutable tuple")
    normalized: list[str] = []
    for value in coins:
        coin = _text(value, name="coin").upper()
        if "," in coin:
            raise ProviderCoreError("Bybit wallet coin entries must not contain commas")
        if coin in normalized:
            raise ProviderCoreError("Bybit wallet coin scope contains duplicates")
        normalized.append(coin)
    query = {"accountType": "UNIFIED"}
    if normalized:
        query["coin"] = ",".join(normalized)
    return prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["WALLET"],
        query=query,
        at=at,
        permission_scope="ACCOUNT.READ",
    )


def parse_wallet_snapshot(
    observation: ProviderResponseObservation,
) -> BybitWalletSnapshot:
    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["WALLET"],
    )
    provider_environment = _text(
        observation.provider_environment,
        name="provider_environment",
    ).upper()
    if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        )
    if (
        _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
        != observation.environment
    ):
        raise ProviderCoreError(
            "Bybit provider environment does not match runtime environment"
        )
    envelope = _mapping(observation.payload, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit wallet response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    rows = result.get("list")
    if not isinstance(rows, (list, tuple)):
        raise ProviderCoreError("result.list must be an array")
    if len(rows) != 1:
        raise ProviderCoreError(
            "Bybit UNIFIED wallet response must contain exactly one account record"
        )
    account = _mapping(rows[0], name="result.list[0]")
    account_type = _text(account.get("accountType"), name="accountType").upper()
    if account_type != "UNIFIED":
        raise ProviderCoreError("Bybit wallet response accountType mismatch")
    raw_coins = account.get("coin")
    if not isinstance(raw_coins, (list, tuple)):
        raise ProviderCoreError("wallet coin must be an array")

    requested_raw = observation.query_binding.query.get("coin")
    requested = (
        None
        if requested_raw is None
        else frozenset(
            _text(item, name="requested coin").upper()
            for item in requested_raw.split(",")
        )
    )
    by_coin: dict[str, BybitWalletCoinObservation] = {}
    for index, value in enumerate(raw_coins):
        row = _mapping(value, name=f"coin[{index}]")
        coin = _text(row.get("coin"), name="coin").upper()
        if requested is not None and coin not in requested:
            raise ProviderCoreError(
                "Bybit wallet response contains coin outside exact query scope"
            )
        item = BybitWalletCoinObservation(
            coin=coin,
            wallet_balance=_decimal(row.get("walletBalance"), name="walletBalance"),
            equity=_decimal(row.get("equity"), name="equity"),
            borrow_amount=_decimal(row.get("borrowAmount"), name="borrowAmount"),
            spot_borrow=_decimal(row.get("spotBorrow"), name="spotBorrow"),
            locked=_decimal(row.get("locked"), name="locked"),
            unrealised_pnl=_decimal(
                row.get("unrealisedPnl"),
                name="unrealisedPnl",
            ),
        )
        previous = by_coin.get(item.coin)
        if previous is not None and previous != item:
            raise ProviderCoreError(
                "Bybit wallet coin has conflicting observations"
            )
        by_coin[item.coin] = item

    return BybitWalletSnapshot(
        account_id=observation.account_id,
        environment=observation.environment,
        provider_environment=provider_environment,
        account_type=account_type,
        coins=tuple(by_coin[key] for key in sorted(by_coin)),
        observed_at=observation.observed_at,
        evidence_ref=observation.evidence_ref,
    )


def provider_wallet_cash(
    snapshot: BybitWalletSnapshot,
) -> Mapping[str, Decimal]:
    """Project provider cash only when no unsupported liability is present."""

    if not isinstance(snapshot, BybitWalletSnapshot):
        raise TypeError("snapshot must be BybitWalletSnapshot")
    borrowed = tuple(
        coin.coin
        for coin in snapshot.coins
        if coin.borrow_amount != 0 or coin.spot_borrow != 0
    )
    if borrowed:
        raise ProviderCoreError(
            "Bybit wallet contains liabilities that generic cash reconciliation "
            "cannot represent safely: "
            + ", ".join(borrowed)
        )
    return MappingProxyType(
        {coin.coin: coin.wallet_balance for coin in snapshot.coins}
    )


@dataclass(frozen=True)
class BybitPositionObservation:
    account_id: str
    environment: str
    provider_environment: str
    category: str
    symbol: str
    position_idx: int
    side: str | None
    size: Decimal
    position_status: str
    updated_at: str
    sequence: int
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        category = _text(self.category, name="category").lower()
        if category not in {"linear", "inverse", "option"}:
            raise ProviderCoreError("unsupported Bybit position category")
        object.__setattr__(self, "category", category)
        symbol = _text(self.symbol, name="symbol")
        if symbol != symbol.upper():
            raise ProviderCoreError("Bybit position symbol must be uppercase")
        object.__setattr__(self, "symbol", symbol)
        idx = _integer(self.position_idx, name="position_idx", minimum=0)
        if idx not in {0, 1, 2}:
            raise ProviderCoreError("unsupported Bybit positionIdx")
        object.__setattr__(self, "position_idx", idx)
        size = _decimal(self.size, name="size")
        if size < 0:
            raise ProviderCoreError("Bybit position size cannot be negative")
        object.__setattr__(self, "size", size)
        if self.side is None or self.side == "":
            side = None
        else:
            side = _text(self.side, name="side").upper()
            if side not in {"BUY", "SELL"}:
                raise ProviderCoreError("Bybit position side must be BUY or SELL")
        if size > 0 and side is None:
            raise ProviderCoreError("non-zero Bybit position requires side")
        if size == 0 and side is not None:
            raise ProviderCoreError("zero Bybit position must not claim a side")
        if idx == 1 and side not in {None, "BUY"}:
            raise ProviderCoreError("Bybit hedge long position must use BUY side")
        if idx == 2 and side not in {None, "SELL"}:
            raise ProviderCoreError("Bybit hedge short position must use SELL side")
        object.__setattr__(self, "side", side)
        status = _text(self.position_status, name="position_status")
        if status not in {"Normal", "Liq", "Adl"}:
            raise ProviderCoreError("unsupported Bybit position status")
        object.__setattr__(self, "position_status", status)
        object.__setattr__(
            self,
            "updated_at",
            _millis_to_utc(self.updated_at, name="updatedTime"),
        )
        sequence = _integer(self.sequence, name="sequence")
        if sequence < -1:
            raise ProviderCoreError("Bybit position seq cannot be below -1")
        object.__setattr__(self, "sequence", sequence)
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")


@dataclass(frozen=True)
class BybitPositionPage:
    account_id: str
    environment: str
    provider_environment: str
    category: str
    positions: tuple[BybitPositionObservation, ...]
    next_cursor: str | None
    evidence_ref: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
            raise ProviderCoreError(
                "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
            )
        if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment] != environment:
            raise ProviderCoreError(
                "Bybit provider environment does not match runtime environment"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)
        category = _text(self.category, name="category").lower()
        if category not in {"linear", "inverse", "option"}:
            raise ProviderCoreError("unsupported Bybit position category")
        object.__setattr__(self, "category", category)
        if not isinstance(self.positions, tuple):
            raise TypeError("positions must be an immutable tuple")
        identities: set[tuple[str, int]] = set()
        for position in self.positions:
            if not isinstance(position, BybitPositionObservation):
                raise TypeError(
                    "positions must contain BybitPositionObservation values"
                )
            if (
                position.account_id != self.account_id
                or position.environment != self.environment
                or position.provider_environment != self.provider_environment
                or position.category != self.category
            ):
                raise ProviderCoreError(
                    "Bybit position page contains cross-scope observation"
                )
            identity = (position.symbol, position.position_idx)
            if identity in identities:
                raise ProviderCoreError(
                    "Bybit position page contains duplicate position identity"
                )
            identities.add(identity)
        if self.next_cursor is not None:
            object.__setattr__(
                self,
                "next_cursor",
                _opaque_cursor(self.next_cursor, name="next_cursor"),
            )
        if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", self.evidence_ref) is None:
            raise ProviderCoreError("evidence_ref must be canonical provider-read evidence")

    @property
    def pagination_complete(self) -> bool:
        return self.next_cursor is None


def prepare_position_read_query(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    category: str,
    symbol: str | None = None,
    settle_coin: str | None = None,
    base_coin: str | None = None,
    cursor: str | None = None,
    limit: object = 200,
) -> AuthenticatedReadQueryBinding:
    normalized_category = _text(category, name="category").lower()
    if normalized_category not in {"linear", "inverse", "option"}:
        raise ProviderCoreError("unsupported Bybit position category")
    normalized_limit = _integer(limit, name="limit", minimum=1)
    if normalized_limit > 200:
        raise ProviderCoreError("Bybit position page limit cannot exceed 200")
    query: dict[str, str] = {
        "category": normalized_category,
        "limit": str(normalized_limit),
    }
    for parameter, value in (
        ("symbol", symbol),
        ("settleCoin", settle_coin),
        ("baseCoin", base_coin),
    ):
        if value is not None:
            normalized = _text(value, name=parameter)
            if normalized != normalized.upper():
                raise ProviderCoreError(f"Bybit {parameter} must be uppercase")
            query[parameter] = normalized
    if normalized_category == "linear" and symbol is None and settle_coin is None:
        raise ProviderCoreError(
            "Bybit linear position query requires symbol or settleCoin"
        )
    if normalized_category != "option" and base_coin is not None:
        raise ProviderCoreError("Bybit baseCoin position filter is option-only")
    if cursor is not None:
        query["cursor"] = _opaque_cursor(cursor)
    return prepare_authenticated_read_query(
        capability=capability,
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["POSITIONS"],
        query=query,
        at=at,
        permission_scope="ACCOUNT.READ",
    )


def parse_position_page(
    observation: ProviderResponseObservation,
) -> BybitPositionPage:
    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["POSITIONS"],
    )
    provider_environment = _text(
        observation.provider_environment,
        name="provider_environment",
    ).upper()
    if provider_environment not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit provider_environment must be MAINNET, TESTNET or DEMO"
        )
    if (
        _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
        != observation.environment
    ):
        raise ProviderCoreError(
            "Bybit provider environment does not match runtime environment"
        )
    envelope = _mapping(observation.payload, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit position response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    query_category = observation.query_binding.query.get("category")
    if query_category is None:
        raise ProviderCoreError("Bybit position query is missing category provenance")
    query_category = _text(query_category, name="query category").lower()
    response_category = _text(result.get("category"), name="result.category").lower()
    if response_category != query_category:
        raise ProviderCoreError(
            "Bybit position response category does not match query"
        )
    rows = result.get("list")
    if not isinstance(rows, (list, tuple)):
        raise ProviderCoreError("result.list must be an array")
    raw_cursor = result.get("nextPageCursor")
    next_cursor = (
        None
        if raw_cursor in (None, "")
        else _opaque_cursor(raw_cursor, name="nextPageCursor")
    )
    by_identity: dict[tuple[str, int], BybitPositionObservation] = {}
    for index, value in enumerate(rows):
        row = _mapping(value, name=f"result.list[{index}]")
        symbol = _text(row.get("symbol"), name="symbol")
        position_idx = _integer(
            row.get("positionIdx"),
            name="positionIdx",
            minimum=0,
        )
        side_raw = row.get("side")
        side = None if side_raw in (None, "") else _text(side_raw, name="side")
        item = BybitPositionObservation(
            account_id=observation.account_id,
            environment=observation.environment,
            provider_environment=provider_environment,
            category=query_category,
            symbol=symbol,
            position_idx=position_idx,
            side=side,
            size=_decimal(row.get("size"), name="size"),
            position_status=_text(
                row.get("positionStatus"),
                name="positionStatus",
            ),
            updated_at=row.get("updatedTime"),
            sequence=_integer(row.get("seq"), name="seq"),
            evidence_ref=observation.evidence_ref,
        )
        identity = (item.symbol, item.position_idx)
        previous = by_identity.get(identity)
        if previous is not None and previous != item:
            raise ProviderCoreError(
                "Bybit position identity has conflicting observations"
            )
        by_identity[identity] = item
    return BybitPositionPage(
        account_id=observation.account_id,
        environment=observation.environment,
        provider_environment=provider_environment,
        category=query_category,
        positions=tuple(by_identity[key] for key in sorted(by_identity)),
        next_cursor=next_cursor,
        evidence_ref=observation.evidence_ref,
    )


def prepare_next_position_read_query(
    *,
    observation: ProviderResponseObservation,
    capability: CapabilitySnapshot,
    at: datetime,
) -> AuthenticatedReadQueryBinding | None:
    page = parse_position_page(observation)
    if page.next_cursor is None:
        return None
    binding = observation.query_binding
    if (
        capability.snapshot_id != binding.capability_snapshot_id
        or capability.provider_id.upper() != binding.provider_id
        or capability.account_id != binding.account_id
        or capability.entity_id != binding.entity_id
        or capability.environment != binding.environment
        or capability.instrument_version != binding.instrument_version
    ):
        raise ProviderCoreError(
            "Bybit position pagination must retain exact verified capability scope"
        )
    query = dict(binding.query)
    query["cursor"] = page.next_cursor
    return prepare_authenticated_read_query(
        capability=capability,
        surface=binding.surface,
        endpoint=binding.endpoint,
        query=query,
        at=at,
        permission_scope=binding.permission_scope,
    )


def provider_position_quantities_from_pages(
    observations: tuple[ProviderResponseObservation, ...],
    *,
    instrument_versions: Mapping[str, str],
) -> Mapping[str, Decimal]:
    """Project complete one-way Bybit positions to canonical signed quantities."""

    if not isinstance(observations, tuple) or not observations:
        raise ProviderCoreError(
            "Bybit position projection requires non-empty immutable pages"
        )
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")
    pages = tuple(parse_position_page(item) for item in observations)
    first_binding = observations[0].query_binding
    if "cursor" in first_binding.query:
        raise ProviderCoreError("Bybit position projection must begin at first page")
    base_query = {
        key: value
        for key, value in first_binding.query.items()
        if key != "cursor"
    }
    first = pages[0]
    scope = (
        first.account_id,
        first.environment,
        first.provider_environment,
        first.category,
    )
    evidence_refs: set[str] = set()
    quantities: dict[str, Decimal] = {}
    seen_provider_positions: set[tuple[str, int]] = set()
    for index, (observation, page) in enumerate(zip(observations, pages)):
        if (
            page.account_id,
            page.environment,
            page.provider_environment,
            page.category,
        ) != scope:
            raise ProviderCoreError(
                "Bybit position pagination crossed provider/account/category scope"
            )
        current_base = {
            key: value
            for key, value in observation.query_binding.query.items()
            if key != "cursor"
        }
        if current_base != base_query:
            raise ProviderCoreError(
                "Bybit position pagination changed the base query"
            )
        expected_cursor = None if index == 0 else pages[index - 1].next_cursor
        actual_cursor = observation.query_binding.query.get("cursor")
        if actual_cursor != expected_cursor:
            raise ProviderCoreError(
                "Bybit position pagination cursor chain is not contiguous"
            )
        if observation.evidence_ref in evidence_refs:
            raise ProviderCoreError(
                "Bybit position pagination repeats exact page evidence"
            )
        evidence_refs.add(observation.evidence_ref)
        if index < len(pages) - 1 and page.next_cursor is None:
            raise ProviderCoreError(
                "Bybit position pagination continues after terminal page"
            )
        for position in page.positions:
            identity = (position.symbol, position.position_idx)
            if identity in seen_provider_positions:
                raise ProviderCoreError(
                    "Bybit position identity repeats across cursor pages"
                )
            seen_provider_positions.add(identity)
            if position.position_status != "Normal":
                raise ProviderCoreError(
                    "Bybit Liq/Adl position state blocks canonical position projection"
                )
            if position.position_idx != 0:
                raise ProviderCoreError(
                    "Bybit hedge-mode positions require side-preserving canonical "
                    "position reconciliation"
                )
            if position.size == 0:
                continue
            try:
                instrument = _text(
                    instrument_versions[position.symbol],
                    name="instrument_version",
                )
            except KeyError as error:
                raise ProviderCoreError(
                    f"unmapped Bybit instrument symbol: {position.symbol}"
                ) from error
            if instrument in quantities:
                raise ProviderCoreError(
                    "multiple Bybit symbols map to one canonical position identity"
                )
            quantities[instrument] = (
                position.size if position.side == "BUY" else -position.size
            )
    if pages[-1].next_cursor is not None:
        raise ProviderCoreError("Bybit position pagination is incomplete")
    return MappingProxyType(dict(sorted(quantities.items())))


def coverage_evidence(
    *,
    account_id: str,
    environment: str,
    provider_environment: str,
    surface: str,
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Create one provider-surface fact without overclaiming proven absence.

    The default deliberately does not claim that missing rows exclude execution.
    That stronger fact must come from recorded qualification evidence for the
    exact endpoint/product/environment before reconciliation may use it.
    """

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise ProviderCoreError("unsupported Bybit reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise ProviderCoreError(f"{name} must be boolean")
    return CoverageSurfaceEvidence(
        provider_id="BYBIT",
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=qualified_exclusion_semantics,
    )
