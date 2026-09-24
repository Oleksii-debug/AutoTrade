"""Pure Kraken Spot REST contract adapter foundation.

This module performs no networking, stores no credentials and grants no trading
authority. It translates already-authorized canonical values and recorded
Kraken Spot REST responses into AutoTrade provider/reconciliation contracts.

Kraken Spot and Kraken Derivatives are deliberately treated as distinct API
families. The documented derivatives demo environment is not used as evidence
that an equivalent Spot sandbox exists.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping
from uuid import UUID

from .provider_core import ProviderCoreError
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


KRAKEN_SPOT_LIVE_BASE_URL = "https://api.kraken.com"
KRAKEN_SPOT_ENDPOINTS: Mapping[str, str] = {
    "SERVER_TIME": "/0/public/Time",
    "ASSET_PAIRS": "/0/public/AssetPairs",
    "ADD_ORDER": "/0/private/AddOrder",
    "OPEN_ORDERS": "/0/private/OpenOrders",
    "CLOSED_ORDERS": "/0/private/ClosedOrders",
    "QUERY_ORDERS": "/0/private/QueryOrders",
    "TRADES_HISTORY": "/0/private/TradesHistory",
    "LEDGERS": "/0/private/Ledgers",
    "BALANCE": "/0/private/Balance",
    "TRADE_BALANCE": "/0/private/TradeBalance",
}

_TIME_IN_FORCE = {
    "GTC": "GTC",
    "IOC": "IOC",
    "GTD": "GTD",
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


def _seconds_to_utc(value: object, *, name: str) -> str:
    seconds = _decimal(value, name=name)
    if seconds < 0:
        raise ProviderCoreError(f"{name} cannot be negative")
    micros = seconds * Decimal("1000000")
    if micros != micros.to_integral_value():
        raise ProviderCoreError(f"{name} has precision finer than one microsecond")
    total_micros = int(micros)
    whole_seconds, remainder = divmod(total_micros, 1_000_000)
    instant = datetime.fromtimestamp(whole_seconds, tz=timezone.utc) + timedelta(
        microseconds=remainder
    )
    return instant.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _response_evidence(
    endpoint: str,
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
        "artifact_id": f"kraken-spot-sha256-{digest}",
        "sha256": f"sha256:{digest}",
        "source_uri": f"{KRAKEN_SPOT_LIVE_BASE_URL}{endpoint}",
        "observed_at": observed_at,
        "rights_id": "provider-observation-kraken",
    }


def kraken_client_order_id(client_order_id: object) -> str:
    """Return a deterministic 18-character provider identity for a UUID intent.

    The mapping is deterministic and must be retained with the canonical UUID in
    the durable journal. It is not a substitute for collision detection when an
    adapter publishes an order.
    """

    canonical = _uuid_text(client_order_id, name="client_order_id").lower()
    digest = sha256(canonical.encode("ascii")).digest()[:12]
    token = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"AT{token}"


def next_nonce(*, previous_nonce: object, observed_time_ms: object) -> int:
    """Compute the next strictly increasing Spot REST nonce.

    This function intentionally has no shared mutable state. The caller must
    atomically persist the returned value under the credential identity before
    a signed request is sent. Separate workers may not allocate nonces from
    independent in-memory counters for the same credential.
    """

    previous = _integer(previous_nonce, name="previous_nonce", minimum=0)
    clock = _integer(observed_time_ms, name="observed_time_ms", minimum=0)
    return max(previous + 1, clock)


def spot_rest_base_url(environment: object) -> str:
    normalized = _text(environment, name="environment").upper()
    if normalized != "LIVE":
        raise ProviderCoreError(
            "Kraken Spot foundation has no qualified demo/test base URL; "
            "Kraken Derivatives demo is a separate API family"
        )
    return KRAKEN_SPOT_LIVE_BASE_URL


def build_spot_order_payload(
    *,
    pair: str,
    side: str,
    order_type: str,
    volume: object,
    client_order_id: object,
    time_in_force: str = "GTC",
    price: object | None = None,
    leverage: str | None = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """Translate a bounded canonical Spot/Margin order to AddOrder fields.

    Only market and limit orders are included in this foundation. Advanced
    stop/take-profit/trailing semantics stay unavailable until separately
    qualified instead of being guessed from provider marketing.
    """

    provider_pair = _text(pair, name="pair")
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")
    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in {"MARKET", "LIMIT"}:
        raise ProviderCoreError("Kraken Spot foundation supports MARKET or LIMIT only")
    tif = _text(time_in_force, name="time_in_force").upper()
    if tif not in _TIME_IN_FORCE:
        raise ProviderCoreError("unsupported time_in_force")
    if normalized_type == "MARKET" and tif == "GTD":
        raise ProviderCoreError("GTD is not admitted for market orders")
    if type(validate_only) is not bool:
        raise ProviderCoreError("validate_only must be boolean")

    payload: dict[str, Any] = {
        "pair": provider_pair,
        "type": normalized_side.lower(),
        "ordertype": normalized_type.lower(),
        "volume": _decimal_text(volume, name="volume", positive=True),
        "timeinforce": _TIME_IN_FORCE[tif],
        "cl_ord_id": kraken_client_order_id(client_order_id),
        "validate": validate_only,
    }

    if normalized_type == "LIMIT":
        if price is None:
            raise ProviderCoreError("limit price is required")
        payload["price"] = _decimal_text(price, name="price", positive=True)
    elif price is not None:
        raise ProviderCoreError("market order must not carry an ignored limit price")

    if leverage is not None:
        normalized_leverage = _text(leverage, name="leverage")
        if normalized_leverage in {"none", "0", "1"}:
            raise ProviderCoreError(
                "explicit leverage must represent a qualified margin setting"
            )
        payload["leverage"] = normalized_leverage

    return payload


def _errors(envelope: Mapping[str, Any]) -> tuple[str, ...]:
    raw = envelope.get("error")
    if not isinstance(raw, list):
        raise ProviderCoreError("Kraken response error must be an array")
    result: list[str] = []
    for index, value in enumerate(raw):
        result.append(_text(value, name=f"error[{index}]"))
    return tuple(result)


def parse_submission_response(
    *,
    attempt_id: object,
    client_order_id: object,
    response: Mapping[str, Any],
    observed_at: str,
) -> dict[str, Any]:
    """Map a recorded AddOrder response without inventing a fill.

    A provider response containing a transaction id is ACKNOWLEDGED only.
    Transport failure after the send boundary is represented separately by
    classify_transport_failure() and is UNKNOWN until reconciliation.
    """

    aid = _uuid_text(attempt_id, name="attempt_id")
    canonical_client_id = _uuid_text(client_order_id, name="client_order_id")
    when = _text(observed_at, name="observed_at")
    envelope = _mapping(response, name="response")
    errors = _errors(envelope)
    evidence = [
        _response_evidence(
            KRAKEN_SPOT_ENDPOINTS["ADD_ORDER"],
            envelope,
            observed_at=when,
        )
    ]

    if errors:
        return {
            "attempt_id": aid,
            "outcome": "REJECTED",
            "client_order_id": canonical_client_id,
            "provider_client_order_id": kraken_client_order_id(canonical_client_id),
            "provider_received_at": when,
            "reason_code": "KRAKEN:" + "|".join(errors),
            "evidence": evidence,
            "retry_disposition": "NEVER",
        }

    result = _mapping(envelope.get("result"), name="result")
    txids = result.get("txid")
    if not isinstance(txids, list) or len(txids) != 1:
        raise ProviderCoreError(
            "Kraken foundation requires exactly one transaction id per AddOrder"
        )
    provider_order_id = _text(txids[0], name="result.txid[0]")
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": provider_order_id,
        "client_order_id": canonical_client_id,
        "provider_client_order_id": kraken_client_order_id(canonical_client_id),
        "provider_received_at": when,
        "evidence": evidence,
        "retry_disposition": "NEVER",
    }


def classify_transport_failure(
    *,
    attempt_id: object,
    client_order_id: object,
    send_started: bool,
    observed_at: str,
    reason_code: str,
) -> dict[str, Any]:
    """Classify a local/transport failure relative to the irreversible send cut."""

    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _uuid_text(client_order_id, name="client_order_id")
    if type(send_started) is not bool:
        raise ProviderCoreError("send_started must be boolean")
    when = _text(observed_at, name="observed_at")
    reason = _text(reason_code, name="reason_code")
    return {
        "attempt_id": aid,
        "outcome": "UNKNOWN" if send_started else "NOT_SENT",
        "client_order_id": cid,
        "provider_client_order_id": kraken_client_order_id(cid),
        "provider_received_at": None,
        "observed_at": when,
        "reason_code": reason,
        "evidence": [],
        "retry_disposition": "RECONCILE_FIRST" if send_started else "NEVER",
    }


def parse_trade_history(
    response: Mapping[str, Any],
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_provider_order: Mapping[str, str],
    fee_currency_by_pair: Mapping[str, str],
) -> tuple[ProviderFillEvidence, ...]:
    """Map recorded TradesHistory rows to provider fill evidence.

    Trade-history rows do not carry enough canonical context to infer instrument
    versions, client identities or fee currencies safely. Those mappings must be
    supplied by previously evidenced provider metadata/order state.
    """

    envelope = _mapping(response, name="response")
    errors = _errors(envelope)
    if errors:
        raise ProviderCoreError("Kraken TradesHistory response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    trades = _mapping(result.get("trades"), name="result.trades")
    if not isinstance(instrument_versions, Mapping):
        raise ProviderCoreError("instrument_versions must be a mapping")
    if not isinstance(client_ids_by_provider_order, Mapping):
        raise ProviderCoreError("client_ids_by_provider_order must be a mapping")
    if not isinstance(fee_currency_by_pair, Mapping):
        raise ProviderCoreError("fee_currency_by_pair must be a mapping")

    fills: list[ProviderFillEvidence] = []
    for trade_id, raw in trades.items():
        execution_id = _text(str(trade_id), name="trade id")
        row = _mapping(raw, name=f"result.trades[{execution_id}]")
        pair = _text(row.get("pair"), name="pair")
        provider_order_id = _text(row.get("ordertxid"), name="ordertxid")
        try:
            instrument = _text(
                instrument_versions[pair], name="instrument_version"
            )
        except KeyError as error:
            raise ProviderCoreError(f"unmapped Kraken pair: {pair}") from error
        try:
            fee_currency = _text(
                fee_currency_by_pair[pair], name="fee_currency"
            )
        except KeyError as error:
            raise ProviderCoreError(
                f"missing evidenced fee currency for Kraken pair: {pair}"
            ) from error
        client_id = client_ids_by_provider_order.get(provider_order_id)
        if client_id is not None:
            client_id = _uuid_text(client_id, name="client_order_id")

        fills.append(
            ProviderFillEvidence.create(
                provider_execution_id=execution_id,
                client_order_id=client_id,
                instrument=instrument,
                quantity=row.get("vol"),
                price=row.get("price"),
                fee_amount=row.get("fee", "0"),
                fee_currency=fee_currency,
                trade_time=_seconds_to_utc(row.get("time"), name="time"),
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
    """Create one Kraken Spot reconciliation coverage fact.

    The default is deliberately non-authoritative for absence. A separately
    recorded qualification for the exact product/environment must establish
    whether the queried surface can exclude execution.
    """

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise ProviderCoreError("unsupported Kraken reconciliation surface")
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
