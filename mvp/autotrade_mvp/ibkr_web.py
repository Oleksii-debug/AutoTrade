"""IBKR Web API non-live adapter foundation.

This module models fail-closed brokerage-session readiness, stable contract
identity and normalized order fields. It does not authenticate, initialize a
brokerage session, serialize provider doubles, suppress order questions or send
an order. Those steps require separate exact-version qualification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping
from weakref import ref as weakref_ref
import hashlib
import json
import re

from autotrade_numeric.exact_decimal import (
    ExactDecimalError,
    parse_bounded_exact_decimal,
)

from .capabilities import CapabilitySnapshot
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderResponseObservation,
    Surface,
)
from .reconciliation import ProviderFillEvidence


class IbkrWebAdapterError(ValueError):
    """Raised when IBKR Web API state or order input cannot be used safely."""


IBKR_WEB_BROKERAGE_STATUS_ENDPOINT = "/iserver/auth/status"


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
_ORDER_TYPES = MappingProxyType({
    "MARKET": "MKT",
    "LIMIT": "LMT",
    "STOP": "STP",
    "STOP_LIMIT": "STP LMT",
})
_PROVIDER_ORDER_TYPES = frozenset(_ORDER_TYPES.values())
_TIFS = frozenset({"DAY", "GTC", "IOC"})
_SIDES = frozenset({"BUY", "SELL"})
_PERMANENT_ORDER_ID = re.compile(r"^(?:0|[1-9][0-9]*)$")
_IBKR_WEB_TRADE_TIME = re.compile(r"^[0-9]{8}-[0-9]{2}:[0-9]{2}:[0-9]{2}$")
_TRADE_CONIDEX = re.compile(r"^(?P<conid>[1-9][0-9]*)(?:@[A-Za-z0-9._-]+)?$")


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise IbkrWebAdapterError(f"{name} is required")
    return value.strip()


def _provider_text(value: object, *, name: str) -> str:
    """Admit canonical inert JSON strings without changing provider identity."""

    if (
        type(value) is not str
        or not value
        or value != value.strip()
    ):
        raise IbkrWebAdapterError(f"{name} must be canonical provider text")
    return value


def _optional_provider_text(value: object, *, name: str) -> str | None:
    """Classify optional provider text without changing provider identity."""

    if value is None:
        return None
    if type(value) is not str:
        raise IbkrWebAdapterError(f"{name} must be provider text")
    if value == "":
        return None
    if value != value.strip():
        raise IbkrWebAdapterError(f"{name} must be canonical provider text")
    return value


def _decimal(value, *, name: str, positive: bool = False) -> Decimal:
    try:
        result = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError) as error:
        raise IbkrWebAdapterError(
            f"{name} must use bounded exact decimal input"
        ) from error
    if positive and result <= 0:
        raise IbkrWebAdapterError(f"{name} must be positive")
    return result


def _instant(value: datetime, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise IbkrWebAdapterError(
            f"{name} must be an exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _canonical_web_trade_time(
    value: object,
    *,
    epoch_milliseconds: object,
) -> str:
    """Cross-bind documented IBKR trade time text to its epoch evidence."""

    text = _provider_text(value, name="trade_time")
    if _IBKR_WEB_TRADE_TIME.fullmatch(text) is None:
        raise IbkrWebAdapterError(
            "trade_time must use documented IBKR UTC format YYYYMMDD-HH:mm:ss"
        )
    try:
        parsed = datetime.strptime(text, "%Y%m%d-%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise IbkrWebAdapterError(
            "trade_time must use documented IBKR UTC format YYYYMMDD-HH:mm:ss"
        ) from error
    if type(epoch_milliseconds) is not int or epoch_milliseconds < 0:
        raise IbkrWebAdapterError(
            "trade_time_r must be a non-negative exact integer"
        )
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    elapsed = parsed - epoch
    expected_epoch_milliseconds = (
        (elapsed.days * 86_400 + elapsed.seconds) * 1000
        + elapsed.microseconds // 1000
    )
    if epoch_milliseconds != expected_epoch_milliseconds:
        raise IbkrWebAdapterError("trade_time and trade_time_r conflict")
    return parsed.isoformat().replace("+00:00", "Z")


def _execution_correction_identity(
    execution_id: str,
) -> tuple[str, tuple[int, str]] | None:
    """Return IBKR execution correction family/revision without integer coercion."""

    family, separator, revision = execution_id.rpartition(".")
    if not separator or not family or re.fullmatch(r"[0-9]+", revision) is None:
        return None
    normalized_revision = revision.lstrip("0") or "0"
    if normalized_revision == "0":
        raise IbkrWebAdapterError(
            "IBKR execution correction revision must be a positive integer"
        )
    return family, (len(normalized_revision), normalized_revision)


def _stable_execution_id(execution_id: str) -> str:
    """Map IBKR correction revisions onto one durable execution identity.

    IBKR documents corrections by changing only the digits after the final
    period (for example .02 correcting .01). AutoTrade's durable correction
    authority requires one provider_execution_id across revisions, so numeric
    IBKR revisions normalize to the .01 root. The exact raw ExecId remains in
    the immutable provider-response evidence referenced by the fill.
    """

    correction = _execution_correction_identity(execution_id)
    if correction is None:
        return execution_id
    family, _revision_key = correction
    return family + ".01"


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


def _install_ibkr_brokerage_session_observation_authority():
    """Bind trade-ready session state to one authenticated provider observation.

    The provider-neutral authenticated-read authority remains the source of
    response-byte, endpoint, account, environment and chronology truth. This
    local registry only prevents a caller-constructed or post-mint-mutated
    session DTO from being used as financial preparation authority.
    """

    session_type = IbkrBrokerageSessionStatus
    observation_type = ProviderResponseObservation
    canonical_require_scope = observation_type.require_scope
    canonical_instant = _instant
    canonical_text = _text
    mapping_proxy_type = MappingProxyType
    surface = Surface.AUTHENTICATED_READ
    status_endpoint = IBKR_WEB_BROKERAGE_STATUS_ENDPOINT
    datetime_type = datetime
    object_getattribute = object.__getattribute__
    canonical_type = type
    canonical_id = id
    canonical_set = set
    weakref = weakref_ref
    states: dict[int, tuple[object, tuple[object, ...]]] = {}

    def authority_changed() -> None:
        raise IbkrWebAdapterError(
            "IBKR brokerage session observation authority changed"
        )

    def implementation_changed() -> None:
        if (
            IbkrBrokerageSessionStatus is not session_type
            or ProviderResponseObservation is not observation_type
            or observation_type.require_scope is not canonical_require_scope
            or IBKR_WEB_BROKERAGE_STATUS_ENDPOINT != status_endpoint
            or datetime is not datetime_type
            or _instant is not canonical_instant
            or _text is not canonical_text
        ):
            authority_changed()

    def prune() -> None:
        for object_id, (value_ref, _snapshot) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def from_observation(
        observation: ProviderResponseObservation,
    ) -> IbkrBrokerageSessionStatus:
        implementation_changed()
        if canonical_type(observation) is not observation_type:
            raise TypeError(
                "observation must be exact ProviderResponseObservation"
            )
        canonical_require_scope(
            observation,
            provider_id="IBKR",
            surface=surface,
            endpoint=status_endpoint,
        )
        payload = object_getattribute(observation, "payload")
        if canonical_type(payload) is not mapping_proxy_type:
            raise IbkrWebAdapterError(
                "brokerage session status payload must be an exact provider object"
            )
        if canonical_set(payload) != {"success"}:
            raise IbkrWebAdapterError(
                "brokerage session status requires documented success envelope"
            )
        success = payload["success"]
        if (
            canonical_type(success) is not mapping_proxy_type
            or canonical_set(success) != {"value"}
        ):
            raise IbkrWebAdapterError(
                "brokerage session status requires documented success.value envelope"
            )
        status_payload = success["value"]
        if canonical_type(status_payload) is not mapping_proxy_type:
            raise IbkrWebAdapterError(
                "brokerage session status value must be an exact provider object"
            )

        flags: dict[str, bool] = {}
        for name in ("connected", "authenticated", "established", "competing"):
            if name not in status_payload:
                raise IbkrWebAdapterError(
                    f"brokerage session status is missing {name}"
                )
            value = status_payload[name]
            if canonical_type(value) is not bool:
                raise IbkrWebAdapterError(
                    f"brokerage session status {name} must be exact boolean"
                )
            flags[name] = value

        if "fail" in status_payload:
            failure = status_payload["fail"]
            if failure is not None:
                if (
                    canonical_type(failure) is not str
                    or failure != failure.strip()
                ):
                    raise IbkrWebAdapterError(
                        "brokerage session status fail must be canonical provider text"
                    )
                if failure:
                    raise IbkrWebAdapterError(
                        "brokerage session status reports provider failure"
                    )

        observed_text = object_getattribute(observation, "observed_at")
        if (
            canonical_type(observed_text) is not str
            or not observed_text.endswith("Z")
        ):
            raise IbkrWebAdapterError(
                "brokerage session observation time must be canonical UTC text"
            )
        try:
            observed_at = datetime_type.fromisoformat(
                observed_text[:-1] + "+00:00"
            )
        except ValueError as error:
            raise IbkrWebAdapterError(
                "brokerage session observation time must be canonical UTC text"
            ) from error
        observed_at = canonical_instant(
            observed_at,
            name="brokerage session observed_at",
        )

        query_binding = object_getattribute(observation, "query_binding")
        query = object_getattribute(query_binding, "query")
        permission_scope = object_getattribute(
            query_binding,
            "permission_scope",
        )
        if canonical_type(query) is not mapping_proxy_type or len(query) != 0:
            raise IbkrWebAdapterError(
                "brokerage session status requires an empty authenticated query"
            )
        if permission_scope != "ORDER.READ":
            raise IbkrWebAdapterError(
                "brokerage session status requires ORDER.READ scope"
            )
        account_id = object_getattribute(query_binding, "account_id")
        environment = object_getattribute(query_binding, "environment")
        evidence_ref = object_getattribute(observation, "evidence_ref")
        response_sha256 = object_getattribute(observation, "response_sha256")
        if (
            canonical_type(account_id) is not str
            or canonical_type(environment) is not str
            or canonical_type(evidence_ref) is not str
            or canonical_type(response_sha256) is not str
        ):
            authority_changed()

        value = session_type(
            connected=flags["connected"],
            authenticated=flags["authenticated"],
            established=flags["established"],
            competing=flags["competing"],
            observed_at=observed_at,
        )
        prune()
        object_id = canonical_id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise IbkrWebAdapterError(
                "brokerage session observation identity collision"
            )
        states[object_id] = (
            weakref(value),
            (
                flags["connected"],
                flags["authenticated"],
                flags["established"],
                flags["competing"],
                observed_at,
                account_id,
                environment,
                evidence_ref,
                response_sha256,
            ),
        )
        return value

    def require(
        value: IbkrBrokerageSessionStatus,
        *,
        account_id: str,
        environment: str,
    ) -> tuple[str, str]:
        implementation_changed()
        if canonical_type(value) is not session_type:
            raise TypeError("session must be exact IbkrBrokerageSessionStatus")

        current_flags: list[bool] = []
        for name in ("connected", "authenticated", "established", "competing"):
            current = object_getattribute(value, name)
            if canonical_type(current) is not bool:
                raise TypeError(
                    f"session {name} must remain exact boolean"
                )
            current_flags.append(current)
        current_observed_at = canonical_instant(
            object_getattribute(value, "observed_at"),
            name="session.observed_at",
        )

        prune()
        state = states.get(canonical_id(value))
        if state is None or state[0]() is not value:
            raise IbkrWebAdapterError(
                "brokerage session must come from authenticated "
                "/iserver/auth/status provider observation"
            )
        (
            connected,
            authenticated,
            established,
            competing,
            observed_at,
            bound_account_id,
            bound_environment,
            evidence_ref,
            response_sha256,
        ) = state[1]
        if (
            tuple(current_flags)
            != (connected, authenticated, established, competing)
            or current_observed_at != observed_at
        ):
            raise IbkrWebAdapterError(
                "brokerage session changed after authenticated provider observation"
            )

        expected_account = canonical_text(account_id, name="account_id")
        expected_environment = canonical_text(
            environment,
            name="environment",
        ).upper()
        if bound_account_id != expected_account:
            raise IbkrWebAdapterError(
                "brokerage session account does not match order authority"
            )
        if bound_environment != expected_environment:
            raise IbkrWebAdapterError(
                "brokerage session environment does not match order authority"
            )
        if not connected:
            raise IbkrWebAdapterError("brokerage session is disconnected")
        if not authenticated:
            raise IbkrWebAdapterError("brokerage session is not authenticated")
        if not established:
            raise IbkrWebAdapterError("brokerage session is not established")
        if competing:
            raise IbkrWebAdapterError(
                "another competing brokerage session is active"
            )
        return evidence_ref, response_sha256

    return from_observation, require


(
    brokerage_session_status_from_observation,
    _require_ibkr_brokerage_session_observation,
) = _install_ibkr_brokerage_session_observation_authority()
del _install_ibkr_brokerage_session_observation_authority


@dataclass(frozen=True)
class IbkrContractIdentity:
    conid: int | None = None
    conidex: str | None = None

    def __post_init__(self) -> None:
        if (self.conid is None) == (self.conidex is None):
            raise IbkrWebAdapterError("exactly one of conid or conidex is required")
        if self.conid is not None:
            if type(self.conid) is not int or self.conid <= 0:
                raise IbkrWebAdapterError("conid must be a positive exact integer")
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
    regulatory_manual_indicator_required: bool = False
    manual_indicator: bool | None = None
    ext_operator: str | None = None

    def __post_init__(self) -> None:
        if type(self.contract) is not IbkrContractIdentity:
            raise TypeError("contract must be exact IbkrContractIdentity")
        side = _text(self.side, name="side").upper()
        order = _text(self.order_type, name="order_type").upper()
        tif = _text(self.time_in_force, name="time_in_force").upper()
        if side not in _SIDES:
            raise IbkrWebAdapterError("side must be BUY or SELL")
        if order not in _ORDER_TYPES:
            raise IbkrWebAdapterError("unsupported normalized order type")
        if tif not in _TIFS:
            raise IbkrWebAdapterError("unsupported time_in_force")
        quantity = _decimal(self.quantity, name="quantity", positive=True)
        limit = None if self.limit_price is None else _decimal(
            self.limit_price, name="limit_price", positive=True
        )
        stop = None if self.stop_price is None else _decimal(
            self.stop_price, name="stop_price", positive=True
        )
        if order in {"LIMIT", "STOP_LIMIT"} and limit is None:
            raise IbkrWebAdapterError("limit_price is required for limit-style orders")
        if order not in {"LIMIT", "STOP_LIMIT"} and limit is not None:
            raise IbkrWebAdapterError("limit_price is not valid for this order type")
        if order in {"STOP", "STOP_LIMIT"} and stop is None:
            raise IbkrWebAdapterError("stop_price is required for stop orders")
        if order not in {"STOP", "STOP_LIMIT"} and stop is not None:
            raise IbkrWebAdapterError("stop_price is not valid for this order type")
        if type(self.regulatory_manual_indicator_required) is not bool:
            raise IbkrWebAdapterError(
                "regulatory_manual_indicator_required must be boolean"
            )
        if self.manual_indicator is not None and type(self.manual_indicator) is not bool:
            raise IbkrWebAdapterError("manual_indicator must be boolean when present")
        if self.regulatory_manual_indicator_required and self.manual_indicator is None:
            raise IbkrWebAdapterError(
                "manual_indicator is required by evidenced instrument regulation"
            )
        operator = None if self.ext_operator is None else _text(
            self.ext_operator, name="ext_operator"
        )
        object.__setattr__(
            self, "instrument_version", _text(self.instrument_version, name="instrument_version")
        )
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "order_type", order)
        object.__setattr__(self, "time_in_force", tif)
        object.__setattr__(self, "quantity", quantity)
        object.__setattr__(self, "limit_price", limit)
        object.__setattr__(self, "stop_price", stop)
        object.__setattr__(self, "ext_operator", operator)

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
        regulatory_manual_indicator_required: bool = False,
        manual_indicator: bool | None = None,
        ext_operator: str | None = None,
    ) -> "IbkrWebOrderIntent":
        if type(contract) is not IbkrContractIdentity:
            raise TypeError("contract must be exact IbkrContractIdentity")
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
        if type(regulatory_manual_indicator_required) is not bool:
            raise IbkrWebAdapterError(
                "regulatory_manual_indicator_required must be boolean"
            )
        if manual_indicator is not None and type(manual_indicator) is not bool:
            raise IbkrWebAdapterError("manual_indicator must be boolean when present")
        if regulatory_manual_indicator_required and manual_indicator is None:
            raise IbkrWebAdapterError(
                "manual_indicator is required by evidenced instrument regulation"
            )
        normalized_operator = (
            None
            if ext_operator is None
            else _text(ext_operator, name="ext_operator")
        )
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
            regulatory_manual_indicator_required=regulatory_manual_indicator_required,
            manual_indicator=manual_indicator,
            ext_operator=normalized_operator,
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
        if type(self.fields) is not dict:
            raise TypeError("normalized order fields must be an exact dict")
        if type(self.provider_serialization_qualified) is not bool:
            raise TypeError("provider_serialization_qualified must be boolean")
        if self.provider_serialization_qualified:
            raise IbkrWebAdapterError(
                "IBKR Web foundation cannot self-assert provider serialization qualification"
            )

        raw_fields = self.fields
        for key in raw_fields:
            if type(key) is not str:
                raise IbkrWebAdapterError(
                    "normalized order field names must be exact text"
                )
        required = {"acctId", "orderType", "side", "tif", "cOID"}
        optional = {"conid", "conidex", "manualIndicator", "extOperator"}
        keys = set(raw_fields)
        if not required.issubset(keys) or not keys.issubset(required | optional):
            raise IbkrWebAdapterError(
                "normalized order fields do not match the supported IBKR shape"
            )
        if ("conid" in raw_fields) == ("conidex" in raw_fields):
            raise IbkrWebAdapterError(
                "normalized order requires exactly one of conid or conidex"
            )

        account_id = _text(raw_fields["acctId"], name="fields.acctId")
        endpoint = _text(self.endpoint, name="endpoint")
        if endpoint != f"/iserver/account/{account_id}/orders":
            raise IbkrWebAdapterError(
                "normalized order endpoint does not match account"
            )
        order_type = _text(raw_fields["orderType"], name="fields.orderType")
        if order_type not in _PROVIDER_ORDER_TYPES:
            raise IbkrWebAdapterError("unsupported provider orderType")
        side = _text(raw_fields["side"], name="fields.side")
        if side not in _SIDES:
            raise IbkrWebAdapterError("normalized provider side must be BUY or SELL")
        tif = _text(raw_fields["tif"], name="fields.tif")
        if tif not in _TIFS:
            raise IbkrWebAdapterError("unsupported normalized provider tif")
        coid = validate_coid(raw_fields["cOID"])

        normalized_fields: dict[str, object] = {
            "acctId": account_id,
            "orderType": order_type,
            "side": side,
            "tif": tif,
            "cOID": coid,
        }
        if "conid" in raw_fields:
            conid = raw_fields["conid"]
            if type(conid) is not int or conid <= 0:
                raise IbkrWebAdapterError(
                    "normalized conid must be a positive exact integer"
                )
            normalized_fields["conid"] = conid
        else:
            conidex = _text(raw_fields["conidex"], name="fields.conidex")
            if _CONIDEX.fullmatch(conidex) is None:
                raise IbkrWebAdapterError(
                    "normalized conidex must have positive-conid@EXCHANGE form"
                )
            normalized_fields["conidex"] = conidex
        if "manualIndicator" in raw_fields:
            manual = raw_fields["manualIndicator"]
            if type(manual) is not bool:
                raise IbkrWebAdapterError(
                    "normalized manualIndicator must be boolean"
                )
            normalized_fields["manualIndicator"] = manual
        if "extOperator" in raw_fields:
            normalized_fields["extOperator"] = _text(
                raw_fields["extOperator"],
                name="fields.extOperator",
            )

        if type(self.exact_quantity_text) is not str:
            raise TypeError("exact_quantity_text must be exact text")
        if (
            self.exact_limit_price_text is not None
            and type(self.exact_limit_price_text) is not str
        ):
            raise TypeError("exact_limit_price_text must be exact text when present")
        if (
            self.exact_stop_price_text is not None
            and type(self.exact_stop_price_text) is not str
        ):
            raise TypeError("exact_stop_price_text must be exact text when present")
        quantity = _decimal(
            self.exact_quantity_text,
            name="exact_quantity_text",
            positive=True,
        )
        limit = (
            None
            if self.exact_limit_price_text is None
            else _decimal(
                self.exact_limit_price_text,
                name="exact_limit_price_text",
                positive=True,
            )
        )
        stop = (
            None
            if self.exact_stop_price_text is None
            else _decimal(
                self.exact_stop_price_text,
                name="exact_stop_price_text",
                positive=True,
            )
        )
        price_shape = {
            "MKT": (False, False),
            "LMT": (True, False),
            "STP": (False, True),
            "STP LMT": (True, True),
        }[order_type]
        if (limit is not None, stop is not None) != price_shape:
            raise IbkrWebAdapterError(
                "normalized exact price evidence does not match orderType"
            )

        if type(self.documentation_refs) is not tuple or any(
            type(value) is not str for value in self.documentation_refs
        ):
            raise TypeError("documentation_refs must be an exact tuple of exact text")
        if self.documentation_refs != tuple(IBKR_WEB_DOCS.values()):
            raise IbkrWebAdapterError(
                "normalized order documentation refs must match canonical IBKR refs"
            )

        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "fields", MappingProxyType(normalized_fields))
        object.__setattr__(
            self,
            "exact_quantity_text",
            _decimal_text(quantity),
        )
        object.__setattr__(
            self,
            "exact_limit_price_text",
            None if limit is None else _decimal_text(limit),
        )
        object.__setattr__(
            self,
            "exact_stop_price_text",
            None if stop is None else _decimal_text(stop),
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _text(self.capability_snapshot_id, name="capability_snapshot_id"),
        )


def _prepare_normalized_order_impl(
    intent: IbkrWebOrderIntent,
    *,
    client_order_id: str,
    capability: CapabilitySnapshot,
    session: IbkrBrokerageSessionStatus,
    at: datetime,
    maximum_session_age_seconds: int,
    _require_session_authority,
    _capability_admits,
) -> IbkrNormalizedOrder:
    """Build normalized fields but deliberately stop before provider serialization.

    IBKR's Web API schemas expose numeric quantities/prices. AutoTrade keeps the
    source decimals exact and refuses to label a float serialization as qualified
    until exact-version adapter tests establish a lossless provider boundary.
    """

    if type(intent) is not IbkrWebOrderIntent:
        raise TypeError("intent must be exact IbkrWebOrderIntent")
    if type(capability) is not CapabilitySnapshot:
        raise TypeError("capability must be exact CapabilitySnapshot")
    if type(session) is not IbkrBrokerageSessionStatus:
        raise TypeError("session must be exact IbkrBrokerageSessionStatus")

    raw_contract = intent.contract
    if type(raw_contract) is not IbkrContractIdentity:
        raise TypeError("intent contract must remain exact IbkrContractIdentity")
    sealed_contract = IbkrContractIdentity(
        conid=raw_contract.conid,
        conidex=raw_contract.conidex,
    )
    sealed_intent = IbkrWebOrderIntent.create(
        instrument_version=intent.instrument_version,
        account_id=intent.account_id,
        contract=sealed_contract,
        side=intent.side,
        order_type=intent.order_type,
        time_in_force=intent.time_in_force,
        quantity=intent.quantity,
        limit_price=intent.limit_price,
        stop_price=intent.stop_price,
        regulatory_manual_indicator_required=(
            intent.regulatory_manual_indicator_required
        ),
        manual_indicator=intent.manual_indicator,
        ext_operator=intent.ext_operator,
    )
    coid = validate_coid(client_order_id)
    point = _instant(at, name="at")
    if (
        type(maximum_session_age_seconds) is not int
        or maximum_session_age_seconds < 0
    ):
        raise IbkrWebAdapterError(
            "maximum_session_age_seconds must be a non-negative integer"
        )
    for field in ("connected", "authenticated", "established", "competing"):
        if type(getattr(session, field)) is not bool:
            raise TypeError(f"session {field} must remain exact boolean")
    session_observed_at = _instant(
        session.observed_at,
        name="session.observed_at",
    )
    if session_observed_at > point:
        raise IbkrWebAdapterError("session evidence is from the future")
    if point - session_observed_at > timedelta(
        seconds=maximum_session_age_seconds
    ):
        raise IbkrWebAdapterError("brokerage session evidence is stale")
    capability_provider_id = _text(
        capability.provider_id,
        name="capability.provider_id",
    ).upper()
    capability_account_id = _text(
        capability.account_id,
        name="capability.account_id",
    )
    capability_environment = _text(
        capability.environment,
        name="capability.environment",
    ).upper()
    capability_instrument_version = _text(
        capability.instrument_version,
        name="capability.instrument_version",
    )
    for field in ("supported_order_types", "time_in_force", "permission_scopes"):
        values = getattr(capability, field)
        if type(values) is not frozenset or any(
            type(value) is not str for value in values
        ):
            raise TypeError(f"capability {field} must remain an exact text frozenset")
    if capability_provider_id != "IBKR":
        raise IbkrWebAdapterError("capability belongs to another provider")
    if capability_account_id != sealed_intent.account_id:
        raise IbkrWebAdapterError("capability account does not match intent account")
    if capability_instrument_version != sealed_intent.instrument_version:
        raise IbkrWebAdapterError("capability instrument version does not match intent")
    _require_session_authority(
        session,
        account_id=sealed_intent.account_id,
        environment=capability_environment,
    )
    if not _capability_admits(
        capability,
        at=point,
        order_type=sealed_intent.order_type,
        time_in_force=sealed_intent.time_in_force,
        permission_scope="ORDER_WRITE",
    ):
        raise IbkrWebAdapterError("exact capability evidence does not admit this order")

    fields: dict[str, object] = {
        "acctId": sealed_intent.account_id,
        "orderType": _ORDER_TYPES[sealed_intent.order_type],
        "side": sealed_intent.side,
        "tif": sealed_intent.time_in_force,
        "cOID": coid,
    }
    if sealed_intent.contract.conid is not None:
        fields["conid"] = sealed_intent.contract.conid
    else:
        fields["conidex"] = sealed_intent.contract.conidex
    if sealed_intent.manual_indicator is not None:
        fields["manualIndicator"] = sealed_intent.manual_indicator
    if sealed_intent.ext_operator is not None:
        fields["extOperator"] = sealed_intent.ext_operator

    return IbkrNormalizedOrder(
        endpoint=f"/iserver/account/{sealed_intent.account_id}/orders",
        fields=fields,
        exact_quantity_text=_decimal_text(sealed_intent.quantity),
        exact_limit_price_text=(
            None if sealed_intent.limit_price is None else _decimal_text(sealed_intent.limit_price)
        ),
        exact_stop_price_text=(
            None if sealed_intent.stop_price is None else _decimal_text(sealed_intent.stop_price)
        ),
        capability_snapshot_id=capability.snapshot_id,
        documentation_refs=tuple(IBKR_WEB_DOCS.values()),
    )


def _bind_prepare_normalized_order(
    implementation,
    require_session_authority,
    capability_admits,
):
    """Keep financial admission guards outside mutable module lookup."""

    def prepare_normalized_order(
        intent: IbkrWebOrderIntent,
        *,
        client_order_id: str,
        capability: CapabilitySnapshot,
        session: IbkrBrokerageSessionStatus,
        at: datetime,
        maximum_session_age_seconds: int,
    ) -> IbkrNormalizedOrder:
        return implementation(
            intent,
            client_order_id=client_order_id,
            capability=capability,
            session=session,
            at=at,
            maximum_session_age_seconds=maximum_session_age_seconds,
            _require_session_authority=require_session_authority,
            _capability_admits=capability_admits,
        )

    return prepare_normalized_order


prepare_normalized_order = _bind_prepare_normalized_order(
    _prepare_normalized_order_impl,
    _require_ibkr_brokerage_session_observation,
    CapabilitySnapshot.admits,
)
del _bind_prepare_normalized_order
del _prepare_normalized_order_impl
del _require_ibkr_brokerage_session_observation


@dataclass(frozen=True)
class IbkrExecutionEvidence:
    execution_id: str
    permanent_order_id: str
    account_id: str
    quantity: Decimal
    price: Decimal

    def __post_init__(self) -> None:
        execution_id = _text(self.execution_id, name="execution_id")
        permanent_order_id = _text(
            self.permanent_order_id, name="permanent_order_id"
        )
        if _PERMANENT_ORDER_ID.fullmatch(permanent_order_id) is None:
            raise IbkrWebAdapterError(
                "permanent_order_id must be canonical non-negative integer text"
            )
        object.__setattr__(self, "execution_id", execution_id)
        object.__setattr__(self, "permanent_order_id", permanent_order_id)
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "quantity",
            _decimal(self.quantity, name="quantity", positive=True),
        )
        object.__setattr__(
            self,
            "price",
            _decimal(self.price, name="price", positive=True),
        )

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
        if type(permanent_order_id) is not int or permanent_order_id < 0:
            raise IbkrWebAdapterError(
                "permanent_order_id must be a non-negative exact integer"
            )
        return cls(
            execution_id=execution_id,
            permanent_order_id=str(permanent_order_id),
            account_id=account_id,
            quantity=_decimal(quantity, name="quantity", positive=True),
            price=_decimal(price, name="price", positive=True),
        )


@dataclass(frozen=True)
class IbkrAbsenceEvidence:
    exact_client_order_lookup_complete: bool
    exact_client_order_absent: bool
    open_orders_complete: bool
    completed_orders_complete: bool
    executions_complete: bool
    account_activity_complete: bool
    consistency_horizon_satisfied: bool
    exclusion_semantics_qualified: bool
    order_found: bool

    def __post_init__(self) -> None:
        for field in (
            "exact_client_order_lookup_complete",
            "exact_client_order_absent",
            "open_orders_complete",
            "completed_orders_complete",
            "executions_complete",
            "account_activity_complete",
            "consistency_horizon_satisfied",
            "exclusion_semantics_qualified",
            "order_found",
        ):
            if type(getattr(self, field)) is not bool:
                raise TypeError(f"{field} must be boolean")
        if self.exclusion_semantics_qualified:
            raise IbkrWebAdapterError(
                "IBKR Web foundation cannot self-assert exclusion semantics qualification"
            )
        if self.order_found and self.exact_client_order_absent:
            raise IbkrWebAdapterError(
                "order_found conflicts with exact_client_order_absent"
            )
        if self.exact_client_order_absent and not self.exact_client_order_lookup_complete:
            raise IbkrWebAdapterError(
                "exact absence requires a completed exact client-order lookup"
            )

    def verdict(self) -> str:
        if self.order_found:
            return "FOUND"
        if (
            self.exact_client_order_lookup_complete
            and self.exact_client_order_absent
            and self.open_orders_complete
            and self.completed_orders_complete
            and self.executions_complete
            and self.account_activity_complete
            and self.consistency_horizon_satisfied
            and self.exclusion_semantics_qualified
        ):
            return "PROVEN_ABSENT"
        return "INCONCLUSIVE"


@dataclass(frozen=True)
class IbkrCancelRequest:
    """One provider-relative cancel request; this object grants no send authority."""

    endpoint: str
    query: Mapping[str, str]
    account_id: str
    provider_order_id: str

    def __post_init__(self) -> None:
        if (
            type(self.account_id) is not str
            or not self.account_id
            or self.account_id != self.account_id.strip()
            or re.fullmatch(r"[A-Za-z0-9._~-]+", self.account_id) is None
            or self.account_id in {".", ".."}
        ):
            raise IbkrWebAdapterError(
                "cancel account_id must be a canonical URI path segment"
            )
        account = self.account_id
        if (
            type(self.provider_order_id) is not str
            or re.fullmatch(r"[1-9][0-9]*", self.provider_order_id) is None
        ):
            raise IbkrWebAdapterError(
                "cancel provider_order_id must be canonical positive integer text"
            )
        order_id = self.provider_order_id
        if type(self.endpoint) is not str:
            raise TypeError("cancel endpoint must be exact text")
        expected_endpoint = f"/iserver/account/{account}/order/{order_id}"
        if self.endpoint != expected_endpoint:
            raise IbkrWebAdapterError(
                "cancel endpoint does not match account/order identity"
            )
        if type(self.query) is not dict:
            raise TypeError("cancel query must be an exact dict")
        # Snapshot exact-dict entries without hashing or virtual dispatching
        # caller-controlled key objects. Key type is proven before allowlist
        # membership, so a str subclass cannot execute __hash__ in this
        # financial cancel boundary.
        items = tuple(dict.items(self.query))
        allowed = frozenset({"manualIndicator", "extOperator"})
        normalized: dict[str, str] = {}
        for key, value in items:
            if type(key) is not str or type(value) is not str:
                raise IbkrWebAdapterError("cancel query must use exact text")
            if key not in allowed:
                raise IbkrWebAdapterError("cancel query contains unsupported fields")
            if not value or value != value.strip():
                raise IbkrWebAdapterError("cancel query must use canonical text")
            normalized[key] = value
        if "manualIndicator" in normalized and normalized["manualIndicator"] not in {
            "true",
            "false",
        }:
            raise IbkrWebAdapterError(
                "cancel manualIndicator must be true or false"
            )
        if ("manualIndicator" in normalized) != ("extOperator" in normalized):
            raise IbkrWebAdapterError(
                "regulated cancel metadata must contain "
                "manualIndicator and extOperator together"
            )
        object.__setattr__(self, "query", MappingProxyType(normalized))


def prepare_cancel_request(
    *,
    account_id: str,
    provider_order_id: str,
    regulatory_manual_indicator_required: bool,
    manual_indicator: bool | None = None,
    ext_operator: str | None = None,
) -> IbkrCancelRequest:
    """Prepare an exact single-order cancel without granting outbound authority.

    IBKR's special order id -1 means cancel all open orders and is deliberately
    outside this adapter. FUT/FOP cancellation metadata is only admitted when
    the caller explicitly supplies the regulatory requirement. The returned
    request must still cross the canonical guarded dispatcher.
    """

    if type(regulatory_manual_indicator_required) is not bool:
        raise TypeError(
            "regulatory_manual_indicator_required must be boolean"
        )
    if (
        type(account_id) is not str
        or not account_id
        or account_id != account_id.strip()
        or re.fullmatch(r"[A-Za-z0-9._~-]+", account_id) is None
        or account_id in {".", ".."}
    ):
        raise IbkrWebAdapterError(
            "cancel account_id must be a canonical URI path segment"
        )
    if (
        type(provider_order_id) is not str
        or re.fullmatch(r"[1-9][0-9]*", provider_order_id) is None
    ):
        raise IbkrWebAdapterError(
            "single-order cancel requires canonical positive provider order id"
        )

    query: dict[str, str] = {}
    if regulatory_manual_indicator_required:
        if type(manual_indicator) is not bool:
            raise IbkrWebAdapterError(
                "regulated IBKR cancel requires exact manual_indicator evidence"
            )
        if (
            type(ext_operator) is not str
            or not ext_operator
            or ext_operator != ext_operator.strip()
        ):
            raise IbkrWebAdapterError(
                "regulated IBKR cancel requires canonical ext_operator evidence"
            )
        query["manualIndicator"] = "true" if manual_indicator else "false"
        query["extOperator"] = ext_operator
    elif manual_indicator is not None or ext_operator is not None:
        raise IbkrWebAdapterError(
            "unqualified cancel metadata is not accepted "
            "for non-regulated request"
        )

    return IbkrCancelRequest(
        endpoint=f"/iserver/account/{account_id}/order/{provider_order_id}",
        query=query,
        account_id=account_id,
        provider_order_id=provider_order_id,
    )


@dataclass(frozen=True)
class IbkrCancelOutcome:
    """Cancel endpoint acknowledgement; never proof of terminal cancellation."""

    provider_order_id: str
    acknowledged: bool
    message: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_order_id",
            _text(self.provider_order_id, name="provider_order_id"),
        )
        if type(self.acknowledged) is not bool:
            raise TypeError("acknowledged must be boolean")
        if self.message is not None:
            object.__setattr__(self, "message", _text(self.message, name="message"))

    @property
    def terminal_cancel_proven(self) -> bool:
        return False


def parse_cancel_response(
    *,
    provider_order_id: str,
    payload: object,
) -> IbkrCancelOutcome:
    """Classify an observed cancel response without inventing terminal state.

    A successful request means only that IBKR acknowledged the cancel request.
    The acknowledgement is accepted only when the documented integer order_id
    names the exact ticket requested. Order truth still comes from subsequent
    order/execution/reconciliation evidence because cancellation can race with
    fills.
    """

    if (
        type(provider_order_id) is not str
        or not provider_order_id
        or provider_order_id != provider_order_id.strip()
    ):
        raise IbkrWebAdapterError(
            "provider_order_id must be canonical exact text"
        )
    order_id = provider_order_id
    if type(payload) is not dict:
        raise TypeError("cancel response must be an exact object")

    error = _optional_provider_text(payload.get("error"), name="error")
    if error is not None:
        if any(key in payload for key in ("order_id", "msg", "conid", "account")):
            raise IbkrWebAdapterError("cancel response shape is ambiguous")
        return IbkrCancelOutcome(
            provider_order_id=order_id,
            acknowledged=False,
            message=error,
        )

    raw_order_id = payload.get("order_id")
    if (
        type(raw_order_id) is not int
        or raw_order_id <= 0
        or re.fullmatch(r"[1-9][0-9]*", order_id) is None
        or order_id != str(raw_order_id)
    ):
        raise IbkrWebAdapterError(
            "cancel acknowledgement order_id does not match requested order"
        )
    message = _provider_text(payload.get("msg"), name="msg")
    return IbkrCancelOutcome(
        provider_order_id=order_id,
        acknowledged=True,
        message=message,
    )


def _reply_id(value: object) -> str:
    if type(value) is not str:
        raise IbkrWebAdapterError("reply id must be a string")
    reply_id = _text(value, name="reply id")
    if (
        re.fullmatch(r"[A-Za-z0-9._~-]+", reply_id) is None
        or reply_id in {".", ".."}
    ):
        raise IbkrWebAdapterError("reply id must be a canonical URI path segment")
    return reply_id


@dataclass(frozen=True)
class IbkrSubmissionOutcome:
    """Provider response classification; acknowledgement is never a fill."""

    status: str
    provider_order_id: str | None = None
    provider_order_status: str | None = None
    reply_id: str | None = None
    messages: tuple[str, ...] = ()
    message_ids: tuple[str, ...] = ()
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ACKNOWLEDGED", "REPLY_REQUIRED", "REJECTED"}:
            raise IbkrWebAdapterError("unsupported submission outcome")
        if self.status == "ACKNOWLEDGED":
            if self.provider_order_id is None or self.provider_order_status is None:
                raise IbkrWebAdapterError("acknowledgement requires provider order identity and status")
            if self.reply_id is not None or self.rejection_reason is not None:
                raise IbkrWebAdapterError("acknowledgement cannot also be reply/rejection")
        elif self.status == "REPLY_REQUIRED":
            if self.reply_id is None or not self.messages:
                raise IbkrWebAdapterError("reply-required outcome needs reply id and message")
            if self.provider_order_id is not None or self.rejection_reason is not None:
                raise IbkrWebAdapterError("reply-required outcome is not an order acknowledgement")
        else:
            if self.rejection_reason is None:
                raise IbkrWebAdapterError("rejected outcome requires a provider reason")
            if self.provider_order_id is not None or self.reply_id is not None:
                raise IbkrWebAdapterError("rejected outcome cannot carry live order/reply identity")

    @property
    def proves_fill(self) -> bool:
        return False

    @property
    def retry_same_economic_action(self) -> bool:
        # A provider response exists after an outbound request. Reconciliation or
        # explicit reply handling is safer than blind resubmission.
        return False


def _single_submission_item(payload: object) -> Mapping[str, object]:
    if type(payload) is dict:
        return payload
    if type(payload) in {list, tuple}:
        if len(payload) != 1 or type(payload[0]) is not dict:
            raise IbkrWebAdapterError(
                "single-order adapter requires exactly one provider response object"
            )
        return payload[0]
    raise TypeError(
        "submission response must be an exact object or one-item sequence"
    )


def parse_order_submission_response(payload: object) -> IbkrSubmissionOutcome:
    """Classify the documented ACK / reply-message / explicit-error shapes.

    A reply message is not an acknowledgement and must never be auto-confirmed.
    Unknown shapes fail closed instead of being guessed into a terminal state.
    """

    item = _single_submission_item(payload)
    order_text = _optional_provider_text(item.get("order_id"), name="order_id")
    error_text = _optional_provider_text(item.get("error"), name="error")
    reply_value = item.get("id")
    message_value = item.get("message")
    has_order = order_text is not None
    has_reply = "id" in item and message_value is not None
    validated_reply_id = _reply_id(reply_value) if has_reply else None
    has_error = error_text is not None

    if sum(bool(value) for value in (has_order, has_reply, has_error)) != 1:
        raise IbkrWebAdapterError("submission response shape is ambiguous or unsupported")

    if has_order:
        return IbkrSubmissionOutcome(
            status="ACKNOWLEDGED",
            provider_order_id=order_text,
            provider_order_status=_provider_text(
                item.get("order_status", ""),
                name="order_status",
            ),
        )

    if has_reply:
        raw_messages = item["message"]
        if type(raw_messages) not in {list, tuple}:
            raise IbkrWebAdapterError(
                "reply message must be an exact sequence of strings"
            )
        messages = tuple(
            _provider_text(value, name="reply message") for value in raw_messages
        )
        raw_ids = item.get("messageIds", ())
        if type(raw_ids) not in {list, tuple}:
            raise IbkrWebAdapterError(
                "messageIds must be an exact sequence when present"
            )
        message_ids = tuple(
            _provider_text(value, name="messageId") for value in raw_ids
        )
        suppressed = item.get("isSuppressed")
        if suppressed is not None and type(suppressed) is not bool:
            raise IbkrWebAdapterError("isSuppressed must be boolean when present")
        return IbkrSubmissionOutcome(
            status="REPLY_REQUIRED",
            reply_id=validated_reply_id,
            messages=messages,
            message_ids=message_ids,
        )

    return IbkrSubmissionOutcome(
        status="REJECTED",
        rejection_reason=error_text,
    )


@dataclass(frozen=True)
class IbkrRecordedSubmission:
    """One durable-attempt observation; provider acknowledgement is never a fill."""

    attempt_id: str
    account_id: str
    environment: str
    client_order_id: str
    observed_at: datetime
    outcome: str
    next_action: str
    response_sha256: str | None
    provider_order_id: str | None = None
    provider_order_status: str | None = None
    reply_id: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        for name in ("attempt_id", "account_id", "environment"):
            object.__setattr__(self, name, _text(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "client_order_id",
            validate_coid(self.client_order_id),
        )
        object.__setattr__(
            self,
            "observed_at",
            _instant(self.observed_at, name="observed_at"),
        )
        outcome = _text(self.outcome, name="outcome").upper()
        if outcome not in {"ACKNOWLEDGED", "REPLY_REQUIRED", "REJECTED", "UNKNOWN"}:
            raise IbkrWebAdapterError("unsupported recorded submission outcome")
        object.__setattr__(self, "outcome", outcome)
        expected = {
            "ACKNOWLEDGED": "OBSERVE_OR_RECONCILE",
            "REPLY_REQUIRED": "EXPLICIT_REPLY_REQUIRED",
            "REJECTED": "DO_NOT_RETRY_BLINDLY",
            "UNKNOWN": "RECONCILE_FIRST",
        }[outcome]
        action = _text(self.next_action, name="next_action").upper()
        if action != expected:
            raise IbkrWebAdapterError(f"{outcome} requires next_action {expected}")
        object.__setattr__(self, "next_action", action)
        if self.response_sha256 is not None:
            digest = _text(self.response_sha256, name="response_sha256")
            if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
                raise IbkrWebAdapterError("response_sha256 must be canonical SHA-256")
            object.__setattr__(self, "response_sha256", digest)
        if outcome == "ACKNOWLEDGED":
            if self.provider_order_id is None or self.provider_order_status is None:
                raise IbkrWebAdapterError(
                    "recorded acknowledgement requires provider order identity and status"
                )
        if outcome == "REPLY_REQUIRED":
            if self.reply_id is None:
                raise IbkrWebAdapterError("recorded reply-required outcome needs reply id")
            object.__setattr__(self, "reply_id", _reply_id(self.reply_id))
        if outcome == "REJECTED" and self.rejection_reason is None:
            raise IbkrWebAdapterError("recorded rejection needs provider reason")
        if outcome == "UNKNOWN":
            if any(
                value is not None
                for value in (
                    self.response_sha256,
                    self.provider_order_id,
                    self.provider_order_status,
                    self.reply_id,
                    self.rejection_reason,
                )
            ):
                raise IbkrWebAdapterError(
                    "unknown transport outcome cannot assert provider response facts"
                )

    @property
    def proves_fill(self) -> bool:
        return False

    @property
    def retry_same_economic_action(self) -> bool:
        return False


def record_order_submission_result(
    normalized: IbkrNormalizedOrder,
    *,
    attempt_id: str,
    account_id: str,
    environment: str,
    observed_at: datetime,
    response_body: str | bytes | None,
    transport_ambiguous: bool = False,
) -> IbkrRecordedSubmission:
    """Bind an IBKR response, or lack of one, to the exact guarded attempt.

    A transport failure after a possible write is UNKNOWN. It cannot be retried
    until reconciliation establishes whether the cOID appeared at the provider.
    """

    if type(normalized) is not IbkrNormalizedOrder:
        raise TypeError("normalized must be exact IbkrNormalizedOrder")
    attempt = _text(attempt_id, name="attempt_id")
    account = _text(account_id, name="account_id")
    environment_value = _text(environment, name="environment").upper()
    point = _instant(observed_at, name="observed_at")
    request_account = _provider_text(
        normalized.fields.get("acctId", ""), name="acctId"
    )
    if request_account != account:
        raise IbkrWebAdapterError(
            "recorded account does not match normalized guarded order"
        )
    coid = validate_coid(
        _provider_text(normalized.fields.get("cOID", ""), name="cOID")
    )

    if transport_ambiguous:
        if response_body is not None:
            raise IbkrWebAdapterError(
                "ambiguous transport cannot also claim authoritative response bytes"
            )
        return IbkrRecordedSubmission(
            attempt_id=attempt,
            account_id=account,
            environment=environment_value,
            client_order_id=coid,
            observed_at=point,
            outcome="UNKNOWN",
            next_action="RECONCILE_FIRST",
            response_sha256=None,
        )

    if response_body is None:
        raise IbkrWebAdapterError(
            "non-ambiguous submission requires authoritative provider response bytes"
        )
    if type(response_body) is bytes:
        try:
            text_body = response_body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise IbkrWebAdapterError("provider response must be UTF-8") from error
        raw = response_body
    elif type(response_body) is str and response_body:
        text_body = response_body
        raw = response_body.encode("utf-8")
    else:
        raise IbkrWebAdapterError("provider response body is required")

    try:
        payload = json.loads(text_body)
    except json.JSONDecodeError as error:
        raise IbkrWebAdapterError("provider response JSON is invalid") from error
    parsed = parse_order_submission_response(payload)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()

    actions = {
        "ACKNOWLEDGED": "OBSERVE_OR_RECONCILE",
        "REPLY_REQUIRED": "EXPLICIT_REPLY_REQUIRED",
        "REJECTED": "DO_NOT_RETRY_BLINDLY",
    }
    return IbkrRecordedSubmission(
        attempt_id=attempt,
        account_id=account,
        environment=environment_value,
        client_order_id=coid,
        observed_at=point,
        outcome=parsed.status,
        next_action=actions[parsed.status],
        response_sha256=digest,
        provider_order_id=parsed.provider_order_id,
        provider_order_status=parsed.provider_order_status,
        reply_id=parsed.reply_id,
        rejection_reason=parsed.rejection_reason,
    )


@dataclass(frozen=True)
class IbkrReplyRequest:
    endpoint: str
    body: Mapping[str, object]
    attempt_id: str
    account_id: str
    client_order_id: str
    response_sha256: str

    def __post_init__(self) -> None:
        endpoint = _text(self.endpoint, name="endpoint")
        prefix = "/iserver/reply/"
        if not endpoint.startswith(prefix):
            raise IbkrWebAdapterError("reply endpoint must use /iserver/reply/<reply-id>")
        _reply_id(endpoint[len(prefix):])
        if type(self.body) is not dict or self.body != {"confirmed": True}:
            raise IbkrWebAdapterError("reply request body must be exactly confirmed=true")
        object.__setattr__(self, "endpoint", endpoint)
        object.__setattr__(self, "body", MappingProxyType(dict(self.body)))
        object.__setattr__(self, "attempt_id", _text(self.attempt_id, name="attempt_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(
            self,
            "client_order_id",
            validate_coid(self.client_order_id),
        )
        digest = _text(self.response_sha256, name="response_sha256")
        if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise IbkrWebAdapterError("response_sha256 must be canonical SHA-256")
        object.__setattr__(self, "response_sha256", digest)


def prepare_reply_confirmation(
    recorded: IbkrRecordedSubmission,
    *,
    expected_attempt_id: str,
    expected_account_id: str,
    expected_client_order_id: str,
    explicit_authorization: bool,
) -> IbkrReplyRequest:
    """Prepare a reply only from the durable, exact guarded attempt observation.

    The returned request is bound to the attempt/account/cOID/response digest
    that produced the reply id and must still cross GuardedDispatcher.
    """

    if type(recorded) is not IbkrRecordedSubmission:
        raise TypeError("recorded must be exact IbkrRecordedSubmission")
    if type(explicit_authorization) is not bool:
        raise TypeError("explicit_authorization must be boolean")
    if recorded.outcome != "REPLY_REQUIRED":
        raise IbkrWebAdapterError("only a recorded reply-required outcome can be confirmed")
    if recorded.response_sha256 is None:
        raise IbkrWebAdapterError("IBKR reply requires durable provider response evidence")
    if recorded.attempt_id != _text(expected_attempt_id, name="expected_attempt_id"):
        raise IbkrWebAdapterError("reply attempt does not match guarded attempt")
    if recorded.account_id != _text(expected_account_id, name="expected_account_id"):
        raise IbkrWebAdapterError("reply account does not match guarded account")
    expected_coid = validate_coid(expected_client_order_id)
    if recorded.client_order_id != expected_coid:
        raise IbkrWebAdapterError("reply cOID does not match guarded client order")
    if not explicit_authorization:
        raise IbkrWebAdapterError("IBKR reply requires explicit authorization")
    return IbkrReplyRequest(
        endpoint=f"/iserver/reply/{recorded.reply_id}",
        body={"confirmed": True},
        attempt_id=recorded.attempt_id,
        account_id=recorded.account_id,
        client_order_id=recorded.client_order_id,
        response_sha256=recorded.response_sha256,
    )


def parse_web_api_trades(
    observation: ProviderResponseObservation,
    *,
    instrument_versions_by_conid: Mapping[int, str],
    fee_currency_by_execution_id: Mapping[str, str],
) -> tuple[ProviderFillEvidence, ...]:
    """Normalize one capability-bound exact-byte IBKR trades read into fills.

    Account and environment are inherited from the pre-I/O VERIFIED capability
    binding. They are deliberately not caller parameters, so reconciliation
    evidence cannot be relabelled after the provider response is observed.
    Numeric JSON floats remain rejected by _decimal; fee currency is separate
    explicit evidence because the trades row does not canonically carry it.
    """

    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be exact ProviderResponseObservation")
    binding = observation.query_binding
    if type(binding) is not AuthenticatedReadQueryBinding:
        raise TypeError(
            "observation query_binding must be exact AuthenticatedReadQueryBinding"
        )
    binding_provider = _provider_text(
        binding.provider_id,
        name="observation.provider_id",
    )
    binding_account = _provider_text(
        binding.account_id,
        name="observation.account_id",
    )
    binding_environment = _provider_text(
        binding.environment,
        name="observation.environment",
    )
    binding_endpoint = _provider_text(
        binding.endpoint,
        name="observation.endpoint",
    )
    if type(binding.surface) is not Surface:
        raise TypeError("observation surface must be exact Surface")
    evidence_ref = _provider_text(
        observation.evidence_ref,
        name="observation.evidence_ref",
    )
    if re.fullmatch(r"provider-read:sha256:[0-9a-f]{64}", evidence_ref) is None:
        raise IbkrWebAdapterError(
            "observation evidence_ref must be canonical provider-read evidence"
        )
    AuthenticatedReadQueryBinding.require_scope(
        binding,
        provider_id="IBKR",
        surface=Surface.ACTIVITIES,
        endpoint="/iserver/account/trades",
    )
    if binding_provider != "IBKR":
        raise IbkrWebAdapterError("trade observation belongs to another provider")
    if binding_endpoint != "/iserver/account/trades":
        raise IbkrWebAdapterError("trade observation endpoint is not canonical")
    payload = observation.payload
    if type(payload) is not tuple:
        raise IbkrWebAdapterError(
            "trades response must be the canonical frozen JSON array"
        )
    if type(instrument_versions_by_conid) is not dict:
        raise TypeError("instrument_versions_by_conid must be an exact dict")
    if type(fee_currency_by_execution_id) is not dict:
        raise TypeError("fee_currency_by_execution_id must be an exact dict")
    for conid_key, instrument_value in instrument_versions_by_conid.items():
        if type(conid_key) is not int or conid_key <= 0:
            raise IbkrWebAdapterError(
                "instrument_versions_by_conid keys must be positive exact integers"
            )
        if (
            type(instrument_value) is not str
            or not instrument_value
            or instrument_value != instrument_value.strip()
        ):
            raise IbkrWebAdapterError(
                "instrument_versions_by_conid values must be exact text"
            )
    for execution_key, currency_value in fee_currency_by_execution_id.items():
        if (
            type(execution_key) is not str
            or not execution_key
            or execution_key != execution_key.strip()
        ):
            raise IbkrWebAdapterError(
                "fee_currency_by_execution_id keys must be exact text"
            )
        if (
            type(currency_value) is not str
            or not currency_value
            or currency_value != currency_value.strip()
        ):
            raise IbkrWebAdapterError(
                "fee_currency_by_execution_id values must be exact text"
            )
    account = binding_account
    environment = binding_environment
    by_execution: dict[str, ProviderFillEvidence] = {}
    selected_by_execution_family: dict[
        tuple[str, str],
        tuple[tuple[int, str] | None, str, ProviderFillEvidence],
    ] = {}

    for index, raw in enumerate(payload):
        if type(raw) is not MappingProxyType:
            raise IbkrWebAdapterError(
                f"trades[{index}] must be a canonical frozen JSON object"
            )
        execution_id = _provider_text(
            raw.get("execution_id"), name="execution_id"
        )
        account_values: list[str] = []
        for field_name in ("account", "accountCode"):
            if field_name in raw and raw[field_name] is not None:
                account_values.append(
                    _provider_text(raw[field_name], name=f"trade.{field_name}")
                )
        if not account_values:
            raise IbkrWebAdapterError(
                "trade response must identify the reconciliation account"
            )
        if any(value != account_values[0] for value in account_values[1:]):
            raise IbkrWebAdapterError(
                "trade account and accountCode identifiers conflict"
            )
        observed_account = account_values[0]
        if observed_account != account:
            raise IbkrWebAdapterError(
                "trade account does not match reconciliation account"
            )

        conid = raw.get("conid")
        if type(conid) is not int or conid <= 0:
            raise IbkrWebAdapterError("trade conid must be a positive exact integer")
        raw_conidex = raw.get("conidEx")
        if raw_conidex is not None:
            conidex = _provider_text(raw_conidex, name="trade.conidEx")
            if ";;;" in conidex:
                raise IbkrWebAdapterError(
                    "IBKR combo/spread trade reconciliation is not qualified"
                )
            conidex_match = _TRADE_CONIDEX.fullmatch(conidex)
            if conidex_match is None or conidex_match.group("conid") != str(conid):
                raise IbkrWebAdapterError(
                    "trade conidEx does not match trade conid"
                )
        if conid not in instrument_versions_by_conid:
            raise IbkrWebAdapterError(f"unmapped IBKR conid: {conid}")
        instrument = _text(
            instrument_versions_by_conid[conid], name="instrument_version"
        )

        raw_client_id = raw.get("order_ref")
        if raw_client_id is None or raw_client_id == "":
            client_id = None
        else:
            client_id = validate_coid(
                _provider_text(raw_client_id, name="order_ref")
            )

        if execution_id not in fee_currency_by_execution_id:
            raise IbkrWebAdapterError(
                f"missing fee currency evidence for IBKR execution: {execution_id}"
            )
        fee_currency = _text(
            fee_currency_by_execution_id[execution_id], name="fee_currency"
        )
        raw_side = _text(raw.get("side"), name="trade.side")
        side_by_provider_value = {
            "B": "BUY",
            "S": "SELL",
        }
        if raw_side not in side_by_provider_value:
            raise IbkrWebAdapterError(
                "trade side must be provider-evidenced B or S"
            )
        side = side_by_provider_value[raw_side]
        trade_time = _canonical_web_trade_time(
            raw.get("trade_time"),
            epoch_milliseconds=raw.get("trade_time_r"),
        )
        fill = ProviderFillEvidence.create(
            provider_id="IBKR",
            account_id=account,
            environment=environment,
            provider_execution_id=_stable_execution_id(execution_id),
            client_order_id=client_id,
            instrument=instrument,
            side=side,
            quantity=_decimal(raw.get("size"), name="trade.size", positive=True),
            price=_decimal(raw.get("price"), name="trade.price", positive=True),
            fee_amount=_decimal(raw.get("commission"), name="trade.commission"),
            fee_currency=fee_currency,
            trade_time=trade_time,
            evidence_refs=(evidence_ref,),
        )
        prior = by_execution.get(execution_id)
        if prior is not None:
            if prior != fill:
                raise IbkrWebAdapterError(
                    "IBKR execution id appears with conflicting economic content"
                )
            continue
        by_execution[execution_id] = fill

        correction = _execution_correction_identity(execution_id)
        if correction is None:
            family_key = ("exact", execution_id)
            selected_by_execution_family[family_key] = (None, execution_id, fill)
            continue

        family, revision_key = correction
        family_key = ("correction", family)
        selected = selected_by_execution_family.get(family_key)
        if selected is None:
            selected_by_execution_family[family_key] = (
                revision_key,
                execution_id,
                fill,
            )
            continue
        selected_revision, selected_execution_id, _selected_fill = selected
        if selected_revision == revision_key and selected_execution_id != execution_id:
            raise IbkrWebAdapterError(
                "IBKR execution correction revision is ambiguous"
            )
        if selected_revision is None or revision_key > selected_revision:
            selected_by_execution_family[family_key] = (
                revision_key,
                execution_id,
                fill,
            )

    return tuple(
        sorted(
            (
                selected_fill
                for _revision, _execution_id, selected_fill
                in selected_by_execution_family.values()
            ),
            key=lambda fill: (fill.trade_time, fill.provider_execution_id),
        )
    )


def execution_to_reconciliation_fill(
    execution: IbkrExecutionEvidence,
    *,
    environment: str,
    client_order_id: str | None,
    expected_account_id: str,
    instrument: str,
    fee_amount,
    fee_currency: str,
    trade_time: str,
    evidence_refs: tuple[str, ...],
) -> ProviderFillEvidence:
    """Bind unique IBKR execution identity into canonical account truth.

    Fee and trade-time evidence are explicit inputs because an execution row
    must not invent commission or timestamp evidence that was not observed.
    """

    if type(execution) is not IbkrExecutionEvidence:
        raise TypeError("execution must be exact IbkrExecutionEvidence")
    execution_id = _text(
        execution.execution_id,
        name="execution.execution_id",
    )
    permanent_order_id = _text(
        execution.permanent_order_id,
        name="execution.permanent_order_id",
    )
    if _PERMANENT_ORDER_ID.fullmatch(permanent_order_id) is None:
        raise IbkrWebAdapterError(
            "execution permanent_order_id must be canonical non-negative integer text"
        )
    execution_account = _text(
        execution.account_id,
        name="execution.account_id",
    )
    execution_quantity = _decimal(
        execution.quantity,
        name="execution.quantity",
        positive=True,
    )
    execution_price = _decimal(
        execution.price,
        name="execution.price",
        positive=True,
    )
    account = _text(expected_account_id, name="expected_account_id")
    if execution_account != account:
        raise IbkrWebAdapterError("execution account does not match reconciliation account")
    client_id = None if client_order_id is None else validate_coid(client_order_id)
    if (
        type(evidence_refs) is not tuple
        or not evidence_refs
        or any(
            type(reference) is not str
            or not reference
            or reference != reference.strip()
            for reference in evidence_refs
        )
        or len(set(evidence_refs)) != len(evidence_refs)
    ):
        raise IbkrWebAdapterError(
            "execution reconciliation requires exact unique evidence_refs"
        )
    return ProviderFillEvidence.create(
        provider_id="IBKR",
        account_id=account,
        environment=environment,
        provider_execution_id=_stable_execution_id(execution_id),
        client_order_id=client_id,
        instrument=_text(instrument, name="instrument"),
        quantity=execution_quantity,
        price=execution_price,
        fee_amount=_decimal(fee_amount, name="fee_amount"),
        fee_currency=_text(fee_currency, name="fee_currency"),
        trade_time=_text(trade_time, name="trade_time"),
        evidence_refs=evidence_refs,
    )
