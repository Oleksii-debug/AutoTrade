"""WhiteBIT adapter foundation with no network or execution authority.

The adapter translates already-authorized canonical intent data into an exact
provider write plan and normalizes provider observations.  It does not own
dispatch, retry, journal, reconciliation, credentials, or live authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from types import MappingProxyType
from typing import Any, Mapping

from .provider_core import ProviderCoreError, WriteOutcome, classify_write_outcome


_CLIENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SUPPORTED_FAMILIES = frozenset({"SPOT", "COLLATERAL", "FUTURES"})
_SUPPORTED_TYPES = frozenset(
    {"MARKET", "LIMIT", "STOP_MARKET", "STOP_LIMIT"}
)
_ENDPOINTS = {
    ("SPOT", "MARKET"): "/api/v4/order/market",
    ("SPOT", "LIMIT"): "/api/v4/order/new",
    ("SPOT", "STOP_MARKET"): "/api/v4/order/stop_market",
    ("SPOT", "STOP_LIMIT"): "/api/v4/order/stop_limit",
    ("COLLATERAL", "MARKET"): "/api/v4/order/collateral/market",
    ("COLLATERAL", "LIMIT"): "/api/v4/order/collateral/limit",
    ("COLLATERAL", "STOP_MARKET"): "/api/v4/order/collateral/trigger-market",
    ("COLLATERAL", "STOP_LIMIT"): "/api/v4/order/collateral/stop-limit",
    ("FUTURES", "MARKET"): "/api/v4/order/collateral/market",
    ("FUTURES", "LIMIT"): "/api/v4/order/collateral/limit",
    ("FUTURES", "STOP_MARKET"): "/api/v4/order/collateral/trigger-market",
    ("FUTURES", "STOP_LIMIT"): "/api/v4/order/collateral/stop-limit",
}
_SECRET_KEYS = frozenset(
    {
        "x-txc-apikey",
        "x-txc-payload",
        "x-txc-signature",
        "api_key",
        "apikey",
        "secret",
        "secret_key",
        "signature",
    }
)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderCoreError(f"{name} is required")
    return value.strip()


def _decimal(value: Any, *, name: str, positive: bool = False) -> Decimal:
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


def _require_multiple(value: Decimal, step: Decimal, *, name: str) -> None:
    if step <= 0:
        raise ProviderCoreError(f"{name} step must be positive")
    if value % step != 0:
        raise ProviderCoreError(f"{name} must be an exact multiple of provider step")


@dataclass(frozen=True)
class WhiteBitCapabilitySnapshot:
    """Account/API-discovered capabilities; never inferred from user location."""

    evidence_id: str
    environment: str
    product_family: str
    allowed_order_types: frozenset[str]
    reduce_only_supported: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, name="evidence_id"))
        object.__setattr__(self, "environment", _text(self.environment, name="environment"))
        family = _text(self.product_family, name="product_family").upper()
        if family not in _SUPPORTED_FAMILIES:
            raise ProviderCoreError("unsupported WhiteBIT product family")
        object.__setattr__(self, "product_family", family)
        order_types = frozenset(
            _text(value, name="allowed_order_type").upper()
            for value in self.allowed_order_types
        )
        if not order_types or not order_types <= _SUPPORTED_TYPES:
            raise ProviderCoreError("allowed_order_types contain unsupported values")
        object.__setattr__(self, "allowed_order_types", order_types)
        if type(self.reduce_only_supported) is not bool:
            raise ProviderCoreError("reduce_only_supported must be boolean")
        if family == "SPOT" and self.reduce_only_supported:
            raise ProviderCoreError("spot capability cannot advertise reduce-only")


@dataclass(frozen=True)
class WhiteBitMarketRules:
    market: str
    amount_step: Decimal
    price_tick: Decimal
    minimum_amount: Decimal
    minimum_total: Decimal
    maximum_total: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        market: str,
        amount_step,
        price_tick,
        minimum_amount,
        minimum_total,
        maximum_total=None,
    ) -> "WhiteBitMarketRules":
        amount_step_value = _decimal(amount_step, name="amount_step", positive=True)
        price_tick_value = _decimal(price_tick, name="price_tick", positive=True)
        minimum_amount_value = _decimal(
            minimum_amount, name="minimum_amount", positive=True
        )
        minimum_total_value = _decimal(
            minimum_total, name="minimum_total", positive=True
        )
        maximum_total_value = (
            None
            if maximum_total is None or str(maximum_total) == "0"
            else _decimal(maximum_total, name="maximum_total", positive=True)
        )
        if (
            maximum_total_value is not None
            and maximum_total_value < minimum_total_value
        ):
            raise ProviderCoreError("maximum_total cannot be below minimum_total")
        return cls(
            market=_text(market, name="market"),
            amount_step=amount_step_value,
            price_tick=price_tick_value,
            minimum_amount=minimum_amount_value,
            minimum_total=minimum_total_value,
            maximum_total=maximum_total_value,
        )


@dataclass(frozen=True)
class WhiteBitWritePlan:
    endpoint: str
    payload: Mapping[str, Any]
    capability_evidence_id: str
    adjusted_quantity: Decimal
    adjustment_reason: str | None


def build_order_write_plan(
    *,
    capability: WhiteBitCapabilitySnapshot,
    rules: WhiteBitMarketRules,
    client_order_id: str,
    side: str,
    order_type: str,
    quantity,
    price=None,
    activation_price=None,
    reduce_only: bool = False,
    current_position=None,
    post_only: bool = False,
    immediate_or_cancel: bool = False,
) -> WhiteBitWritePlan:
    """Build a provider request without sending it.

    Quantity/price must already be aligned to provider market rules.  The
    adapter refuses silent economic rounding.  The sole quantity adjustment
    allowed here is documented reduce-only clipping to the current position.
    """

    if not isinstance(capability, WhiteBitCapabilitySnapshot):
        raise TypeError("capability must be WhiteBitCapabilitySnapshot")
    if not isinstance(rules, WhiteBitMarketRules):
        raise TypeError("rules must be WhiteBitMarketRules")
    cid = _text(client_order_id, name="client_order_id")
    if not _CLIENT_ID.fullmatch(cid):
        raise ProviderCoreError("client_order_id violates WhiteBIT format")
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ProviderCoreError("side must be BUY or SELL")
    normalized_type = _text(order_type, name="order_type").upper()
    if normalized_type not in _SUPPORTED_TYPES:
        raise ProviderCoreError("unsupported order_type")
    if normalized_type not in capability.allowed_order_types:
        raise ProviderCoreError(
            "order_type is not allowed by the account capability snapshot"
        )
    if type(reduce_only) is not bool or type(post_only) is not bool or type(immediate_or_cancel) is not bool:
        raise ProviderCoreError("order flags must be boolean")
    if post_only and immediate_or_cancel:
        raise ProviderCoreError("post_only and immediate_or_cancel are mutually exclusive")
    if immediate_or_cancel and not (
        capability.product_family == "SPOT" and normalized_type == "LIMIT"
    ):
        raise ProviderCoreError("immediate_or_cancel is permitted only for spot limit plans")
    if post_only and normalized_type not in {"LIMIT", "STOP_LIMIT"}:
        raise ProviderCoreError("post_only requires a limit-type order")

    requested = _decimal(quantity, name="quantity", positive=True)
    adjusted = requested
    adjustment_reason = None
    if reduce_only:
        if capability.product_family == "SPOT" or not capability.reduce_only_supported:
            raise ProviderCoreError("reduce_only is not supported by this capability")
        if current_position is None:
            raise ProviderCoreError("reduce_only requires current_position evidence")
        position = _decimal(current_position, name="current_position")
        if position == 0:
            raise ProviderCoreError("reduce_only requires an open position")
        if position > 0 and normalized_side != "SELL":
            raise ProviderCoreError("reduce_only side would increase a long position")
        if position < 0 and normalized_side != "BUY":
            raise ProviderCoreError("reduce_only side would increase a short position")
        position_size = abs(position)
        if adjusted > position_size:
            adjusted = position_size
            adjustment_reason = "REDUCE_ONLY_CLIPPED_TO_POSITION"

    _require_multiple(adjusted, rules.amount_step, name="quantity")
    if adjusted < rules.minimum_amount:
        raise ProviderCoreError("quantity is below provider minimum amount")

    limit_price = None
    trigger = None
    if normalized_type in {"LIMIT", "STOP_LIMIT"}:
        if price is None:
            raise ProviderCoreError("limit-type order requires price")
        limit_price = _decimal(price, name="price", positive=True)
        _require_multiple(limit_price, rules.price_tick, name="price")
    elif price is not None:
        raise ProviderCoreError("market-type order must not carry price")

    if normalized_type in {"STOP_MARKET", "STOP_LIMIT"}:
        if activation_price is None:
            raise ProviderCoreError("stop order requires activation_price")
        trigger = _decimal(
            activation_price, name="activation_price", positive=True
        )
        _require_multiple(trigger, rules.price_tick, name="activation_price")
    elif activation_price is not None:
        raise ProviderCoreError("non-stop order must not carry activation_price")

    # Minimum/maximum total is provable only when a limit price is present.
    if limit_price is not None:
        total = adjusted * limit_price
        if total < rules.minimum_total:
            raise ProviderCoreError("order total is below provider minimum")
        if rules.maximum_total is not None and total > rules.maximum_total:
            raise ProviderCoreError("order total exceeds provider maximum")

    endpoint = _ENDPOINTS[(capability.product_family, normalized_type)]
    payload: dict[str, Any] = {
        "market": rules.market,
        "side": normalized_side.lower(),
        "amount": _decimal_text(adjusted),
        "clientOrderId": cid,
    }
    if limit_price is not None:
        payload["price"] = _decimal_text(limit_price)
    if trigger is not None:
        payload["activation_price"] = _decimal_text(trigger)
    if post_only:
        payload["postOnly"] = True
    if immediate_or_cancel:
        payload["ioc"] = True
    if reduce_only:
        payload["reduceOnly"] = True

    return WhiteBitWritePlan(
        endpoint=endpoint,
        payload=MappingProxyType(payload),
        capability_evidence_id=capability.evidence_id,
        adjusted_quantity=adjusted,
        adjustment_reason=adjustment_reason,
    )


@dataclass(frozen=True)
class WhiteBitOrderObservation:
    provider_order_id: str
    client_order_id: str | None
    status: str
    executed_quantity: Decimal
    remaining_quantity: Decimal
    terminal: bool
    terminal_remainder_cancelled: bool
    economic_fill_authoritative: bool = False


def normalize_order_observation(response: Mapping[str, Any]) -> WhiteBitOrderObservation:
    """Normalize order state without inventing unique executions.

    Aggregated dealStock/dealMoney values are useful reconciliation evidence but
    are not unique execution identities and therefore are not ledger fills.
    """

    if not isinstance(response, Mapping):
        raise TypeError("response must be a mapping")
    provider_order_id = _text(str(response.get("orderId", "")), name="orderId")
    raw_client = response.get("clientOrderId")
    client_order_id = (
        None if raw_client in {None, ""} else _text(str(raw_client), name="clientOrderId")
    )
    status = _text(str(response.get("status", "")), name="status").upper()
    executed = _decimal(response.get("dealStock", "0"), name="dealStock")
    remaining = _decimal(response.get("left", "0"), name="left")
    if executed < 0 or remaining < 0:
        raise ProviderCoreError("provider order quantities cannot be negative")
    cancelled_remainder = status in {
        "CANCELED_TAKER_BAND",
        "AUTO_CANCELED_REDUCE_ONLY",
    }
    terminal = status in {
        "FILLED",
        "CANCELED",
        "CANCELLED",
        "CANCELED_TAKER_BAND",
        "AUTO_CANCELED_REDUCE_ONLY",
        "REJECTED",
    }
    return WhiteBitOrderObservation(
        provider_order_id=provider_order_id,
        client_order_id=client_order_id,
        status=status,
        executed_quantity=executed,
        remaining_quantity=remaining,
        terminal=terminal,
        terminal_remainder_cancelled=cancelled_remainder,
        economic_fill_authoritative=False,
    )


def classify_whitebit_write(
    *,
    transport_started: bool,
    http_response_received: bool,
    accepted_order_response: bool = False,
    explicit_validation_rejection: bool = False,
) -> WriteOutcome:
    """Classify a provider write; ambiguity is never transformed into retry."""

    if accepted_order_response and explicit_validation_rejection:
        raise ProviderCoreError("provider response cannot accept and reject one write")
    if (accepted_order_response or explicit_validation_rejection) and not http_response_received:
        raise ProviderCoreError("provider result requires an HTTP response")
    return classify_write_outcome(
        transport_started=transport_started,
        provider_acknowledged=accepted_order_response,
        provider_rejected=explicit_validation_rejection,
    )


def redact_whitebit_debug(value: Any) -> Any:
    """Recursively redact authentication material before diagnostics."""

    if isinstance(value, Mapping):
        return {
            key: (
                "<redacted>"
                if str(key).lower() in _SECRET_KEYS
                else redact_whitebit_debug(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_whitebit_debug(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_whitebit_debug(item) for item in value)
    return value
