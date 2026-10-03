"""Exact content identity for one financially admitted provider request.

This module deliberately does not issue trading authority.  It defines the
immutable, versioned material that the product-owned #987 financial composition
must seal and that dispatch/recovery/reconciliation may later retain.  A valid
value proves only canonical content identity; it is not by itself permission to
send a provider request.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
from uuid import UUID

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_canonical_decimal_text,
)
from .persistence import canonical_json


_SCHEMA_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RISK_SNAPSHOT_ID_RE = re.compile(r"^risk-snapshot:sha256:[0-9a-f]{64}$")
_RISK_DECISION_ID_RE = re.compile(r"^risk:sha256:[0-9a-f]{64}$")
_PROVIDER_SCOPE_DIGEST_RE = re.compile(
    r"^provider-financial-scope:sha256:[0-9a-f]{64}$"
)
_PROVIDER_QUALIFICATION_DIGEST_RE = re.compile(
    r"^provider-qualification:sha256:[0-9a-f]{64}$"
)
_PROVIDER_ACCOUNT_CUT_ID_RE = re.compile(
    r"^provider-account-cut:sha256:[0-9a-f]{64}$"
)


class FinancialRequestBindingError(ValueError):
    """Raised when financially meaningful request identity is non-canonical."""


def _text(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise FinancialRequestBindingError(f"{name} must be canonical non-empty text")
    return value.upper() if upper else value


def _digest(value: object, *, name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise FinancialRequestBindingError(
            f"{name} must be canonical lowercase sha256:<64-hex>"
        )
    return value


def _namespaced_digest(
    value: object,
    *,
    name: str,
    pattern: re.Pattern[str],
    expected: str,
) -> str:
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise FinancialRequestBindingError(f"{name} must be canonical {expected}")
    return value


def _nonnegative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise FinancialRequestBindingError(f"{name} must be a non-negative exact integer")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise FinancialRequestBindingError(f"{name} must be a positive exact integer")
    return value


def _canonical_decimal(value: object, *, name: str, positive: bool = False) -> str:
    if type(value) is not str:
        raise FinancialRequestBindingError(f"{name} must be canonical Decimal text")
    try:
        parsed = parse_canonical_decimal_text(value)
        canonical = canonical_decimal_text(parsed)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise FinancialRequestBindingError(
            f"{name} must be canonical bounded Decimal text"
        ) from error
    if canonical != value:
        raise FinancialRequestBindingError(f"{name} is not canonical Decimal text")
    if positive and parsed <= 0:
        raise FinancialRequestBindingError(f"{name} must be positive")
    return canonical


def _uuid(value: object, *, name: str) -> str:
    raw = _text(value, name=name)
    try:
        canonical = str(UUID(raw))
    except (ValueError, TypeError, AttributeError) as error:
        raise FinancialRequestBindingError(f"{name} must be a canonical UUID") from error
    if raw != canonical:
        raise FinancialRequestBindingError(f"{name} must use canonical UUID text")
    return canonical


@dataclass(frozen=True, slots=True)
class FinancialRequestBindingMaterial:
    """Canonical financial + wire identity for exactly one prepared request."""

    risk_snapshot_id: str
    risk_decision_id: str
    admitted_journal_sequence_cut: int
    account_cut_id: str
    account_cut_digest: str
    account_head_journal_sequence: int
    reservation_id: str
    reservation_scope_digest: str
    reservation_version: int
    reservation_state_digest: str
    provider_scope_digest: str
    provider_id: str
    account_id: str
    runtime_environment: str
    provider_environment: str
    entity_policy_id: str
    instrument_id: str
    instrument_version: int
    quantity_unit: str
    equivalent_exposure_digest: str
    capability_snapshot_id: str
    qualification_identity_digest: str
    client_order_id: str
    side: str
    quantity: str
    price: str | None
    price_semantics_digest: str
    order_type: str
    time_in_force: str
    reduce_only: bool
    trigger_protection_digest: str
    endpoint: str
    query_sha256: str
    body_sha256: str
    request_sha256: str
    submission_scope_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "risk_snapshot_id",
            _namespaced_digest(
                self.risk_snapshot_id,
                name="risk_snapshot_id",
                pattern=_RISK_SNAPSHOT_ID_RE,
                expected="risk-snapshot:sha256:<64-hex>",
            ),
        )
        object.__setattr__(
            self,
            "risk_decision_id",
            _namespaced_digest(
                self.risk_decision_id,
                name="risk_decision_id",
                pattern=_RISK_DECISION_ID_RE,
                expected="risk:sha256:<64-hex>",
            ),
        )
        object.__setattr__(
            self,
            "admitted_journal_sequence_cut",
            _nonnegative_int(
                self.admitted_journal_sequence_cut,
                name="admitted_journal_sequence_cut",
            ),
        )
        object.__setattr__(
            self,
            "account_cut_id",
            _namespaced_digest(
                self.account_cut_id,
                name="account_cut_id",
                pattern=_PROVIDER_ACCOUNT_CUT_ID_RE,
                expected="provider-account-cut:sha256:<64-hex>",
            ),
        )
        object.__setattr__(
            self,
            "account_cut_digest",
            _digest(self.account_cut_digest, name="account_cut_digest"),
        )
        object.__setattr__(
            self,
            "account_head_journal_sequence",
            _nonnegative_int(
                self.account_head_journal_sequence,
                name="account_head_journal_sequence",
            ),
        )
        if self.account_head_journal_sequence > self.admitted_journal_sequence_cut:
            raise FinancialRequestBindingError(
                "account head cannot be newer than admitted journal cut"
            )
        object.__setattr__(
            self,
            "reservation_id",
            _text(self.reservation_id, name="reservation_id"),
        )
        object.__setattr__(
            self,
            "provider_scope_digest",
            _namespaced_digest(
                self.provider_scope_digest,
                name="provider_scope_digest",
                pattern=_PROVIDER_SCOPE_DIGEST_RE,
                expected="provider-financial-scope:sha256:<64-hex>",
            ),
        )
        object.__setattr__(
            self,
            "qualification_identity_digest",
            _namespaced_digest(
                self.qualification_identity_digest,
                name="qualification_identity_digest",
                pattern=_PROVIDER_QUALIFICATION_DIGEST_RE,
                expected="provider-qualification:sha256:<64-hex>",
            ),
        )
        for name in (
            "reservation_scope_digest",
            "reservation_state_digest",
            "equivalent_exposure_digest",
            "price_semantics_digest",
            "trigger_protection_digest",
            "query_sha256",
            "body_sha256",
            "request_sha256",
            "submission_scope_digest",
        ):
            object.__setattr__(self, name, _digest(getattr(self, name), name=name))
        object.__setattr__(
            self,
            "reservation_version",
            _nonnegative_int(self.reservation_version, name="reservation_version"),
        )
        provider_id = _text(self.provider_id, name="provider_id", upper=True)
        runtime_environment = _text(
            self.runtime_environment,
            name="runtime_environment",
            upper=True,
        )
        if runtime_environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise FinancialRequestBindingError("runtime_environment is unsupported")
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "runtime_environment", runtime_environment)
        object.__setattr__(
            self,
            "provider_environment",
            _text(
                self.provider_environment,
                name="provider_environment",
                upper=True,
            ),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="account_id"),
        )
        object.__setattr__(
            self,
            "entity_policy_id",
            _text(self.entity_policy_id, name="entity_policy_id"),
        )
        object.__setattr__(
            self,
            "instrument_id",
            _uuid(self.instrument_id, name="instrument_id"),
        )
        object.__setattr__(
            self,
            "instrument_version",
            _positive_int(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(
            self,
            "quantity_unit",
            _text(self.quantity_unit, name="quantity_unit", upper=True),
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _text(self.capability_snapshot_id, name="capability_snapshot_id"),
        )
        object.__setattr__(
            self,
            "client_order_id",
            _text(self.client_order_id, name="client_order_id"),
        )
        side = _text(self.side, name="side", upper=True)
        if side not in {"BUY", "SELL"}:
            raise FinancialRequestBindingError("side must be BUY or SELL")
        object.__setattr__(self, "side", side)
        object.__setattr__(
            self,
            "quantity",
            _canonical_decimal(self.quantity, name="quantity", positive=True),
        )
        if self.price is not None:
            object.__setattr__(
                self,
                "price",
                _canonical_decimal(self.price, name="price", positive=True),
            )
        object.__setattr__(
            self,
            "order_type",
            _text(self.order_type, name="order_type", upper=True),
        )
        object.__setattr__(
            self,
            "time_in_force",
            _text(self.time_in_force, name="time_in_force", upper=True),
        )
        if type(self.reduce_only) is not bool:
            raise FinancialRequestBindingError("reduce_only must be an exact boolean")
        object.__setattr__(
            self,
            "endpoint",
            _text(self.endpoint, name="endpoint"),
        )

    def payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "risk_snapshot_id": self.risk_snapshot_id,
            "risk_decision_id": self.risk_decision_id,
            "admitted_journal_sequence_cut": self.admitted_journal_sequence_cut,
            "account_cut_id": self.account_cut_id,
            "account_cut_digest": self.account_cut_digest,
            "account_head_journal_sequence": self.account_head_journal_sequence,
            "reservation_id": self.reservation_id,
            "reservation_scope_digest": self.reservation_scope_digest,
            "reservation_version": self.reservation_version,
            "reservation_state_digest": self.reservation_state_digest,
            "provider_scope_digest": self.provider_scope_digest,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "runtime_environment": self.runtime_environment,
            "provider_environment": self.provider_environment,
            "entity_policy_id": self.entity_policy_id,
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "quantity_unit": self.quantity_unit,
            "equivalent_exposure_digest": self.equivalent_exposure_digest,
            "capability_snapshot_id": self.capability_snapshot_id,
            "qualification_identity_digest": self.qualification_identity_digest,
            "client_order_id": self.client_order_id,
            "side": self.side,
            "quantity": self.quantity,
            "price": self.price,
            "price_semantics_digest": self.price_semantics_digest,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "trigger_protection_digest": self.trigger_protection_digest,
            "endpoint": self.endpoint,
            "query_sha256": self.query_sha256,
            "body_sha256": self.body_sha256,
            "request_sha256": self.request_sha256,
            "submission_scope_digest": self.submission_scope_digest,
        }

    @property
    def binding_id(self) -> str:
        digest = sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()
        return "financial-request:sha256:" + digest
