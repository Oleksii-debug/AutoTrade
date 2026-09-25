"""Alpaca non-live order adapter foundation.

The Trading API shares an order endpoint across several security types, but
their order constraints are not interchangeable. This module encodes a narrow,
fail-closed subset for equities, crypto and single-leg options without making
network requests or granting financial authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from .capabilities import CapabilitySnapshot
from .reconciliation import CoverageSurfaceEvidence, ProviderFillEvidence


class AlpacaAdapterError(ValueError):
    """Raised when an Alpaca order cannot be represented safely."""


ALPACA_DOCS = MappingProxyType(
    {
        "orders": "https://docs.alpaca.markets/us/reference/postorder",
        "options": "https://docs.alpaca.markets/us/docs/options-trading",
        "paper": "https://docs.alpaca.markets/us/docs/paper-trading",
        "activities": "https://docs.alpaca.markets/eu/docs/activities",
    }
)

_ASSET_CLASSES = frozenset({"EQUITY", "CRYPTO", "OPTION"})
_SIDES = frozenset({"BUY", "SELL"})
_CLIENT_ID = re.compile(r"^[\x21-\x7e]{1,128}$")
_ORDER_TYPES = {
    "EQUITY": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
    "CRYPTO": frozenset({"MARKET", "LIMIT", "STOP_LIMIT"}),
    "OPTION": frozenset({"MARKET", "LIMIT", "STOP", "STOP_LIMIT"}),
}
_TIFS = {
    "EQUITY": frozenset({"DAY", "GTC"}),
    "CRYPTO": frozenset({"GTC", "IOC"}),
    "OPTION": frozenset({"DAY", "GTC"}),
}


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlpacaAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise AlpacaAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AlpacaAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise AlpacaAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise AlpacaAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AlpacaAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_client_order_id(value: str) -> str:
    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise AlpacaAdapterError("client_order_id must be printable ASCII of at most 128 characters")
    return client_id


@dataclass(frozen=True)
class AlpacaOrderIntent:
    instrument_version: str
    asset_class: str
    symbol: str
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    extended_hours: bool = False
    position_intent: str | None = None

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        asset_class: str,
        symbol: str,
        side: str,
        order_type: str,
        time_in_force: str,
        quantity=None,
        notional=None,
        limit_price=None,
        stop_price=None,
        extended_hours: bool = False,
        position_intent: str | None = None,
    ) -> "AlpacaOrderIntent":
        asset = _text(asset_class, name="asset_class").upper()
        side_value = _text(side, name="side").upper()
        order = _text(order_type, name="order_type").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if asset not in _ASSET_CLASSES:
            raise AlpacaAdapterError("unsupported asset_class")
        if side_value not in _SIDES:
            raise AlpacaAdapterError("side must be BUY or SELL")
        if order not in _ORDER_TYPES[asset]:
            raise AlpacaAdapterError(f"{order} is not admitted for {asset}")
        if tif not in _TIFS[asset]:
            raise AlpacaAdapterError(f"{tif} is not admitted for {asset}")
        if type(extended_hours) is not bool:
            raise AlpacaAdapterError("extended_hours must be boolean")

        qty = None if quantity is None else _decimal(quantity, name="quantity", positive=True)
        notion = None if notional is None else _decimal(notional, name="notional", positive=True)
        if (qty is None) == (notion is None):
            raise AlpacaAdapterError("exactly one of quantity or notional is required")
        if notion is not None:
            if asset == "OPTION":
                raise AlpacaAdapterError("options cannot use notional sizing")
            if order not in {"MARKET", "LIMIT"}:
                raise AlpacaAdapterError("notional sizing is admitted only for market/limit orders")
            if asset == "EQUITY" and tif != "DAY":
                raise AlpacaAdapterError("equity notional sizing is admitted only for DAY orders")
            # Alpaca documents crypto notional or qty with its native GTC/IOC TIFs.
        if asset == "OPTION" and qty != qty.to_integral_value():
            raise AlpacaAdapterError("option quantity must be a whole number of contracts")

        limit = None if limit_price is None else _decimal(limit_price, name="limit_price", positive=True)
        stop = None if stop_price is None else _decimal(stop_price, name="stop_price", positive=True)
        if order in {"LIMIT", "STOP_LIMIT"} and limit is None:
            raise AlpacaAdapterError("limit_price is required for limit-style orders")
        if order not in {"LIMIT", "STOP_LIMIT"} and limit is not None:
            raise AlpacaAdapterError("limit_price is not valid for this order type")
        if order in {"STOP", "STOP_LIMIT"} and stop is None:
            raise AlpacaAdapterError("stop_price is required for stop orders")
        if order not in {"STOP", "STOP_LIMIT"} and stop is not None:
            raise AlpacaAdapterError("stop_price is not valid for this order type")

        if extended_hours:
            if asset != "EQUITY" or order != "LIMIT" or tif not in {"DAY", "GTC"}:
                raise AlpacaAdapterError(
                    "extended_hours is admitted only for DAY/GTC equity limit orders"
                )

        normalized_position_intent = None
        if position_intent is not None:
            normalized_position_intent = _text(position_intent, name="position_intent").lower()
            if normalized_position_intent not in {
                "buy_to_open",
                "buy_to_close",
                "sell_to_open",
                "sell_to_close",
            }:
                raise AlpacaAdapterError("unsupported position_intent")
            if asset != "OPTION":
                raise AlpacaAdapterError("position_intent is admitted only for options in this foundation")

        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            asset_class=asset,
            symbol=_text(symbol, name="symbol").upper(),
            side=side_value,
            order_type=order,
            time_in_force=tif,
            quantity=qty,
            notional=notion,
            limit_price=limit,
            stop_price=stop,
            extended_hours=extended_hours,
            position_intent=normalized_position_intent,
        )


_ALPACA_PREPARED_REQUEST_FACTORY_TOKEN = object()


@dataclass(frozen=True)
class AlpacaPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    account_id: str
    environment: str
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]
    instrument_versions: tuple[str, ...] = ()
    capability_snapshot_ids: tuple[str, ...] = ()
    body_sha256: str = field(init=False)
    _factory_token: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self._factory_token is not _ALPACA_PREPARED_REQUEST_FACTORY_TOKEN:
            raise AlpacaAdapterError(
                "AlpacaPreparedRequest must be created by a canonical preparation factory"
            )
        endpoint = _text(self.endpoint, name="endpoint")
        if endpoint != "/v2/orders":
            raise AlpacaAdapterError("prepared order endpoint must be /v2/orders")
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be a mapping")
        body = dict(self.body)
        body["client_order_id"] = validate_client_order_id(body.get("client_order_id"))
        try:
            rendered_body = json.dumps(
                body,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as error:
            raise AlpacaAdapterError(
                "prepared order body must be canonical JSON"
            ) from error
        account = _text(self.account_id, name="account_id")
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise AlpacaAdapterError("environment must be PAPER or LIVE")
        capability_snapshot_id = _text(
            self.capability_snapshot_id,
            name="capability_snapshot_id",
        )
        refs = tuple(
            _text(value, name="documentation_ref")
            for value in self.documentation_refs
        )
        if not refs:
            raise AlpacaAdapterError("documentation_refs must not be empty")
        if not isinstance(self.instrument_versions, tuple):
            raise TypeError("instrument_versions must be a tuple")
        instrument_versions = tuple(
            _text(value, name="instrument_version")
            for value in self.instrument_versions
        )
        if not instrument_versions:
            raise AlpacaAdapterError("instrument_versions must not be empty")
        if len(instrument_versions) != len(set(instrument_versions)):
            raise AlpacaAdapterError("instrument_versions must be unique")
        raw_snapshot_ids = (
            self.capability_snapshot_ids
            if self.capability_snapshot_ids
            else (capability_snapshot_id,)
        )
        if not isinstance(raw_snapshot_ids, tuple):
            raise TypeError("capability_snapshot_ids must be a tuple")
        snapshot_ids = tuple(
            _text(value, name="capability_snapshot_id")
            for value in raw_snapshot_ids
        )
        if len(snapshot_ids) != len(set(snapshot_ids)):
            raise AlpacaAdapterError("capability_snapshot_ids must be unique")
        if capability_snapshot_id not in snapshot_ids:
            raise AlpacaAdapterError(
                "primary capability_snapshot_id must be included in capability_snapshot_ids"
            )
        if len(snapshot_ids) != len(instrument_versions):
            raise AlpacaAdapterError(
                "capability snapshot identities must match instrument versions one-for-one"
            )
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "body", MappingProxyType(body))
        object.__setattr__(
            self,
            "body_sha256",
            "sha256:" + sha256(rendered_body.encode("utf-8")).hexdigest(),
        )
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "capability_snapshot_id", capability_snapshot_id)
        object.__setattr__(self, "documentation_refs", refs)
        object.__setattr__(self, "instrument_versions", instrument_versions)
        object.__setattr__(self, "capability_snapshot_ids", snapshot_ids)


def prepare_order_request(
    intent: AlpacaOrderIntent,
    *,
    client_order_id: str,
    account_id: str,
    environment: str,
    capability: CapabilitySnapshot,
    at: datetime,
) -> AlpacaPreparedRequest:
    if not isinstance(intent, AlpacaOrderIntent):
        raise TypeError("intent must be AlpacaOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    account = _text(account_id, name="account_id")
    environment_value = _text(environment, name="environment").upper()
    if environment_value not in {"PAPER", "LIVE"}:
        raise AlpacaAdapterError("environment must be PAPER or LIVE")
    if capability.provider_id.upper() != "ALPACA":
        raise AlpacaAdapterError("capability belongs to another provider")
    if capability.account_id != account:
        raise AlpacaAdapterError("capability account does not match target account")
    if capability.environment.upper() != environment_value:
        raise AlpacaAdapterError("capability environment does not match target environment")
    if capability.instrument_version != intent.instrument_version:
        raise AlpacaAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise AlpacaAdapterError("exact capability evidence does not admit this order")

    body: dict[str, object] = {
        "symbol": intent.symbol,
        "side": intent.side.lower(),
        "type": intent.order_type.lower(),
        "time_in_force": intent.time_in_force.lower(),
        "client_order_id": client_id,
        "extended_hours": intent.extended_hours,
    }
    if intent.quantity is not None:
        body["qty"] = _decimal_text(intent.quantity)
    else:
        body["notional"] = _decimal_text(intent.notional)
    if intent.limit_price is not None:
        body["limit_price"] = _decimal_text(intent.limit_price)
    if intent.stop_price is not None:
        body["stop_price"] = _decimal_text(intent.stop_price)
    if intent.position_intent is not None:
        body["position_intent"] = intent.position_intent

    return AlpacaPreparedRequest(
        endpoint="/v2/orders",
        body=body,
        account_id=account,
        environment=environment_value,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(ALPACA_DOCS.values()),
        instrument_versions=(intent.instrument_version,),
        _factory_token=_ALPACA_PREPARED_REQUEST_FACTORY_TOKEN,
    )


@dataclass(frozen=True)
class AlpacaOrderObservation:
    provider_order_id: str
    client_order_id: str
    symbol: str
    provider_status: str
    filled_quantity: Decimal
    average_fill_price: Decimal | None

    @property
    def proves_economic_fill(self) -> bool:
        """Order state alone is not the economic fill identity authority."""

        return False


def parse_order_observation(payload: Mapping[str, object]) -> AlpacaOrderObservation:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    provider_order_id = _text(payload.get("id"), name="id")
    client_id = validate_client_order_id(payload.get("client_order_id"))
    symbol = _text(payload.get("symbol"), name="symbol").upper()
    status = _text(payload.get("status"), name="status").lower()
    if "filled_qty" not in payload or payload["filled_qty"] is None:
        raise AlpacaAdapterError(
            "filled_qty is required; missing financial quantity cannot be treated as zero"
        )
    filled = _decimal(payload["filled_qty"], name="filled_qty")
    if filled < 0:
        raise AlpacaAdapterError("filled_qty cannot be negative")
    average_value = payload.get("filled_avg_price")
    average = None
    if average_value not in (None, ""):
        average = _decimal(average_value, name="filled_avg_price", positive=True)
        if filled == 0:
            raise AlpacaAdapterError("filled_avg_price cannot exist when filled_qty is zero")
    elif filled > 0:
        raise AlpacaAdapterError(
            "filled_avg_price is required when filled_qty is positive"
        )
    return AlpacaOrderObservation(
        provider_order_id=provider_order_id,
        client_order_id=client_id,
        symbol=symbol,
        provider_status=status,
        filled_quantity=filled,
        average_fill_price=average,
    )


@dataclass(frozen=True)
class AlpacaAbsenceEvidence:
    by_client_order_id_complete: bool
    orders_history_complete: bool
    trade_events_complete: bool
    activities_complete: bool
    consistency_horizon_satisfied: bool
    order_found: bool
    qualified_exclusion_semantics: bool = False

    def __post_init__(self) -> None:
        for field in (
            "by_client_order_id_complete",
            "orders_history_complete",
            "trade_events_complete",
            "activities_complete",
            "consistency_horizon_satisfied",
            "order_found",
            "qualified_exclusion_semantics",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.by_client_order_id_complete
            and self.orders_history_complete
            and self.trade_events_complete
            and self.activities_complete
            and self.consistency_horizon_satisfied
            and self.qualified_exclusion_semantics
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


def paper_evidence_proves_live_execution_realism() -> bool:
    """Paper omits important live effects and cannot prove live execution realism."""

    return False



def _utc_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise AlpacaAdapterError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise AlpacaAdapterError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _uuid_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        UUID(text)
    except ValueError as error:
        raise AlpacaAdapterError(f"{name} must be a UUID") from error
    return text


def _response_evidence(
    response: Mapping[str, object],
    *,
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
    env = _text(environment, name="environment").upper()
    if env == "PAPER":
        host = "paper-api.alpaca.markets"
    elif env == "LIVE":
        host = "api.alpaca.markets"
    else:
        raise AlpacaAdapterError("response environment must be PAPER or LIVE")
    return {
        "artifact_id": str(
            uuid5(
                NAMESPACE_URL,
                f"https://{host}/v2/orders#sha256:{digest}",
            )
        ),
        "sha256": "sha256:" + digest,
        "source_uri": f"https://{host}/v2/orders",
        "observed_at": _utc_text(observed_at, name="observed_at"),
        "rights_id": "provider-observation-alpaca",
    }


def parse_submission_response(
    *,
    attempt_id: str,
    client_order_id: str,
    response: Mapping[str, object] | None,
    observed_at: str,
    environment: str,
    transport_ambiguous: bool = False,
) -> dict[str, object]:
    """Map a recorded successful create-order response to SubmissionResult.

    A returned Order object is acknowledgement only. Even if its status says
    filled, unique execution economics must come from activity evidence.
    """

    aid = _uuid_text(attempt_id, name="attempt_id")
    cid = validate_client_order_id(client_order_id)
    when = _utc_text(observed_at, name="observed_at")
    env = _text(environment, name="environment").upper()
    if env not in {"PAPER", "LIVE"}:
        raise AlpacaAdapterError("response environment must be PAPER or LIVE")
    if type(transport_ambiguous) is not bool:
        raise TypeError("transport_ambiguous must be boolean")
    if transport_ambiguous:
        if response is not None:
            raise AlpacaAdapterError(
                "ambiguous transport must not fabricate a provider response"
            )
        # Scope/time remain on the durable SubmissionAttempt. Transport
        # ambiguity has no authoritative provider receive timestamp.
        return {
            "attempt_id": aid,
            "outcome": "UNKNOWN",
            "client_order_id": cid,
            "reason_code": "ALPACA_TRANSPORT_AMBIGUOUS",
            "evidence": [],
            "retry_disposition": "RECONCILE_FIRST",
        }
    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    provider_order_id = _uuid_text(response.get("id"), name="response.id")
    echoed = validate_client_order_id(
        _text(response.get("client_order_id"), name="response.client_order_id")
    )
    if echoed != cid:
        raise AlpacaAdapterError(
            "Alpaca client_order_id response does not match request"
        )
    return {
        "attempt_id": aid,
        "outcome": "ACKNOWLEDGED",
        "provider_order_id": provider_order_id,
        "client_order_id": cid,
        "evidence": [
            _response_evidence(
                response,
                observed_at=when,
                environment=env,
            )
        ],
        "retry_disposition": "NEVER",
    }


def parse_trade_activities(
    activities: object,
    *,
    account_id: str,
    environment: str,
    instrument_versions: Mapping[str, str],
    client_ids_by_order_id: Mapping[str, str | None],
    fees_by_activity_id: Mapping[str, tuple[object, str]],
) -> tuple[ProviderFillEvidence, ...]:
    """Map FILL activities only when separate fee evidence is bound.

    The documented trade-activity row contains execution quantity/price and
    order identity but not canonical per-fill fee amount/currency. AutoTrade
    refuses to invent zero fees.
    """

    if not isinstance(activities, list):
        raise AlpacaAdapterError("activities must be an array")
    for name, mapping in (
        ("instrument_versions", instrument_versions),
        ("client_ids_by_order_id", client_ids_by_order_id),
        ("fees_by_activity_id", fees_by_activity_id),
    ):
        if not isinstance(mapping, Mapping):
            raise AlpacaAdapterError(f"{name} must be a mapping")

    by_activity: dict[str, ProviderFillEvidence] = {}
    for index, raw in enumerate(activities):
        if not isinstance(raw, Mapping):
            raise AlpacaAdapterError(f"activities[{index}] must be an object")
        if _text(raw.get("activity_type"), name="activity_type").upper() != "FILL":
            continue
        activity_id = _text(raw.get("id"), name="activity.id")
        order_id = _uuid_text(raw.get("order_id"), name="activity.order_id")
        symbol = _text(raw.get("symbol"), name="activity.symbol")
        if symbol not in instrument_versions:
            raise AlpacaAdapterError(
                f"unmapped Alpaca instrument symbol: {symbol}"
            )
        instrument = _text(
            instrument_versions[symbol], name="instrument_version"
        )
        if order_id not in client_ids_by_order_id:
            raise AlpacaAdapterError(
                f"missing Alpaca order-to-client identity mapping: {order_id}"
            )
        client_id = client_ids_by_order_id[order_id]
        if client_id is not None:
            client_id = validate_client_order_id(client_id)
        if activity_id not in fees_by_activity_id:
            raise AlpacaAdapterError(
                f"missing fee evidence for Alpaca activity: {activity_id}"
            )
        fee_amount, fee_currency = fees_by_activity_id[activity_id]
        fill = ProviderFillEvidence.create(
            provider_id="ALPACA",
            account_id=account_id,
            environment=environment,
            provider_execution_id=activity_id,
            client_order_id=client_id,
            instrument=instrument,
            quantity=raw.get("qty"),
            price=raw.get("price"),
            fee_amount=fee_amount,
            fee_currency=_text(fee_currency, name="fee_currency"),
            trade_time=_utc_text(
                raw.get("transaction_time"), name="transaction_time"
            ),
        )
        previous = by_activity.get(activity_id)
        if previous is not None and previous != fill:
            raise AlpacaAdapterError(
                "Alpaca activity id appears with conflicting economic content"
            )
        by_activity[activity_id] = fill
    return tuple(by_activity.values())


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
    """Create canonical fail-closed reconciliation coverage evidence."""

    normalized = _text(surface, name="surface").upper()
    if normalized not in {
        "OPEN_ORDERS",
        "ORDER_HISTORY",
        "EXECUTIONS",
        "ACTIVITIES",
    }:
        raise AlpacaAdapterError("unsupported Alpaca reconciliation surface")
    for name, value in (
        ("pagination_complete", pagination_complete),
        ("consistency_horizon_satisfied", consistency_horizon_satisfied),
        ("qualified_exclusion_semantics", qualified_exclusion_semantics),
    ):
        if type(value) is not bool:
            raise AlpacaAdapterError(f"{name} must be boolean")
    return CoverageSurfaceEvidence(
        provider_id="ALPACA",
        account_id=account_id,
        environment=environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=qualified_exclusion_semantics,
    )


# Multi-leg options remain a narrow provider translation over the same
# capability and reconciliation authorities. The foundation intentionally
# supports option-only legs; equity-option combinations require separate
# deliverable/margin qualification before they can be admitted.

_ALPACA_MLEG_DOC = "https://docs.alpaca.markets/us/docs/options-level-3-trading"
_MLEG_POSITION_INTENTS = frozenset(
    {"buy_to_open", "buy_to_close", "sell_to_open", "sell_to_close"}
)


def _positive_integer(value, *, name: str) -> int:
    exact = _decimal(value, name=name, positive=True)
    if exact != exact.to_integral_value():
        raise AlpacaAdapterError(f"{name} must be a positive whole number")
    return int(exact)


@dataclass(frozen=True)
class AlpacaMlegLeg:
    instrument_version: str
    underlying_version: str
    symbol: str
    ratio_quantity: int
    side: str
    position_intent: str

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        underlying_version: str,
        symbol: str,
        ratio_quantity,
        side: str,
        position_intent: str,
    ) -> "AlpacaMlegLeg":
        normalized_side = _text(side, name="side").upper()
        if normalized_side not in _SIDES:
            raise AlpacaAdapterError("leg side must be BUY or SELL")
        intent = _text(position_intent, name="position_intent").lower()
        if intent not in _MLEG_POSITION_INTENTS:
            raise AlpacaAdapterError("unsupported multi-leg position_intent")
        if intent.startswith("buy_") and normalized_side != "BUY":
            raise AlpacaAdapterError("buy position_intent requires BUY leg side")
        if intent.startswith("sell_") and normalized_side != "SELL":
            raise AlpacaAdapterError("sell position_intent requires SELL leg side")
        return cls(
            instrument_version=_text(
                instrument_version, name="instrument_version"
            ),
            underlying_version=_text(
                underlying_version, name="underlying_version"
            ),
            symbol=_text(symbol, name="symbol").upper(),
            ratio_quantity=_positive_integer(
                ratio_quantity, name="ratio_quantity"
            ),
            side=normalized_side,
            position_intent=intent,
        )


@dataclass(frozen=True)
class AlpacaMlegOrderIntent:
    underlying_version: str
    quantity: int
    order_type: str
    time_in_force: str
    legs: tuple[AlpacaMlegLeg, ...]
    limit_price: Decimal | None

    @classmethod
    def create(
        cls,
        *,
        underlying_version: str,
        quantity,
        order_type: str,
        time_in_force: str,
        legs,
        limit_price=None,
    ) -> "AlpacaMlegOrderIntent":
        underlying = _text(underlying_version, name="underlying_version")
        qty = _positive_integer(quantity, name="quantity")
        order = _text(order_type, name="order_type").upper()
        if order not in {"MARKET", "LIMIT"}:
            raise AlpacaAdapterError(
                "multi-leg options foundation supports only MARKET and LIMIT"
            )
        tif = _text(time_in_force, name="time_in_force").upper()
        # Current Trading API reference remains DAY-only for options. GTC has
        # separate changelog evidence but is not silently generalized to MLeg.
        if tif != "DAY":
            raise AlpacaAdapterError(
                "multi-leg options foundation currently requires DAY"
            )
        if isinstance(legs, (str, bytes)):
            raise AlpacaAdapterError("legs must be a collection")
        try:
            leg_values = tuple(legs)
        except TypeError as error:
            raise AlpacaAdapterError("legs must be a collection") from error
        if not 2 <= len(leg_values) <= 4:
            raise AlpacaAdapterError("multi-leg order requires 2 to 4 legs")
        if any(not isinstance(leg, AlpacaMlegLeg) for leg in leg_values):
            raise AlpacaAdapterError("legs must contain AlpacaMlegLeg values")
        if any(leg.underlying_version != underlying for leg in leg_values):
            raise AlpacaAdapterError(
                "every multi-leg option must share the canonical underlying"
            )
        instrument_versions = [leg.instrument_version for leg in leg_values]
        if len(instrument_versions) != len(set(instrument_versions)):
            raise AlpacaAdapterError(
                "multi-leg instrument versions must be unique; use ratio_quantity"
            )
        provider_symbols = [leg.symbol for leg in leg_values]
        if len(provider_symbols) != len(set(provider_symbols)):
            raise AlpacaAdapterError(
                "multi-leg provider symbols must be unique; use ratio_quantity"
            )

        common_divisor = 0
        for leg in leg_values:
            a, b = common_divisor, leg.ratio_quantity
            while b:
                a, b = b, a % b
            common_divisor = a
        if common_divisor != 1:
            raise AlpacaAdapterError(
                "multi-leg ratio quantities must be in simplest form"
            )

        price = None
        if limit_price is not None:
            price = _decimal(limit_price, name="limit_price")
            if price == 0:
                raise AlpacaAdapterError(
                    "multi-leg limit_price must be non-zero debit or credit"
                )
        if order == "LIMIT" and price is None:
            raise AlpacaAdapterError("LIMIT multi-leg order requires limit_price")
        if order == "MARKET" and price is not None:
            raise AlpacaAdapterError(
                "MARKET multi-leg order must not carry limit_price"
            )

        return cls(
            underlying_version=underlying,
            quantity=qty,
            order_type=order,
            time_in_force=tif,
            legs=leg_values,
            limit_price=price,
        )


def prepare_mleg_order_request(
    intent: AlpacaMlegOrderIntent,
    *,
    client_order_id: str,
    capabilities: Mapping[str, CapabilitySnapshot],
    at: datetime,
) -> AlpacaPreparedRequest:
    """Prepare an option-only MLeg request without signing or sending it."""

    if not isinstance(intent, AlpacaMlegOrderIntent):
        raise TypeError("intent must be AlpacaMlegOrderIntent")
    if not isinstance(capabilities, Mapping):
        raise TypeError("capabilities must be a mapping")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)

    identities: set[tuple[str, str, str]] = set()
    for leg in intent.legs:
        capability = capabilities.get(leg.instrument_version)
        if not isinstance(capability, CapabilitySnapshot):
            raise AlpacaAdapterError(
                f"missing exact capability for leg {leg.instrument_version}"
            )
        if capability.provider_id.upper() != "ALPACA":
            raise AlpacaAdapterError("multi-leg capability belongs to another provider")
        if capability.instrument_version != leg.instrument_version:
            raise AlpacaAdapterError(
                "multi-leg capability instrument version does not match leg"
            )
        if not capability.admits(
            at=point,
            order_type=intent.order_type,
            time_in_force=intent.time_in_force,
            permission_scope="ORDER_WRITE",
        ):
            raise AlpacaAdapterError(
                "exact capability evidence does not admit every multi-leg option"
            )
        identities.add(
            (
                capability.account_id,
                capability.environment,
                capability.entity_id,
            )
        )
    if len(identities) != 1:
        raise AlpacaAdapterError(
            "all multi-leg capabilities must bind the same account/environment/entity"
        )

    body: dict[str, object] = {
        "order_class": "mleg",
        "qty": str(intent.quantity),
        "type": intent.order_type.lower(),
        "time_in_force": intent.time_in_force.lower(),
        "client_order_id": client_id,
        "legs": [
            {
                "symbol": leg.symbol,
                "ratio_qty": str(leg.ratio_quantity),
                "side": leg.side.lower(),
                "position_intent": leg.position_intent,
            }
            for leg in intent.legs
        ],
    }
    if intent.limit_price is not None:
        body["limit_price"] = _decimal_text(intent.limit_price)

    first_capability = capabilities[intent.legs[0].instrument_version]
    return AlpacaPreparedRequest(
        endpoint="/v2/orders",
        body=body,
        account_id=first_capability.account_id,
        environment=first_capability.environment,
        capability_snapshot_id=first_capability.snapshot_id,
        documentation_refs=tuple(ALPACA_DOCS.values()) + (_ALPACA_MLEG_DOC,),
        instrument_versions=tuple(leg.instrument_version for leg in intent.legs),
        capability_snapshot_ids=tuple(
            capabilities[leg.instrument_version].snapshot_id
            for leg in intent.legs
        ),
        _factory_token=_ALPACA_PREPARED_REQUEST_FACTORY_TOKEN,
    )
