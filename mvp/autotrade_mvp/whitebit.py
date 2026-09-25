"""WhiteBIT non-live adapter contract foundation.

This module deliberately does not perform network requests or hold credentials.
It translates already-admitted AutoTrade intents into provider-shaped request
payloads and normalizes provider evidence. Live authority remains with the
guarded dispatcher and exact capability evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
import base64
import hashlib
import hmac
import json
import re

from .capabilities import CapabilitySnapshot
from .reconciliation import ProviderFillEvidence


class WhiteBitAdapterError(ValueError):
    """Raised when WhiteBIT-specific input cannot be represented safely."""


WHITEBIT_OFFICIAL_DOCS = MappingProxyType(
    {
        "api": "https://docs.whitebit.com/api-reference/overview",
        "order_types": "https://docs.whitebit.com/concepts/order-types",
        "client_order_id": "https://docs.whitebit.com/guides/client-order-id",
    }
)

_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_PRODUCTS = frozenset({"SPOT", "COLLATERAL", "FUTURES"})
_ORDER_TYPES = frozenset({"MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"})
_SIDES = frozenset({"BUY", "SELL"})


def decode_whitebit_json(raw: str | bytes):
    """Decode provider JSON while preserving every decimal token exactly."""
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WhiteBitAdapterError("provider JSON must be UTF-8") from error
    if not isinstance(raw, str) or not raw.strip():
        raise WhiteBitAdapterError("provider JSON is required")

    def reject_constant(value: str):
        raise WhiteBitAdapterError(
            f"provider JSON contains non-finite numeric token: {value}"
        )

    try:
        return json.loads(
            raw,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=reject_constant,
        )
    except json.JSONDecodeError as error:
        raise WhiteBitAdapterError("provider JSON is invalid") from error


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WhiteBitAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise WhiteBitAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise WhiteBitAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise WhiteBitAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise WhiteBitAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise WhiteBitAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_client_order_id(value: str) -> str:
    client_id = _text(value, name="client_order_id")
    if _CLIENT_ID.fullmatch(client_id) is None:
        raise WhiteBitAdapterError(
            "client_order_id must be 1-64 characters using only letters, numbers, dash, dot or underscore"
        )
    return client_id


@dataclass(frozen=True)
class WhiteBitOrderIntent:
    instrument_version: str
    product_family: str
    market: str
    side: str
    order_type: str
    amount: Decimal
    price: Decimal | None = None
    activation_price: Decimal | None = None
    time_in_force: str = "GTC"
    post_only: bool = False
    reduce_only: bool = False
    position_side: str | None = None

    def __post_init__(self) -> None:
        product = _text(self.product_family, name="product_family").upper()
        order = _text(self.order_type, name="order_type").upper()
        side_value = _text(self.side, name="side").upper()
        tif = _text(self.time_in_force, name="time_in_force").upper()
        if product not in _PRODUCTS:
            raise WhiteBitAdapterError("unsupported WhiteBIT product family")
        if order not in _ORDER_TYPES:
            raise WhiteBitAdapterError("unsupported WhiteBIT order type")
        if side_value not in _SIDES:
            raise WhiteBitAdapterError("side must be BUY or SELL")
        if tif not in {"GTC", "IOC"}:
            raise WhiteBitAdapterError("time_in_force must be GTC or IOC")
        if type(self.post_only) is not bool or type(self.reduce_only) is not bool:
            raise WhiteBitAdapterError("post_only and reduce_only must be boolean")
        amount = _decimal(self.amount, name="amount", positive=True)
        price = None if self.price is None else _decimal(
            self.price, name="price", positive=True
        )
        activation = None if self.activation_price is None else _decimal(
            self.activation_price, name="activation_price", positive=True
        )
        if order in {"LIMIT", "STOP_LIMIT"} and price is None:
            raise WhiteBitAdapterError("price is required for limit orders")
        if order in {"MARKET", "STOP_MARKET"} and price is not None:
            raise WhiteBitAdapterError("price is not valid for market orders")
        if order.startswith("STOP_") and activation is None:
            raise WhiteBitAdapterError("activation_price is required for stop orders")
        if not order.startswith("STOP_") and activation is not None:
            raise WhiteBitAdapterError("activation_price is only valid for stop orders")
        if self.reduce_only and product == "SPOT":
            raise WhiteBitAdapterError("reduce_only is not available for spot orders")
        if tif == "IOC" and not (product == "SPOT" and order == "LIMIT"):
            raise WhiteBitAdapterError(
                "IOC is admitted only for spot limit orders in this adapter foundation"
            )
        if self.post_only and order not in {"LIMIT", "STOP_LIMIT"}:
            raise WhiteBitAdapterError("post_only is valid only for limit-style orders")
        if self.post_only and tif == "IOC":
            raise WhiteBitAdapterError("post_only and IOC are mutually exclusive")
        position_side = None
        if self.position_side is not None:
            position_side = _text(self.position_side, name="position_side").upper()
            if position_side not in {"LONG", "SHORT"}:
                raise WhiteBitAdapterError("position_side must be LONG or SHORT")
            if product == "SPOT":
                raise WhiteBitAdapterError("position_side is not valid for spot")
        object.__setattr__(
            self, "instrument_version", _text(self.instrument_version, name="instrument_version")
        )
        object.__setattr__(self, "product_family", product)
        object.__setattr__(self, "market", _text(self.market, name="market").upper())
        object.__setattr__(self, "side", side_value)
        object.__setattr__(self, "order_type", order)
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "activation_price", activation)
        object.__setattr__(self, "time_in_force", tif)
        object.__setattr__(self, "position_side", position_side)

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        product_family: str,
        market: str,
        side: str,
        order_type: str,
        amount,
        price=None,
        activation_price=None,
        time_in_force: str = "GTC",
        post_only: bool = False,
        reduce_only: bool = False,
        position_side: str | None = None,
    ) -> "WhiteBitOrderIntent":
        product = _text(product_family, name="product_family").upper()
        order = _text(order_type, name="order_type").upper()
        side_value = _text(side, name="side").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if product not in _PRODUCTS:
            raise WhiteBitAdapterError("unsupported WhiteBIT product family")
        if order not in _ORDER_TYPES:
            raise WhiteBitAdapterError("unsupported WhiteBIT order type")
        if side_value not in _SIDES:
            raise WhiteBitAdapterError("side must be BUY or SELL")
        if tif not in {"GTC", "IOC"}:
            raise WhiteBitAdapterError("time_in_force must be GTC or IOC")
        if type(post_only) is not bool or type(reduce_only) is not bool:
            raise WhiteBitAdapterError("post_only and reduce_only must be boolean")
        amount_value = _decimal(amount, name="amount", positive=True)
        price_value = None if price is None else _decimal(price, name="price", positive=True)
        activation_value = (
            None
            if activation_price is None
            else _decimal(activation_price, name="activation_price", positive=True)
        )
        if order in {"LIMIT", "STOP_LIMIT"} and price_value is None:
            raise WhiteBitAdapterError("price is required for limit orders")
        if order in {"MARKET", "STOP_MARKET"} and price_value is not None:
            raise WhiteBitAdapterError("price is not valid for market orders")
        if order.startswith("STOP_") and activation_value is None:
            raise WhiteBitAdapterError("activation_price is required for stop orders")
        if not order.startswith("STOP_") and activation_value is not None:
            raise WhiteBitAdapterError("activation_price is only valid for stop orders")
        if reduce_only and product == "SPOT":
            raise WhiteBitAdapterError("reduce_only is not available for spot orders")
        if tif == "IOC" and not (product == "SPOT" and order == "LIMIT"):
            raise WhiteBitAdapterError("IOC is admitted only for spot limit orders in this adapter foundation")
        if post_only and order not in {"LIMIT", "STOP_LIMIT"}:
            raise WhiteBitAdapterError("post_only is valid only for limit-style orders")
        if post_only and tif == "IOC":
            raise WhiteBitAdapterError("post_only and IOC are mutually exclusive")
        normalized_position_side = None
        if position_side is not None:
            normalized_position_side = _text(position_side, name="position_side").upper()
            if normalized_position_side not in {"LONG", "SHORT"}:
                raise WhiteBitAdapterError("position_side must be LONG or SHORT")
            if product == "SPOT":
                raise WhiteBitAdapterError("position_side is not valid for spot")
        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            product_family=product,
            market=_text(market, name="market").upper(),
            side=side_value,
            order_type=order,
            amount=amount_value,
            price=price_value,
            activation_price=activation_value,
            time_in_force=tif,
            post_only=post_only,
            reduce_only=reduce_only,
            position_side=normalized_position_side,
        )


@dataclass(frozen=True)
class WhiteBitPreparedRequest:
    endpoint: str
    body: Mapping[str, object]
    account_id: str
    environment: str
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(
            self,
            "environment",
            _text(self.environment, name="environment").upper(),
        )


@dataclass(frozen=True)
class WhiteBitMarketRules:
    market: str
    market_type: str
    is_collateral: bool
    trades_enabled: bool
    step_size: Decimal
    tick_size: Decimal
    min_amount: Decimal
    min_total: Decimal
    max_total: Decimal | None
    delisted_at: int | None

    @classmethod
    def from_provider(cls, payload: Mapping[str, object]) -> "WhiteBitMarketRules":
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        required = {
            "name",
            "type",
            "isCollateral",
            "tradesEnabled",
            "stepSize",
            "tickSize",
            "minAmount",
            "minTotal",
            "maxTotal",
            "delistedAt",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise WhiteBitAdapterError(
                "market metadata missing required fields: " + ", ".join(missing)
            )
        if type(payload["isCollateral"]) is not bool:
            raise WhiteBitAdapterError("isCollateral must be boolean")
        if type(payload["tradesEnabled"]) is not bool:
            raise WhiteBitAdapterError("tradesEnabled must be boolean")
        step_raw = _text(str(payload["stepSize"]), name="stepSize")
        tick_raw = _text(str(payload["tickSize"]), name="tickSize")
        step = _decimal(step_raw, name="stepSize", positive=True)
        tick = _decimal(tick_raw, name="tickSize", positive=True)
        min_amount = _decimal(payload["minAmount"], name="minAmount", positive=True)
        min_total = _decimal(payload["minTotal"], name="minTotal", positive=True)
        max_raw = _decimal(payload["maxTotal"], name="maxTotal")
        max_total = None if max_raw == 0 else max_raw
        if max_total is not None and max_total < min_total:
            raise WhiteBitAdapterError("maxTotal cannot be below minTotal")
        delisted = payload["delistedAt"]
        if delisted is not None:
            if not isinstance(delisted, int) or isinstance(delisted, bool) or delisted < 0:
                raise WhiteBitAdapterError("delistedAt must be a non-negative integer or null")
        market_type = _text(str(payload["type"]), name="type").upper()
        if market_type not in {"SPOT", "FUTURES", "TRADFIFUTURES"}:
            raise WhiteBitAdapterError("unsupported WhiteBIT market type")
        return cls(
            market=_text(str(payload["name"]), name="name").upper(),
            market_type=market_type,
            is_collateral=payload["isCollateral"],
            trades_enabled=payload["tradesEnabled"],
            step_size=step,
            tick_size=tick,
            min_amount=min_amount,
            min_total=min_total,
            max_total=max_total,
            delisted_at=delisted,
        )


def _require_multiple(value: Decimal, step: Decimal, *, field: str) -> None:
    if value % step != 0:
        raise WhiteBitAdapterError(
            f"{field} must be an exact multiple of provider {field} step"
        )


def validate_intent_market_rules(
    intent: WhiteBitOrderIntent,
    rules: WhiteBitMarketRules,
    *,
    at: datetime,
) -> None:
    if not isinstance(intent, WhiteBitOrderIntent):
        raise TypeError("intent must be WhiteBitOrderIntent")
    if not isinstance(rules, WhiteBitMarketRules):
        raise TypeError("rules must be WhiteBitMarketRules")
    point = _instant(at, name="at")
    if rules.market != intent.market:
        raise WhiteBitAdapterError("market metadata does not match intent market")
    if not rules.trades_enabled:
        raise WhiteBitAdapterError("provider market is not trade-enabled")
    if intent.product_family == "SPOT" and rules.market_type != "SPOT":
        raise WhiteBitAdapterError("spot intent requires spot market metadata")
    if intent.product_family == "FUTURES" and rules.market_type != "FUTURES":
        raise WhiteBitAdapterError("futures intent requires futures market metadata")
    if intent.product_family == "COLLATERAL" and not rules.is_collateral:
        raise WhiteBitAdapterError("collateral intent requires collateral-enabled market")
    if (
        rules.delisted_at is not None
        and int(point.timestamp()) >= rules.delisted_at
    ):
        raise WhiteBitAdapterError("market delisting time has passed")
    if intent.amount < rules.min_amount:
        raise WhiteBitAdapterError("amount is below provider minAmount")
    _require_multiple(intent.amount, rules.step_size, field="amount")
    if intent.price is not None:
        _require_multiple(intent.price, rules.tick_size, field="price")
        total = intent.amount * intent.price
        if total < rules.min_total:
            raise WhiteBitAdapterError("order total is below provider minTotal")
        if rules.max_total is not None and total > rules.max_total:
            raise WhiteBitAdapterError("order total exceeds provider maxTotal")
    if intent.activation_price is not None:
        _require_multiple(
            intent.activation_price,
            rules.tick_size,
            field="activation_price",
        )


_SPOT_ENDPOINTS = {
    "MARKET": "/api/v4/order/market",
    "LIMIT": "/api/v4/order/new",
    "STOP_MARKET": "/api/v4/order/stop_market",
    "STOP_LIMIT": "/api/v4/order/stop_limit",
}
_COLLATERAL_ENDPOINTS = {
    "MARKET": "/api/v4/order/collateral/market",
    "LIMIT": "/api/v4/order/collateral/limit",
    "STOP_MARKET": "/api/v4/order/collateral/trigger-market",
    "STOP_LIMIT": "/api/v4/order/collateral/stop-limit",
}


def prepare_order_request(
    intent: WhiteBitOrderIntent,
    *,
    client_order_id: str,
    account_id: str,
    environment: str,
    capability: CapabilitySnapshot,
    market_rules: WhiteBitMarketRules,
    at: datetime,
) -> WhiteBitPreparedRequest:
    """Translate an admitted intent without sending it.

    `request` and `nonce` are intentionally not added here. A transport wrapper
    must add provider authentication fields after the guarded dispatcher's final
    send barrier, so a stale signed payload cannot become a second send authority.
    """

    if not isinstance(intent, WhiteBitOrderIntent):
        raise TypeError("intent must be WhiteBitOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    point = _instant(at, name="at")
    client_id = validate_client_order_id(client_order_id)
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    if capability.provider_id.upper() != "WHITEBIT":
        raise WhiteBitAdapterError("capability belongs to another provider")
    if capability.account_id != account:
        raise WhiteBitAdapterError("capability account does not match target account")
    if capability.environment.upper() != env:
        raise WhiteBitAdapterError("capability environment does not match target environment")
    if capability.instrument_version != intent.instrument_version:
        raise WhiteBitAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise WhiteBitAdapterError("exact capability evidence does not admit this order")

    validate_intent_market_rules(intent, market_rules, at=point)
    if (
        intent.product_family == "SPOT"
        and intent.order_type == "STOP_MARKET"
        and intent.side == "BUY"
    ):
        raise WhiteBitAdapterError(
            "spot STOP_MARKET BUY uses quote-currency amount and is not "
            "representable by this base-quantity intent"
        )
    endpoints = _SPOT_ENDPOINTS if intent.product_family == "SPOT" else _COLLATERAL_ENDPOINTS
    endpoint = endpoints[intent.order_type]
    if (
        intent.product_family == "SPOT"
        and intent.order_type == "MARKET"
        and intent.side == "BUY"
    ):
        endpoint = "/api/v4/order/stock_market"
    body: dict[str, object] = {
        "market": intent.market,
        "side": intent.side.lower(),
        "amount": _decimal_text(intent.amount),
        "clientOrderId": client_id,
    }
    if intent.price is not None:
        body["price"] = _decimal_text(intent.price)
    if intent.activation_price is not None:
        body["activation_price"] = _decimal_text(intent.activation_price)
    if intent.order_type in {"LIMIT", "STOP_LIMIT"}:
        body["postOnly"] = intent.post_only
    if intent.time_in_force == "IOC":
        body["ioc"] = True
    if intent.product_family != "SPOT":
        body["reduceOnly"] = intent.reduce_only
        if intent.position_side is not None:
            body["positionSide"] = intent.position_side

    return WhiteBitPreparedRequest(
        endpoint=endpoint,
        body=body,
        account_id=account,
        environment=env,
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(WHITEBIT_OFFICIAL_DOCS.values()),
    )


@dataclass(frozen=True)
class WhiteBitSubmissionResult:
    """Recorded result of one guarded outbound attempt; never a fill authority."""

    attempt_id: str
    account_id: str
    environment: str
    client_order_id: str
    outcome: str
    next_action: str
    observed_at: datetime
    response_sha256: str | None
    http_status: int | None
    provider_order_id: str | None = None
    provider_reported_status: str | None = None
    rejection_code: str | None = None
    rejection_message: str | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "account_id", "environment"):
            object.__setattr__(self, name, _text(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "client_order_id",
            validate_client_order_id(self.client_order_id),
        )
        outcome = _text(self.outcome, name="outcome").upper()
        if outcome not in {"ACKNOWLEDGED", "REJECTED", "UNKNOWN"}:
            raise WhiteBitAdapterError("unsupported submission outcome")
        object.__setattr__(self, "outcome", outcome)
        action = _text(self.next_action, name="next_action").upper()
        expected_action = {
            "ACKNOWLEDGED": "OBSERVE_OR_RECONCILE",
            "REJECTED": "DO_NOT_RETRY_BLINDLY",
            "UNKNOWN": "RECONCILE_FIRST",
        }[outcome]
        if action != expected_action:
            raise WhiteBitAdapterError(
                f"{outcome} requires next_action {expected_action}"
            )
        object.__setattr__(self, "next_action", action)
        object.__setattr__(self, "observed_at", _instant(self.observed_at, name="observed_at"))
        if self.response_sha256 is not None:
            digest = _text(self.response_sha256, name="response_sha256")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise WhiteBitAdapterError("response_sha256 must be canonical SHA-256")
            object.__setattr__(self, "response_sha256", digest)
        if self.http_status is not None:
            if (
                isinstance(self.http_status, bool)
                or not isinstance(self.http_status, int)
                or not 100 <= self.http_status <= 599
            ):
                raise WhiteBitAdapterError("http_status must be an HTTP status integer")
        if outcome == "ACKNOWLEDGED" and self.provider_order_id is None:
            raise WhiteBitAdapterError("acknowledged submission requires provider_order_id")
        if outcome == "UNKNOWN" and self.provider_order_id is not None:
            raise WhiteBitAdapterError("unknown submission cannot assert provider_order_id")


def _response_bytes(raw: str | bytes) -> bytes:
    if isinstance(raw, bytes):
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise WhiteBitAdapterError("provider response must be UTF-8") from error
        return raw
    if isinstance(raw, str) and raw:
        return raw.encode("utf-8")
    raise WhiteBitAdapterError("provider response body is required")


def parse_submission_result(
    prepared: WhiteBitPreparedRequest,
    *,
    attempt_id: str,
    account_id: str,
    environment: str,
    observed_at: datetime,
    response_body: str | bytes | None,
    http_status: int | None,
    transport_ambiguous: bool = False,
) -> WhiteBitSubmissionResult:
    """Bind one authoritative response, or an ambiguous transport, to one attempt.

    A successful create response is only ACKNOWLEDGED here. Economic fills must
    still come from provider execution evidence. A send that may have reached
    WhiteBIT but lacks an authoritative response becomes UNKNOWN and must be
    reconciled before any retry.
    """

    if not isinstance(prepared, WhiteBitPreparedRequest):
        raise TypeError("prepared must be WhiteBitPreparedRequest")
    client_id = validate_client_order_id(str(prepared.body.get("clientOrderId", "")))
    attempt = _text(attempt_id, name="attempt_id")
    account = _text(account_id, name="account_id")
    env = _text(environment, name="environment").upper()
    point = _instant(observed_at, name="observed_at")
    if account != prepared.account_id:
        raise WhiteBitAdapterError(
            "submission account does not match prepared guarded request"
        )
    if env != prepared.environment:
        raise WhiteBitAdapterError(
            "submission environment does not match prepared guarded request"
        )

    if transport_ambiguous:
        if response_body is not None or http_status is not None:
            raise WhiteBitAdapterError(
                "ambiguous transport cannot claim an authoritative provider response"
            )
        return WhiteBitSubmissionResult(
            attempt_id=attempt,
            account_id=account,
            environment=env,
            client_order_id=client_id,
            outcome="UNKNOWN",
            next_action="RECONCILE_FIRST",
            observed_at=point,
            response_sha256=None,
            http_status=None,
        )

    if response_body is None or http_status is None:
        raise WhiteBitAdapterError(
            "non-ambiguous submission requires status and authoritative response body"
        )
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        raise WhiteBitAdapterError("http_status must be an integer")
    raw = _response_bytes(response_body)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    payload = decode_whitebit_json(raw)
    if not isinstance(payload, Mapping):
        raise WhiteBitAdapterError("provider submission response must be an object")

    if http_status == 200:
        provider_order_id = _text(str(payload.get("orderId", "")), name="orderId")
        response_client_id = validate_client_order_id(
            str(payload.get("clientOrderId", ""))
        )
        if response_client_id != client_id:
            raise WhiteBitAdapterError(
                "provider response clientOrderId does not match guarded attempt"
            )
        provider_market = _text(str(payload.get("market", "")), name="market").upper()
        request_market = _text(str(prepared.body.get("market", "")), name="market").upper()
        if provider_market != request_market:
            raise WhiteBitAdapterError(
                "provider response market does not match guarded attempt"
            )
        raw_status = payload.get("status")
        reported_status = (
            None
            if raw_status is None or raw_status == ""
            else _text(str(raw_status), name="status").upper()
        )
        return WhiteBitSubmissionResult(
            attempt_id=attempt,
            account_id=account,
            environment=env,
            client_order_id=client_id,
            outcome="ACKNOWLEDGED",
            next_action="OBSERVE_OR_RECONCILE",
            observed_at=point,
            response_sha256=digest,
            http_status=http_status,
            provider_order_id=provider_order_id,
            provider_reported_status=reported_status,
        )

    # WhiteBIT documents HTTP 422 validation errors for these order surfaces.
    # Do not generalize other HTTP failures into definitive rejection: a timeout,
    # proxy failure or server error may still follow an accepted write.
    if http_status == 422:
        code = payload.get("code")
        message = payload.get("message")
        errors = payload.get("errors")
        if code is None or message is None or message == "" or not isinstance(errors, Mapping):
            raise WhiteBitAdapterError(
                "422 rejection is missing documented code/message/errors evidence"
            )
        return WhiteBitSubmissionResult(
            attempt_id=attempt,
            account_id=account,
            environment=env,
            client_order_id=client_id,
            outcome="REJECTED",
            next_action="DO_NOT_RETRY_BLINDLY",
            observed_at=point,
            response_sha256=digest,
            http_status=http_status,
            rejection_code=_text(str(code), name="rejection code"),
            rejection_message=_text(str(message), name="rejection message"),
        )

    raise WhiteBitAdapterError(
        "HTTP outcome is not qualified as definitive acceptance or rejection; "
        "record transport as UNKNOWN and reconcile"
    )


@dataclass(frozen=True)
class WhiteBitOrderSnapshot:
    provider_order_id: str
    client_order_id: str | None
    market: str
    provider_status: str
    normalized_status: str
    provider_amount: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal | None
    deal_money: Decimal | None
    average_fill_price: Decimal | None
    terminal_remainder_cancelled: bool
    reduce_only: bool | None


_PROVIDER_STATUSES = frozenset(
    {
        "NEW",
        "FILLED",
        "PARTIALLY_FILLED",
        "CANCELED",
        "CANCELED_TAKER_BAND",
        "AUTO_CANCELED_REDUCE_ONLY",
        "AUTO_CANCELED_LIQUIDATION",
        "CANCELED_STP",
    }
)


def parse_order_snapshot(payload: Mapping[str, object]) -> WhiteBitOrderSnapshot:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    provider_order_id = _text(str(payload.get("orderId", "")), name="orderId")
    raw_client_order_id = payload.get("clientOrderId")
    client_order_id = (
        None
        if raw_client_order_id in {None, ""}
        else validate_client_order_id(str(raw_client_order_id))
    )
    market = _text(str(payload.get("market", "")), name="market").upper()
    status = _text(str(payload.get("status", "")), name="status").upper()
    if status not in _PROVIDER_STATUSES:
        raise WhiteBitAdapterError(f"unsupported provider order status: {status}")

    amount = _decimal(payload.get("amount"), name="amount", positive=True)
    filled = _decimal(payload.get("dealStock"), name="dealStock")
    if filled < 0 or filled > amount:
        raise WhiteBitAdapterError("filled quantity must be between zero and provider amount")
    left_value = payload.get("left")
    remaining = None if left_value is None else _decimal(left_value, name="left")
    if remaining is not None and (remaining < 0 or remaining > amount):
        raise WhiteBitAdapterError("remaining quantity must be between zero and provider amount")
    deal_money_value = payload.get("dealMoney")
    deal_money = None if deal_money_value is None else _decimal(deal_money_value, name="dealMoney")
    if deal_money is not None and deal_money < 0:
        raise WhiteBitAdapterError("dealMoney cannot be negative")
    average = None
    if filled > 0 and deal_money is not None:
        average = deal_money / filled

    if status == "FILLED":
        normalized = "FILLED"
        cancelled_remainder = False
    elif status == "PARTIALLY_FILLED":
        normalized = "PARTIALLY_FILLED"
        cancelled_remainder = False
    elif status == "NEW":
        normalized = "WORKING"
        cancelled_remainder = False
    else:
        normalized = "PARTIALLY_FILLED_CANCELLED" if filled > 0 else "CANCELLED"
        cancelled_remainder = True

    reduce_only_value = payload.get("reduceOnly")
    if reduce_only_value is not None and type(reduce_only_value) is not bool:
        raise WhiteBitAdapterError("reduceOnly must be boolean when present")

    return WhiteBitOrderSnapshot(
        provider_order_id=provider_order_id,
        client_order_id=client_order_id,
        market=market,
        provider_status=status,
        normalized_status=normalized,
        provider_amount=amount,
        filled_quantity=filled,
        remaining_quantity=remaining,
        deal_money=deal_money,
        average_fill_price=average,
        terminal_remainder_cancelled=cancelled_remainder,
        reduce_only=reduce_only_value,
    )


@dataclass(frozen=True)
class WhiteBitLookupRequest:
    surface: str
    endpoint: str
    body: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))


def order_lookup_requests(*, market: str, client_order_id: str) -> tuple[WhiteBitLookupRequest, ...]:
    """Return exact-ID order lookup requests; these alone never prove absence."""

    market_value = _text(market, name="market").upper()
    client_id = validate_client_order_id(client_order_id)
    body = {"market": market_value, "clientOrderId": client_id}
    return (
        WhiteBitLookupRequest("OPEN_ORDERS", "/api/v4/orders", body),
        WhiteBitLookupRequest("ORDER_HISTORY", "/api/v4/trade-account/order/history", body),
    )


@dataclass(frozen=True)
class WhiteBitAbsenceEvidence:
    order_found: bool
    open_orders_complete: bool
    order_history_complete: bool
    executions_complete: bool
    activities_complete: bool
    consistency_horizon_satisfied: bool
    qualified_exclusion_semantics: bool = False

    def __post_init__(self) -> None:
        for field in (
            "order_found",
            "open_orders_complete",
            "order_history_complete",
            "executions_complete",
            "activities_complete",
            "consistency_horizon_satisfied",
            "qualified_exclusion_semantics",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")
        if self.qualified_exclusion_semantics:
            raise WhiteBitAdapterError(
                "WhiteBIT foundation cannot self-assert provider exclusion semantics"
            )

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        return "INCONCLUSIVE"


def _unix_instant(value, *, name: str) -> str:
    """Convert an exact Unix timestamp to UTC without binary-float rounding."""
    instant = _decimal(value, name=name)
    if instant < 0:
        raise WhiteBitAdapterError(f"{name} cannot be negative")
    whole = int(instant)
    fractional = instant - Decimal(whole)
    microseconds = fractional * Decimal("1000000")
    if microseconds != microseconds.to_integral_value():
        raise WhiteBitAdapterError(f"{name} exceeds microsecond precision")
    parsed = datetime.fromtimestamp(whole, tz=timezone.utc).replace(
        microsecond=int(microseconds)
    )
    return parsed.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class WhiteBitExecutionDeal:
    provider_execution_id: str
    provider_order_id: str
    client_order_id: str | None
    market: str
    side: str
    role: str
    quantity: Decimal
    price: Decimal
    deal_value: Decimal
    fee_amount: Decimal
    fee_currency: str
    trade_time: str

    def to_reconciliation_fill(self) -> ProviderFillEvidence:
        return ProviderFillEvidence.create(
            provider_execution_id=self.provider_execution_id,
            client_order_id=self.client_order_id,
            instrument=self.market,
            quantity=self.quantity,
            price=self.price,
            fee_amount=self.fee_amount,
            fee_currency=self.fee_currency,
            trade_time=self.trade_time,
        )


def parse_execution_deal(
    payload: Mapping[str, object],
    *,
    market: str,
) -> WhiteBitExecutionDeal:
    """Normalize one provider deal; the deal id is the unique fill identity."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    required = (
        "id",
        "orderId",
        "time",
        "side",
        "role",
        "amount",
        "price",
        "deal",
        "fee",
        "feeAsset",
    )
    missing = [name for name in required if name not in payload]
    if missing:
        raise WhiteBitAdapterError(
            "execution deal missing required fields: " + ", ".join(missing)
        )

    side = _text(str(payload["side"]), name="side").upper()
    if side not in _SIDES:
        raise WhiteBitAdapterError("execution side must be BUY or SELL")

    raw_role = payload["role"]
    if raw_role == 1 or raw_role == "1":
        role = "MAKER"
    elif raw_role == 2 or raw_role == "2":
        role = "TAKER"
    else:
        raise WhiteBitAdapterError("execution role must be 1 (maker) or 2 (taker)")

    quantity = _decimal(payload["amount"], name="amount", positive=True)
    price = _decimal(payload["price"], name="price", positive=True)
    deal_value = _decimal(payload["deal"], name="deal", positive=True)
    if deal_value != quantity * price:
        raise WhiteBitAdapterError(
            "execution deal value must equal exact amount multiplied by price"
        )
    fee = _decimal(payload["fee"], name="fee")
    if fee < 0:
        raise WhiteBitAdapterError("execution fee cannot be negative")

    raw_client_id = payload.get("clientOrderId")
    client_order_id = (
        None
        if raw_client_id in {None, ""}
        else validate_client_order_id(str(raw_client_id))
    )

    return WhiteBitExecutionDeal(
        provider_execution_id=_text(
            str(payload["id"]),
            name="execution id",
        ),
        provider_order_id=_text(
            str(payload["orderId"]),
            name="order id",
        ),
        client_order_id=client_order_id,
        market=_text(market, name="market").upper(),
        side=side,
        role=role,
        quantity=quantity,
        price=price,
        deal_value=deal_value,
        fee_amount=fee,
        fee_currency=_text(str(payload["feeAsset"]), name="feeAsset").upper(),
        trade_time=_unix_instant(payload["time"], name="time"),
    )


def parse_execution_history(
    records: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
    *,
    market: str,
) -> tuple[WhiteBitExecutionDeal, ...]:
    """Deduplicate exact repeated deal observations and reject contradictions."""
    if not isinstance(records, (list, tuple)):
        raise TypeError("records must be a list or tuple")
    by_id: dict[str, WhiteBitExecutionDeal] = {}
    for payload in records:
        deal = parse_execution_deal(payload, market=market)
        existing = by_id.get(deal.provider_execution_id)
        if existing is not None:
            if existing != deal:
                raise WhiteBitAdapterError(
                    "provider execution id has conflicting observations"
                )
            continue
        by_id[deal.provider_execution_id] = deal
    return tuple(by_id[key] for key in sorted(by_id))


@dataclass(frozen=True)
class WhiteBitPageEvidence:
    offset: int
    limit: int
    record_count: int

    def __post_init__(self) -> None:
        for name in ("offset", "limit", "record_count"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                raise WhiteBitAdapterError(f"{name} must be an integer")
        if self.offset < 0:
            raise WhiteBitAdapterError("offset cannot be negative")
        if self.limit < 1:
            raise WhiteBitAdapterError("limit must be positive")
        if self.record_count < 0 or self.record_count > self.limit:
            raise WhiteBitAdapterError(
                "record_count must be between zero and the page limit"
            )

    @property
    def proves_last_page(self) -> bool:
        return self.record_count < self.limit


class WhiteBitPaginationCoverage:
    """Contiguous offset pagination proof for one provider surface."""

    def __init__(
        self,
        *,
        surface: str,
        maximum_limit: int,
        initial_offset: int = 0,
    ) -> None:
        normalized_surface = _text(surface, name="surface").upper()
        if normalized_surface not in {
            "OPEN_ORDERS",
            "ORDER_HISTORY",
            "EXECUTIONS",
        }:
            raise WhiteBitAdapterError("unsupported pagination surface")
        if (
            not isinstance(maximum_limit, int)
            or isinstance(maximum_limit, bool)
            or maximum_limit < 1
        ):
            raise WhiteBitAdapterError("maximum_limit must be positive")
        if (
            not isinstance(initial_offset, int)
            or isinstance(initial_offset, bool)
            or initial_offset < 0
        ):
            raise WhiteBitAdapterError("initial_offset must be non-negative")
        self.surface = normalized_surface
        self.maximum_limit = maximum_limit
        self.initial_offset = initial_offset
        self._pages: list[WhiteBitPageEvidence] = []

    def add_page(self, page: WhiteBitPageEvidence) -> None:
        if not isinstance(page, WhiteBitPageEvidence):
            raise TypeError("page must be WhiteBitPageEvidence")
        if page.limit > self.maximum_limit:
            raise WhiteBitAdapterError(
                f"page limit cannot exceed {self.maximum_limit}"
            )
        if self.complete:
            raise WhiteBitAdapterError("pagination coverage is already complete")
        expected = (
            self.initial_offset
            if not self._pages
            else self._pages[-1].offset + self._pages[-1].limit
        )
        if page.offset != expected:
            raise WhiteBitAdapterError(
                f"pagination gap: expected offset {expected}"
            )
        self._pages.append(page)

    @property
    def complete(self) -> bool:
        return bool(self._pages and self._pages[-1].proves_last_page)

    @property
    def next_offset(self) -> int:
        if not self._pages:
            return self.initial_offset
        return self._pages[-1].offset + self._pages[-1].limit

    @property
    def pages(self) -> tuple[WhiteBitPageEvidence, ...]:
        return tuple(self._pages)


def order_history_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(
        surface="ORDER_HISTORY",
        maximum_limit=500,
    )


def execution_history_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(
        surface="EXECUTIONS",
        maximum_limit=500,
    )


def open_order_coverage() -> WhiteBitPaginationCoverage:
    return WhiteBitPaginationCoverage(
        surface="OPEN_ORDERS",
        maximum_limit=100,
    )


def _history_window(*, start_unix: int, end_unix: int) -> tuple[int, int]:
    for value, name in ((start_unix, "start_unix"), (end_unix, "end_unix")):
        if not isinstance(value, int) or isinstance(value, bool):
            raise WhiteBitAdapterError(f"{name} must be an integer")
        if value < 0:
            raise WhiteBitAdapterError(f"{name} cannot be negative")
    if end_unix < start_unix:
        raise WhiteBitAdapterError("end_unix must not precede start_unix")
    if end_unix - start_unix > 31 * 24 * 60 * 60:
        raise WhiteBitAdapterError(
            "WhiteBIT history request window cannot exceed 31 days"
        )
    return start_unix, end_unix


def paged_order_history_request(
    *,
    start_unix: int,
    end_unix: int,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    _history_window(start_unix=start_unix, end_unix=end_unix)
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 500:
        raise WhiteBitAdapterError("order-history limit cannot exceed 500")
    body: dict[str, object] = {
        "startDate": start_unix,
        "endDate": end_unix,
        "offset": offset,
        "limit": limit,
    }
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "ORDER_HISTORY",
        "/api/v4/trade-account/order/history",
        body,
    )


def paged_execution_history_request(
    *,
    start_unix: int,
    end_unix: int,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    _history_window(start_unix=start_unix, end_unix=end_unix)
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 500:
        raise WhiteBitAdapterError("execution-history limit cannot exceed 500")
    body: dict[str, object] = {
        "startDate": start_unix,
        "endDate": end_unix,
        "offset": offset,
        "limit": limit,
    }
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "EXECUTIONS",
        "/api/v4/trade-account/executed-history",
        body,
    )


def paged_open_orders_request(
    *,
    offset: int,
    limit: int = 50,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    page = WhiteBitPageEvidence(offset=offset, limit=limit, record_count=0)
    if page.limit > 100:
        raise WhiteBitAdapterError("open-orders limit cannot exceed 100")
    body: dict[str, object] = {"offset": offset, "limit": limit}
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "OPEN_ORDERS",
        "/api/v4/orders",
        body,
    )


_SECRET_FIELDS = frozenset(
    {
        "x-txc-apikey",
        "x-txc-payload",
        "x-txc-signature",
        "api_key",
        "apikey",
        "api_secret",
        "secret",
        "secret_key",
        "signature",
    }
)


def redact_whitebit_debug(value):
    """Recursively remove authentication material from diagnostic values."""
    if isinstance(value, Mapping):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in _SECRET_FIELDS
                else redact_whitebit_debug(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_whitebit_debug(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_whitebit_debug(item) for item in value)
    return value


def _reject_binary_float(value, *, path: str = "parameters") -> None:
    if isinstance(value, float):
        raise WhiteBitAdapterError(f"{path} must not contain binary float")
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_binary_float(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_binary_float(item, path=f"{path}[{index}]")


@dataclass(frozen=True)
class WhiteBitSignedRequest:
    endpoint: str
    body: bytes
    headers: Mapping[str, str]
    nonce: int
    nonce_window: bool

    def safe_debug(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "endpoint": self.endpoint,
                "nonce": self.nonce,
                "nonce_window": self.nonce_window,
                "body_length": len(self.body),
                "headers": redact_whitebit_debug(self.headers),
            }
        )


def sign_private_request(
    *,
    endpoint: str,
    parameters: Mapping[str, object],
    nonce: int,
    api_key: str,
    api_secret: str | bytes,
    nonce_window: bool = False,
    server_time_ms: int | None = None,
) -> WhiteBitSignedRequest:
    """Sign exact request bytes without allocating nonce or performing I/O."""
    path = _text(endpoint, name="endpoint")
    if not path.startswith("/api/v4/"):
        raise WhiteBitAdapterError("endpoint must be an absolute WhiteBIT v4 path")
    if not isinstance(parameters, Mapping):
        raise TypeError("parameters must be a mapping")
    if not isinstance(nonce, int) or isinstance(nonce, bool) or nonce <= 0:
        raise WhiteBitAdapterError("nonce must be a positive integer")
    if type(nonce_window) is not bool:
        raise WhiteBitAdapterError("nonce_window must be boolean")
    if nonce_window:
        if (
            not isinstance(server_time_ms, int)
            or isinstance(server_time_ms, bool)
            or server_time_ms <= 0
        ):
            raise WhiteBitAdapterError(
                "nonceWindow requires positive server_time_ms evidence"
            )
        if abs(nonce - server_time_ms) > 5000:
            raise WhiteBitAdapterError(
                "nonce is outside the WhiteBIT ±5 second nonceWindow"
            )
    elif server_time_ms is not None:
        raise WhiteBitAdapterError(
            "server_time_ms is valid only when nonce_window is enabled"
        )

    key = _text(api_key, name="api_key")
    if isinstance(api_secret, str):
        secret = _text(api_secret, name="api_secret").encode("utf-8")
    elif isinstance(api_secret, bytes) and api_secret:
        secret = api_secret
    else:
        raise WhiteBitAdapterError("api_secret is required")

    body_object = dict(parameters)
    if "request" in body_object and body_object["request"] != path:
        raise WhiteBitAdapterError("request field conflicts with endpoint")
    if "nonce" in body_object and body_object["nonce"] != nonce:
        raise WhiteBitAdapterError("nonce field conflicts with caller-owned nonce")
    if (
        "nonceWindow" in body_object
        and body_object["nonceWindow"] != nonce_window
    ):
        raise WhiteBitAdapterError(
            "nonceWindow field conflicts with requested signing mode"
        )
    body_object["request"] = path
    body_object["nonce"] = nonce
    if nonce_window:
        body_object["nonceWindow"] = True
    else:
        body_object.pop("nonceWindow", None)

    _reject_binary_float(body_object)
    try:
        body = json.dumps(
            body_object,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise WhiteBitAdapterError(
            "private request parameters must be JSON serializable"
        ) from error
    encoded_payload = base64.b64encode(body)
    signature = hmac.new(secret, encoded_payload, hashlib.sha512).hexdigest()
    headers = MappingProxyType(
        {
            "Content-Type": "application/json",
            "X-TXC-APIKEY": key,
            "X-TXC-PAYLOAD": encoded_payload.decode("ascii"),
            "X-TXC-SIGNATURE": signature,
        }
    )
    return WhiteBitSignedRequest(
        endpoint=path,
        body=body,
        headers=headers,
        nonce=nonce,
        nonce_window=nonce_window,
    )


def absence_evidence_from_coverages(
    *,
    order_found: bool,
    open_orders: WhiteBitPaginationCoverage,
    order_history: WhiteBitPaginationCoverage,
    executions: WhiteBitPaginationCoverage,
    activities_complete: bool,
    consistency_horizon_satisfied: bool,
    qualified_exclusion_semantics: bool = False,
) -> WhiteBitAbsenceEvidence:
    """Bind absence semantics to concrete, surface-typed pagination proof."""
    for coverage, expected in (
        (open_orders, "OPEN_ORDERS"),
        (order_history, "ORDER_HISTORY"),
        (executions, "EXECUTIONS"),
    ):
        if not isinstance(coverage, WhiteBitPaginationCoverage):
            raise TypeError(f"{expected} coverage has wrong type")
        if coverage.surface != expected:
            raise WhiteBitAdapterError(
                f"coverage surface mismatch: expected {expected}"
            )
    if type(order_found) is not bool:
        raise TypeError("order_found must be boolean")
    if type(activities_complete) is not bool:
        raise TypeError("activities_complete must be boolean")
    if type(consistency_horizon_satisfied) is not bool:
        raise TypeError("consistency_horizon_satisfied must be boolean")
    if type(qualified_exclusion_semantics) is not bool:
        raise TypeError("qualified_exclusion_semantics must be boolean")
    return WhiteBitAbsenceEvidence(
        order_found=order_found,
        open_orders_complete=open_orders.complete,
        order_history_complete=order_history.complete,
        executions_complete=executions.complete,
        activities_complete=activities_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        qualified_exclusion_semantics=qualified_exclusion_semantics,
    )


@dataclass(frozen=True)
class WhiteBitPositionObservation:
    position_id: str
    market: str
    position_side: str
    amount: Decimal
    base_price: Decimal
    realized_pnl: Decimal
    margin: Decimal
    free_margin: Decimal
    funding: Decimal
    unrealized_funding: Decimal
    unrealized_pnl: Decimal | None
    liquidation_price: Decimal | None
    liquidation_state: str | None
    opened_at: str
    modified_at: str


def parse_open_position(payload: Mapping[str, object]) -> WhiteBitPositionObservation:
    """Normalize one collateral/futures position without collapsing hedge semantics."""
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    required = {
        "positionId",
        "market",
        "openDate",
        "modifyDate",
        "amount",
        "basePrice",
        "pnl",
        "margin",
        "freeMargin",
        "funding",
        "unrealizedFunding",
        "positionSide",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise WhiteBitAdapterError(
            "open position missing required fields: " + ", ".join(missing)
        )

    amount = _decimal(payload["amount"], name="amount")
    if amount < 0:
        raise WhiteBitAdapterError("position amount cannot be negative")
    base_price = _decimal(payload["basePrice"], name="basePrice", positive=True)
    margin = _decimal(payload["margin"], name="margin")
    free_margin = _decimal(payload["freeMargin"], name="freeMargin")
    if margin < 0 or free_margin < 0:
        raise WhiteBitAdapterError("position margin values cannot be negative")
    side = _text(str(payload["positionSide"]), name="positionSide").upper()
    if side not in {"LONG", "SHORT", "BOTH"}:
        raise WhiteBitAdapterError("unsupported positionSide")

    liquidation_price_raw = payload.get("liquidationPrice")
    liquidation_price = (
        None
        if liquidation_price_raw is None
        else _decimal(
            liquidation_price_raw,
            name="liquidationPrice",
            positive=True,
        )
    )
    liquidation_state_raw = payload.get("liquidationState")
    liquidation_state = (
        None
        if liquidation_state_raw is None
        else _text(
            str(liquidation_state_raw),
            name="liquidationState",
        ).lower()
    )
    if liquidation_state not in {None, "margin_call", "liquidation"}:
        raise WhiteBitAdapterError("unsupported liquidationState")

    unrealized_pnl_raw = payload.get("unrealizedPnl")
    unrealized_pnl = (
        None
        if unrealized_pnl_raw is None
        else _decimal(unrealized_pnl_raw, name="unrealizedPnl")
    )

    return WhiteBitPositionObservation(
        position_id=_text(str(payload["positionId"]), name="positionId"),
        market=_text(str(payload["market"]), name="market").upper(),
        position_side=side,
        amount=amount,
        base_price=base_price,
        realized_pnl=_decimal(payload["pnl"], name="pnl"),
        margin=margin,
        free_margin=free_margin,
        funding=_decimal(payload["funding"], name="funding"),
        unrealized_funding=_decimal(
            payload["unrealizedFunding"],
            name="unrealizedFunding",
        ),
        unrealized_pnl=unrealized_pnl,
        liquidation_price=liquidation_price,
        liquidation_state=liquidation_state,
        opened_at=_unix_instant(payload["openDate"], name="openDate"),
        modified_at=_unix_instant(payload["modifyDate"], name="modifyDate"),
    )


def parse_open_positions(
    records: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
) -> tuple[WhiteBitPositionObservation, ...]:
    if not isinstance(records, (list, tuple)):
        raise TypeError("records must be a list or tuple")
    by_id: dict[str, WhiteBitPositionObservation] = {}
    for payload in records:
        position = parse_open_position(payload)
        existing = by_id.get(position.position_id)
        if existing is not None:
            if existing != position:
                raise WhiteBitAdapterError(
                    "provider position id has conflicting observations"
                )
            continue
        by_id[position.position_id] = position
    return tuple(by_id[key] for key in sorted(by_id))


def open_positions_request(*, market: str | None = None) -> WhiteBitLookupRequest:
    body: dict[str, object] = {}
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "OPEN_POSITIONS",
        "/api/v4/collateral-account/positions/open",
        body,
    )


def hedge_mode_request() -> WhiteBitLookupRequest:
    return WhiteBitLookupRequest(
        "HEDGE_MODE",
        "/api/v4/collateral-account/hedge-mode",
        {},
    )


def parse_hedge_mode(payload: Mapping[str, object]) -> bool:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    if set(payload) != {"hedgeMode"}:
        raise WhiteBitAdapterError(
            "hedge-mode response must contain only hedgeMode"
        )
    value = payload["hedgeMode"]
    if type(value) is not bool:
        raise WhiteBitAdapterError("hedgeMode must be boolean")
    return value


def signed_position_quantities(
    positions: list[WhiteBitPositionObservation]
    | tuple[WhiteBitPositionObservation, ...],
    *,
    hedge_mode: bool,
) -> Mapping[str, Decimal]:
    """Project provider positions only when side semantics are unambiguous."""
    if type(hedge_mode) is not bool:
        raise TypeError("hedge_mode must be boolean")
    totals: dict[str, Decimal] = {}
    for position in positions:
        if not isinstance(position, WhiteBitPositionObservation):
            raise TypeError("positions contain invalid observation")
        if hedge_mode:
            if position.position_side == "BOTH":
                raise WhiteBitAdapterError(
                    "hedge-mode position cannot use ambiguous BOTH side"
                )
            signed = (
                position.amount
                if position.position_side == "LONG"
                else -position.amount
            )
        else:
            if position.position_side != "BOTH":
                raise WhiteBitAdapterError(
                    "one-way position must use BOTH side before signed projection"
                )
            raise WhiteBitAdapterError(
                "WhiteBIT one-way BOTH amount sign semantics are not qualified"
            )
        totals[position.market] = totals.get(position.market, Decimal("0")) + signed
    return MappingProxyType(dict(sorted(totals.items())))


@dataclass(frozen=True)
class WhiteBitCollateralBalanceObservation:
    """Collateral account balance without treating borrow capacity as owned cash."""

    asset: str
    balance: Decimal
    borrowed: Decimal
    available_without_borrow: Decimal
    available_with_borrow: Decimal

    @classmethod
    def from_provider(
        cls,
        payload: Mapping[str, object],
    ) -> "WhiteBitCollateralBalanceObservation":
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        required = {
            "asset",
            "balance",
            "borrow",
            "availableWithoutBorrow",
            "availableWithBorrow",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise WhiteBitAdapterError(
                "collateral balance missing required fields: "
                + ", ".join(missing)
            )
        balance = _decimal(payload["balance"], name="balance")
        borrowed = _decimal(payload["borrow"], name="borrow")
        available_without = _decimal(
            payload["availableWithoutBorrow"],
            name="availableWithoutBorrow",
        )
        available_with = _decimal(
            payload["availableWithBorrow"],
            name="availableWithBorrow",
        )
        if borrowed < 0:
            raise WhiteBitAdapterError("borrow cannot be negative")
        if available_with < available_without:
            raise WhiteBitAdapterError(
                "availableWithBorrow cannot be below availableWithoutBorrow"
            )
        return cls(
            asset=_text(str(payload["asset"]), name="asset").upper(),
            balance=balance,
            borrowed=borrowed,
            available_without_borrow=available_without,
            available_with_borrow=available_with,
        )


def collateral_balance_request(
    *,
    asset: str | None = None,
) -> WhiteBitLookupRequest:
    body: dict[str, object] = {}
    if asset is not None:
        body["ticker"] = _text(asset, name="asset").upper()
    return WhiteBitLookupRequest(
        "COLLATERAL_BALANCE",
        "/api/v4/collateral-account/balance-summary",
        body,
    )


def parse_collateral_balances(
    records: list[Mapping[str, object]] | tuple[Mapping[str, object], ...],
) -> tuple[WhiteBitCollateralBalanceObservation, ...]:
    if not isinstance(records, (list, tuple)):
        raise TypeError("records must be a list or tuple")
    by_asset: dict[str, WhiteBitCollateralBalanceObservation] = {}
    for payload in records:
        observation = WhiteBitCollateralBalanceObservation.from_provider(payload)
        existing = by_asset.get(observation.asset)
        if existing is not None:
            if existing != observation:
                raise WhiteBitAdapterError(
                    "collateral asset has conflicting balance observations"
                )
            continue
        by_asset[observation.asset] = observation
    return tuple(by_asset[key] for key in sorted(by_asset))


def provider_collateral_cash(
    observations: list[WhiteBitCollateralBalanceObservation]
    | tuple[WhiteBitCollateralBalanceObservation, ...],
) -> Mapping[str, Decimal]:
    """Return provider cash/equity input using balance, never borrow capacity."""
    cash: dict[str, Decimal] = {}
    for observation in observations:
        if not isinstance(observation, WhiteBitCollateralBalanceObservation):
            raise TypeError(
                "observations must contain WhiteBitCollateralBalanceObservation"
            )
        if observation.asset in cash:
            raise WhiteBitAdapterError(
                "duplicate collateral asset after normalization"
            )
        cash[observation.asset] = observation.balance
    return MappingProxyType(dict(sorted(cash.items())))


def provider_collateral_borrow(
    observations: list[WhiteBitCollateralBalanceObservation]
    | tuple[WhiteBitCollateralBalanceObservation, ...],
) -> Mapping[str, Decimal]:
    """Return liabilities separately from balance and available buying power."""
    borrowed: dict[str, Decimal] = {}
    for observation in observations:
        if not isinstance(observation, WhiteBitCollateralBalanceObservation):
            raise TypeError(
                "observations must contain WhiteBitCollateralBalanceObservation"
            )
        if observation.asset in borrowed:
            raise WhiteBitAdapterError(
                "duplicate collateral asset after normalization"
            )
        borrowed[observation.asset] = observation.borrowed
    return MappingProxyType(dict(sorted(borrowed.items())))


@dataclass(frozen=True)
class WhiteBitSpotBalanceObservation:
    asset: str
    available: Decimal
    frozen: Decimal

    @property
    def total(self) -> Decimal:
        return self.available + self.frozen

    @classmethod
    def create(
        cls,
        *,
        asset: str,
        payload: Mapping[str, object],
    ) -> "WhiteBitSpotBalanceObservation":
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if set(payload) != {"available", "freeze"}:
            raise WhiteBitAdapterError(
                "spot balance record must contain available and freeze"
            )
        available = _decimal(payload["available"], name="available")
        frozen = _decimal(payload["freeze"], name="freeze")
        if available < 0 or frozen < 0:
            raise WhiteBitAdapterError(
                "spot available and frozen balances cannot be negative"
            )
        return cls(
            asset=_text(asset, name="asset").upper(),
            available=available,
            frozen=frozen,
        )


def spot_balance_request(
    *,
    asset: str | None = None,
) -> WhiteBitLookupRequest:
    body: dict[str, object] = {}
    if asset is not None:
        body["ticker"] = _text(asset, name="asset").upper()
    return WhiteBitLookupRequest(
        "SPOT_BALANCE",
        "/api/v4/trade-account/balance",
        body,
    )


def parse_spot_balances(
    payload: Mapping[str, object],
) -> tuple[WhiteBitSpotBalanceObservation, ...]:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    by_asset: dict[str, WhiteBitSpotBalanceObservation] = {}
    for raw_asset, record in payload.items():
        observation = WhiteBitSpotBalanceObservation.create(
            asset=str(raw_asset),
            payload=record,
        )
        if observation.asset in by_asset:
            raise WhiteBitAdapterError(
                "spot balance contains duplicate normalized asset"
            )
        by_asset[observation.asset] = observation
    return tuple(by_asset[key] for key in sorted(by_asset))


def provider_spot_cash(
    observations: list[WhiteBitSpotBalanceObservation]
    | tuple[WhiteBitSpotBalanceObservation, ...],
) -> Mapping[str, Decimal]:
    """Provider-owned spot cash includes both free and order-frozen holdings."""
    cash: dict[str, Decimal] = {}
    for observation in observations:
        if not isinstance(observation, WhiteBitSpotBalanceObservation):
            raise TypeError(
                "observations must contain WhiteBitSpotBalanceObservation"
            )
        if observation.asset in cash:
            raise WhiteBitAdapterError(
                "duplicate spot asset after normalization"
            )
        cash[observation.asset] = observation.total
    return MappingProxyType(dict(sorted(cash.items())))


WHITEBIT_WEBSOCKET_ENDPOINT = "wss://wss.whitebit.com/ws"
WHITEBIT_DEPRECATED_WEBSOCKET_ENDPOINT = "wss://api.whitebit.com/ws"


@dataclass(frozen=True)
class WhiteBitStreamRecoveryPolicy:
    channel: str
    state_model: str
    query_method: str | None
    subscribe_method: str
    reconnect_action: str
    requires_backfill: bool
    full_snapshot_on_subscribe: bool

    def __post_init__(self) -> None:
        channel = _text(self.channel, name="channel").upper()
        object.__setattr__(self, "channel", channel)
        state_model = _text(self.state_model, name="state_model").upper()
        if state_model not in {
            "INCREMENTAL_DELTA",
            "PERIODIC_FULL_SNAPSHOT",
            "EVENT_STREAM",
        }:
            raise WhiteBitAdapterError("unsupported stream state model")
        object.__setattr__(self, "state_model", state_model)
        if self.query_method is not None:
            object.__setattr__(
                self,
                "query_method",
                _text(self.query_method, name="query_method"),
            )
        object.__setattr__(
            self,
            "subscribe_method",
            _text(self.subscribe_method, name="subscribe_method"),
        )
        object.__setattr__(
            self,
            "reconnect_action",
            _text(self.reconnect_action, name="reconnect_action"),
        )
        if type(self.requires_backfill) is not bool:
            raise WhiteBitAdapterError("requires_backfill must be boolean")
        if type(self.full_snapshot_on_subscribe) is not bool:
            raise WhiteBitAdapterError(
                "full_snapshot_on_subscribe must be boolean"
            )


_WHITEBIT_STREAM_RECOVERY = MappingProxyType(
    {
        "BALANCE_SPOT": WhiteBitStreamRecoveryPolicy(
            channel="BALANCE_SPOT",
            state_model="INCREMENTAL_DELTA",
            query_method="balanceSpot_request",
            subscribe_method="balanceSpot_subscribe",
            reconnect_action="QUERY_THEN_SUBSCRIBE",
            requires_backfill=True,
            full_snapshot_on_subscribe=False,
        ),
        "POSITIONS": WhiteBitStreamRecoveryPolicy(
            channel="POSITIONS",
            state_model="PERIODIC_FULL_SNAPSHOT",
            query_method=None,
            subscribe_method="positions_subscribe",
            reconnect_action="SUBSCRIBE_AND_WAIT_FOR_FULL_SNAPSHOT",
            requires_backfill=False,
            full_snapshot_on_subscribe=True,
        ),
        "DEALS": WhiteBitStreamRecoveryPolicy(
            channel="DEALS",
            state_model="EVENT_STREAM",
            query_method="deals_request",
            subscribe_method="deals_subscribe",
            reconnect_action="BACKFILL_AND_RESUBSCRIBE",
            requires_backfill=True,
            full_snapshot_on_subscribe=False,
        ),
        "ORDERS_EXECUTED": WhiteBitStreamRecoveryPolicy(
            channel="ORDERS_EXECUTED",
            state_model="EVENT_STREAM",
            query_method="ordersExecuted_request",
            subscribe_method="ordersExecuted_subscribe",
            reconnect_action="BACKFILL_AND_RESUBSCRIBE",
            requires_backfill=True,
            full_snapshot_on_subscribe=False,
        ),
    }
)


def websocket_recovery_policy(channel: str) -> WhiteBitStreamRecoveryPolicy:
    normalized = _text(channel, name="channel").upper()
    try:
        return _WHITEBIT_STREAM_RECOVERY[normalized]
    except KeyError as error:
        raise WhiteBitAdapterError(
            f"unqualified WhiteBIT stream channel: {normalized}"
        ) from error


def validate_websocket_endpoint(endpoint: str) -> str:
    value = _text(endpoint, name="endpoint")
    if value == WHITEBIT_DEPRECATED_WEBSOCKET_ENDPOINT:
        raise WhiteBitAdapterError(
            "deprecated WhiteBIT WebSocket host is forbidden"
        )
    if value != WHITEBIT_WEBSOCKET_ENDPOINT:
        raise WhiteBitAdapterError(
            "unqualified WhiteBIT WebSocket endpoint"
        )
    return value


@dataclass(frozen=True)
class WhiteBitRecoveryCheckpoint:
    channel: str
    baseline_observed: bool
    subscription_confirmed: bool
    backfill_complete: bool
    full_snapshot_observed: bool

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "channel",
            _text(self.channel, name="channel").upper(),
        )
        for field in (
            "baseline_observed",
            "subscription_confirmed",
            "backfill_complete",
            "full_snapshot_observed",
        ):
            if type(getattr(self, field)) is not bool:
                raise WhiteBitAdapterError(f"{field} must be boolean")

    @property
    def recovered(self) -> bool:
        policy = websocket_recovery_policy(self.channel)
        if not self.subscription_confirmed:
            return False
        if policy.full_snapshot_on_subscribe:
            return self.full_snapshot_observed
        if policy.requires_backfill:
            return self.baseline_observed and self.backfill_complete
        return self.baseline_observed


@dataclass(frozen=True)
class WhiteBitFeeSchedule:
    evidence_id: str
    observed_at: datetime
    spot_taker_percent: Decimal
    spot_maker_percent: Decimal
    futures_taker_percent: Decimal
    futures_maker_percent: Decimal
    spot_rpi_maker_premium_percent: Decimal | None
    futures_rpi_maker_premium_percent: Decimal | None
    custom_fee_percent: Mapping[str, Mapping[str, Decimal]]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_id",
            _text(self.evidence_id, name="evidence_id"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _instant(self.observed_at, name="observed_at"),
        )
        custom_copy = {
            market: MappingProxyType(dict(rates))
            for market, rates in self.custom_fee_percent.items()
        }
        object.__setattr__(
            self,
            "custom_fee_percent",
            MappingProxyType(custom_copy),
        )

    def effective_percent(
        self,
        *,
        product_family: str,
        role: str,
        market: str,
        rpi: bool = False,
    ) -> Decimal:
        product = _text(product_family, name="product_family").upper()
        normalized_role = _text(role, name="role").upper()
        normalized_market = _text(market, name="market").upper()
        if product not in {"SPOT", "COLLATERAL", "FUTURES"}:
            raise WhiteBitAdapterError("unsupported fee product family")
        if normalized_role not in {"MAKER", "TAKER"}:
            raise WhiteBitAdapterError("role must be MAKER or TAKER")
        if type(rpi) is not bool:
            raise WhiteBitAdapterError("rpi must be boolean")
        if rpi and normalized_role != "MAKER":
            raise WhiteBitAdapterError("RPI premium is maker-only")

        if product == "FUTURES":
            base = (
                self.futures_maker_percent
                if normalized_role == "MAKER"
                else self.futures_taker_percent
            )
            premium = self.futures_rpi_maker_premium_percent
        else:
            base = (
                self.spot_maker_percent
                if normalized_role == "MAKER"
                else self.spot_taker_percent
            )
            premium = self.spot_rpi_maker_premium_percent

        override = self.custom_fee_percent.get(normalized_market)
        if override is not None:
            base = override[normalized_role.lower()]

        if rpi:
            if premium is None:
                raise WhiteBitAdapterError(
                    "RPI fee premium is not configured for this account"
                )
            base += premium
        return base

    def effective_fraction(self, **kwargs) -> Decimal:
        return self.effective_percent(**kwargs) / Decimal("100")


def _fee_percent(value, *, name: str, nullable: bool = False) -> Decimal | None:
    if value is None and nullable:
        return None
    result = _decimal(value, name=name)
    if result < 0 or result > 100:
        raise WhiteBitAdapterError(
            f"{name} must be a percentage between 0 and 100"
        )
    return result


def parse_fee_schedule(
    payload: Mapping[str, object],
    *,
    evidence_id: str,
    observed_at: datetime,
) -> WhiteBitFeeSchedule:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    required = {
        "error",
        "taker",
        "maker",
        "futures_taker",
        "futures_maker",
        "rpi_maker_fee_premium",
        "futures_rpi_maker_fee_premium",
        "custom_fee",
    }
    missing = sorted(required - set(payload))
    if missing:
        raise WhiteBitAdapterError(
            "market fee response missing required fields: " + ", ".join(missing)
        )
    if payload["error"] is not None:
        raise WhiteBitAdapterError(
            "market fee response contains provider error"
        )
    custom_raw = payload["custom_fee"]
    if not isinstance(custom_raw, Mapping):
        raise WhiteBitAdapterError("custom_fee must be an object")
    custom: dict[str, Mapping[str, Decimal]] = {}
    for raw_market, raw_rates in custom_raw.items():
        market = _text(str(raw_market), name="custom fee market").upper()
        if not isinstance(raw_rates, Mapping):
            raise WhiteBitAdapterError(
                "custom fee market value must be an object"
            )
        if set(raw_rates) != {"maker", "taker"}:
            raise WhiteBitAdapterError(
                "custom fee must contain exactly maker and taker"
            )
        if market in custom:
            raise WhiteBitAdapterError(
                "duplicate normalized custom fee market"
            )
        custom[market] = {
            "maker": _fee_percent(
                raw_rates["maker"],
                name=f"{market}.maker",
            ),
            "taker": _fee_percent(
                raw_rates["taker"],
                name=f"{market}.taker",
            ),
        }

    return WhiteBitFeeSchedule(
        evidence_id=evidence_id,
        observed_at=observed_at,
        spot_taker_percent=_fee_percent(
            payload["taker"],
            name="taker",
        ),
        spot_maker_percent=_fee_percent(
            payload["maker"],
            name="maker",
        ),
        futures_taker_percent=_fee_percent(
            payload["futures_taker"],
            name="futures_taker",
        ),
        futures_maker_percent=_fee_percent(
            payload["futures_maker"],
            name="futures_maker",
        ),
        spot_rpi_maker_premium_percent=_fee_percent(
            payload["rpi_maker_fee_premium"],
            name="rpi_maker_fee_premium",
            nullable=True,
        ),
        futures_rpi_maker_premium_percent=_fee_percent(
            payload["futures_rpi_maker_fee_premium"],
            name="futures_rpi_maker_fee_premium",
            nullable=True,
        ),
        custom_fee_percent=custom,
    )


def market_fee_request() -> WhiteBitLookupRequest:
    return WhiteBitLookupRequest(
        "MARKET_FEES",
        "/api/v4/market/fee",
        {},
    )


@dataclass(frozen=True)
class WhiteBitFundingObservation:
    market: str
    funding_time: str
    funding_rate: Decimal
    funding_amount: Decimal
    position_amount: Decimal
    settlement_price: Decimal
    rate_calculated_time: str
    observation_fingerprint: str
    economic_event_identity_authoritative: bool = False

    @classmethod
    def from_provider(
        cls,
        payload: Mapping[str, object],
    ) -> "WhiteBitFundingObservation":
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        required = {
            "market",
            "fundingTime",
            "fundingRate",
            "fundingAmount",
            "positionAmount",
            "settlementPrice",
            "rateCalculatedTime",
        }
        missing = sorted(required - set(payload))
        if missing:
            raise WhiteBitAdapterError(
                "funding observation missing required fields: "
                + ", ".join(missing)
            )

        market = _text(str(payload["market"]), name="market").upper()
        funding_time = _unix_instant(
            payload["fundingTime"],
            name="fundingTime",
        )
        rate_calculated_time = _unix_instant(
            payload["rateCalculatedTime"],
            name="rateCalculatedTime",
        )
        if rate_calculated_time > funding_time:
            raise WhiteBitAdapterError(
                "rateCalculatedTime cannot follow fundingTime"
            )
        funding_rate = _decimal(
            payload["fundingRate"],
            name="fundingRate",
        )
        funding_amount = _decimal(
            payload["fundingAmount"],
            name="fundingAmount",
        )
        position_amount = _decimal(
            payload["positionAmount"],
            name="positionAmount",
        )
        settlement_price = _decimal(
            payload["settlementPrice"],
            name="settlementPrice",
            positive=True,
        )

        canonical = {
            "market": market,
            "fundingTime": funding_time,
            "fundingRate": str(funding_rate),
            "fundingAmount": str(funding_amount),
            "positionAmount": str(position_amount),
            "settlementPrice": str(settlement_price),
            "rateCalculatedTime": rate_calculated_time,
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                canonical,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            market=market,
            funding_time=funding_time,
            funding_rate=funding_rate,
            funding_amount=funding_amount,
            position_amount=position_amount,
            settlement_price=settlement_price,
            rate_calculated_time=rate_calculated_time,
            observation_fingerprint=f"sha256:{fingerprint}",
            economic_event_identity_authoritative=False,
        )


@dataclass(frozen=True)
class WhiteBitFundingPage:
    records: tuple[WhiteBitFundingObservation, ...]
    offset: int
    limit: int

    @property
    def short_page(self) -> bool:
        return len(self.records) < self.limit


def funding_history_request(
    *,
    offset: int = 0,
    limit: int = 100,
    market: str | None = None,
) -> WhiteBitLookupRequest:
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise WhiteBitAdapterError("offset must be a non-negative integer")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise WhiteBitAdapterError("limit must be a positive integer")
    body: dict[str, object] = {"offset": offset, "limit": limit}
    if market is not None:
        body["market"] = _text(market, name="market").upper()
    return WhiteBitLookupRequest(
        "FUNDING_HISTORY",
        "/api/v4/collateral-account/funding-history",
        body,
    )


def parse_funding_page(
    payload: Mapping[str, object],
    *,
    expected_offset: int,
    expected_limit: int,
) -> WhiteBitFundingPage:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    for field in ("records", "offset", "limit"):
        if field not in payload:
            raise WhiteBitAdapterError(
                f"funding page missing required field: {field}"
            )
    if payload["offset"] != expected_offset or payload["limit"] != expected_limit:
        raise WhiteBitAdapterError(
            "funding page pagination does not match requested offset/limit"
        )
    records_raw = payload["records"]
    if not isinstance(records_raw, list):
        raise WhiteBitAdapterError("funding records must be a list")
    if len(records_raw) > expected_limit:
        raise WhiteBitAdapterError(
            "funding page contains more records than requested limit"
        )
    records = tuple(
        WhiteBitFundingObservation.from_provider(item)
        for item in records_raw
    )
    return WhiteBitFundingPage(
        records=records,
        offset=expected_offset,
        limit=expected_limit,
    )
