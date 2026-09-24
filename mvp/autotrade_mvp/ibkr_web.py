"""IBKR Web API non-live adapter foundation.

This module models fail-closed brokerage-session readiness, stable contract
identity and normalized order fields. It does not authenticate, initialize a
brokerage session, serialize provider doubles, suppress order questions or send
an order. Those steps require separate exact-version qualification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping
import re

from .capabilities import CapabilitySnapshot


class IbkrWebAdapterError(ValueError):
    """Raised when IBKR Web API state or order input cannot be used safely."""


IBKR_WEB_DOCS = MappingProxyType(
    {
        "session": "https://www.interactivebrokers.com/docs/web-api/trading/trading-sessions-in-the-web-api",
        "place_order": "https://www.interactivebrokers.com/docs/web-api/v1/endpoints/orders/place-order",
        "modify_order": "https://www.interactivebrokers.com/docs/web-api/api-reference/trading/trading-orders/modify-open-order",
        "execution": "https://www.interactivebrokers.com/docs/tws-api/ref/execution",
    }
)

_COID = re.compile(r"^[\x21-\x7e]{1,64}$")
_CONIDEX = re.compile(r"^(?P<conid>[1-9][0-9]*)@(?P<exchange>[A-Za-z0-9._-]+)$")
_ORDER_TYPES = {
    "MARKET": "MKT",
    "LIMIT": "LMT",
    "STOP": "STP",
    "STOP_LIMIT": "STP LMT",
}
_TIFS = frozenset({"DAY", "GTC", "IOC"})
_SIDES = frozenset({"BUY", "SELL"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IbkrWebAdapterError(f"{name} is required")
    return value.strip()


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise IbkrWebAdapterError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise IbkrWebAdapterError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise IbkrWebAdapterError(f"{name} must be a finite decimal")
    if positive and result <= 0:
        raise IbkrWebAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise IbkrWebAdapterError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def validate_coid(value: str) -> str:
    coid = _text(value, name="cOID")
    if _COID.fullmatch(coid) is None:
        raise IbkrWebAdapterError("cOID must be printable ASCII of at most 64 characters")
    return coid


@dataclass(frozen=True)
class IbkrBrokerageSessionStatus:
    connected: bool
    authenticated: bool
    established: bool
    competing: bool
    observed_at: datetime

    def __post_init__(self) -> None:
        for field in ("connected", "authenticated", "established", "competing"):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")
        object.__setattr__(self, "observed_at", _instant(self.observed_at, name="observed_at"))

    @property
    def trade_ready(self) -> bool:
        return self.connected and self.authenticated and self.established and not self.competing

    def require_trade_ready(self) -> None:
        if not self.connected:
            raise IbkrWebAdapterError("brokerage session is disconnected")
        if not self.authenticated:
            raise IbkrWebAdapterError("brokerage session is not authenticated")
        if not self.established:
            raise IbkrWebAdapterError("brokerage session is not established")
        if self.competing:
            raise IbkrWebAdapterError("another competing brokerage session is active")


@dataclass(frozen=True)
class IbkrContractIdentity:
    conid: int | None = None
    conidex: str | None = None

    def __post_init__(self) -> None:
        if (self.conid is None) == (self.conidex is None):
            raise IbkrWebAdapterError("exactly one of conid or conidex is required")
        if self.conid is not None:
            if not isinstance(self.conid, int) or isinstance(self.conid, bool) or self.conid <= 0:
                raise IbkrWebAdapterError("conid must be a positive integer")
        if self.conidex is not None:
            value = _text(self.conidex, name="conidex")
            match = _CONIDEX.fullmatch(value)
            if match is None:
                raise IbkrWebAdapterError("conidex must have positive-conid@EXCHANGE form")
            object.__setattr__(self, "conidex", value)

    @property
    def contract_key(self) -> str:
        return str(self.conid) if self.conid is not None else self.conidex


@dataclass(frozen=True)
class IbkrWebOrderIntent:
    instrument_version: str
    account_id: str
    contract: IbkrContractIdentity
    side: str
    order_type: str
    time_in_force: str
    quantity: Decimal
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        instrument_version: str,
        account_id: str,
        contract: IbkrContractIdentity,
        side: str,
        order_type: str,
        time_in_force: str,
        quantity,
        limit_price=None,
        stop_price=None,
    ) -> "IbkrWebOrderIntent":
        if not isinstance(contract, IbkrContractIdentity):
            raise TypeError("contract must be IbkrContractIdentity")
        side_value = _text(side, name="side").upper()
        order = _text(order_type, name="order_type").upper()
        tif = _text(time_in_force, name="time_in_force").upper()
        if side_value not in _SIDES:
            raise IbkrWebAdapterError("side must be BUY or SELL")
        if order not in _ORDER_TYPES:
            raise IbkrWebAdapterError("unsupported normalized order type")
        if tif not in _TIFS:
            raise IbkrWebAdapterError("unsupported time_in_force")
        qty = _decimal(quantity, name="quantity", positive=True)
        limit = None if limit_price is None else _decimal(limit_price, name="limit_price", positive=True)
        stop = None if stop_price is None else _decimal(stop_price, name="stop_price", positive=True)
        if order in {"LIMIT", "STOP_LIMIT"} and limit is None:
            raise IbkrWebAdapterError("limit_price is required for limit-style orders")
        if order not in {"LIMIT", "STOP_LIMIT"} and limit is not None:
            raise IbkrWebAdapterError("limit_price is not valid for this order type")
        if order in {"STOP", "STOP_LIMIT"} and stop is None:
            raise IbkrWebAdapterError("stop_price is required for stop orders")
        if order not in {"STOP", "STOP_LIMIT"} and stop is not None:
            raise IbkrWebAdapterError("stop_price is not valid for this order type")
        return cls(
            instrument_version=_text(instrument_version, name="instrument_version"),
            account_id=_text(account_id, name="account_id"),
            contract=contract,
            side=side_value,
            order_type=order,
            time_in_force=tif,
            quantity=qty,
            limit_price=limit,
            stop_price=stop,
        )


@dataclass(frozen=True)
class IbkrNormalizedOrder:
    endpoint: str
    fields: Mapping[str, object]
    exact_quantity_text: str
    exact_limit_price_text: str | None
    exact_stop_price_text: str | None
    capability_snapshot_id: str
    documentation_refs: tuple[str, ...]
    provider_serialization_qualified: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


def prepare_normalized_order(
    intent: IbkrWebOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    session: IbkrBrokerageSessionStatus,
    at: datetime,
) -> IbkrNormalizedOrder:
    """Build normalized fields but deliberately stop before provider serialization.

    IBKR's Web API schemas expose numeric quantities/prices. AutoTrade keeps the
    source decimals exact and refuses to label a float serialization as qualified
    until exact-version adapter tests establish a lossless provider boundary.
    """

    if not isinstance(intent, IbkrWebOrderIntent):
        raise TypeError("intent must be IbkrWebOrderIntent")
    if not isinstance(capability, CapabilitySnapshot):
        raise TypeError("capability must be CapabilitySnapshot")
    if not isinstance(session, IbkrBrokerageSessionStatus):
        raise TypeError("session must be IbkrBrokerageSessionStatus")
    point = _instant(at, name="at")
    if session.observed_at > point:
        raise IbkrWebAdapterError("session evidence is from the future")
    session.require_trade_ready()
    coid = validate_coid(client_order_id)
    if capability.provider_id.upper() != "IBKR":
        raise IbkrWebAdapterError("capability belongs to another provider")
    if capability.account_id != intent.account_id:
        raise IbkrWebAdapterError("capability account does not match intent account")
    if capability.instrument_version != intent.instrument_version:
        raise IbkrWebAdapterError("capability instrument version does not match intent")
    if not capability.admits(
        at=point,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise IbkrWebAdapterError("exact capability evidence does not admit this order")

    fields: dict[str, object] = {
        "acctId": intent.account_id,
        "orderType": _ORDER_TYPES[intent.order_type],
        "side": intent.side,
        "tif": intent.time_in_force,
        "cOID": coid,
    }
    if intent.contract.conid is not None:
        fields["conid"] = intent.contract.conid
    else:
        fields["conidex"] = intent.contract.conidex

    return IbkrNormalizedOrder(
        endpoint=f"/iserver/account/{intent.account_id}/orders",
        fields=fields,
        exact_quantity_text=_decimal_text(intent.quantity),
        exact_limit_price_text=(
            None if intent.limit_price is None else _decimal_text(intent.limit_price)
        ),
        exact_stop_price_text=(
            None if intent.stop_price is None else _decimal_text(intent.stop_price)
        ),
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(IBKR_WEB_DOCS.values()),
    )


@dataclass(frozen=True)
class IbkrExecutionEvidence:
    execution_id: str
    permanent_order_id: str
    account_id: str
    quantity: Decimal
    price: Decimal

    @classmethod
    def create(
        cls,
        *,
        execution_id: str,
        permanent_order_id,
        account_id: str,
        quantity,
        price,
    ) -> "IbkrExecutionEvidence":
        return cls(
            execution_id=_text(execution_id, name="execution_id"),
            permanent_order_id=_text(str(permanent_order_id), name="permanent_order_id"),
            account_id=_text(account_id, name="account_id"),
            quantity=_decimal(quantity, name="quantity", positive=True),
            price=_decimal(price, name="price", positive=True),
        )


@dataclass(frozen=True)
class IbkrAbsenceEvidence:
    open_orders_complete: bool
    completed_orders_complete: bool
    executions_complete: bool
    account_activity_complete: bool
    consistency_horizon_satisfied: bool
    order_found: bool

    def __post_init__(self) -> None:
        for field in (
            "open_orders_complete",
            "completed_orders_complete",
            "executions_complete",
            "account_activity_complete",
            "consistency_horizon_satisfied",
            "order_found",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.open_orders_complete
            and self.completed_orders_complete
            and self.executions_complete
            and self.account_activity_complete
            and self.consistency_horizon_satisfied
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"
