"""Network-free Alpaca Trading API contract adapter foundation.

The adapter has no HTTP client, credentials, retry authority, journal or live
trading authority. It translates already-admitted canonical order intent into
a bounded Alpaca v2 payload and maps recorded provider evidence into existing
AutoTrade provider/reconciliation contracts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .capabilities import CapabilitySnapshot
from .provider_core import ProviderCoreError
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


_SUPPORTED_ORDER_TYPES = {
    "EQUITIES": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
    "CRYPTO": frozenset({"MARKET", "LIMIT", "STOP_LIMIT"}),
    "OPTIONS": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
}
_SUPPORTED_TIF = {
    "EQUITIES": frozenset({"DAY", "GTC"}),
    "CRYPTO": frozenset({"GTC", "IOC"}),
    "OPTIONS": frozenset({"DAY", "GTC"}),
}
_CLIENT_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


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


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _instant(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ProviderCoreError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise ProviderCoreError(f"{name} must be a UUID") from error
    return text


def _client_order_id(value: object) -> str:
    text = _text(value, name="client_order_id")
    if not _CLIENT_ID.fullmatch(text):
        raise ProviderCoreError(
            "client_order_id must be 1-128 safe ASCII identifier characters"
        )
    return text


def _evidence(
    *,
    endpoint: str,
    environment: str,
    observed_at: str,
    payload: object,
) -> dict[str, str]:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    digest = sha256(encoded).hexdigest()
    env = _text(environment, name="environment").upper()
    if env == "PAPER":
        host = "paper-api.alpaca.markets"
    elif env == "LIVE":
        host = "api.alpaca.markets"
    else:
        raise ProviderCoreError("Alpaca response evidence environment must be PAPER or LIVE")
    return {
        "artifact_id": str(
            uuid5(NAMESPACE_URL, f"https://{host}{endpoint}#sha256:{digest}")
        ),
        "sha256": "sha256:" + digest,
        "source_uri": f"https://{host}{endpoint}",
        "observed_at": _instant(observed_at, name="observed_at"),
        "rights_id": "provider-observation-alpaca",
    }


def build_order_payload(
    *,
    capability: CapabilitySnapshot,
    at: datetime,
    instrument_version: str,
    asset_class: str,
    symbol: str,
    side: str,
    order_type: str,
    quantity: object,
    time_in_force: str,
    client_order_id: str,
    limit_price: object | None = None,
    stop_price: object | None = None,
    extended_hours: bool = False,
) -> dict[str, object]:
    """Translate one already-admitted single-leg order without sending it."""

    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if capability.provider_id.upper() != "ALPACA":
        raise ProviderCoreError("capability belongs to another provider")
    instrument = _text(instrument_version, name="instrument_version")
    if capability.instrument_version != instrument:
        raise ProviderCoreError("capability instrument version does not match order")

    family = _text(asset_class, name="asset_class").upper()
    if family not in _SUPPORTED_ORDER_TYPES:
        raise ProviderCoreError("unsupported Alpaca asset class")

    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in _SUPPORTED_ORDER_TYPES[family]:
        raise ProviderCoreError("order type is not supported for Alpaca asset class")

    tif = _text(time_in_force, name="time_in_force").upper()
    if tif not in _SUPPORTED_TIF[family]:
        raise ProviderCoreError("time_in_force is not supported for Alpaca asset class")

    point = at
    if not capability.admits(
        at=point,
        order_type=normalized_type,
        time_in_force=tif,
        permission_scope="ORDER_WRITE",
    ):
        raise ProviderCoreError("exact capability evidence does not admit this order")

    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")
    if type(extended_hours) is not bool:
        raise ProviderCoreError("extended_hours must be boolean")
    if extended_hours and not (
        family == "EQUITIES"
        and normalized_type == "LIMIT"
        and tif in {"DAY", "GTC"}
    ):
        raise ProviderCoreError(
            "extended hours requires an equity limit order with DAY or GTC"
        )

    qty = _decimal(quantity, name="quantity", positive=True)
    if family == "OPTIONS" and qty != qty.to_integral_value():
        raise ProviderCoreError("Alpaca option quantity must be whole contracts")

    limit = (
        None
        if limit_price is None
        else _decimal(limit_price, name="limit_price", positive=True)
    )
    stop = (
        None
        if stop_price is None
        else _decimal(stop_price, name="stop_price", positive=True)
    )
    if normalized_type in {"LIMIT", "STOP_LIMIT"} and limit is None:
        raise ProviderCoreError("limit_price is required")
    if normalized_type not in {"LIMIT", "STOP_LIMIT"} and limit is not None:
        raise ProviderCoreError("limit_price is not valid for this order type")
    if normalized_type in {"STOP", "STOP_LIMIT"} and stop is None:
        raise ProviderCoreError("stop_price is required")
    if normalized_type not in {"STOP", "STOP_LIMIT"} and stop is not None:
        raise ProviderCoreError("stop_price is not valid for this order type")

    payload: dict[str, object] = {
        "symbol": _text(symbol, name="symbol"),
        "qty": _decimal_text(qty),
        "side": normalized_side.lower(),
        "type": normalized_type.lower(),
        "time_in_force": tif.lower(),
        "client_order_id": _client_order_id(client_order_id),
    }
    if limit is not None:
        payload["limit_price"] = _decimal_text(limit)
    if stop is not None:
        payload["stop_price"] = _decimal_text(stop)
    if extended_hours:
        payload["extended_hours"] = True
    return payload


def parse_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    response: Mapping[str, Any],
    observed_at: str,
    environment: str,
) -> dict[str, object]:
    """Map a recorded successful POST /v2/orders response to SubmissionResult.

    An Order object is only acknowledgement of the write. Even if its status
    happens to be filled, this function never manufactures an execution fill;
    financial execution comes from separately identified activity evidence.
    """

    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = _client_order_id(client_order_id)
    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    provider_order_id = _uuid_text(response.get("id"), name="response.id")
    echoed = _client_order_id(response.get("client_order_id"))
    if echoed != cid:
        raise ProviderCoreError("Alpaca client_order_id response does not match request")
    when = _instant(observed_at, name="observed_at")
    endpoint = "/v2/orders"
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": provider_order_id,
        "client_order_id": cid,
        "provider_received_at": when,
        "evidence": [
            _evidence(
                endpoint=endpoint,
                environment=environment,
                observed_at=when,
                payload=response,
            )
        ],
        "retry_disposition": "NEVER",
    }


def parse_trade_activities(
    activities: object,
    *,
    instrument_versions: Mapping[str, str],
    client_ids_by_order_id: Mapping[str, str | None],
    fees_by_activity_id: Mapping[str, tuple[object, str]],
) -> tuple[ProviderFillEvidence, ...]:
    """Map recorded Alpaca FILL activities only when fee evidence is supplied.

    Alpaca trade-activity rows identify fills and order IDs but do not carry a
    canonical per-fill fee currency/amount in the documented TradeActivity
    shape. AutoTrade therefore refuses to invent zero fees: callers must bind
    each activity ID to independently reconciled fee evidence before producing
    ProviderFillEvidence.
    """

    if not isinstance(activities, list):
        raise ProviderCoreError("activities must be an array")
    for name, mapping in (
        ("instrument_versions", instrument_versions),
        ("client_ids_by_order_id", client_ids_by_order_id),
        ("fees_by_activity_id", fees_by_activity_id),
    ):
        if not isinstance(mapping, Mapping):
            raise ProviderCoreError(f"{name} must be a mapping")

    result: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(activities):
        if not isinstance(raw, Mapping):
            raise ProviderCoreError(f"activities[{index}] must be an object")
        if _text(raw.get("activity_type"), name="activity_type").upper() != "FILL":
            continue

        activity_id = _text(raw.get("id"), name="activity.id")
        order_id = _uuid_text(raw.get("order_id"), name="activity.order_id")
        symbol = _text(raw.get("symbol"), name="activity.symbol")
        try:
            instrument = _text(
                instrument_versions[symbol], name="instrument_version"
            )
        except KeyError as error:
            raise ProviderCoreError(
                f"unmapped Alpaca instrument symbol: {symbol}"
            ) from error
        if order_id not in client_ids_by_order_id:
            raise ProviderCoreError(
                f"missing Alpaca order-to-client identity mapping: {order_id}"
            )
        client_id = client_ids_by_order_id[order_id]
        if client_id is not None:
            client_id = _client_order_id(client_id)
        try:
            fee_amount, fee_currency = fees_by_activity_id[activity_id]
        except KeyError as error:
            raise ProviderCoreError(
                f"missing fee evidence for Alpaca activity: {activity_id}"
            ) from error

        fill = ProviderFillEvidence.create(
            provider_execution_id=activity_id,
            client_order_id=client_id,
            instrument=instrument,
            quantity=raw.get("qty"),
            price=raw.get("price"),
            fee_amount=fee_amount,
            fee_currency=_text(fee_currency, name="fee_currency"),
            trade_time=_instant(
                raw.get("transaction_time"), name="transaction_time"
            ),
        )
        previous = result.get(activity_id)
        if previous is not None and previous != fill:
            raise ProviderCoreError(
                "Alpaca activity id appears with conflicting economic content"
            )
        result[activity_id] = fill
    return tuple(result.values())


def coverage_evidence(
    *,
    surface: str,
    coverage_start: str,
    coverage_end: str,
    pagination_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> CoverageSurfaceEvidence:
    """Create one reconciliation coverage fact; absence is fail-closed by default."""

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise ProviderCoreError("unsupported Alpaca reconciliation surface")
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
