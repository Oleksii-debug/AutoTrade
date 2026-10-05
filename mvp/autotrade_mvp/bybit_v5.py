"""Pure Bybit V5 contract adapter foundation.

This module deliberately performs no networking, stores no credentials and
cannot grant trading authority. It translates already-authorized canonical
values and recorded Bybit responses into AutoTrade provider/reconciliation
contracts. Live, demo and test environments remain unqualified until exact
adapter evidence satisfies the canonical provider qualification authority.
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
from weakref import ref as weakref_ref

from .capabilities import CapabilityError, CapabilitySnapshot
from .instruments import (
    InstrumentRegistry,
    InstrumentRegistryError,
    authenticated_price_semantics_digest,
)
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
    if capability.provider_environment != provider_env:
        raise ProviderCoreError(
            "capability provider environment does not match target Bybit provider environment"
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
    if family == "OPTIONS":
        raise ProviderCoreError(
            "Bybit option payload serialization requires dedicated option semantics"
        )

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
    price_rule_instrument_id: str | None = None
    price_rule_instrument_version: int | None = None
    price_semantics_digest: str | None = None
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
        price_authority = (
            self.price_rule_instrument_id,
            self.price_rule_instrument_version,
            self.price_semantics_digest,
        )
        if any(value is not None for value in price_authority):
            if any(value is None for value in price_authority):
                raise ProviderCoreError(
                    "Bybit prepared price semantics authority must be complete"
                )
            raw_instrument_id = self.price_rule_instrument_id
            try:
                canonical_instrument_id = str(UUID(raw_instrument_id))
            except (ValueError, TypeError, AttributeError) as error:
                raise ProviderCoreError(
                    "Bybit prepared price-rule instrument_id must be a UUID"
                ) from error
            if raw_instrument_id != canonical_instrument_id:
                raise ProviderCoreError(
                    "Bybit prepared price-rule instrument_id must be canonical"
                )
            if (
                type(self.price_rule_instrument_version) is not int
                or self.price_rule_instrument_version < 1
            ):
                raise ProviderCoreError(
                    "Bybit prepared price-rule instrument version must be positive"
                )
            if (
                type(self.price_semantics_digest) is not str
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.price_semantics_digest)
                is None
            ):
                raise ProviderCoreError(
                    "Bybit prepared price semantics digest must be canonical SHA-256"
                )
            object.__setattr__(
                self,
                "price_rule_instrument_id",
                canonical_instrument_id,
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
    price_semantics_registry: InstrumentRegistry | None = None,
    price_semantics_artifact_store: object | None = None,
    price_semantics_instrument_version: str | None = None,
    entity_policy_id: str | None = None,
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
    if capability.provider_environment != provider_env:
        raise ProviderCoreError(
            "capability provider environment does not match target Bybit provider environment"
        )
    point = (
        at.astimezone(timezone.utc)
        if isinstance(at, datetime) and at.tzinfo is not None
        else None
    )
    if point is None:
        raise ProviderCoreError("at must be timezone-aware")
    normalized_family = _text(product_family, name="product_family").upper()
    if normalized_family == "MARGIN":
        raise ProviderCoreError(
            "Bybit MARGIN canonical preparation requires dedicated "
            "spot-margin borrow/collateral authority"
        )
    if normalized_family == "OPTIONS":
        raise ProviderCoreError(
            "Bybit OPTIONS canonical preparation requires dedicated "
            "option capability/payoff authority"
        )
    normalized_type = _text(order_type, name="order_type").upper()
    normalized_tif = _text(time_in_force, name="time_in_force").upper()
    price_rule_instrument_id = None
    price_rule_instrument_version = None
    price_semantics_digest = None
    price_authority_inputs = (
        price_semantics_registry,
        price_semantics_artifact_store,
        price_semantics_instrument_version,
        entity_policy_id,
    )
    if any(value is not None for value in price_authority_inputs):
        if any(value is None for value in price_authority_inputs):
            raise ProviderCoreError(
                "Bybit authenticated price semantics authority must be complete"
            )
        if type(price_semantics_registry) is not InstrumentRegistry:
            raise TypeError(
                "price_semantics_registry must be exact InstrumentRegistry"
            )
        raw_instrument_version = _text(
            price_semantics_instrument_version,
            name="price_semantics_instrument_version",
        )
        parts = raw_instrument_version.split("@")
        if len(parts) != 2:
            raise ProviderCoreError(
                "price_semantics_instrument_version must be canonical instrument_id@version"
            )
        try:
            canonical_instrument_id = str(UUID(parts[0]))
            canonical_version = int(parts[1])
        except (ValueError, TypeError, AttributeError) as error:
            raise ProviderCoreError(
                "price_semantics_instrument_version must be canonical instrument_id@version"
            ) from error
        if (
            canonical_version < 1
            or parts[1] != str(canonical_version)
            or raw_instrument_version
            != f"{canonical_instrument_id}@{canonical_version}"
        ):
            raise ProviderCoreError(
                "price_semantics_instrument_version must be canonical instrument_id@version"
            )
        provider_symbol = _text(symbol, name="symbol")
        try:
            price_semantics_digest = authenticated_price_semantics_digest(
                price_semantics_registry,
                price_semantics_artifact_store,
                instrument_version=raw_instrument_version,
                evaluated_at=point,
                provider_id="BYBIT",
                provider_symbol=provider_symbol,
                entity_policy_id=entity_policy_id,
                side=side,
                order_type=normalized_type,
                price=price,
            )
        except (InstrumentRegistryError, TypeError, ValueError) as error:
            raise ProviderCoreError(
                "Bybit order violates authenticated instrument price semantics"
            ) from error
        price_rule_instrument_id = canonical_instrument_id
        price_rule_instrument_version = canonical_version
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
        product_family=normalized_family,
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
        price_rule_instrument_id=price_rule_instrument_id,
        price_rule_instrument_version=price_rule_instrument_version,
        price_semantics_digest=price_semantics_digest,
        _factory_token=_BYBIT_PREPARED_SUBMISSION_TOKEN,
    )


def _install_bybit_prepared_submission_authority(
    builder,
):
    """Wrap canonical preparation with closure-owned issued-object provenance."""

    prepared_type = BybitPreparedSubmission
    capability_type = CapabilitySnapshot
    datetime_type = datetime
    timezone_type = timezone
    instrument_registry_type = InstrumentRegistry
    instrument_registry_error_type = InstrumentRegistryError
    uuid_type = UUID
    canonical_str = str
    canonical_int = int
    canonical_any = any
    type_error_type = TypeError
    value_error_type = ValueError
    price_semantics_authority = authenticated_price_semantics_digest
    price_semantics_authority_code = price_semantics_authority.__code__
    regex_module = re
    regex_fullmatch = re.fullmatch
    regex_fullmatch_code = regex_fullmatch.__code__
    prepared_ref = weakref_ref
    prepared_init = prepared_type.__init__
    prepared_init_code = prepared_init.__code__
    prepared_post_init = prepared_type.__post_init__
    prepared_post_init_code = prepared_post_init.__code__
    builder_code = builder.__code__
    canonical_build_order_payload = build_order_payload
    canonical_build_order_payload_code = build_order_payload.__code__
    canonical_text = _text
    canonical_text_code = _text.__code__
    canonical_decimal_text = _decimal_text
    canonical_decimal_text_code = _decimal_text.__code__
    canonical_client_order_id = _client_order_id
    canonical_client_order_id_code = _client_order_id.__code__
    canonical_position_idx = _position_idx_from_capability
    canonical_position_idx_code = _position_idx_from_capability.__code__
    canonical_capability_admits = capability_type.admits
    canonical_capability_admits_code = capability_type.admits.__code__
    error_type = ProviderCoreError
    canonical_type = type
    canonical_id = id
    canonical_tuple = tuple
    canonical_getattr = getattr
    canonical_isinstance = isinstance
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    attribute_error_type = AttributeError
    mapping_proxy_type = MappingProxyType

    bindings: dict[int, tuple[object, tuple[object, ...]]] = {}

    def authority_changed():
        raise error_type("Bybit prepared submission authority changed")

    def implementation_changed():
        if (
            ProviderCoreError is not error_type
            or type is not canonical_type
            or id is not canonical_id
            or tuple is not canonical_tuple
            or getattr is not canonical_getattr
            or isinstance is not canonical_isinstance
            or object is not canonical_object
            or AttributeError is not attribute_error_type
            or MappingProxyType is not mapping_proxy_type
            or datetime is not datetime_type
            or timezone is not timezone_type
            or CapabilitySnapshot is not capability_type
            or BybitPreparedSubmission is not prepared_type
            or InstrumentRegistry is not instrument_registry_type
            or InstrumentRegistryError is not instrument_registry_error_type
            or UUID is not uuid_type
            or str is not canonical_str
            or int is not canonical_int
            or any is not canonical_any
            or TypeError is not type_error_type
            or ValueError is not value_error_type
            or re is not regex_module
            or regex_module.fullmatch is not regex_fullmatch
            or canonical_getattr(regex_fullmatch, "__code__", None)
            is not regex_fullmatch_code
            or authenticated_price_semantics_digest is not price_semantics_authority
            or canonical_getattr(price_semantics_authority, "__code__", None)
            is not price_semantics_authority_code
            or prepared_type.__init__ is not prepared_init
            or canonical_getattr(prepared_init, "__code__", None)
            is not prepared_init_code
            or prepared_type.__post_init__ is not prepared_post_init
            or canonical_getattr(prepared_post_init, "__code__", None)
            is not prepared_post_init_code
            or canonical_getattr(builder, "__code__", None) is not builder_code
            or build_order_payload is not canonical_build_order_payload
            or canonical_getattr(
                canonical_build_order_payload,
                "__code__",
                None,
            )
            is not canonical_build_order_payload_code
            or _text is not canonical_text
            or canonical_getattr(canonical_text, "__code__", None)
            is not canonical_text_code
            or _decimal_text is not canonical_decimal_text
            or canonical_getattr(canonical_decimal_text, "__code__", None)
            is not canonical_decimal_text_code
            or _client_order_id is not canonical_client_order_id
            or canonical_getattr(canonical_client_order_id, "__code__", None)
            is not canonical_client_order_id_code
            or _position_idx_from_capability is not canonical_position_idx
            or canonical_getattr(canonical_position_idx, "__code__", None)
            is not canonical_position_idx_code
            or capability_type.admits is not canonical_capability_admits
            or canonical_getattr(canonical_capability_admits, "__code__", None)
            is not canonical_capability_admits_code
        ):
            authority_changed()

    def snapshot(value):
        try:
            return (
                object_getattribute(value, "endpoint"),
                object_getattribute(value, "body"),
                object_getattribute(value, "account_id"),
                object_getattribute(value, "environment"),
                object_getattribute(value, "provider_environment"),
                object_getattribute(value, "capability_snapshot_id"),
                object_getattribute(value, "entity_id"),
                object_getattribute(value, "instrument_version"),
                object_getattribute(value, "price_rule_instrument_id"),
                object_getattribute(value, "price_rule_instrument_version"),
                object_getattribute(value, "price_semantics_digest"),
                object_getattribute(value, "body_sha256"),
            )
        except attribute_error_type:
            authority_changed()

    def require_canonical_bybit_prepared_submission(value):
        implementation_changed()
        if canonical_type(value) is not prepared_type:
            authority_changed()
        binding = bindings.get(canonical_id(value))
        if binding is None:
            authority_changed()
        bound_ref, expected = binding
        if bound_ref() is not value:
            authority_changed()
        current = snapshot(value)
        if current[1] is not expected[1] or current[:1] + current[2:] != expected[:1] + expected[2:]:
            authority_changed()
        if canonical_type(current[1]) is not mapping_proxy_type:
            authority_changed()
        return value

    def canonical_prepare_order_submission(
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
        price_semantics_registry: InstrumentRegistry | None = None,
        price_semantics_artifact_store: object | None = None,
        price_semantics_instrument_version: str | None = None,
        entity_policy_id: str | None = None,
        reduce_only: bool = False,
        position_side: str | None = None,
        position_idx: int | None = None,
    ) -> BybitPreparedSubmission:
        implementation_changed()
        if canonical_type(capability) is not capability_type:
            raise error_type(
                "Bybit preparation requires exact CapabilitySnapshot authority"
            )
        if canonical_type(at) is not datetime_type:
            raise error_type("Bybit preparation time must be exact datetime")
        if canonical_type(at.tzinfo) is not timezone_type:
            raise error_type(
                "Bybit preparation time must use exact stdlib timezone"
            )

        prepared = builder(
            capability=capability,
            at=at,
            provider_environment=provider_environment,
            product_family=product_family,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            client_order_id=client_order_id,
            time_in_force=time_in_force,
            price=price,
            price_semantics_registry=price_semantics_registry,
            price_semantics_artifact_store=price_semantics_artifact_store,
            price_semantics_instrument_version=price_semantics_instrument_version,
            entity_policy_id=entity_policy_id,
            reduce_only=reduce_only,
            position_side=position_side,
            position_idx=position_idx,
        )
        implementation_changed()
        if canonical_type(prepared) is not prepared_type:
            authority_changed()

        dead = [
            key
            for key, (existing_ref, _snapshot) in canonical_tuple(bindings.items())
            if existing_ref() is None
        ]
        for key in dead:
            bindings.pop(key, None)

        bindings[canonical_id(prepared)] = (
            prepared_ref(prepared),
            snapshot(prepared),
        )
        require_canonical_bybit_prepared_submission(prepared)
        return prepared

    return canonical_prepare_order_submission, require_canonical_bybit_prepared_submission


_unissued_prepare_order_submission = prepare_order_submission
(
    prepare_order_submission,
    require_canonical_bybit_prepared_submission,
) = _install_bybit_prepared_submission_authority(_unissued_prepare_order_submission)
del _unissued_prepare_order_submission
del _install_bybit_prepared_submission_authority

def _install_guarded_order_projection(verifier):
    """Capture the final prepared-request projection TCB outside module aliases."""

    verifier_code = verifier.__code__
    prepared_type = BybitPreparedSubmission
    error_type = ProviderCoreError
    canonical_object = object
    object_getattribute = canonical_object.__getattribute__
    canonical_dict = dict
    mapping_proxy_type = MappingProxyType
    canonical_getattr = getattr

    def guarded_order_projection(
        prepared_request: BybitPreparedSubmission,
    ) -> Mapping[str, object]:
        """Project one canonical preparation without late-bound caller callbacks."""

        if (
            require_canonical_bybit_prepared_submission is not verifier
            or canonical_getattr(verifier, "__code__", None) is not verifier_code
            or BybitPreparedSubmission is not prepared_type
            or ProviderCoreError is not error_type
            or object is not canonical_object
            or dict is not canonical_dict
            or MappingProxyType is not mapping_proxy_type
            or getattr is not canonical_getattr
        ):
            raise error_type("Bybit guarded projection authority changed")

        verifier(prepared_request)
        endpoint = object_getattribute(prepared_request, "endpoint")
        body = object_getattribute(prepared_request, "body")
        account_id = object_getattribute(prepared_request, "account_id")
        environment = object_getattribute(prepared_request, "environment")
        provider_environment = object_getattribute(
            prepared_request,
            "provider_environment",
        )
        capability_snapshot_id = object_getattribute(
            prepared_request,
            "capability_snapshot_id",
        )
        entity_id = object_getattribute(prepared_request, "entity_id")
        instrument_version = object_getattribute(
            prepared_request,
            "instrument_version",
        )
        body_sha256 = object_getattribute(prepared_request, "body_sha256")

        return mapping_proxy_type(
            {
                "endpoint": endpoint,
                "body": canonical_dict(body),
                "account_id": account_id,
                "environment": environment,
                "provider_environment": provider_environment,
                "capability_snapshot_id": capability_snapshot_id,
                "entity_id": entity_id,
                "capability_snapshot_ids": [capability_snapshot_id],
                "instrument_versions": [instrument_version],
                "body_sha256": body_sha256,
            }
        )

    return guarded_order_projection


guarded_order_projection = _install_guarded_order_projection(
    require_canonical_bybit_prepared_submission
)
del _install_guarded_order_projection


def _install_submission_response_parser(
    prepared_projection,
    observation_projection,
):
    """Seal final Bybit ACK/reject normalization behind canonical projections."""

    prepared_projection_code = prepared_projection.__code__
    observation_projection_code = observation_projection.__code__
    observation_type = ProviderSubmissionObservation
    error_type = ProviderCoreError
    type_error = TypeError
    value_error = ValueError
    canonical_type = type
    canonical_str = str
    canonical_int = int
    canonical_bool = bool
    canonical_tuple = tuple
    canonical_mapping_proxy = MappingProxyType
    canonical_uuid = UUID
    canonical_uuid5 = uuid5
    canonical_namespace = NAMESPACE_URL
    canonical_datetime = datetime
    canonical_timezone = timezone
    canonical_timedelta = timedelta
    canonical_divmod = divmod
    client_id_pattern = _CLIENT_ID
    rest_bases = MappingProxyType(dict(_REST_BASE_BY_ENVIRONMENT))
    ambiguous_codes = frozenset(_AMBIGUOUS_RESPONSE_CODES)

    def authority_changed():
        raise error_type("Bybit submission response parser authority changed")

    def implementation_changed():
        if (
            guarded_order_projection is not prepared_projection
            or prepared_projection.__code__ is not prepared_projection_code
            or provider_submission_observation_projection
            is not observation_projection
            or observation_projection.__code__ is not observation_projection_code
            or ProviderSubmissionObservation is not observation_type
            or ProviderCoreError is not error_type
            or TypeError is not type_error
            or ValueError is not value_error
            or type is not canonical_type
            or str is not canonical_str
            or int is not canonical_int
            or bool is not canonical_bool
            or tuple is not canonical_tuple
            or MappingProxyType is not canonical_mapping_proxy
            or UUID is not canonical_uuid
            or uuid5 is not canonical_uuid5
            or NAMESPACE_URL is not canonical_namespace
            or datetime is not canonical_datetime
            or timezone is not canonical_timezone
            or timedelta is not canonical_timedelta
            or divmod is not canonical_divmod
            or _CLIENT_ID is not client_id_pattern
        ):
            authority_changed()

    def exact_text(value, name):
        if canonical_type(value) is not canonical_str:
            raise error_type(f"{name} is required")
        normalized = value.strip()
        if not normalized:
            raise error_type(f"{name} is required")
        return normalized

    def exact_client_id(value):
        client_id = exact_text(value, "client_order_id")
        if client_id_pattern.fullmatch(client_id) is None:
            raise error_type(
                "client_order_id must be 1-36 letters, numbers, dashes or underscores"
            )
        return client_id

    def exact_uuid_text(value, name):
        text = exact_text(value, name)
        try:
            canonical_uuid(text)
        except value_error as error:
            raise error_type(f"{name} must be a UUID") from error
        return text

    def exact_mapping(value, name):
        if canonical_type(value) is not canonical_mapping_proxy:
            raise error_type(f"{name} must be an object")
        return value

    def exact_integer(value, name):
        if canonical_type(value) is not canonical_int:
            raise error_type(f"{name} must be an integer")
        return value

    def millis_to_utc(value, name):
        milliseconds = exact_integer(value, name)
        if milliseconds < 0:
            raise error_type(f"{name} must be at least 0")
        seconds, remainder = canonical_divmod(milliseconds, 1000)
        instant = canonical_datetime.fromtimestamp(
            seconds,
            tz=canonical_timezone.utc,
        ) + canonical_timedelta(milliseconds=remainder)
        return instant.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def evidence_from_projection(observed, prepared):
        source_uri = (
            rest_bases[prepared["provider_environment"]]
            + prepared["endpoint"]
        )
        return {
            "artifact_id": canonical_str(
                canonical_uuid5(
                    canonical_namespace,
                    f"{source_uri}#{observed['evidence_ref']}",
                )
            ),
            "sha256": observed["response_sha256"],
            "http_status": observed["http_status"],
            "source_uri": source_uri,
            "observed_at": observed["sent_at"],
            "rights_id": "provider-observation-bybit",
        }

    def parse_submission_response(
        *,
        attempt_id: str,
        prepared_request: BybitPreparedSubmission,
        observation: ProviderSubmissionObservation | None = None,
        transport_ambiguous: bool = False,
    ) -> dict[str, Any]:
        """Map one authenticated durable Bybit create-order response."""

        implementation_changed()
        aid = exact_uuid_text(attempt_id, "attempt_id")
        prepared = prepared_projection(prepared_request)
        cid = exact_client_id(prepared["body"].get("orderLinkId"))

        if canonical_type(transport_ambiguous) is not canonical_bool:
            raise error_type("transport_ambiguous must be boolean")
        if transport_ambiguous:
            if observation is not None:
                raise error_type(
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

        if canonical_type(observation) is not observation_type:
            raise type_error(
                "observation must be durable ProviderSubmissionObservation"
            )
        observed = observation_projection(observation)
        if observed["attempt_id"] != aid:
            raise error_type("Bybit submission observation attempt_id mismatch")
        if (
            observed["provider_id"] != "BYBIT"
            or observed["endpoint"] != prepared["endpoint"]
            or observed["request_sha256"] != prepared["body_sha256"]
            or observed["capability_snapshot_ids"]
            != canonical_tuple(prepared["capability_snapshot_ids"])
            or observed["instrument_versions"]
            != canonical_tuple(prepared["instrument_versions"])
            or observed["account_id"] != prepared["account_id"]
            or observed["environment"] != prepared["environment"]
            or observed["client_order_id"] != cid
        ):
            raise error_type("provider-write provenance scope mismatch")

        evidence = [evidence_from_projection(observed, prepared)]
        http_status = observed["http_status"]
        if canonical_type(http_status) is not canonical_int:
            raise error_type("Bybit submission response HTTP status is invalid")
        if http_status < 200 or http_status > 299:
            return {
                "attempt_id": aid,
                "outcome": "UNKNOWN",
                "client_order_id": cid,
                "reason_code": "BYBIT_HTTP_NON_2XX",
                "evidence": evidence,
                "retry_disposition": "RECONCILE_FIRST",
            }
        envelope = exact_mapping(observed["payload"], "response")
        code = exact_integer(envelope.get("retCode"), "retCode")
        response_time = envelope.get("time")
        provider_received_at = (
            millis_to_utc(response_time, "response.time")
            if response_time is not None
            else None
        )

        if code == 0:
            result = exact_mapping(envelope.get("result"), "result")
            provider_order_id = exact_text(
                result.get("orderId"),
                "result.orderId",
            )
            echoed_client_id = exact_text(
                result.get("orderLinkId"),
                "result.orderLinkId",
            )
            if echoed_client_id != cid:
                raise error_type(
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

        outcome = "UNKNOWN" if code in ambiguous_codes else "REJECTED"
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

    return parse_submission_response


parse_submission_response = _install_submission_response_parser(
    guarded_order_projection,
    provider_submission_observation_projection,
)
del _install_submission_response_parser


def parse_executions(
    observation: ProviderResponseObservation,
    *,
    instrument_versions: Mapping[str, str],
    qualified_fee_currencies: Mapping[str, str] | None = None,
) -> tuple[ProviderFillEvidence, ...]:
    """Map one authenticated, exact-byte Bybit execution read into fills."""

    if not isinstance(observation, ProviderResponseObservation):
        raise TypeError("observation must be ProviderResponseObservation")
    observation.require_scope(
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint=BYBIT_DOCUMENTED_ENDPOINTS["EXECUTIONS"],
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
    """Create diagnostic Bybit coverage without minting absence authority.

    ``qualified_exclusion_semantics`` remains only as a compatibility trap for
    older callers. A scalar supplied by a caller is never provider qualification
    authority. Until an exact-current, immutable provider-Q absence-semantics
    issuer is wired to reconciliation, this adapter must stay fail-closed and
    emit ``provider_semantics_exclude_execution=False``.
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
    if qualified_exclusion_semantics:
        raise ProviderCoreError(
            "Bybit exclusion semantics require canonical provider qualification "
            "authority; a caller boolean cannot grant absence authority"
        )
    return CoverageSurfaceEvidence(
        provider_id="BYBIT",
        account_id=account_id,
        environment=environment,
        surface=normalized,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        pagination_complete=pagination_complete,
        consistency_horizon_satisfied=consistency_horizon_satisfied,
        provider_semantics_exclude_execution=False,
    )