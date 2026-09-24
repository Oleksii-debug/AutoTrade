"""Pure Kraken Spot/Futures adapter foundation.

No networking or credential storage lives here.  Spot and Futures remain
separate provider surfaces because their endpoints, identity, authentication,
history and demo semantics differ.  A successful order response is an
acknowledgement only; it is never treated as a fill.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .provider_core import ProviderCoreError
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


KRAKEN_SPOT_BASE_URL = "https://api.kraken.com"
KRAKEN_FUTURES_BASE_URLS: Mapping[str, str] = {
    "LIVE": "https://futures.kraken.com",
    "DEMO": "https://demo-futures.kraken.com",
}
KRAKEN_ENDPOINTS: Mapping[str, str] = {
    "SPOT_PLACE_ORDER": "/0/private/AddOrder",
    "SPOT_OPEN_ORDERS": "/0/private/OpenOrders",
    "SPOT_CLOSED_ORDERS": "/0/private/ClosedOrders",
    "SPOT_TRADES": "/0/private/TradesHistory",
    "FUTURES_PLACE_ORDER": "/derivatives/api/v3/sendorder",
    "FUTURES_OPEN_ORDERS": "/derivatives/api/v3/openorders",
    "FUTURES_FILLS": "/derivatives/api/v3/fills",
    "FUTURES_ORDER_HISTORY": "/api/history/v3/orders",
    "FUTURES_POSITION_HISTORY": "/api/history/v3/positions",
}

# Kraken documents free-text cl_ord_id up to 18 ASCII chars as well as
# 32-character short UUID and canonical hyphenated UUID.  We accept only those
# documented forms so a client identity can be used consistently.
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


def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be a UUID") from error
    return text


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
        "client_order_id must be a Kraken UUID/short UUID or 1-18 printable ASCII characters"
    )


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


def _seconds_decimal_to_utc(value: object, *, name: str) -> str:
    seconds = _decimal(value, name=name)
    if seconds < 0:
        raise ProviderCoreError(f"{name} must be non-negative")
    whole = int(seconds)
    micros = (seconds - Decimal(whole)) * Decimal("1000000")
    if micros != micros.to_integral_value():
        raise ProviderCoreError(f"{name} has precision finer than one microsecond")
    instant = datetime.fromtimestamp(whole, tz=timezone.utc) + timedelta(
        microseconds=int(micros)
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _response_evidence(
    *,
    family: str,
    endpoint: str,
    response: Mapping[str, Any],
    observed_at: str,
    environment: str = "LIVE",
) -> dict[str, str]:
    encoded = json.dumps(
        response,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = sha256(encoded).hexdigest()
    if family == "SPOT":
        base = KRAKEN_SPOT_BASE_URL
    else:
        env = _text(environment, name="environment").upper()
        try:
            base = KRAKEN_FUTURES_BASE_URLS[env]
        except KeyError as error:
            raise ProviderCoreError("unsupported Kraken Futures environment") from error
    source = f"{base}{endpoint}"
    return {
        "artifact_id": str(uuid5(NAMESPACE_URL, f"{source}#sha256:{digest}")),
        "sha256": f"sha256:{digest}",
        "source_uri": source,
        "observed_at": _iso_utc(observed_at, name="observed_at"),
        "rights_id": "provider-observation-kraken",
    }


def validate_spot_nonce(*, candidate: object, last_accepted: object | None = None) -> int:
    """Validate a caller-owned Spot nonce without inventing retry authority."""

    nonce = _integer(candidate, name="candidate nonce", minimum=1)
    if last_accepted is not None:
        previous = _integer(last_accepted, name="last accepted nonce", minimum=1)
        if nonce <= previous:
            raise ProviderCoreError("Kraken Spot nonce must strictly increase for an API key")
    return nonce


def futures_base_url(environment: str) -> str:
    env = _text(environment, name="environment").upper()
    try:
        return KRAKEN_FUTURES_BASE_URLS[env]
    except KeyError as error:
        raise ProviderCoreError("Kraken Futures environment must be LIVE or DEMO") from error


def build_spot_order_payload(
    *,
    pair: str,
    side: str,
    order_type: str,
    volume: object,
    client_order_id: str,
    nonce: object,
    price: object | None = None,
    time_in_force: str = "GTC",
) -> dict[str, str]:
    """Build a bounded Kraken Spot REST AddOrder payload.

    Spot demo/testnet is intentionally not modeled because Kraken's public Spot
    API documentation does not establish a general public Spot sandbox.
    """

    provider_pair = _text(pair, name="pair")
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")
    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in {"MARKET", "LIMIT"}:
        raise ProviderCoreError("Kraken Spot foundation supports MARKET or LIMIT only")
    tif = _text(time_in_force, name="time_in_force").upper()
    if tif not in {"GTC", "IOC"}:
        raise ProviderCoreError("time_in_force must be GTC or IOC")
    if normalized_type == "MARKET" and price is not None:
        raise ProviderCoreError("market order must not carry an ignored limit price")
    if normalized_type == "LIMIT" and price is None:
        raise ProviderCoreError("limit price is required")

    payload = {
        "nonce": str(validate_spot_nonce(candidate=nonce)),
        "pair": provider_pair,
        "type": normalized_side.lower(),
        "ordertype": normalized_type.lower(),
        "volume": _decimal_text(volume, name="volume", positive=True),
        "cl_ord_id": _client_order_id(client_order_id),
        "timeinforce": tif,
    }
    if price is not None:
        payload["price"] = _decimal_text(price, name="price", positive=True)
    return payload


def build_futures_order_payload(
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
    """Build a bounded Kraken Futures sendorder payload."""

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


def _unknown_submission(
    *,
    attempt_id: str,
    client_order_id: str,
    family: str,
    observed_at: str,
    reason_code: str,
) -> dict[str, Any]:
    return {
        "attempt_id": _uuid_text(attempt_id, name="attempt_id"),
        "outcome": "UNKNOWN",
        "client_order_id": _client_order_id(client_order_id),
        "provider_received_at": _iso_utc(observed_at, name="observed_at"),
        "reason_code": reason_code,
        "evidence": [],
        "retry_disposition": "RECONCILE_FIRST",
    }


def parse_spot_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    observed_at: str,
    response: Mapping[str, Any] | None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map a recorded Spot AddOrder response to SubmissionResult."""

    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if response is not None:
            raise ProviderCoreError("ambiguous transport must not fabricate a provider response")
        return _unknown_submission(
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            family="SPOT",
            observed_at=observed_at,
            reason_code="KRAKEN_SPOT_TRANSPORT_AMBIGUOUS",
        )
    envelope = _mapping(response, name="response")
    errors = envelope.get("error")
    if not isinstance(errors, list) or any(not isinstance(item, str) for item in errors):
        raise ProviderCoreError("Kraken Spot response.error must be a string array")
    evidence = [
        _response_evidence(
            family="SPOT",
            endpoint=KRAKEN_ENDPOINTS["SPOT_PLACE_ORDER"],
            response=envelope,
            observed_at=observed_at,
        )
    ]
    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _client_order_id(client_order_id)
    when = _iso_utc(observed_at, name="observed_at")
    if errors:
        return {
            "attempt_id": aid,
            "outcome": "REJECTED",
            "client_order_id": cid,
            "provider_received_at": when,
            "reason_code": "KRAKEN_SPOT_" + "|".join(errors),
            "evidence": evidence,
            "retry_disposition": "NEVER",
        }
    result = _mapping(envelope.get("result"), name="result")
    txids = result.get("txid")
    if not isinstance(txids, list) or len(txids) != 1:
        raise ProviderCoreError("Kraken Spot successful AddOrder must return exactly one txid")
    provider_order_id = _text(txids[0], name="result.txid[0]")
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": provider_order_id,
        "client_order_id": cid,
        "provider_received_at": when,
        "evidence": evidence,
        "retry_disposition": "NEVER",
    }


def parse_futures_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    environment: str,
    observed_at: str,
    response: Mapping[str, Any] | None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map a recorded Futures sendorder response without inferring a fill."""

    futures_base_url(environment)
    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if response is not None:
            raise ProviderCoreError("ambiguous transport must not fabricate a provider response")
        return _unknown_submission(
            attempt_id=attempt_id,
            client_order_id=client_order_id,
            family="FUTURES",
            observed_at=observed_at,
            reason_code="KRAKEN_FUTURES_TRANSPORT_AMBIGUOUS",
        )
    envelope = _mapping(response, name="response")
    evidence = [
        _response_evidence(
            family="FUTURES",
            endpoint=KRAKEN_ENDPOINTS["FUTURES_PLACE_ORDER"],
            response=envelope,
            observed_at=observed_at,
            environment=environment,
        )
    ]
    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _client_order_id(client_order_id)
    when = _iso_utc(observed_at, name="observed_at")
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
            "provider_received_at": when,
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
        "provider_received_at": when,
        "evidence": evidence,
        "retry_disposition": "NEVER",
    }


def parse_futures_position_executions(
    response: Mapping[str, Any],
    *,
    instrument_versions: Mapping[str, str],
    execution_client_ids: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Extract trade-caused executions from authenticated Futures position history."""

    envelope = _mapping(response, name="response")
    elements = envelope.get("elements")
    if not isinstance(elements, list):
        raise ProviderCoreError("Futures history elements must be an array")
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


def parse_spot_trades(
    response: Mapping[str, Any],
    *,
    instrument_versions: Mapping[str, tuple[str, str]],
    order_client_ids: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map authenticated Spot trade-history rows to exact reconciliation fills."""

    envelope = _mapping(response, name="response")
    errors = envelope.get("error")
    if errors not in (None, []):
        raise ProviderCoreError("Kraken Spot trade-history response contains errors")
    result = _mapping(envelope.get("result"), name="result")
    trades = _mapping(result.get("trades"), name="result.trades")
    clients = order_client_ids or {}
    fills: list[ProviderFillEvidence] = []
    for trade_id, value in trades.items():
        row = _mapping(value, name=f"trade {trade_id}")
        pair = _text(row.get("pair"), name="pair")
        try:
            instrument, fee_currency = instrument_versions[pair]
        except KeyError as error:
            raise ProviderCoreError(f"unmapped Kraken Spot pair: {pair}") from error
        order_id = _text(row.get("ordertxid"), name="ordertxid")
        client_id = clients.get(order_id)
        if client_id is not None:
            client_id = _client_order_id(client_id)
        fills.append(
            ProviderFillEvidence.create(
                provider_execution_id=_text(trade_id, name="trade id"),
                client_order_id=client_id,
                instrument=_text(instrument, name="instrument_version"),
                quantity=row.get("vol"),
                price=row.get("price"),
                fee_amount=row.get("fee", "0"),
                fee_currency=_text(fee_currency, name="fee_currency"),
                trade_time=_seconds_decimal_to_utc(row.get("time"), name="time"),
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
    return CoverageSurfaceEvidence(
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=qualified_exclusion_semantics,
    )
