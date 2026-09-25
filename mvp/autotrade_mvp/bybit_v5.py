"""Pure Bybit V5 contract adapter foundation.

This module deliberately performs no networking, stores no credentials and
cannot grant trading authority. It translates already-authorized canonical
values and recorded Bybit responses into AutoTrade provider/reconciliation
contracts. Live, demo and test environments remain unqualified until exact
adapter evidence satisfies provider_core.QualificationEvidence.
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
    position_idx: int | None = None,
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

    if position_idx is not None:
        if family not in {"LINEAR_DERIVATIVES", "INVERSE_DERIVATIVES"}:
            raise ProviderCoreError("position_idx is only supported for derivative orders")
        if type(position_idx) is not int or position_idx not in {0, 1, 2}:
            raise ProviderCoreError("position_idx must be 0, 1 or 2")

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
        if position_idx is not None:
            payload["positionIdx"] = position_idx

    return payload


def parse_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    response: Mapping[str, Any] | None,
    environment: str,
    observed_at: str | None = None,
    transport_ambiguous: bool = False,
) -> dict[str, Any]:
    """Map a recorded Bybit create-order response to SubmissionResult.

    A successful HTTP/API acknowledgement is intentionally only ACKNOWLEDGED.
    It is never converted into a fill. Timeout/server/duplicate ambiguity is
    UNKNOWN and requires reconciliation before any economic retry.
    """

    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _client_order_id(client_order_id)
    normalized_environment = _text(environment, name="environment").upper()
    if normalized_environment not in _REST_BASE_BY_ENVIRONMENT:
        raise ProviderCoreError(
            "Bybit evidence environment must be MAINNET, TESTNET or DEMO"
        )
    if type(transport_ambiguous) is not bool:
        raise ProviderCoreError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if response is not None:
            raise ProviderCoreError(
                "ambiguous transport cannot also claim an authoritative response"
            )
        if observed_at is None:
            raise ProviderCoreError(
                "ambiguous transport requires explicit local observed_at"
            )
        _utc_text(observed_at, name="observed_at")
        # Local observed_at and provider environment belong to the durable
        # SubmissionAttempt. With no authoritative provider response there is
        # deliberately no provider_received_at and no response EvidenceRef.
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "BYBIT_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }
    if response is None:
        raise ProviderCoreError(
            "submission response is required unless transport is explicitly ambiguous"
        )
    envelope = _mapping(response, name="response")
    code = _integer(envelope.get("retCode"), name="retCode")
    local_observed_at = (
        _utc_text(observed_at, name="observed_at")
        if observed_at is not None
        else None
    )
    provider_received_at = (
        _millis_to_utc(envelope.get("time"), name="response.time")
        if envelope.get("time") is not None
        else None
    )
    evidence_observed_at = local_observed_at or provider_received_at
    if evidence_observed_at is None:
        raise ProviderCoreError(
            "provider response requires observed_at when response.time is absent"
        )
    evidence = [
        _response_evidence(
            BYBIT_DOCUMENTED_ENDPOINTS["PLACE_ORDER"],
            envelope,
            observed_at=evidence_observed_at,
            environment=normalized_environment,
        )
    ]

    if code == 0:
        result = _mapping(envelope.get("result"), name="result")
        provider_order_id = _text(result.get("orderId"), name="result.orderId")
        echoed_client_id = _text(
            result.get("orderLinkId"), name="result.orderLinkId"
        )
        if echoed_client_id != cid:
            raise ProviderCoreError("Bybit orderLinkId response does not match request")
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
    response: Mapping[str, Any],
    *,
    account_id: str,
    environment: str,
    instrument_versions: Mapping[str, str],
    qualified_fee_currencies: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map recorded /v5/execution/list rows to reconciliation fill evidence."""

    envelope = _mapping(response, name="response")
    if _integer(envelope.get("retCode"), name="retCode") != 0:
        raise ProviderCoreError("Bybit execution response was not successful")
    result = _mapping(envelope.get("result"), name="result")
    rows = result.get("list")
    if not isinstance(rows, list):
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
        if extra_fees not in (None, "", [], {}):
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

        fill = ProviderFillEvidence.create(
        fill =     provider_id="BYBIT",
        fill =     account_id=account_id,
        fill =     environment=environment,
            provider_execution_id=execution_id,
            client_order_id=client_id,
            instrument=instrument,
            quantity=row.get("execQty"),
            price=row.get("execPrice"),
            fee_amount=row.get("execFee"),
            fee_currency=fee_currency,
            trade_time=_millis_to_utc(row.get("execTime"), name="execTime"),
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
