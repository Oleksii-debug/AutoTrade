"""Pure Kraken Futures contract-adapter foundation.

This module deliberately performs no networking, stores no credentials and
cannot grant trading authority. It keeps Kraken Futures live/demo semantics
separate from Kraken Spot and maps only recorded provider facts into AutoTrade
provider/reconciliation contracts.
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

from .capabilities import CapabilitySnapshot
from .provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    ProviderSubmissionObservation,
    Surface,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


KRAKEN_FUTURES_BASE_URLS: Mapping[str, str] = {
    "LIVE": "https://futures.kraken.com",
    "DEMO": "https://demo-futures.kraken.com",
}
_RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT: Mapping[str, str] = {
    "LIVE": "LIVE",
    "DEMO": "PAPER",
}
KRAKEN_FUTURES_ENDPOINTS: Mapping[str, str] = {
    "PLACE_ORDER": "/derivatives/api/v3/sendorder",
    "OPEN_ORDERS": "/derivatives/api/v3/openorders",
    "FILLS": "/derivatives/api/v3/fills",
    "EXECUTION_HISTORY": "/api/history/v3/executions",
    "ORDER_HISTORY": "/api/history/v3/orders",
    "POSITION_HISTORY": "/api/history/v3/positions",
    "CANCEL_AFTER": "/derivatives/api/v3/cancelallordersafter",
}

_FREE_CLIENT_ID = re.compile(r"^[\x21-\x7E]{1,18}$")
_SHORT_UUID = re.compile(r"^[0-9a-fA-F]{32}$")


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


def _mapping(value: object, *, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProviderCoreError(f"{name} must be an object")
    return value


def _integer(value: object, *, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ProviderCoreError(f"{name} must be an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isdigit():
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
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _client_order_id(value: object) -> str:
    client_id = _text(value, name="client_order_id")
    if _SHORT_UUID.fullmatch(client_id):
        return client_id
    try:
        UUID(client_id)
        return client_id
    except ValueError:
        pass
    if _FREE_CLIENT_ID.fullmatch(client_id):
        return client_id
    raise ProviderCoreError(
        "client_order_id must be a UUID/short UUID or 1-18 printable ASCII characters"
    )


def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be a UUID") from error
    return text


def _iso_utc(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be RFC3339") from error
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


def futures_base_url(environment: str) -> str:
    env = _text(environment, name="environment").upper()
    try:
        return KRAKEN_FUTURES_BASE_URLS[env]
    except KeyError as error:
        raise ProviderCoreError("Kraken Futures environment must be LIVE or DEMO") from error


def _response_evidence(
    observation: ProviderSubmissionObservation,
    *,
    prepared_request: "KrakenFuturesPreparedRequest",
) -> dict[str, str]:
    """Bind one durable exact provider response to the guarded Futures request."""

    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    if not isinstance(prepared_request, KrakenFuturesPreparedRequest):
        raise TypeError("prepared_request must be KrakenFuturesPreparedRequest")
    cid = _client_order_id(prepared_request.body.get("cliOrdId"))
    observation.require_scope(
        provider_id="KRAKEN",
        endpoint=prepared_request.endpoint,
        prepared_request_sha256=prepared_request.body_sha256,
        capability_snapshot_ids=(prepared_request.capability_snapshot_id,),
        instrument_versions=(prepared_request.instrument_version,),
        account_id=prepared_request.account_id,
        environment=prepared_request.environment,
        client_order_id=cid,
    )
    source = (
        futures_base_url(prepared_request.provider_environment)
        + prepared_request.endpoint
    )
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"{source}#{observation.evidence_ref}",
            )
        ),
        "sha256": observation.response_sha256,
        "source_uri": source,
        "observed_at": observation.observed_at,
        "rights_id": "provider-observation-kraken-futures",
    }


def build_order_payload(
    *,
    environment: str,
    symbol: str,
    side: str,
    order_type: str,
    size: object,
    client_order_id: str,
    price: object | None = None,
    reduce_only: bool = False,
) -> dict[str, str]:
    """Build a bounded Futures sendorder payload without sending it."""

    futures_base_url(environment)
    provider_symbol = _text(symbol, name="symbol")
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")
    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in {"MARKET", "LIMIT"}:
        raise ProviderCoreError("Kraken Futures foundation supports MARKET or LIMIT only")
    if type(reduce_only) is not bool:
        raise ProviderCoreError("reduce_only must be boolean")
    if normalized_type == "MARKET" and price is not None:
        raise ProviderCoreError("market order must not carry an ignored limit price")
    if normalized_type == "LIMIT" and price is None:
        raise ProviderCoreError("limit price is required")

    payload = {
        "orderType": "mkt" if normalized_type == "MARKET" else "lmt",
        "symbol": provider_symbol,
        "side": normalized_side.lower(),
        "size": _decimal_text(size, name="size", positive=True),
        "cliOrdId": _client_order_id(client_order_id),
    }
    if price is not None:
        payload["limitPrice"] = _decimal_text(price, name="price", positive=True)
    if reduce_only:
        payload["reduceOnly"] = "true"
    return payload



_KRAKEN_FUTURES_PREPARED_REQUEST_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class KrakenFuturesPreparedRequest:
    """Canonical Futures sendorder scope fixed before the send barrier."""

    endpoint: str
    body: Mapping[str, str]
    account_id: str
    environment: str
    provider_environment: str
    capability_snapshot_id: str
    instrument_version: str
    body_sha256: str = field(init=False)
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _KRAKEN_FUTURES_PREPARED_REQUEST_FACTORY_TOKEN:
            raise ProviderCoreError(
                "KrakenFuturesPreparedRequest must come from canonical preparation"
            )
        endpoint = _text(self.endpoint, name="endpoint")
        if endpoint != KRAKEN_FUTURES_ENDPOINTS["PLACE_ORDER"]:
            raise ProviderCoreError(
                "prepared Kraken Futures endpoint must be sendorder"
            )
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be a mapping")
        body = dict(self.body)
        body["cliOrdId"] = _client_order_id(body.get("cliOrdId"))
        try:
            encoded = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ProviderCoreError(
                "prepared Kraken Futures body must be canonical JSON"
            ) from error
        account = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise ProviderCoreError(
                "Kraken Futures runtime environment must be PAPER or LIVE"
            )
        provider_environment = _text(
            self.provider_environment,
            name="provider_environment",
        ).upper()
        futures_base_url(provider_environment)
        if (
            _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_environment]
            != environment
        ):
            raise ProviderCoreError(
                "Kraken Futures provider environment does not match runtime environment"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "body", MappingProxyType(body))
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "provider_environment",
            provider_environment,
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _text(self.capability_snapshot_id, name="capability_snapshot_id"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(
            self,
            "body_sha256",
            "sha256:" + sha256(encoded).hexdigest(),
        )


def prepare_order_request(
    *,
    capability: CapabilitySnapshot,
    account_id: str,
    provider_environment: str,
    instrument_version: str,
    at: datetime,
    symbol: str,
    side: str,
    order_type: str,
    size: object,
    client_order_id: str,
    price: object | None = None,
    reduce_only: bool = False,
    time_in_force: str = "GTC",
) -> KrakenFuturesPreparedRequest:
    """Prepare an admitted Futures request without sending it."""

    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if not isinstance(at, datetime) or at.tzinfo is None:
        raise ProviderCoreError("at must be timezone-aware")
    point = at.astimezone(timezone.utc)
    account = _text(account_id, name="account_id")
    provider_env = _text(
        provider_environment,
        name="provider_environment",
    ).upper()
    futures_base_url(provider_env)
    runtime_env = _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_env]
    instrument = _text(instrument_version, name="instrument_version")
    if capability.provider_id.upper() != "KRAKEN":
        raise ProviderCoreError("capability belongs to another provider")
    if capability.account_id != account:
        raise ProviderCoreError("capability account does not match target account")
    if capability.environment.upper() != runtime_env:
        raise ProviderCoreError(
            "capability environment does not match target runtime environment"
        )
    if capability.instrument_version != instrument:
        raise ProviderCoreError(
            "capability instrument version does not match target instrument"
        )
    tif = _text(time_in_force, name="time_in_force").upper()
    if not capability.admits(
        at=point,
        order_type=_text(order_type, name="order_type").upper(),
        time_in_force=tif,
        permission_scope="ORDER_WRITE",
    ):
        raise ProviderCoreError("exact capability evidence does not admit this order")
    body = build_order_payload(
        environment=provider_env,
        symbol=symbol,
        side=side,
        order_type=order_type,
        size=size,
        client_order_id=client_order_id,
        price=price,
        reduce_only=reduce_only,
    )
    return KrakenFuturesPreparedRequest(
        endpoint=KRAKEN_FUTURES_ENDPOINTS["PLACE_ORDER"],
        body=body,
        account_id=account,
        environment=runtime_env,
        provider_environment=provider_env,
        capability_snapshot_id=capability.snapshot_id,
        instrument_version=instrument,
        _factory_token=_KRAKEN_FUTURES_PREPARED_REQUEST_FACTORY_TOKEN,
    )

def parse_submission_response(
    *,
    attempt_id: str,
    prepared_request: KrakenFuturesPreparedRequest,
    observation: ProviderSubmissionObservation | None = None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map one durable exact Futures sendorder response into SubmissionResult."""

    aid = _uuid_text(attempt_id, name="attempt_id")
    if not isinstance(prepared_request, KrakenFuturesPreparedRequest):
        raise TypeError("prepared_request must be KrakenFuturesPreparedRequest")
    cid = _client_order_id(prepared_request.body.get("cliOrdId"))
    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if observation is not None:
            raise ProviderCoreError(
                "ambiguous transport must not claim authoritative provider response"
            )
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "KRAKEN_FUTURES_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }
    if not isinstance(observation, ProviderSubmissionObservation):
        raise TypeError(
            "observation must be durable ProviderSubmissionObservation"
        )
    if observation.response_binding.attempt_id != aid:
        raise ProviderCoreError("Kraken Futures submission observation attempt_id mismatch")
    evidence = [
        _response_evidence(
            observation,
            prepared_request=prepared_request,
        )
    ]
    envelope = _mapping(observation.payload, name="response")
    result = _text(envelope.get("result"), name="result").lower()
    if result != "success":
        error = envelope.get("error")
        if error in (None, ""):
            errors = envelope.get("errors")
            error = errors if errors not in (None, (), []) else "UNKNOWN_ERROR"
        return {
            "attempt_id": aid,
            "outcome": "REJECTED",
            "client_order_id": cid,
            "reason_code": "KRAKEN_FUTURES_" + str(error),
            "evidence": evidence,
            "retry_disposition": "NEVER",
        }

    send_status = envelope.get("sendStatus")
    if isinstance(send_status, str):
        try:
            send_status = json.loads(send_status)
        except json.JSONDecodeError as error:
            raise ProviderCoreError(
                "Kraken Futures sendStatus string is invalid JSON"
            ) from error
    status = _mapping(send_status, name="sendStatus")
    echoed = status.get("cliOrdId")
    if echoed is None:
        echoed = status.get("cli_ord_id")
    if echoed not in (None, "") and _client_order_id(echoed) != cid:
        raise ProviderCoreError(
            "Kraken Futures client identity does not match guarded request"
        )
    provider_order_id = status.get("order_id")
    if provider_order_id in (None, ""):
        provider_order_id = status.get("orderId")
    provider_order_id = _text(provider_order_id, name="sendStatus.order_id")
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": provider_order_id,
        "client_order_id": cid,
        "evidence": evidence,
        "retry_disposition": "NEVER",
    }


def parse_execution_events(
    observation: ProviderResponseObservation,
    *,
    provider_environment: str,
    instrument_versions: Mapping[str, str],
    qualified_fee_currencies: Mapping[str, str],
) -> tuple[ProviderFillEvidence, ...]:
    """Map authenticated Kraken execution-history facts into provider fill truth.

    Direction comes only from the exact provider order embedded in the
    authenticated execution event. Fee currency is never guessed: callers must
    supply a separately qualified mapping for every tradeable that is booked.
    """

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    provider_env = _text(provider_environment, name="provider_environment").upper()
    futures_base_url(provider_env)
    observation.require_scope(
        provider_id="KRAKEN",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=KRAKEN_FUTURES_ENDPOINTS["EXECUTION_HISTORY"],
        provider_environment=provider_env,
    )
    if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_env] != observation.environment:
        raise ProviderCoreError(
            "Kraken Futures provider environment does not match runtime environment"
        )
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")
    if not isinstance(qualified_fee_currencies, Mapping):
        raise ProviderCoreError("qualified_fee_currencies must be a mapping")

    envelope = _mapping(observation.payload, name="response")
    elements = envelope.get("elements")
    if not isinstance(elements, (list, tuple)):
        raise ProviderCoreError("Futures execution history elements must be an array")

    by_execution: dict[str, ProviderFillEvidence] = {}
    for index, value in enumerate(elements):
        row = _mapping(value, name=f"elements[{index}]")
        event = _mapping(row.get("event"), name=f"elements[{index}].event")
        execution_event = _mapping(
            event.get("execution"),
            name=f"elements[{index}].event.execution",
        )
        execution = _mapping(
            execution_event.get("execution"),
            name=f"elements[{index}].event.execution.execution",
        )
        execution_id = _text(execution.get("uid"), name="execution.uid")
        order = _mapping(execution.get("order"), name="execution.order")

        symbol = _text(order.get("tradeable"), name="order.tradeable")
        try:
            instrument = _text(
                instrument_versions[symbol],
                name="instrument_version",
            )
        except KeyError as error:
            raise ProviderCoreError(
                f"unmapped Kraken Futures instrument: {symbol}"
            ) from error
        try:
            fee_currency = _text(
                qualified_fee_currencies[symbol],
                name="qualified fee currency",
            ).upper()
        except KeyError as error:
            raise ProviderCoreError(
                f"unqualified Kraken Futures fee currency: {symbol}"
            ) from error

        raw_side = _text(order.get("direction"), name="order.direction")
        side_by_provider_value = {"Buy": "BUY", "Sell": "SELL"}
        if raw_side not in side_by_provider_value:
            raise ProviderCoreError(
                "Kraken Futures execution direction must be provider-evidenced Buy or Sell"
            )
        side = side_by_provider_value[raw_side]

        client_id_value = order.get("clientId")
        client_id = (
            None
            if client_id_value in (None, "")
            else _client_order_id(client_id_value)
        )
        order_data = _mapping(execution.get("orderData"), name="execution.orderData")
        fee = order_data.get("fee")
        if fee is None:
            raise ProviderCoreError(
                f"missing provider fee amount for Kraken Futures execution: {execution_id}"
            )

        fill = ProviderFillEvidence.create(
            provider_id="KRAKEN",
            account_id=observation.account_id,
            environment=observation.environment,
            provider_environment=provider_env,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=instrument,
            side=side,
            quantity=execution.get("quantity"),
            price=execution.get("price"),
            fee_amount=fee,
            fee_currency=fee_currency,
            trade_time=_millis_to_utc(
                execution.get("timestamp"),
                name="execution.timestamp",
            ),
            evidence_refs=(observation.evidence_ref,),
        )
        previous = by_execution.get(execution_id)
        if previous is not None and previous != fill:
            raise ProviderCoreError(
                "Kraken Futures execution id has conflicting economic content"
            )
        by_execution[execution_id] = fill

    return tuple(by_execution.values())


def parse_position_executions(
    observation: ProviderResponseObservation,
    *,
    instrument_versions: Mapping[str, str],
    execution_client_ids: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Extract trade executions from one capability-bound exact-byte history read."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    provider_env = _text(
        observation.provider_environment,
        name="provider_environment",
    ).upper()
    futures_base_url(provider_env)
    observation.require_scope(
        provider_id="KRAKEN",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=KRAKEN_FUTURES_ENDPOINTS["POSITION_HISTORY"],
        provider_environment=provider_env,
    )
    if _RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT[provider_env] != observation.environment:
        raise ProviderCoreError(
            "Kraken Futures provider environment does not match runtime environment"
        )
    response = observation.payload
    account_id = observation.account_id
    environment = observation.environment
    envelope = _mapping(response, name="response")
    elements = envelope.get("elements")
    if not isinstance(elements, (list, tuple)):
        raise ProviderCoreError("Futures history elements must be an array")
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")
    clients = execution_client_ids or {}
    by_execution: dict[str, ProviderFillEvidence] = {}
    for index, value in enumerate(elements):
        row = _mapping(value, name=f"elements[{index}]")
        if row.get("updateReason") != "trade":
            continue
        execution_id = _text(row.get("executionUid"), name="executionUid")
        symbol = _text(row.get("tradeable"), name="tradeable")
        try:
            instrument = _text(instrument_versions[symbol], name="instrument_version")
        except KeyError as error:
            raise ProviderCoreError(f"unmapped Kraken Futures instrument: {symbol}") from error
        fill_time = row.get("fillTime")
        if fill_time is None:
            raise ProviderCoreError("trade position event must include fillTime")
        client_id = clients.get(execution_id)
        if client_id is not None:
            client_id = _client_order_id(client_id)
        fee = row.get("fee")
        if fee is None:
            raise ProviderCoreError(
                f"missing provider fee amount for Kraken Futures position execution: {execution_id}"
            )
        fill = ProviderFillEvidence.create(
            provider_id="KRAKEN",
            account_id=account_id,
            environment=environment,
            provider_environment=provider_env,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=instrument,
            quantity=row.get("executionSize"),
            price=row.get("executionPrice"),
            fee_amount=fee,
            fee_currency=_text(row.get("feeCurrency"), name="feeCurrency"),
            trade_time=_millis_to_utc(fill_time, name="fillTime"),
            evidence_refs=(observation.evidence_ref,),
        )
        previous = by_execution.get(execution_id)
        if previous is not None and previous != fill:
            raise ProviderCoreError(
                "Kraken Futures execution id has conflicting economic content"
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
    """Create canonical absence evidence, fail-closed by default."""

    normalized = _text(surface, name="surface").upper()
    if normalized not in {"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS", "ACTIVITIES"}:
        raise ProviderCoreError("unsupported canonical reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise ProviderCoreError(f"{name} must be boolean")
    if qualified_exclusion_semantics:
        raise ProviderCoreError(
            "Kraken Futures foundation cannot self-assert provider exclusion semantics"
        )
    return CoverageSurfaceEvidence(
        provider_id="KRAKEN",
        account_id=account_id,
        environment=environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=False,
    )
