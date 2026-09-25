"""Pure Kraken Futures contract-adapter foundation.

This module deliberately performs no networking, stores no credentials and
cannot grant trading authority. It keeps Kraken Futures live/demo semantics
separate from Kraken Spot and maps only recorded provider facts into AutoTrade
provider/reconciliation contracts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .provider_core import (
    BoundReconciliationResponse,
    ProviderCoreError,
    require_reconciliation_response,
)
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


KRAKEN_FUTURES_BASE_URLS: Mapping[str, str] = {
    "LIVE": "https://futures.kraken.com",
    "DEMO": "https://demo-futures.kraken.com",
}
KRAKEN_FUTURES_ENDPOINTS: Mapping[str, str] = {
    "PLACE_ORDER": "/derivatives/api/v3/sendorder",
    "OPEN_ORDERS": "/derivatives/api/v3/openorders",
    "FILLS": "/derivatives/api/v3/fills",
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
    *,
    endpoint: str,
    response: Mapping[str, Any],
    observed_at: str,
    environment: str,
) -> dict[str, str]:
    encoded = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = sha256(encoded).hexdigest()
    source = f"{futures_base_url(environment)}{endpoint}"
    return {
        "artifact_id": str(uuid5(NAMESPACE_URL, f"{source}#sha256:{digest}")),
        "sha256": f"sha256:{digest}",
        "source_uri": source,
        "observed_at": _iso_utc(observed_at, name="observed_at"),
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


def parse_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    environment: str,
    observed_at: str,
    response: Mapping[str, Any] | None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map recorded Futures sendorder response into canonical SubmissionResult."""

    futures_base_url(environment)
    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _client_order_id(client_order_id)
    when = _iso_utc(observed_at, name="observed_at")
    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if response is not None:
            raise ProviderCoreError("ambiguous transport must not fabricate a provider response")
        # The durable SubmissionAttempt owns local observed_at/environment.
        # No provider response exists, so provider_received_at must be omitted.
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "KRAKEN_FUTURES_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }

    envelope = _mapping(response, name="response")
    evidence = [
        _response_evidence(
            endpoint=KRAKEN_FUTURES_ENDPOINTS["PLACE_ORDER"],
            response=envelope,
            observed_at=when,
            environment=environment,
        )
    ]
    result = _text(envelope.get("result"), name="result").lower()
    if result != "success":
        error = envelope.get("error")
        if error in (None, ""):
            errors = envelope.get("errors")
            error = errors if errors not in (None, []) else "UNKNOWN_ERROR"
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
            raise ProviderCoreError("Kraken Futures sendStatus string is invalid JSON") from error
    status = _mapping(send_status, name="sendStatus")
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


def parse_position_executions(
    evidence: BoundReconciliationResponse,
    *,
    instrument_versions: Mapping[str, str],
    execution_client_ids: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Extract trade executions from a provenance-bound position-history read."""

    response, account_id, environment = require_reconciliation_response(
        evidence,
        provider_id="KRAKEN",
        surface="EXECUTIONS",
        endpoint=KRAKEN_FUTURES_ENDPOINTS["POSITION_HISTORY"],
    )
    envelope = _mapping(response, name="response")
    elements = envelope.get("elements")
    if not isinstance(elements, list):
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
        fill = ProviderFillEvidence.create(
            provider_id="KRAKEN",
            account_id=account_id,
            environment=environment,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=instrument,
            quantity=row.get("executionSize"),
            price=row.get("executionPrice"),
            fee_amount=row.get("fee", "0"),
            fee_currency=_text(row.get("feeCurrency"), name="feeCurrency"),
            trade_time=_millis_to_utc(fill_time, name="fillTime"),
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
