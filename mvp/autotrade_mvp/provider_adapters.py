"""Fail-closed request planning for real-provider adapters.

This module never opens a network connection and never resolves credentials.  It turns
an already-admitted canonical order into an unsigned provider request plan.  The
execution boundary remains responsible for credentials, signing, final authority,
transport and reconciliation.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping


class ProviderAdapterError(ValueError):
    """Raised when a provider request cannot be proven safe to shape."""


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderAdapterError(f"{name} is required")
    return value.strip()


def _positive_decimal_text(value: str | int | Decimal, *, name: str) -> str:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must be exact Decimal/string/integer input")
    try:
        number = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ProviderAdapterError(f"{name} must be a finite decimal") from error
    if not number.is_finite() or number <= 0:
        raise ProviderAdapterError(f"{name} must be positive and finite")
    rendered = format(number.normalize(), "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


ASSET_SPOT_CRYPTO = "SPOT_CRYPTO"
ASSET_PERPETUAL = "PERPETUAL"
ASSET_FUTURE = "FUTURE"
ASSET_OPTION = "OPTION"
ASSET_EQUITY = "EQUITY"
ASSET_FOREX = "FOREX"


@dataclass(frozen=True)
class ProviderProfile:
    provider_id: str
    supported_assets: frozenset[str]
    live_base_url: str
    nonlive_base_urls: Mapping[str, str]
    create_order_path: str | None
    auth_scope: str
    acknowledgement_mode: str
    status_confirmation_required: bool
    documentation_evidence: tuple[str, ...]
    endpoint_verified_as_of: str

    def endpoint(self, environment: str, *, allow_live: bool = False) -> tuple[str, str]:
        env = _text(environment, name="environment").upper()
        if env == "LIVE":
            if not allow_live:
                raise ProviderAdapterError("live endpoint requires explicit allow_live")
            base = self.live_base_url
        else:
            try:
                base = self.nonlive_base_urls[env]
            except KeyError as error:
                raise ProviderAdapterError(
                    f"provider {self.provider_id} has no evidenced {env} endpoint"
                ) from error
        if not self.create_order_path:
            raise ProviderAdapterError(
                f"provider {self.provider_id} order endpoint is not evidence-qualified"
            )
        return base.rstrip("/"), self.create_order_path


@dataclass(frozen=True)
class CanonicalOrder:
    client_order_id: str
    asset_family: str
    symbol: str
    side: str
    order_type: str
    quantity: str | int | Decimal
    price: str | int | Decimal | None = None
    time_in_force: str = "GTC"
    reduce_only: bool = False
    account_id: str | None = None
    provider_instrument_id: str | int | None = None
    metadata: Mapping[str, Any] | None = None

    def normalized(self) -> "CanonicalOrder":
        cid = _text(self.client_order_id, name="client_order_id")
        asset = _text(self.asset_family, name="asset_family").upper()
        symbol = _text(self.symbol, name="symbol")
        side = _text(self.side, name="side").upper()
        if side not in {"BUY", "SELL"}:
            raise ProviderAdapterError("side must be BUY or SELL")
        order_type = _text(self.order_type, name="order_type").upper()
        if order_type not in {"MARKET", "LIMIT"}:
            raise ProviderAdapterError("foundation supports only MARKET or LIMIT")
        quantity = _positive_decimal_text(self.quantity, name="quantity")
        price = None
        if order_type == "LIMIT":
            if self.price is None:
                raise ProviderAdapterError("LIMIT order requires price")
            price = _positive_decimal_text(self.price, name="price")
        elif self.price is not None:
            raise ProviderAdapterError("MARKET order must not carry an invented limit price")
        tif = _text(self.time_in_force, name="time_in_force").upper()
        if tif not in {"GTC", "IOC", "FOK", "DAY"}:
            raise ProviderAdapterError("unsupported time_in_force")
        return CanonicalOrder(
            client_order_id=cid,
            asset_family=asset,
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            time_in_force=tif,
            reduce_only=bool(self.reduce_only),
            account_id=self.account_id.strip() if isinstance(self.account_id, str) and self.account_id.strip() else None,
            provider_instrument_id=self.provider_instrument_id,
            metadata=MappingProxyType(dict(self.metadata or {})),
        )


@dataclass(frozen=True)
class UnsignedRequestPlan:
    provider_id: str
    environment: str
    method: str
    url: str
    payload: Mapping[str, Any]
    auth_scope: str
    client_order_id: str
    status_confirmation_required: bool
    acknowledgement_mode: str
    documentation_evidence: tuple[str, ...]
    contains_secret: bool = False


PROFILES: dict[str, ProviderProfile] = {
    "BYBIT": ProviderProfile(
        provider_id="BYBIT",
        supported_assets=frozenset({ASSET_SPOT_CRYPTO, ASSET_PERPETUAL, ASSET_FUTURE, ASSET_OPTION}),
        live_base_url="https://api.bybit.com",
        nonlive_base_urls={
            "TEST": "https://api-testnet.bybit.com",
            "DEMO": "https://api-demo.bybit.com",
        },
        create_order_path="/v5/order/create",
        auth_scope="TRADE_ORDER",
        acknowledgement_mode="ASYNC_ACCEPTED_REQUIRES_STATUS_CONFIRMATION",
        status_confirmation_required=True,
        documentation_evidence=(
            "https://bybit-exchange.github.io/docs/v5/order/create-order",
            "https://bybit-exchange.github.io/docs/v5/demo",
        ),
        endpoint_verified_as_of="2026-09-24",
    ),
    "KRAKEN": ProviderProfile(
        provider_id="KRAKEN",
        supported_assets=frozenset({ASSET_SPOT_CRYPTO}),
        live_base_url="https://api.kraken.com/0",
        nonlive_base_urls={},
        create_order_path="/private/AddOrder",
        auth_scope="MODIFY_TRADES",
        acknowledgement_mode="SYNCHRONOUS_ACCEPTANCE_WITH_TXID",
        status_confirmation_required=True,
        documentation_evidence=("https://docs.kraken.com/api-reference/trading/add-order",),
        endpoint_verified_as_of="2026-09-24",
    ),
    "WHITEBIT": ProviderProfile(
        provider_id="WHITEBIT",
        supported_assets=frozenset({ASSET_SPOT_CRYPTO}),
        live_base_url="https://whitebit.com",
        nonlive_base_urls={},
        # Current official index confirms V4 spot create-order surfaces, but this
        # baseline deliberately fails closed until the exact V4 path is captured
        # in repository evidence rather than reusing deprecated V1 knowledge.
        create_order_path=None,
        auth_scope="ORDER_CREATE",
        acknowledgement_mode="UNQUALIFIED",
        status_confirmation_required=True,
        documentation_evidence=(
            "https://docs.whitebit.com/llms.txt",
            "https://docs.whitebit.com/api-reference/spot-trading/create-limit-order.md",
        ),
        endpoint_verified_as_of="2026-09-24",
    ),
    "BINANCE": ProviderProfile(
        provider_id="BINANCE",
        supported_assets=frozenset({ASSET_SPOT_CRYPTO}),
        live_base_url="https://api.binance.com",
        nonlive_base_urls={"TEST": "https://testnet.binance.vision"},
        create_order_path="/api/v3/order",
        auth_scope="TRADE",
        acknowledgement_mode="STATUS_MAY_BE_UNKNOWN_ON_TIMEOUT",
        status_confirmation_required=True,
        documentation_evidence=(
            "https://developers.binance.com/en/docs/products/spot/rest-api",
            "https://developers.binance.com/en/docs/products/spot/testnet/web-socket-streams",
        ),
        endpoint_verified_as_of="2026-09-24",
    ),
    "IBKR": ProviderProfile(
        provider_id="IBKR",
        supported_assets=frozenset({ASSET_EQUITY, ASSET_OPTION, ASSET_FUTURE, ASSET_FOREX}),
        live_base_url="https://api.ibkr.com/v1/api",
        nonlive_base_urls={"LOCAL_GATEWAY": "https://localhost:5000/v1/api"},
        create_order_path="/iserver/account/{account_id}/orders",
        auth_scope="TRADING",
        acknowledgement_mode="WARNING_REPLY_MAY_REQUIRE_CONFIRMATION",
        status_confirmation_required=True,
        documentation_evidence=(
            "https://ibkrcampus.com/docs/web-api/v1/endpoints/orders/place-order",
        ),
        endpoint_verified_as_of="2026-09-24",
    ),
    "ALPACA": ProviderProfile(
        provider_id="ALPACA",
        supported_assets=frozenset({ASSET_EQUITY, ASSET_OPTION, ASSET_SPOT_CRYPTO}),
        live_base_url="https://api.alpaca.markets",
        nonlive_base_urls={"PAPER": "https://paper-api.alpaca.markets"},
        create_order_path="/v2/orders",
        auth_scope="TRADING",
        acknowledgement_mode="SYNCHRONOUS_ORDER_OBJECT_REQUIRES_STREAM_RECONCILIATION",
        status_confirmation_required=True,
        documentation_evidence=("https://docs.alpaca.markets/us/v1.1/reference/postorder",),
        endpoint_verified_as_of="2026-09-24",
    ),
}


def _require_asset(profile: ProviderProfile, order: CanonicalOrder) -> None:
    if order.asset_family not in profile.supported_assets:
        raise ProviderAdapterError(
            f"{profile.provider_id} adapter has no qualified mapping for {order.asset_family}"
        )


def _bybit_payload(order: CanonicalOrder) -> dict[str, Any]:
    if order.asset_family == ASSET_SPOT_CRYPTO:
        category = "spot"
    elif order.asset_family == ASSET_OPTION:
        category = "option"
    else:
        # Linear/inverse is a provider-specific settlement/category property, not
        # derivable from the generic FUTURE/PERPETUAL family. Never guess it.
        category = str((order.metadata or {}).get("bybit_category", "")).lower()
        if category not in {"linear", "inverse"}:
            raise ProviderAdapterError(
                "Bybit derivative request requires evidenced bybit_category linear/inverse"
            )
    body: dict[str, Any] = {
        "category": category,
        "symbol": order.symbol.upper(),
        "side": order.side.title(),
        "orderType": order.order_type.title(),
        "qty": order.quantity,
        "orderLinkId": order.client_order_id,
        "timeInForce": "IOC" if order.order_type == "MARKET" else order.time_in_force,
    }
    if order.price is not None:
        body["price"] = order.price
    if order.asset_family in {ASSET_PERPETUAL, ASSET_FUTURE}:
        body["reduceOnly"] = order.reduce_only
    return body


def _kraken_payload(order: CanonicalOrder) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ordertype": order.order_type.lower(),
        "type": order.side.lower(),
        "volume": order.quantity,
        "pair": order.symbol,
        "cl_ord_id": order.client_order_id,
        "timeinforce": order.time_in_force,
    }
    if order.price is not None:
        body["price"] = order.price
    if order.reduce_only:
        body["reduce_only"] = True
    # Nonce is intentionally absent: it belongs to the credential/signing boundary.
    return body


def _binance_payload(order: CanonicalOrder) -> dict[str, Any]:
    body: dict[str, Any] = {
        "symbol": order.symbol.upper(),
        "side": order.side,
        "type": order.order_type,
        "quantity": order.quantity,
        "newClientOrderId": order.client_order_id,
    }
    if order.order_type == "LIMIT":
        body["timeInForce"] = order.time_in_force
        body["price"] = order.price
    if order.reduce_only:
        raise ProviderAdapterError("spot Binance request cannot express reduce_only")
    # timestamp/signature are intentionally absent and must be added immediately at send.
    return body


def _ibkr_payload(order: CanonicalOrder) -> tuple[str, dict[str, Any]]:
    if not order.account_id:
        raise ProviderAdapterError("IBKR order requires exact account_id")
    if order.provider_instrument_id is None:
        raise ProviderAdapterError("IBKR order requires provider conid")
    body: dict[str, Any] = {
        "conid": int(order.provider_instrument_id),
        "orderType": "MKT" if order.order_type == "MARKET" else "LMT",
        "side": order.side,
        "tif": order.time_in_force,
        "quantity": order.quantity,
        "cOID": order.client_order_id,
    }
    if order.price is not None:
        body["price"] = order.price
    if order.reduce_only:
        # Client Portal has no generic cross-asset reduce-only flag.  Risk must
        # express the intended final position and reconciliation must verify it.
        raise ProviderAdapterError("IBKR generic request cannot assert reduce_only safely")
    return order.account_id, {"orders": [body]}


def _alpaca_payload(order: CanonicalOrder) -> dict[str, Any]:
    if order.time_in_force not in {"DAY", "GTC", "IOC", "FOK"}:
        raise ProviderAdapterError("unsupported Alpaca time_in_force")
    body: dict[str, Any] = {
        "symbol": order.symbol,
        "qty": order.quantity,
        "side": order.side.lower(),
        "type": order.order_type.lower(),
        "time_in_force": order.time_in_force.lower(),
        "client_order_id": order.client_order_id,
    }
    if order.price is not None:
        body["limit_price"] = order.price
    if order.reduce_only:
        raise ProviderAdapterError("Alpaca request needs explicit position intent, not generic reduce_only")
    return body


def build_unsigned_order_request(
    provider_id: str,
    environment: str,
    order: CanonicalOrder,
    *,
    allow_live: bool = False,
) -> UnsignedRequestPlan:
    provider = _text(provider_id, name="provider_id").upper()
    try:
        profile = PROFILES[provider]
    except KeyError as error:
        raise ProviderAdapterError(f"unknown provider: {provider}") from error
    normalized = order.normalized()
    _require_asset(profile, normalized)
    base, path = profile.endpoint(environment, allow_live=allow_live)

    if provider == "BYBIT":
        payload = _bybit_payload(normalized)
    elif provider == "KRAKEN":
        payload = _kraken_payload(normalized)
    elif provider == "BINANCE":
        payload = _binance_payload(normalized)
    elif provider == "IBKR":
        account_id, payload = _ibkr_payload(normalized)
        path = path.format(account_id=account_id)
    elif provider == "ALPACA":
        payload = _alpaca_payload(normalized)
    elif provider == "WHITEBIT":  # profile.endpoint already fails closed
        raise AssertionError("unreachable")
    else:  # defensive if profiles expand without a builder
        raise ProviderAdapterError("provider profile has no request builder")

    return UnsignedRequestPlan(
        provider_id=provider,
        environment=_text(environment, name="environment").upper(),
        method="POST",
        url=base + path,
        payload=MappingProxyType(payload),
        auth_scope=profile.auth_scope,
        client_order_id=normalized.client_order_id,
        status_confirmation_required=profile.status_confirmation_required,
        acknowledgement_mode=profile.acknowledgement_mode,
        documentation_evidence=profile.documentation_evidence,
        contains_secret=False,
    )


def provider_qualification_matrix() -> Mapping[str, Mapping[str, Any]]:
    """Return immutable implementation facts, not claims of live qualification."""
    rows: dict[str, Mapping[str, Any]] = {}
    for provider, profile in sorted(PROFILES.items()):
        rows[provider] = MappingProxyType(
            {
                "assets": tuple(sorted(profile.supported_assets)),
                "nonlive_environments": tuple(sorted(profile.nonlive_base_urls)),
                "create_order_path_evidenced": bool(profile.create_order_path),
                "verified_as_of": profile.endpoint_verified_as_of,
                "status_confirmation_required": profile.status_confirmation_required,
                "live_qualified": False,
            }
        )
    return MappingProxyType(rows)
