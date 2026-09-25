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
    ProviderCoreError,
    ProviderResponseObservation,
    ProviderSubmissionObservation,
    Surface,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


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
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=qualified_exclusion_semantics,
    )
