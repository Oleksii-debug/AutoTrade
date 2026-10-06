"""Deterministic authority and confirmation gates for AutoTrade."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import threading
import weakref
from types import MappingProxyType
from typing import Any, Callable, FrozenSet, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from .allocation import (
    AllocationPolicy,
    EvidenceBoundObjectiveAllocationResult,
    ImmutableAllocationEvidence,
    revalidate_evidence_bound_allocation,
)
from .durable_reservations import DurableReservationBook
from .durable_settlement import DurableSettlementBook
from .exact_decimal import exact_add, exact_subtract, exact_abs
from .provider_activity_accounting import DurableProviderEconomicBook
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .reconciliation_journal import load_account_resource_availability_evidence
from .securities_borrow import (
    BorrowAvailabilityEvidence,
    DurableBorrowRecallProjection,
    borrow_resource_key,
    incremental_short_borrow_quantity,
)
from .settlement import BuyingPowerEvidence
from .risk import (
    LiquidationHeadroomEvidence,
    LiquidationScope,
    RiskContext,
    _canonical_decimal_text,
    RiskDecision,
    RiskIntent,
    RiskPolicy,
    RISK_ARITHMETIC_POLICY_ID,
    evaluate_bound_risk,
    normalize_reservation_requirements,
    reservation_requirements_payload,
    risk_decision_fingerprint,
    validate_bound_risk_decision,
)
from .risk_policy_authority import (
    DurableRiskPolicyRegistry, ResolvedRiskPolicy, RiskPolicyScope,
    canonical_risk_policy, risk_policy_payload,
    require_registry_issued_resolved_policy, journal_store_identity_digest,
)


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _authority_text(value: object, *, name: str) -> str:
    """Detach authority-bearing text without invoking polymorphic callbacks."""

    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _authority_decimal(value: object, *, name: str) -> Decimal:
    """Parse financial authority scalars only after exact built-in admission."""

    if type(value) not in {Decimal, str, int}:
        raise TypeError(
            f"{name} must use exact Decimal, string or integer input"
        )
    return _decimal(value, name=name)


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)

def _authority_datetime_utc(value: object, *, name: str) -> datetime:
    """Normalize exact built-in datetime authority without hostile tzinfo callbacks."""

    if type(value) is not datetime:
        raise TypeError(f"{name} must be an exact datetime")
    selected_timezone = value.tzinfo
    if type(selected_timezone) is not type(timezone.utc):
        raise TypeError(f"{name} must use an exact built-in timezone")
    if value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, order=True)
class InstrumentVersionIdentity:
    """Immutable authority reference to one canonical instrument version."""

    instrument_id: str
    version: int

    def __post_init__(self) -> None:
        raw_id = _text(self.instrument_id, name="instrument_id")
        try:
            canonical_id = str(UUID(raw_id))
        except (ValueError, TypeError, AttributeError) as error:
            raise ValueError("instrument_id must be a UUID") from error
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("instrument_version must be a positive integer")
        object.__setattr__(self, "instrument_id", canonical_id)


def _instrument_identity(value, *, name: str = "instrument") -> InstrumentVersionIdentity:
    if isinstance(value, InstrumentVersionIdentity):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return InstrumentVersionIdentity(value[0], value[1])
    raise TypeError(
        f"{name} must be InstrumentVersionIdentity or an (instrument_id, version) pair"
    )


@dataclass(frozen=True)
class AuthorityPolicy:
    policy_id: str
    account_id: str
    environments: FrozenSet[str]
    instruments: FrozenSet[InstrumentVersionIdentity]
    actions: FrozenSet[str]
    max_notional: Decimal
    expires_at: str
    autonomous: bool
    valid_from: str = "1970-01-01T00:00:00Z"
    protection_only: bool = False
    version: int = 1

    def __post_init__(self) -> None:
        # The policy object itself is an authority boundary.  Callers can
        # instantiate dataclasses directly, so validation cannot live only in
        # create(); otherwise truthy non-booleans such as "false" could bypass
        # the confirmation requirement through policy.autonomous.
        if not isinstance(self.autonomous, bool) or not isinstance(
            self.protection_only, bool
        ):
            raise TypeError("autonomous and protection_only must be booleans")
        if (
            not isinstance(self.version, int)
            or isinstance(self.version, bool)
            or self.version < 1
        ):
            raise ValueError("authority policy version must be a positive integer")
        if isinstance(self.environments, (str, bytes)):
            raise TypeError("environments must be a collection")
        if isinstance(self.instruments, (str, bytes)):
            raise TypeError("instruments must be a collection")
        if isinstance(self.actions, (str, bytes)):
            raise TypeError("actions must be a collection")

        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in self.environments
        )
        if (
            not normalized_environments
            or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}
        ):
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in self.instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in self.actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")

        notional = _decimal(self.max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from = _text(self.valid_from, name="valid_from")
        expires_at = _text(self.expires_at, name="expires_at")
        if _instant(valid_from, name="valid_from") >= _instant(
            expires_at, name="expires_at"
        ):
            raise ValueError("valid_from must precede expires_at")

        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        object.__setattr__(self, "environments", normalized_environments)
        object.__setattr__(self, "instruments", normalized_instruments)
        object.__setattr__(self, "actions", normalized_actions)
        object.__setattr__(self, "max_notional", notional)
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "expires_at", expires_at)

    @classmethod
    def create(
        cls,
        *,
        policy_id: str,
        account_id: str,
        environments,
        instruments,
        actions,
        max_notional,
        expires_at: str,
        autonomous: bool,
        valid_from: str = "1970-01-01T00:00:00Z",
        protection_only: bool = False,
        version: int = 1,
    ) -> "AuthorityPolicy":
        normalized_environments = frozenset(
            _text(item, name="environment").upper() for item in environments
        )
        if not normalized_environments or not normalized_environments <= {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environments must contain supported values")
        normalized_instruments = frozenset(
            _instrument_identity(item) for item in instruments
        )
        normalized_actions = frozenset(
            _text(item, name="action").upper() for item in actions
        )
        if not normalized_instruments or not normalized_actions:
            raise ValueError("instruments and actions must be non-empty")
        notional = _decimal(max_notional, name="max_notional")
        if notional <= 0:
            raise ValueError("max_notional must be positive")
        valid_from_instant = _instant(valid_from, name="valid_from")
        expires_at_instant = _instant(expires_at, name="expires_at")
        if valid_from_instant >= expires_at_instant:
            raise ValueError("valid_from must precede expires_at")
        if not isinstance(autonomous, bool) or not isinstance(protection_only, bool):
            raise TypeError("autonomous and protection_only must be booleans")
        return cls(
            policy_id=_text(policy_id, name="policy_id"),
            account_id=_text(account_id, name="account_id"),
            environments=normalized_environments,
            instruments=normalized_instruments,
            actions=normalized_actions,
            max_notional=notional,
            expires_at=expires_at,
            autonomous=autonomous,
            valid_from=valid_from,
            protection_only=protection_only,
            version=version,
        )


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
    action: str
    notional: Decimal
    expires_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "confirmation_id", _text(self.confirmation_id, name="confirmation_id")
        )
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(
            self, "intent_hash", _text(self.intent_hash, name="intent_hash")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("confirmation environment is unsupported")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "action", _text(self.action, name="action").upper())
        notional = _decimal(self.notional, name="notional")
        if notional < 0:
            raise ValueError("confirmation notional must be non-negative")
        object.__setattr__(self, "notional", notional)
        expires_at = _text(self.expires_at, name="expires_at")
        _instant(expires_at, name="confirmation.expires_at")
        object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    policy_id: str
    intent_hash: str
    account_id: str
    environment: str
    instrument_version: InstrumentVersionIdentity
    action: str
    notional: Decimal
    risk_reducing: bool
    state_version: int
    authority_epoch: int
    outcome: str
    admitted_at: str
    confirmation_id: str | None
    reason: str
    request_fingerprint: str
    intent_id: str | None = None
    risk_decision_id: str | None = None
    reservation_id: str | None = None
    capability_snapshot_id: str | None = None
    risk_valid_until: str | None = None
    policy_version: int | None = None
    financial_command_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "admission_id", _text(self.admission_id, name="admission_id")
        )
        object.__setattr__(self, "policy_id", _text(self.policy_id, name="policy_id"))
        object.__setattr__(
            self, "intent_hash", _text(self.intent_hash, name="intent_hash")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("admission environment is unsupported")
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(self, "action", _text(self.action, name="action").upper())
        notional = _decimal(self.notional, name="notional")
        if notional < 0:
            raise ValueError("admission notional must be non-negative")
        object.__setattr__(self, "notional", notional)
        if not isinstance(self.risk_reducing, bool):
            raise TypeError("admission risk_reducing must be boolean")
        if (
            not isinstance(self.state_version, int)
            or isinstance(self.state_version, bool)
            or self.state_version < 0
        ):
            raise ValueError("admission state_version is invalid")
        if (
            not isinstance(self.authority_epoch, int)
            or isinstance(self.authority_epoch, bool)
            or self.authority_epoch < 0
        ):
            raise ValueError("admission authority_epoch is invalid")
        outcome = _text(self.outcome, name="outcome").upper()
        if outcome not in {"ADMITTED", "REJECTED"}:
            raise ValueError("admission outcome is invalid")
        object.__setattr__(self, "outcome", outcome)
        admitted_at = _text(self.admitted_at, name="admitted_at")
        _instant(admitted_at, name="admitted_at")
        object.__setattr__(self, "admitted_at", admitted_at)
        if self.confirmation_id is not None:
            confirmation_id = _text(self.confirmation_id, name="confirmation_id")
            if outcome != "ADMITTED":
                raise ValueError("rejected admission cannot consume confirmation")
            object.__setattr__(self, "confirmation_id", confirmation_id)
        object.__setattr__(self, "reason", _text(self.reason, name="reason"))
        fingerprint = _text(
            self.request_fingerprint, name="request_fingerprint"
        ).lower()
        if len(fingerprint) != 64 or any(
            character not in "0123456789abcdef" for character in fingerprint
        ):
            raise ValueError("request_fingerprint must be a SHA-256 hex digest")
        object.__setattr__(self, "request_fingerprint", fingerprint)

        evidence_values = (
            self.intent_id,
            self.risk_decision_id,
            self.reservation_id,
            self.capability_snapshot_id,
            self.risk_valid_until,
            self.policy_version,
            self.financial_command_id,
        )
        if any(value is not None for value in evidence_values):
            if outcome == "ADMITTED" and any(value is None for value in evidence_values):
                raise ValueError(
                    "admitted financial record requires complete risk/reservation evidence"
                )
            for field_name, value in (
                ("intent_id", self.intent_id),
                ("risk_decision_id", self.risk_decision_id),
                ("reservation_id", self.reservation_id),
                ("capability_snapshot_id", self.capability_snapshot_id),
                ("risk_valid_until", self.risk_valid_until),
                ("financial_command_id", self.financial_command_id),
            ):
                if value is not None:
                    object.__setattr__(
                        self, field_name, _text(value, name=field_name)
                    )
            if self.risk_valid_until is not None:
                _instant(self.risk_valid_until, name="risk_valid_until")
            if self.policy_version is not None and (
                not isinstance(self.policy_version, int)
                or isinstance(self.policy_version, bool)
                or self.policy_version < 1
            ):
                raise ValueError("admission policy_version must be positive")


class AuthorityConflict(ValueError):
    """Raised when immutable authority identity is reused inconsistently."""


def _authority_service_store_operations():
    """Seal one AuthorityService to one exact JournalStore generation.

    The selected store and physical identity live in closure-owned process state,
    not in caller-writable AuthorityService instance/module data. Callback-free
    weakrefs let dead services/stores be reclaimed; stale object-id entries are
    discarded lazily on reuse without reopening a live one-shot binding.
    """

    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            weakref.ReferenceType | None,
            object | None,
            tuple[str, ...] | None,
        ],
    ] = {}
    state_lock = threading.RLock()
    missing = object()

    def register(service: object, store: JournalStore | None, risk_policy_scope: RiskPolicyScope | None = None) -> None:
        scope_payload = None
        if risk_policy_scope is not None:
            if type(risk_policy_scope) is not RiskPolicyScope or store is None:
                raise TypeError("durable RiskPolicy scope requires exact scope and JournalStore")
            scope_payload = tuple(risk_policy_scope.payload().values())
        identity = None
        store_ref = None
        if store is not None:
            identity = require_exact_journal_store_authority(
                store,
                subject="AuthorityService journal store",
            )
            # Keep process registry references callback-free. A weakref callback
            # is discoverable from the live referent and can otherwise be
            # invoked manually to erase a one-shot authority binding.
            store_ref = weakref.ref(store)

        object_id = id(service)
        with state_lock:
            current = states.get(object_id)
            if current is not None:
                current_service = current[0]()
                if current_service is service:
                    raise AuthorityConflict(
                        "AuthorityService journal composition is already initialized"
                    )
                if current_service is not None:
                    raise AuthorityConflict(
                        "AuthorityService journal binding identity collision"
                    )
                states.pop(object_id, None)
            states[object_id] = (
                weakref.ref(service),
                store_ref,
                identity,
                scope_payload,
            )

    def binding(
        service: object,
        *,
        required: bool = False,
    ) -> tuple[JournalStore | None, object | None]:
        with state_lock:
            state = states.get(id(service))
        if state is None or state[0]() is not service:
            raise AuthorityConflict(
                "AuthorityService journal process state is unavailable"
            )

        expected_identity = state[2]
        store = None if state[1] is None else state[1]()
        if expected_identity is not None and store is None:
            raise AuthorityConflict(
                "AuthorityService selected journal store was lost"
            )
        visible_store = vars(service).get("store", missing)
        if visible_store is not store:
            raise AuthorityConflict(
                "AuthorityService journal store binding was modified"
            )

        if store is None:
            if required:
                raise AuthorityConflict(
                    "AuthorityService operation requires a JournalStore"
                )
            return None, None

        try:
            current_identity = require_exact_journal_store_authority(
                store,
                subject="AuthorityService selected journal store",
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise AuthorityConflict(
                "AuthorityService selected journal authority is no longer valid"
            ) from error
        if current_identity != expected_identity:
            raise AuthorityConflict(
                "AuthorityService journal store generation changed"
            )
        return store, expected_identity

    def policy_scope(service: object) -> RiskPolicyScope | None:
        binding(service)
        with state_lock:
            payload = states[id(service)][3]
        return None if payload is None else RiskPolicyScope(*payload)

    return register, binding, policy_scope


(
    _register_authority_service_store,
    _authority_service_store_binding,
    _authority_service_risk_policy_scope,
) = _authority_service_store_operations()
del _authority_service_store_operations


def _authority_service_store(
    service: object,
    *,
    required: bool = False,
) -> JournalStore | None:
    return _authority_service_store_binding(service, required=required)[0]


def _authority_store_call(
    service: object,
    method_name: str,
    /,
    *args,
    **kwargs,
):
    store, expected_identity = _authority_service_store_binding(
        service,
        required=True,
    )
    assert store is not None
    assert expected_identity is not None
    method = getattr(JournalStore, method_name, None)
    if not callable(method):
        raise AuthorityConflict(
            f"unsupported AuthorityService journal operation: {method_name}"
        )
    with journal_store_authority_scope(store, expected_identity):
        return method(store, *args, **kwargs)


def _authority_service_capital_operations():
    """Retain the selected local-capital composition outside caller-writable state."""

    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            weakref.ReferenceType | None,
            weakref.ReferenceType | None,
        ],
    ] = {}
    lock = threading.RLock()

    def register(
        service: object,
        settlement_book: DurableSettlementBook | None,
        economic_book: DurableProviderEconomicBook | None,
    ) -> None:
        if (settlement_book is None) != (economic_book is None):
            raise TypeError(
                "settlement_book and economic_book must be supplied together"
            )
        if settlement_book is not None:
            if type(settlement_book) is not DurableSettlementBook:
                raise TypeError(
                    "settlement_book must be exact DurableSettlementBook"
                )
            if type(economic_book) is not DurableProviderEconomicBook:
                raise TypeError(
                    "economic_book must be exact DurableProviderEconomicBook"
                )
            store = _authority_service_store(service, required=True)
            selected_store, _identity, scope, _scope_id = (
                DurableSettlementBook._selected_authority(settlement_book)
            )
            DurableProviderEconomicBook.read_cut(economic_book)
            if selected_store is not store or economic_book.store is not store:
                raise AuthorityConflict(
                    "capital authorities must share the AuthorityService JournalStore"
                )
            if (
                scope.provider_id != economic_book.provider_id
                or scope.account_id != economic_book.account_id
                or scope.environment != economic_book.environment
                or scope.provider_environment != economic_book.provider_environment
            ):
                raise AuthorityConflict(
                    "settlement and economic capital scopes do not match"
                )

        object_id = id(service)

        # The owning service retains the books. The private registry retains
        # only callback-free weakrefs, so neither authority erasure callbacks
        # nor process-lifetime book graphs are exposed by getweakrefs().
        service_ref = weakref.ref(service)
        with lock:
            current = states.get(object_id)
            if current is not None:
                current_service = current[0]()
                if current_service is service:
                    raise AuthorityConflict(
                        "AuthorityService capital composition is already initialized"
                    )
                if current_service is not None:
                    raise AuthorityConflict(
                        "AuthorityService capital binding identity collision"
                    )
                states.pop(object_id, None)
            service._selected_capital_books = (settlement_book, economic_book)
            states[object_id] = (
                service_ref,
                None if settlement_book is None else weakref.ref(settlement_book),
                None if economic_book is None else weakref.ref(economic_book),
            )

    def binding(
        service: object,
        *,
        required: bool = False,
    ) -> tuple[
        DurableSettlementBook | None,
        DurableProviderEconomicBook | None,
    ]:
        with lock:
            state = states.get(id(service))
        if state is None or state[0]() is not service:
            raise AuthorityConflict(
                "AuthorityService capital process state is unavailable"
            )
        settlement_book = None if state[1] is None else state[1]()
        economic_book = None if state[2] is None else state[2]()
        visible = vars(service).get("_selected_capital_books")
        if (type(visible) is not tuple or len(visible) != 2
                or visible[0] is not settlement_book or visible[1] is not economic_book
                or (state[1] is not None and settlement_book is None)
                or (state[2] is not None and economic_book is None)):
            raise AuthorityConflict("AuthorityService capital lifetime binding was modified")
        if settlement_book is None:
            if required:
                raise AuthorityConflict(
                    "financial admission requires configured local capital authority"
                )
            return None, None

        store = _authority_service_store(service, required=True)
        selected_store, _identity, scope, _scope_id = (
            DurableSettlementBook._selected_authority(settlement_book)
        )
        DurableProviderEconomicBook.read_cut(economic_book)
        if selected_store is not store or economic_book.store is not store:
            raise AuthorityConflict(
                "capital authority JournalStore binding changed"
            )
        if (
            scope.provider_id != economic_book.provider_id
            or scope.account_id != economic_book.account_id
            or scope.environment != economic_book.environment
            or scope.provider_environment != economic_book.provider_environment
        ):
            raise AuthorityConflict("capital authority scope changed")
        return settlement_book, economic_book

    def resolve(
        service: object,
        provider_available: Mapping[str, object],
        resources: tuple[str, ...],
        *,
        provider_evidence: Mapping[str, object] | None = None,
        as_of: datetime | None = None,
        required: bool = False,
    ) -> dict[str, object] | None:
        settlement_book, economic_book = binding(service, required=required)
        if settlement_book is None:
            return None
        if (
            type(provider_available) is not dict
            or any(type(key) is not str for key in provider_available)
        ):
            raise AuthorityConflict(
                "settlement capital provider availability is malformed"
            )
        if (
            type(resources) is not tuple
            or any(type(resource) is not str for resource in resources)
        ):
            raise AuthorityConflict("settlement capital resources are malformed")
        capital_resources = tuple(
            sorted(
                resource
                for resource in resources
                if resource.startswith(("CASH:", "MARGIN_CREDIT:"))
            )
        )
        if not capital_resources:
            return None
        if (
            type(provider_evidence) is not dict
            or any(type(key) is not str for key in provider_evidence)
        ):
            raise AuthorityConflict(
                "settlement capital requires provider availability evidence"
            )

        store = _authority_service_store(service, required=True)
        # Capture before reading either provider provenance or local economics.
        # A writer between the causal history read and projection must not turn
        # newer cash into capital backed by the older provider snapshot.
        before = _authority_store_call(service, "current_journal_sequence")
        checkpoint_event_id = _authority_text(
            provider_evidence.get("checkpoint_event_id"),
            name="checkpoint_event_id",
        )
        checkpoint = _authority_store_call(service, "get_event", checkpoint_event_id)
        if (
            type(checkpoint) is not dict
            or any(type(key) is not str for key in checkpoint)
            or checkpoint.get("event_type") != "AccountReconciled"
            or checkpoint.get("aggregate_type") != "account_reconciliation"
        ):
            raise AuthorityConflict(
                "settlement capital provider checkpoint is unavailable"
            )
        checkpoint_sequence = checkpoint.get("journal_sequence")
        if type(checkpoint_sequence) is not int or checkpoint_sequence <= 0:
            raise AuthorityConflict(
                "settlement capital provider checkpoint sequence is invalid"
            )
        if (
            provider_evidence.get("scope_latest_checkpoint_event_id")
            != checkpoint_event_id
            or provider_evidence.get("checkpoint_payload_hash")
            != checkpoint.get("payload_hash")
            or provider_evidence.get("scope_latest_checkpoint_journal_sequence")
            != checkpoint_sequence
        ):
            raise AuthorityConflict(
                "settlement capital requires the exact current provider checkpoint"
            )
        checkpoint_payload = checkpoint.get("payload")
        if (
            type(checkpoint_payload) is not dict
            or any(type(key) is not str for key in checkpoint_payload)
        ):
            raise AuthorityConflict(
                "settlement capital provider checkpoint is malformed"
            )
        resource_evidence = checkpoint_payload.get("resource_availability")
        if (
            type(resource_evidence) is not dict
            or any(type(key) is not str for key in resource_evidence)
        ):
            raise AuthorityConflict(
                "settlement capital provider resource evidence is missing"
            )
        provider_query_started = _instant(
            _authority_text(
                resource_evidence.get("query_started_at"),
                name="resource_availability.query_started_at",
            ),
            name="resource_availability.query_started_at",
        )

        economic_events = _authority_store_call(
            service,
            "load_events",
            "economic_book",
            economic_book.book_id,
        )
        if economic_events:
            economic_head = economic_events[-1]
            economic_sequence = economic_head.get("journal_sequence")
            if type(economic_sequence) is not int or economic_sequence <= 0:
                raise AuthorityConflict(
                    "settlement capital economic head sequence is invalid"
                )
            if economic_sequence >= checkpoint_sequence:
                raise AuthorityConflict(
                    "provider availability predates local economic financial truth"
                )
            for economic_event in economic_events:
                if (
                    type(economic_event) is not dict
                    or any(type(key) is not str for key in economic_event)
                ):
                    raise AuthorityConflict(
                        "settlement capital economic event is malformed"
                    )
                economic_committed_at = _instant(
                    _authority_text(
                        economic_event.get("committed_at"),
                        name="economic_book.committed_at",
                    ),
                    name="economic_book.committed_at",
                )
                if economic_committed_at >= provider_query_started:
                    raise AuthorityConflict(
                        "provider availability predates local economic financial truth"
                    )

        DurableProviderEconomicBook.refresh(economic_book)
        projection = DurableSettlementBook.project(
            settlement_book,
            economic_book,
        )
        after = _authority_store_call(service, "current_journal_sequence")
        if before != after:
            raise AuthorityConflict(
                "capital authority changed while spendable cash was projected"
            )

        _selected_store, _identity, scope, scope_id = (
            DurableSettlementBook._selected_authority(settlement_book)
        )
        raw_details = provider_evidence.get("resource_details", {})
        if type(raw_details) is not dict:
            raise AuthorityConflict(
                "settlement capital provider resource details are malformed"
            )
        if as_of is None:
            projection_at = _instant(
                _authority_text(
                    provider_evidence.get("snapshot_query_completed_at"),
                    name="snapshot_query_completed_at",
                ),
                name="snapshot_query_completed_at",
            )
        else:
            projection_at = _authority_datetime_utc(
                as_of,
                name="settlement capital as_of",
            )
        adjustments: dict[str, dict[str, str]] = {}
        for resource in capital_resources:
            raw_provider = provider_available.get(resource)
            if raw_provider is None:
                raise AuthorityConflict(
                    f"provider availability lacks required capital resource {resource}"
                )
            provider_amount = _authority_decimal(
                raw_provider,
                name=f"provider availability[{resource}]",
            )
            if provider_amount < 0:
                raise AuthorityConflict(
                    "provider capital availability must be non-negative"
                )
            if resource.startswith("CASH:"):
                currency = resource.removeprefix("CASH:")
                buying_power = None
                require_buying_power = False
            else:
                currency = resource.removeprefix("MARGIN_CREDIT:")
                detail = raw_details.get(resource)
                if type(detail) is not dict:
                    raise AuthorityConflict(
                        "margin-credit capital requires typed provider evidence"
                    )
                try:
                    buying_power = BuyingPowerEvidence.from_resource_detail(
                        detail
                    )
                except (TypeError, ValueError) as error:
                    raise AuthorityConflict(
                        "margin-credit provider evidence is invalid"
                    ) from error
                if (
                    buying_power.scope != scope
                    or buying_power.resource_key != resource
                    or buying_power.additional_credit != provider_amount
                ):
                    raise AuthorityConflict(
                        "margin-credit provider evidence differs from capital scope"
                    )
                require_buying_power = True

            capital = projection.available_capital(
                scope=scope,
                currency=currency,
                as_of=projection_at,
                buying_power_evidence=buying_power,
                require_buying_power_evidence=require_buying_power,
            )
            if capital.blocks_new_risk:
                raise AuthorityConflict(
                    "local settlement capital is unresolved and blocks new risk"
                )
            local_amount = (
                capital.available_cash
                if resource.startswith("CASH:")
                else capital.additional_buying_power
            )
            if local_amount < 0:
                raise AuthorityConflict(
                    "local capital availability must be non-negative"
                )
            effective = min(provider_amount, local_amount)
            adjustments[resource] = {
                "provider_available": _canonical_decimal_text(provider_amount),
                "local_available": _canonical_decimal_text(local_amount),
                "effective_available": _canonical_decimal_text(effective),
            }
        return {
            "schema_version": (
                "settlement-capital-cut.v2"
                if any(
                    resource.startswith("MARGIN_CREDIT:")
                    for resource in capital_resources
                )
                else "settlement-capital-cut.v1"
            ),
            "journal_sequence": after,
            "provider_id": scope.provider_id,
            "account_id": scope.account_id,
            "environment": scope.environment,
            "provider_environment": scope.provider_environment,
            "settlement_scope_id": scope_id,
            "economic_book_id": economic_book.book_id,
            "resources": adjustments,
        }

    return register, binding, resolve


(
    _register_authority_service_capital,
    _authority_service_capital_binding,
    _resolve_authority_service_capital,
) = _authority_service_capital_operations()
del _authority_service_capital_operations


def _canonical_settlement_capital_adjustment(
    value: object,
    *,
    provider_available: Mapping[str, object],
    required_resources: tuple[str, ...],
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
    risk_journal_sequence: int | None = None,
    expected_journal_sequence: int | None = None,
) -> tuple[dict[str, object], dict[str, Decimal]]:
    if (
        type(value) is not dict
        or any(type(key) is not str for key in value)
    ):
        raise AuthorityConflict("settlement capital adjustment is malformed")
    if (
        type(provider_available) is not dict
        or any(type(key) is not str for key in provider_available)
    ):
        raise AuthorityConflict(
            "authoritative provider availability is malformed"
        )
    if (
        type(required_resources) is not tuple
        or any(type(resource) is not str for resource in required_resources)
    ):
        raise AuthorityConflict("required settlement resources are malformed")
    expected_fields = {
        "schema_version",
        "journal_sequence",
        "provider_id",
        "account_id",
        "environment",
        "provider_environment",
        "settlement_scope_id",
        "economic_book_id",
        "resources",
    }
    if set(value) != expected_fields:
        raise AuthorityConflict("settlement capital adjustment is malformed")
    schema_version = value.get("schema_version")
    if schema_version not in {
        "settlement-capital-cut.v1",
        "settlement-capital-cut.v2",
    }:
        raise AuthorityConflict("settlement capital schema is unsupported")
    journal_sequence = value.get("journal_sequence")
    if type(journal_sequence) is not int or journal_sequence < 0:
        raise AuthorityConflict("settlement capital journal cut is invalid")
    if (
        expected_journal_sequence is not None
        and (
            type(expected_journal_sequence) is not int
            or expected_journal_sequence < 0
            or journal_sequence != expected_journal_sequence
        )
    ):
        raise AuthorityConflict(
            "settlement capital cut does not match expected journal cut"
        )
    if (
        risk_journal_sequence is not None
        and journal_sequence >= risk_journal_sequence
    ):
        raise AuthorityConflict(
            "settlement capital cut must precede the durable risk decision"
        )
    canonical_provider = _authority_text(
        provider_id, name="provider_id"
    ).upper()
    canonical_account = _authority_text(account_id, name="account_id")
    canonical_environment = _authority_text(
        environment, name="environment"
    ).upper()
    value_provider = _authority_text(
        value.get("provider_id"), name="capital.provider_id"
    ).upper()
    value_account = _authority_text(
        value.get("account_id"), name="capital.account_id"
    )
    value_environment = _authority_text(
        value.get("environment"), name="capital.environment"
    ).upper()
    if (
        value_provider != canonical_provider
        or value_account != canonical_account
        or value_environment != canonical_environment
    ):
        raise AuthorityConflict("settlement capital scope is inconsistent")
    if canonical_provider == "BYBIT" and provider_environment is None:
        raise AuthorityConflict(
            "BYBIT settlement capital requires exact provider_environment"
        )
    expected_provider_environment = (
        canonical_environment
        if provider_environment is None
        else _authority_text(
            provider_environment,
            name="expected_provider_environment",
        ).upper()
    )
    if canonical_provider == "BYBIT":
        if expected_provider_environment not in {"MAINNET", "TESTNET", "DEMO"}:
            raise AuthorityConflict(
                "BYBIT settlement capital provider_environment is unsupported"
            )
        expected_runtime = (
            "LIVE" if expected_provider_environment == "MAINNET" else "PAPER"
        )
        if canonical_environment != expected_runtime:
            raise AuthorityConflict(
                "BYBIT settlement capital provider domain does not match runtime"
            )
    provider_environment = _authority_text(
        value.get("provider_environment"),
        name="provider_environment",
    ).upper()
    if provider_environment != expected_provider_environment:
        raise AuthorityConflict(
            "settlement capital provider domain is inconsistent"
        )
    settlement_scope_id = _authority_text(
        value.get("settlement_scope_id"),
        name="settlement_scope_id",
    )
    economic_book_id = _authority_text(
        value.get("economic_book_id"),
        name="economic_book_id",
    )
    raw_resources = value.get("resources")
    if (
        type(raw_resources) is not dict
        or any(type(key) is not str for key in raw_resources)
    ):
        raise AuthorityConflict("settlement capital resources are malformed")
    capital_resources = tuple(
        sorted(
            resource
            for resource in required_resources
            if resource.startswith(("CASH:", "MARGIN_CREDIT:"))
        )
    )
    expected_schema = (
        "settlement-capital-cut.v2"
        if any(
            resource.startswith("MARGIN_CREDIT:")
            for resource in capital_resources
        )
        else "settlement-capital-cut.v1"
    )
    if schema_version != expected_schema:
        raise AuthorityConflict(
            "settlement capital schema does not match reservation resources"
        )
    if set(raw_resources) != set(capital_resources):
        raise AuthorityConflict(
            "settlement capital resources do not match reservation requirements"
        )

    effective: dict[str, Decimal] = {}
    canonical_resources: dict[str, dict[str, str]] = {}
    for resource in capital_resources:
        raw = raw_resources.get(resource)
        if (
            type(raw) is not dict
            or any(type(key) is not str for key in raw)
            or set(raw) != {
                "provider_available",
                "local_available",
                "effective_available",
            }
        ):
            raise AuthorityConflict(
                "settlement capital resource adjustment is malformed"
            )
        provider_amount = _authority_decimal(
            raw.get("provider_available"),
            name=f"capital.provider_available[{resource}]",
        )
        local_amount = _authority_decimal(
            raw.get("local_available"),
            name=f"capital.local_available[{resource}]",
        )
        effective_amount = _authority_decimal(
            raw.get("effective_available"),
            name=f"capital.effective_available[{resource}]",
        )
        current_provider = _authority_decimal(
            provider_available.get(resource),
            name=f"authoritative availability[{resource}]",
        )
        if (
            provider_amount < 0
            or local_amount < 0
            or effective_amount < 0
            or provider_amount != current_provider
            or effective_amount != min(provider_amount, local_amount)
        ):
            raise AuthorityConflict(
                "settlement capital adjustment is inconsistent"
            )
        effective[resource] = effective_amount
        canonical_resources[resource] = {
            "provider_available": _canonical_decimal_text(provider_amount),
            "local_available": _canonical_decimal_text(local_amount),
            "effective_available": _canonical_decimal_text(effective_amount),
        }
    return (
        {
            "schema_version": schema_version,
            "journal_sequence": journal_sequence,
            "provider_id": canonical_provider,
            "account_id": canonical_account,
            "environment": canonical_environment,
            "provider_environment": provider_environment,
            "settlement_scope_id": settlement_scope_id,
            "economic_book_id": economic_book_id,
            "resources": canonical_resources,
        },
        effective,
    )


@dataclass(frozen=True)
class AllocationAuthoritySnapshot:
    """Canonical facts used to revalidate an allocation at admission.

    The snapshot is produced by an AuthorityService-owned resolver, never by
    the admission caller. It adapts existing canonical evidence/account stores;
    it is not a second financial authority or persistence layer.
    """

    resolved_evidence: Mapping[str, ImmutableAllocationEvidence]
    provider_id: str
    account_id: str
    policy_version: str
    allocation_policy: AllocationPolicy
    max_candidate_sets: int
    instrument_versions: Mapping[str, str]
    financial_instruments: Mapping[str, InstrumentVersionIdentity]
    capability_snapshot_ids: Mapping[str, str]
    account_snapshot_id: str
    reconciliation_run_id: str
    account_state_version: int

    def __post_init__(self) -> None:
        if type(self.allocation_policy) is not AllocationPolicy:
            raise TypeError("allocation_policy must be exact AllocationPolicy")
        if (
            type(self.max_candidate_sets) is not int
            or self.max_candidate_sets < 1
        ):
            raise ValueError("max_candidate_sets must be a positive exact integer")
        if not isinstance(self.resolved_evidence, Mapping):
            raise TypeError("resolved_evidence must be a mapping")
        evidence: dict[str, ImmutableAllocationEvidence] = {}
        for raw_id, item in self.resolved_evidence.items():
            evidence_id = _text(raw_id, name="allocation evidence id")
            if not isinstance(item, ImmutableAllocationEvidence):
                raise TypeError(
                    "resolved allocation evidence values must be ImmutableAllocationEvidence"
                )
            if evidence_id != item.evidence_id:
                raise ValueError(
                    "resolved allocation evidence key must match evidence_id"
                )
            if evidence_id in evidence:
                raise ValueError("resolved allocation evidence ids must be unique")
            evidence[evidence_id] = item

        def normalized_scope(
            raw: Mapping[str, str],
            *,
            name: str,
        ) -> Mapping[str, str]:
            if not isinstance(raw, Mapping):
                raise TypeError(f"{name} must be a mapping")
            result: dict[str, str] = {}
            for raw_symbol, raw_identity in raw.items():
                symbol = _text(raw_symbol, name=f"{name} symbol")
                if symbol in result:
                    raise ValueError(f"{name} symbols must be unique")
                result[symbol] = _text(
                    raw_identity,
                    name=f"{name} identity",
                )
            return MappingProxyType(dict(sorted(result.items())))

        if (
            not isinstance(self.account_state_version, int)
            or isinstance(self.account_state_version, bool)
            or self.account_state_version < 0
        ):
            raise ValueError(
                "allocation account_state_version must be a non-negative integer"
            )
        object.__setattr__(
            self,
            "resolved_evidence",
            MappingProxyType(dict(sorted(evidence.items()))),
        )
        object.__setattr__(
            self,
            "provider_id",
            _text(self.provider_id, name="allocation provider_id"),
        )
        object.__setattr__(
            self,
            "account_id",
            _text(self.account_id, name="allocation account_id"),
        )
        object.__setattr__(
            self,
            "policy_version",
            _text(self.policy_version, name="allocation policy_version"),
        )
        object.__setattr__(
            self,
            "instrument_versions",
            normalized_scope(
                self.instrument_versions,
                name="allocation instrument_versions",
            ),
        )
        if not isinstance(self.financial_instruments, Mapping):
            raise TypeError("financial_instruments must be a mapping")
        normalized_financial_instruments: dict[str, InstrumentVersionIdentity] = {}
        for raw_symbol, raw_identity in self.financial_instruments.items():
            symbol = _text(
                raw_symbol,
                name="allocation financial_instruments symbol",
            )
            if symbol in normalized_financial_instruments:
                raise ValueError(
                    "allocation financial_instruments symbols must be unique"
                )
            normalized_financial_instruments[symbol] = _instrument_identity(
                raw_identity,
                name=f"allocation financial instrument {symbol}",
            )
        object.__setattr__(
            self,
            "financial_instruments",
            MappingProxyType(dict(sorted(normalized_financial_instruments.items()))),
        )
        object.__setattr__(
            self,
            "capability_snapshot_ids",
            normalized_scope(
                self.capability_snapshot_ids,
                name="allocation capability_snapshot_ids",
            ),
        )
        object.__setattr__(
            self,
            "account_snapshot_id",
            _text(
                self.account_snapshot_id,
                name="allocation account_snapshot_id",
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_run_id",
            _text(
                self.reconciliation_run_id,
                name="allocation reconciliation_run_id",
            ),
        )



def _risk_context_graph_value(value: object, *, name: str) -> object:
    """Detach only the exact base-value graph emitted by RiskContext.create()."""

    if value is None:
        return None
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    if type(value) is str:
        return value
    if type(value) is Decimal:
        if not value.is_finite():
            raise ValueError(f"{name} Decimal must be finite")
        return value
    if type(value) is datetime:
        if type(value.tzinfo) is not timezone:
            raise TypeError(f"{name} datetime must use exact datetime.timezone")
        return value
    if type(value) is dict:
        items = tuple(value.items())
        if any(type(key) is not str for key, _item in items):
            raise TypeError(f"{name} mapping keys must be exact str")
        return {
            key: _risk_context_graph_value(item, name=f"{name}[{key!r}]")
            for key, item in items
        }
    if type(value) is tuple:
        return tuple(
            _risk_context_graph_value(item, name=f"{name}[{index}]")
            for index, item in enumerate(value)
        )
    if type(value) in {LiquidationScope, LiquidationHeadroomEvidence}:
        expected_type = type(value)
        state = vars(value)
        state_names = tuple(state)
        if any(type(key) is not str for key in state_names):
            raise TypeError(f"{name} state keys must be exact str")
        expected_names = tuple(field.name for field in fields(expected_type))
        if frozenset(state_names) != frozenset(expected_names):
            raise TypeError(f"{name} state shape is non-canonical")
        detached = {
            key: _risk_context_graph_value(state[key], name=f"{name}.{key}")
            for key in expected_names
        }
        if expected_type is LiquidationScope:
            return LiquidationScope(**detached)
        return LiquidationHeadroomEvidence.create(**detached)
    raise TypeError(
        f"{name} contains non-canonical {type(value).__name__}"
    )


def _canonical_risk_context(context: RiskContext) -> RiskContext:
    if type(context) is not RiskContext:
        raise TypeError("risk context must be exact RiskContext")
    state = vars(context)
    state_names = tuple(state)
    if any(type(key) is not str for key in state_names):
        raise TypeError("risk context state keys must be exact str")
    expected_names = tuple(field.name for field in fields(RiskContext))
    if frozenset(state_names) != frozenset(expected_names):
        raise TypeError("risk context state shape is non-canonical")
    detached = {
        key: _risk_context_graph_value(state[key], name=f"risk context.{key}")
        for key in expected_names
    }
    return RiskContext.create(**detached)


def _canonical_risk_intent(intent: RiskIntent) -> RiskIntent:
    if type(intent) is not RiskIntent:
        raise TypeError("risk intent must be exact RiskIntent")
    state = vars(intent)
    state_names = tuple(state)
    if any(type(key) is not str for key in state_names):
        raise TypeError("risk intent state keys must be exact str")
    expected_names = tuple(field.name for field in fields(RiskIntent))
    if frozenset(state_names) != frozenset(expected_names):
        raise TypeError("risk intent state shape is non-canonical")
    detached = {
        key: _risk_context_graph_value(state[key], name=f"risk intent.{key}")
        for key in expected_names
    }
    return RiskIntent.create(**detached)


def _risk_intent_payload(intent: RiskIntent) -> dict[str, object]:
    canonical = _canonical_risk_intent(intent)
    return {
        "symbol": canonical.symbol,
        "side": canonical.side,
        "quantity": _canonical_decimal_text(canonical.quantity),
        "price": _canonical_decimal_text(canonical.price),
        "expected_state_version": canonical.expected_state_version,
        "reduce_only": canonical.reduce_only,
        "action": canonical.action,
        "instrument_type": canonical.instrument_type,
    }


def _durable_risk_intent_payload(value: object) -> dict[str, object]:
    expected = {
        "symbol",
        "side",
        "quantity",
        "price",
        "expected_state_version",
        "reduce_only",
        "action",
        "instrument_type",
    }
    if type(value) is not dict or set(value) != expected:
        raise AuthorityConflict("durable financial retry risk_intent is malformed")
    try:
        canonical = RiskIntent.create(
            symbol=value["symbol"],
            side=value["side"],
            quantity=value["quantity"],
            price=value["price"],
            expected_state_version=value["expected_state_version"],
            reduce_only=value["reduce_only"],
            action=value["action"],
            instrument_type=value["instrument_type"],
        )
    except (TypeError, ValueError) as error:
        raise AuthorityConflict(
            "durable financial retry risk_intent is malformed"
        ) from error
    payload = _risk_intent_payload(canonical)
    if value != payload:
        raise AuthorityConflict(
            "durable financial retry risk_intent is non-canonical"
        )
    return payload


def _risk_snapshot_value(value):
    if isinstance(value, Decimal):
        return _canonical_decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("authoritative risk snapshot datetime must include timezone")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if hasattr(value, "__dataclass_fields__"):
        return _risk_snapshot_value(vars(value))
    if isinstance(value, Mapping):
        return {
            str(key): _risk_snapshot_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [_risk_snapshot_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_risk_snapshot_value(item) for item in value]
        return sorted(
            normalized,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        )
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(
        "unsupported authoritative risk snapshot value: "
        + type(value).__name__
    )


def _risk_object_fingerprint(value) -> str:
    payload = _risk_snapshot_value(value)
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _risk_context_compatibility_payload(context: RiskContext) -> object:
    return _risk_snapshot_value(_canonical_risk_context(context))


def _risk_context_fingerprint(context: RiskContext) -> str:
    return _risk_object_fingerprint(_canonical_risk_context(context))


def _risk_policy_compatibility_payload(policy: RiskPolicy) -> dict[str, object]:
    payload = risk_policy_payload(policy)
    compatibility = dict(payload)
    compatibility.pop("schema_version")
    return compatibility


def _risk_policy_fingerprint(policy: RiskPolicy) -> str:
    payload = _risk_policy_compatibility_payload(policy)
    return sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class RiskAuthorityRequest:
    """Scope presented to the service-owned authoritative risk resolver.

    Caller-supplied financial state and risk policy are deliberately absent.
    The resolver receives only the requested intent and immutable authority cut.
    """

    risk_intent: RiskIntent
    account_id: str
    environment: str
    provider_id: str
    instrument_version: InstrumentVersionIdentity
    capability_snapshot_id: str
    reconciliation_checkpoint_event_id: str
    journal_sequence_cut: int
    reservation_version: int
    reservation_state_digest: str
    authority_policy_id: str
    authority_policy_version: int
    evaluated_at: str
    resolved_risk_policy: ResolvedRiskPolicy | None = None
    provider_environment: str | None = None
    entity_policy_id: str | None = None
    instrument_family: str | None = None

    def __post_init__(self) -> None:
        canonical_intent = _canonical_risk_intent(self.risk_intent)
        account = _authority_text(
            self.account_id, name="risk authority account_id"
        )
        environment = _authority_text(
            self.environment, name="risk authority environment"
        ).upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("risk authority environment is unsupported")
        provider = _authority_text(
            self.provider_id, name="risk authority provider_id"
        ).upper()
        provider_environment = None
        entity_policy_id = None
        instrument_family = None
        if self.resolved_risk_policy is not None:
            resolved = require_registry_issued_resolved_policy(self.resolved_risk_policy)
            scope = resolved.identity.scope
            raw_provider_environment = _authority_text(
                self.provider_environment,
                name="risk authority provider_environment",
            )
            raw_entity_policy_id = _authority_text(
                self.entity_policy_id,
                name="risk authority entity_policy_id",
            )
            raw_instrument_family = _authority_text(
                self.instrument_family,
                name="risk authority instrument_family",
            )
            if (
                scope.provider_id,
                scope.account_id,
                scope.environment,
                scope.provider_environment,
                scope.entity_policy_id,
                scope.instrument_family,
            ) != (
                provider,
                account,
                environment,
                raw_provider_environment,
                raw_entity_policy_id,
                raw_instrument_family,
            ):
                raise AuthorityConflict(
                    "resolved RiskPolicy scope differs from financial cut"
                )
            provider_environment = scope.provider_environment
            entity_policy_id = scope.entity_policy_id
            instrument_family = scope.instrument_family
            if resolved.resolved_journal_sequence_cut != self.journal_sequence_cut:
                raise AuthorityConflict(
                    "resolved RiskPolicy journal cut differs from financial cut"
                )
        elif any(value is not None for value in (
            self.provider_environment, self.entity_policy_id, self.instrument_family)):
            raise AuthorityConflict("provider-domain dimensions require resolved RiskPolicy authority")
        if type(self.journal_sequence_cut) is not int or self.journal_sequence_cut < 0:
            raise ValueError("journal_sequence_cut must be a non-negative integer")
        if type(self.reservation_version) is not int or self.reservation_version < 0:
            raise ValueError("reservation_version must be a non-negative integer")
        if (
            type(self.authority_policy_version) is not int
            or self.authority_policy_version < 1
        ):
            raise ValueError("authority_policy_version must be positive")
        evaluated = _authority_text(
            self.evaluated_at, name="risk authority evaluated_at"
        )
        _instant(evaluated, name="risk authority evaluated_at")
        object.__setattr__(self, "risk_intent", canonical_intent)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(
            self, "provider_environment", provider_environment
        )
        object.__setattr__(self, "entity_policy_id", entity_policy_id)
        object.__setattr__(self, "instrument_family", instrument_family)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(
                self.instrument_version,
                name="risk authority instrument_version",
            ),
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _authority_text(
                self.capability_snapshot_id,
                name="risk authority capability_snapshot_id",
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_checkpoint_event_id",
            _authority_text(
                self.reconciliation_checkpoint_event_id,
                name="risk authority reconciliation_checkpoint_event_id",
            ),
        )
        object.__setattr__(
            self,
            "reservation_state_digest",
            _authority_text(
                self.reservation_state_digest,
                name="risk authority reservation_state_digest",
            ),
        )
        object.__setattr__(
            self,
            "authority_policy_id",
            _authority_text(
                self.authority_policy_id,
                name="risk authority policy_id",
            ),
        )
        object.__setattr__(self, "evaluated_at", evaluated)


class _CanonicalEvidenceRefs(Mapping[str, str]):
    """Exact immutable evidence-ref mapping detached from caller-owned mappings."""

    __slots__ = ("_items",)

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("_CanonicalEvidenceRefs cannot be subclassed")

    def __init__(self, items: tuple[tuple[str, str], ...]) -> None:
        if type(items) is not tuple:
            raise TypeError("canonical evidence refs must use an exact tuple")
        for item in items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise TypeError(
                    "canonical evidence refs must contain exact string pairs"
                )
        object.__setattr__(self, "_items", items)

    def __setattr__(self, _name: str, _value: object) -> None:
        raise AttributeError("canonical evidence refs are immutable")

    def __delattr__(self, _name: str) -> None:
        raise AttributeError("canonical evidence refs are immutable")

    def _validated_items(self) -> tuple[tuple[str, str], ...]:
        items = object.__getattribute__(self, "_items")
        if type(items) is not tuple:
            raise TypeError("canonical evidence refs state is non-canonical")
        for item in items:
            if (
                type(item) is not tuple
                or len(item) != 2
                or type(item[0]) is not str
                or type(item[1]) is not str
            ):
                raise TypeError("canonical evidence refs state is non-canonical")
        return items

    def __len__(self) -> int:
        return len(self._validated_items())

    def __iter__(self):
        return (key for key, _value in self._validated_items())

    def __getitem__(self, key: str) -> str:
        if type(key) is not str:
            raise KeyError(key)
        for current_key, value in self._validated_items():
            if current_key == key:
                return value
        raise KeyError(key)

    def items(self) -> tuple[tuple[str, str], ...]:
        return self._validated_items()


@dataclass(frozen=True)
class AuthoritativeRiskSnapshot:
    """Immutable risk state assembled by the financial writer's resolver."""

    context: RiskContext
    risk_policy: RiskPolicy
    account_id: str
    environment: str
    provider_id: str
    instrument_version: InstrumentVersionIdentity
    capability_snapshot_id: str
    reconciliation_checkpoint_event_id: str
    journal_sequence_cut: int
    reservation_version: int
    reservation_state_digest: str
    authority_policy_id: str
    authority_policy_version: int
    evaluated_at: str
    valid_until: str
    evidence_refs: Mapping[str, str]
    resolved_risk_policy: ResolvedRiskPolicy | None = None
    provider_environment: str | None = None
    entity_policy_id: str | None = None
    instrument_family: str | None = None

    def __post_init__(self) -> None:
        normalized_context = _canonical_risk_context(self.context)
        canonical_policy = canonical_risk_policy(self.risk_policy)
        account = _authority_text(
            self.account_id, name="authoritative risk account_id"
        )
        environment = _authority_text(
            self.environment, name="authoritative risk environment"
        ).upper()
        if environment not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("authoritative risk environment is unsupported")
        provider = _authority_text(
            self.provider_id, name="authoritative risk provider_id"
        ).upper()
        provider_environment = None
        entity_policy_id = None
        instrument_family = None
        if self.resolved_risk_policy is not None:
            resolved = require_registry_issued_resolved_policy(self.resolved_risk_policy)
            scope = resolved.identity.scope
            raw_provider_environment = _authority_text(
                self.provider_environment,
                name="authoritative risk provider_environment",
            )
            raw_entity_policy_id = _authority_text(
                self.entity_policy_id,
                name="authoritative risk entity_policy_id",
            )
            raw_instrument_family = _authority_text(
                self.instrument_family,
                name="authoritative risk instrument_family",
            )
            if (
                scope.provider_id,
                scope.account_id,
                scope.environment,
                scope.provider_environment,
                scope.entity_policy_id,
                scope.instrument_family,
            ) != (
                provider,
                account,
                environment,
                raw_provider_environment,
                raw_entity_policy_id,
                raw_instrument_family,
            ):
                raise AuthorityConflict(
                    "resolved RiskPolicy scope differs from financial cut"
                )
            provider_environment = scope.provider_environment
            entity_policy_id = scope.entity_policy_id
            instrument_family = scope.instrument_family
            if resolved.resolved_journal_sequence_cut != self.journal_sequence_cut:
                raise AuthorityConflict(
                    "resolved RiskPolicy journal cut differs from financial cut"
                )
        elif any(value is not None for value in (
            self.provider_environment, self.entity_policy_id, self.instrument_family)):
            raise AuthorityConflict("provider-domain dimensions require resolved RiskPolicy authority")
        if self.resolved_risk_policy is not None and risk_policy_payload(canonical_policy) != risk_policy_payload(resolved.policy):
            raise AuthorityConflict("snapshot RiskPolicy content differs from registry authority")
        if type(self.journal_sequence_cut) is not int or self.journal_sequence_cut < 0:
            raise ValueError("journal_sequence_cut must be a non-negative integer")
        if type(self.reservation_version) is not int or self.reservation_version < 0:
            raise ValueError("reservation_version must be a non-negative integer")
        if (
            type(self.authority_policy_version) is not int
            or self.authority_policy_version < 1
        ):
            raise ValueError("authority_policy_version must be positive")
        evaluated = _authority_text(
            self.evaluated_at, name="authoritative risk evaluated_at"
        )
        evaluated_instant = _instant(
            evaluated, name="authoritative risk evaluated_at"
        )
        valid_until = _authority_text(
            self.valid_until, name="authoritative risk valid_until"
        )
        valid_until_instant = _instant(
            valid_until, name="authoritative risk valid_until"
        )
        if valid_until_instant <= evaluated_instant:
            raise ValueError(
                "authoritative risk valid_until must be after evaluated_at"
            )
        if type(self.evidence_refs) is dict:
            raw_evidence_items = tuple(self.evidence_refs.items())
        elif type(self.evidence_refs) is _CanonicalEvidenceRefs:
            raw_evidence_items = self.evidence_refs.items()
        else:
            raise TypeError(
                "authoritative risk evidence_refs must be an exact dict "
                "or canonical evidence refs"
            )
        refs: dict[str, str] = {}
        for raw_dimension, raw_ref in raw_evidence_items:
            if type(raw_dimension) is not str or type(raw_ref) is not str:
                raise TypeError(
                    "authoritative risk evidence refs must use exact strings"
                )
            dimension = _text(
                raw_dimension, name="risk evidence dimension"
            ).upper()
            if dimension in refs:
                raise ValueError("risk evidence dimensions must be unique")
            refs[dimension] = _text(
                raw_ref,
                name=f"risk evidence ref {dimension}",
            )
        required = {
            "PORTFOLIO",
            "MARKET",
            "MARGIN",
            "POLICY",
            "RECONCILIATION",
            "CAPABILITY",
        }
        if normalized_context.fx_required:
            required.add("FX")
        if normalized_context.borrow_available is not None:
            required.add("BORROW")
        if normalized_context.stress_scenarios:
            required.add("STRESS")
        if normalized_context.factor_loadings:
            required.add("FACTORS")
        if (
            normalized_context.liquidity_capacity
            or normalized_context.spread_fraction
            or normalized_context.slippage_fraction
        ):
            required.add("LIQUIDITY")
        if normalized_context.liquidation_headroom_evidence is not None:
            required.add("LIQUIDATION")
        if normalized_context.settlement_allowed is not None:
            required.add("SETTLEMENT")
        if (
            normalized_context.option_deliverable_verified is not None
            or normalized_context.option_exercise_cash_required is not None
            or normalized_context.option_exercise_cash_available is not None
        ):
            required.add("OPTION_LIFECYCLE")
        if normalized_context.futures_delivery_headroom_seconds:
            required.add("FUTURES_LIFECYCLE")
        if self.resolved_risk_policy is not None and refs.get("POLICY") != resolved.registration_event_id:
            raise AuthorityConflict("POLICY evidence differs from durable registration")
        missing = sorted(required - set(refs))
        if missing:
            raise ValueError(
                "authoritative risk snapshot is missing evidence dimensions: "
                + ", ".join(missing)
            )
        object.__setattr__(self, "context", normalized_context)
        object.__setattr__(self, "risk_policy", canonical_policy)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(
            self, "provider_environment", provider_environment
        )
        object.__setattr__(self, "entity_policy_id", entity_policy_id)
        object.__setattr__(self, "instrument_family", instrument_family)
        object.__setattr__(
            self,
            "instrument_version",
            _instrument_identity(
                self.instrument_version,
                name="authoritative risk instrument_version",
            ),
        )
        object.__setattr__(
            self,
            "capability_snapshot_id",
            _authority_text(
                self.capability_snapshot_id,
                name="authoritative risk capability_snapshot_id",
            ),
        )
        object.__setattr__(
            self,
            "reconciliation_checkpoint_event_id",
            _authority_text(
                self.reconciliation_checkpoint_event_id,
                name="authoritative risk reconciliation_checkpoint_event_id",
            ),
        )
        object.__setattr__(
            self,
            "reservation_state_digest",
            _authority_text(
                self.reservation_state_digest,
                name="authoritative risk reservation_state_digest",
            ),
        )
        object.__setattr__(
            self,
            "authority_policy_id",
            _authority_text(
                self.authority_policy_id,
                name="authoritative risk policy_id",
            ),
        )
        object.__setattr__(self, "evaluated_at", evaluated)
        object.__setattr__(self, "valid_until", valid_until)
        object.__setattr__(
            self,
            "evidence_refs",
            _CanonicalEvidenceRefs(tuple(sorted(refs.items()))),
        )

    def _identity_payload(self) -> dict[str, Any]:
        resolved_risk_policy = self.resolved_risk_policy
        if resolved_risk_policy is not None:
            require_registry_issued_resolved_policy(resolved_risk_policy)
        # Keep every activation/registration/cut/store field from evidence_payload.
        # registration_event_id, activation_event_id, resolved_journal_sequence_cut,
        # journal_store_identity_digest are canonical registry-owned identities.
        return {
            **({"resolved_risk_policy": resolved_risk_policy.evidence_payload,
                "provider_environment": self.provider_environment,
                "entity_policy_id": self.entity_policy_id,
                "instrument_family": self.instrument_family}
               if resolved_risk_policy is not None else {}),
            "context_fingerprint": _risk_context_fingerprint(self.context),
            "context_state_version": self.context.state_version,
            "risk_policy_fingerprint": _risk_policy_fingerprint(self.risk_policy),
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_id": self.provider_id,
            "instrument": {
                "instrument_id": self.instrument_version.instrument_id,
                "version": self.instrument_version.version,
            },
            "capability_snapshot_id": self.capability_snapshot_id,
            "reconciliation_checkpoint_event_id": (
                self.reconciliation_checkpoint_event_id
            ),
            "journal_sequence_cut": self.journal_sequence_cut,
            "reservation_version": self.reservation_version,
            "reservation_state_digest": self.reservation_state_digest,
            "authority_policy_id": self.authority_policy_id,
            "authority_policy_version": self.authority_policy_version,
            "evaluated_at": self.evaluated_at,
            "valid_until": self.valid_until,
            "evidence_refs": dict(self.evidence_refs.items()),
        }

    @property
    def snapshot_id(self) -> str:
        digest = sha256(
            json.dumps(
                self._identity_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        return "risk-snapshot:sha256:" + digest

    def evidence_payload(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            **self._identity_payload(),
        }


def _authoritative_risk_provider_scope(
    authoritative_snapshot: Mapping[str, Any],
) -> tuple[str, str]:
    """Resolve the exact provider domain already bound by the risk snapshot."""

    if type(authoritative_snapshot) is not dict:
        raise AuthorityConflict("authoritative risk snapshot is malformed")
    provider = _authority_text(
        authoritative_snapshot.get("provider_id"),
        name="authoritative risk provider_id",
    ).upper()
    runtime = _authority_text(
        authoritative_snapshot.get("environment"),
        name="authoritative risk environment",
    ).upper()
    raw_provider_environment = authoritative_snapshot.get(
        "provider_environment"
    )
    if raw_provider_environment is None:
        if provider == "BYBIT":
            raise AuthorityConflict(
                "BYBIT authoritative risk snapshot lacks provider_environment"
            )
        provider_environment = runtime
    else:
        provider_environment = _authority_text(
            raw_provider_environment,
            name="authoritative risk provider_environment",
        ).upper()
    if provider == "BYBIT":
        if provider_environment not in {"MAINNET", "TESTNET", "DEMO"}:
            raise AuthorityConflict(
                "BYBIT authoritative risk provider_environment is unsupported"
            )
        expected_runtime = (
            "LIVE" if provider_environment == "MAINNET" else "PAPER"
        )
        if runtime != expected_runtime:
            raise AuthorityConflict(
                "BYBIT authoritative risk provider domain does not match runtime"
            )
    return provider, provider_environment


def _require_provider_scope_matches_authoritative_risk_snapshot(
    authoritative_snapshot: Mapping[str, Any],
    evidence: Mapping[str, Any],
    *,
    evidence_name: str,
) -> tuple[str, str]:
    """Reject provider/account evidence that self-asserts a different domain."""

    expected_provider, expected_provider_environment = (
        _authoritative_risk_provider_scope(authoritative_snapshot)
    )
    expected_account = _authority_text(
        authoritative_snapshot.get("account_id"),
        name="authoritative risk account_id",
    )
    expected_runtime = _authority_text(
        authoritative_snapshot.get("environment"),
        name="authoritative risk environment",
    ).upper()
    if type(evidence) is not dict:
        raise AuthorityConflict(f"{evidence_name} is malformed")
    evidence_provider = _authority_text(
        evidence.get("provider_id"),
        name=f"{evidence_name} provider_id",
    ).upper()
    evidence_account = _authority_text(
        evidence.get("account_id"),
        name=f"{evidence_name} account_id",
    )
    evidence_runtime = _authority_text(
        evidence.get("environment"),
        name=f"{evidence_name} environment",
    ).upper()
    raw_provider_environment = evidence.get("provider_environment")
    if raw_provider_environment is None:
        if evidence_provider == "BYBIT":
            raise AuthorityConflict(
                f"{evidence_name} lacks exact BYBIT provider_environment"
            )
        evidence_provider_environment = evidence_runtime
    else:
        evidence_provider_environment = _authority_text(
            raw_provider_environment,
            name=f"{evidence_name} provider_environment",
        ).upper()
    if (
        evidence_provider != expected_provider
        or evidence_account != expected_account
        or evidence_runtime != expected_runtime
        or evidence_provider_environment != expected_provider_environment
    ):
        raise AuthorityConflict(
            f"{evidence_name} provider/account scope differs from authoritative risk snapshot"
        )
    return expected_provider, expected_provider_environment


def _authority_event_id(event_type: str, key: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"https://events.autotrade.local/authority/{event_type}/{key}"))


class AuthorityService:
    def __init__(
        self,
        store: JournalStore | None = None,
        *,
        evidence_artifact_store: ArtifactStore | None = None,
        allocation_authority_resolver: Callable[
            [EvidenceBoundObjectiveAllocationResult],
            AllocationAuthoritySnapshot,
        ]
        | None = None,
        risk_policy_scope: RiskPolicyScope | None = None,
        risk_authority_resolver: Callable[
            [RiskAuthorityRequest],
            AuthoritativeRiskSnapshot,
        ]
        | None = None,
        settlement_book: DurableSettlementBook | None = None,
        economic_book: DurableProviderEconomicBook | None = None,
    ):
        # Validate all composition inputs before publishing any process binding.
        # Explicit __init__ re-entry must fail before it can reset established
        # in-memory authority state or retarget durable journal history.
        if (
            evidence_artifact_store is not None
            and not isinstance(evidence_artifact_store, ArtifactStore)
        ):
            raise TypeError("evidence_artifact_store must be ArtifactStore")
        if (
            allocation_authority_resolver is not None
            and not callable(allocation_authority_resolver)
        ):
            raise TypeError("allocation_authority_resolver must be callable")
        if risk_authority_resolver is not None and not callable(
            risk_authority_resolver
        ):
            raise TypeError("risk_authority_resolver must be callable")

        _register_authority_service_store(self, store, risk_policy_scope)
        # Capital validation reads the already sealed store binding, including
        # its diagnostic view. Publish that view before validating composition.
        self.store = store
        _register_authority_service_capital(
            self,
            settlement_book,
            economic_book,
        )
        # Compatibility/diagnostic view only. Internal authority always resolves
        # the closure-owned binding and rejects caller retargeting.
        self.store = store
        self.evidence_artifact_store = evidence_artifact_store
        # Generic callable resolver injection is a SIMULATION-only seam. PAPER/LIVE
        # must use the product-owned typed issuer/composition tracked by #987;
        # a caller callback remains untrusted even when its returned value is sealed.
        self.allocation_authority_resolver = allocation_authority_resolver
        self.risk_authority_resolver = risk_authority_resolver
        self._policies: dict[str, AuthorityPolicy] = {}
        self._revocations: dict[str, tuple[str, str]] = {}
        self._confirmations: dict[str, Confirmation] = {}
        self._used_confirmations: set[str] = set()
        self._admissions: dict[str, AdmissionRecord] = {}
        self._new_exposure_blocks: dict[tuple[str, str], dict[str, str]] = {}
        self._epoch = 0
        # Last authority aggregate version this process has actually replayed
        # or committed. This is deliberately separate from authority epoch:
        # confirmations/admissions advance the journal even when they do not
        # change the policy/revocation epoch.
        self._journal_version = 0
        if store is not None:
            self._restore_journal()

    def _risk_policy_cut(self, *, provider_id: str, account_id: str,
                         environment: str, journal_sequence_cut: int) -> dict[str, object]:
        scope = _authority_service_risk_policy_scope(self)
        if scope is None:
            return {}
        if (scope.provider_id, scope.account_id, scope.environment) != (
            provider_id.upper(), account_id, environment.upper()):
            raise AuthorityConflict("service RiskPolicy scope differs from financial request")
        store, identity = _authority_service_store_binding(self, required=True)
        resolved = DurableRiskPolicyRegistry(store).resolve_current(
            scope, journal_sequence_cut=journal_sequence_cut)
        require_registry_issued_resolved_policy(resolved)
        if resolved.journal_store_identity_digest != journal_store_identity_digest(identity):
            raise AuthorityConflict("RiskPolicy belongs to another JournalStore generation")
        return {"resolved_risk_policy": resolved,
                "provider_environment": scope.provider_environment,
                "entity_policy_id": scope.entity_policy_id,
                "instrument_family": scope.instrument_family}

    def _resolve_authoritative_risk_snapshot(
        self,
        request: RiskAuthorityRequest,
    ) -> AuthoritativeRiskSnapshot:
        if type(request) is not RiskAuthorityRequest:
            raise TypeError("request must be exact RiskAuthorityRequest")
        if request.environment != "SIMULATION":
            raise AuthorityConflict(
                "PAPER/LIVE financial admission requires a product-owned "
                "authoritative risk resolver; injected callable resolvers are "
                "non-production"
            )
        if self.risk_authority_resolver is None:
            raise AuthorityConflict(
                "financial admission requires a service-owned authoritative risk resolver"
            )
        request = replace(request)  # revalidate frozen-object mutation at use time
        if request.resolved_risk_policy is not None:
            selected = self._risk_policy_cut(provider_id=request.provider_id,
                account_id=request.account_id, environment=request.environment,
                journal_sequence_cut=request.journal_sequence_cut)
            if not selected or selected["resolved_risk_policy"].evidence_payload != request.resolved_risk_policy.evidence_payload:
                raise AuthorityConflict("request RiskPolicy differs from service-selected journal authority")
        snapshot = self.risk_authority_resolver(request)
        if type(snapshot) is not AuthoritativeRiskSnapshot:
            raise AuthorityConflict(
                "risk authority resolver must return exact AuthoritativeRiskSnapshot"
            )
        snapshot = replace(snapshot)
        expected = (
            request.resolved_risk_policy.evidence_payload if request.resolved_risk_policy else None,
            request.provider_environment, request.entity_policy_id, request.instrument_family,
            request.account_id,
            request.environment,
            request.provider_id,
            request.instrument_version,
            request.capability_snapshot_id,
            request.reconciliation_checkpoint_event_id,
            request.journal_sequence_cut,
            request.reservation_version,
            request.reservation_state_digest,
            request.authority_policy_id,
            request.authority_policy_version,
            request.evaluated_at,
        )
        actual = (
            snapshot.resolved_risk_policy.evidence_payload if snapshot.resolved_risk_policy else None,
            snapshot.provider_environment, snapshot.entity_policy_id, snapshot.instrument_family,
            snapshot.account_id,
            snapshot.environment,
            snapshot.provider_id,
            snapshot.instrument_version,
            snapshot.capability_snapshot_id,
            snapshot.reconciliation_checkpoint_event_id,
            snapshot.journal_sequence_cut,
            snapshot.reservation_version,
            snapshot.reservation_state_digest,
            snapshot.authority_policy_id,
            snapshot.authority_policy_version,
            snapshot.evaluated_at,
        )
        if actual != expected:
            raise AuthorityConflict(
                "authoritative risk snapshot does not match the financial writer cut"
            )
        return snapshot

    @staticmethod
    def _instrument_payload(value: InstrumentVersionIdentity) -> dict[str, Any]:
        return {"instrument_id": value.instrument_id, "version": value.version}

    @classmethod
    def _policy_payload(cls, policy: AuthorityPolicy) -> dict[str, Any]:
        return {
            "policy_id": policy.policy_id,
            "account_id": policy.account_id,
            "environments": sorted(policy.environments),
            "instruments": [cls._instrument_payload(item) for item in sorted(policy.instruments)],
            "actions": sorted(policy.actions),
            "max_notional": _canonical_decimal_text(policy.max_notional),
            "expires_at": policy.expires_at,
            "autonomous": policy.autonomous,
            "valid_from": policy.valid_from,
            "protection_only": policy.protection_only,
            "version": policy.version,
        }

    @classmethod
    def _confirmation_payload(cls, confirmation: Confirmation) -> dict[str, Any]:
        return {
            "confirmation_id": confirmation.confirmation_id,
            "policy_id": confirmation.policy_id,
            "intent_hash": confirmation.intent_hash,
            "account_id": confirmation.account_id,
            "environment": confirmation.environment,
            "instrument": cls._instrument_payload(confirmation.instrument_version),
            "action": confirmation.action,
            "notional": _canonical_decimal_text(confirmation.notional),
            "expires_at": confirmation.expires_at,
        }

    @classmethod
    def _admission_payload(cls, record: AdmissionRecord) -> dict[str, Any]:
        return {
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "intent_hash": record.intent_hash,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument": cls._instrument_payload(record.instrument_version),
            "action": record.action,
            "notional": _canonical_decimal_text(record.notional),
            "risk_reducing": record.risk_reducing,
            "state_version": record.state_version,
            "authority_epoch": record.authority_epoch,
            "outcome": record.outcome,
            "admitted_at": record.admitted_at,
            "confirmation_id": record.confirmation_id,
            "reason": record.reason,
            "request_fingerprint": record.request_fingerprint,
            "intent_id": record.intent_id,
            "risk_decision_id": record.risk_decision_id,
            "reservation_id": record.reservation_id,
            "capability_snapshot_id": record.capability_snapshot_id,
            "risk_valid_until": record.risk_valid_until,
            "policy_version": record.policy_version,
            "financial_command_id": record.financial_command_id,
        }

    def _persist(self, event_type: str, key: str, payload: dict[str, Any], *, committed_at: str) -> None:
        if _authority_service_store(self) is None:
            return

        # Compare-and-append from this process's observed journal position.
        # Reading "next version" from the shared DB here would let a stale
        # AuthorityService silently append after another process and make
        # decisions from obsolete confirmation/revocation state.
        durable_next = _authority_store_call(self, "next_aggregate_version",
            "authority_state", "canonical"
        )
        durable_version = durable_next - 1
        if durable_version != self._journal_version:
            raise AuthorityConflict(
                "durable authority journal advanced; reload required"
            )

        event_id = _authority_event_id(event_type, key)
        existing = _authority_store_call(self, "get_event", event_id)
        if existing is not None:
            if existing["event_type"] != event_type or existing["payload"] != payload:
                raise AuthorityConflict("durable authority event conflicts with existing content")
            # If our observed version matches durable state, this event was
            # already part of our replay. Callers should have handled the
            # corresponding in-memory idempotency path before reaching here.
            return
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": "authority_state",
            "aggregate_id": "canonical",
            "aggregate_version": str(self._journal_version + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            result = _authority_store_call(self, "append_event", envelope)
        except ValueError as error:
            # A concurrent writer may have won after the version check but
            # before our append. Never reinterpret that race as idempotency:
            # the caller must reload and re-evaluate authority state.
            existing = _authority_store_call(self, "get_event", event_id)
            if (
                existing is not None
                and existing["event_type"] == event_type
                and existing["payload"] == payload
                and _authority_store_call(self, "next_aggregate_version",
                    "authority_state", "canonical"
                ) - 1 == self._journal_version
            ):
                return
            raise AuthorityConflict(
                "durable authority journal changed concurrently; reload required"
            ) from error
        if result.inserted:
            self._journal_version += 1

    def _restore_journal(self) -> None:
        assert _authority_service_store(self, required=True) is not None
        for event in _authority_store_call(self, "load_events", "authority_state", "canonical"):
            payload = event["payload"]
            event_type = event["event_type"]
            event_version = int(event["aggregate_version"])
            if event_version != self._journal_version + 1:
                raise AuthorityConflict(
                    "durable authority journal version sequence is invalid"
                )
            if event_type == "AuthorityPolicyRegistered":
                instruments = payload.get("instruments")
                if not isinstance(instruments, list):
                    raise AuthorityConflict("durable policy instruments malformed")
                policy = AuthorityPolicy.create(
                    policy_id=payload["policy_id"],
                    account_id=payload["account_id"],
                    environments=payload["environments"],
                    instruments=[
                        InstrumentVersionIdentity(item["instrument_id"], item["version"])
                        for item in instruments
                        if isinstance(item, dict)
                    ],
                    actions=payload["actions"],
                    max_notional=payload["max_notional"],
                    expires_at=payload["expires_at"],
                    autonomous=payload["autonomous"],
                    valid_from=payload["valid_from"],
                    protection_only=payload["protection_only"],
                    version=payload.get("version", 1),
                )
                if len(policy.instruments) != len(instruments):
                    raise AuthorityConflict("durable policy instrument entry malformed")
                existing = self._policies.get(policy.policy_id)
                if existing is not None and existing != policy:
                    raise AuthorityConflict("durable policy history conflicts")
                if existing is None:
                    self._policies[policy.policy_id] = policy
                    self._epoch += 1
            elif event_type == "AuthorityPolicyRevoked":
                policy_id = _text(payload.get("policy_id"), name="policy_id")
                if policy_id not in self._policies:
                    raise AuthorityConflict("durable revocation references missing policy")
                reason = _text(payload.get("reason"), name="reason")
                revoked_at = _text(payload.get("revoked_at"), name="revoked_at")
                _instant(revoked_at, name="revoked_at")
                value = (reason, revoked_at)
                existing = self._revocations.get(policy_id)
                if existing is not None and existing != value:
                    raise AuthorityConflict("durable revocation history conflicts")
                if existing is None:
                    self._revocations[policy_id] = value
                    self._epoch += 1
            elif event_type == "AuthorityNewExposureBlocked":
                command_id = _text(payload.get("command_id"), name="command_id")
                account_id = _text(payload.get("account_id"), name="account_id")
                environment = _text(
                    payload.get("environment"), name="environment"
                ).upper()
                if environment not in {"SIMULATION", "PAPER", "LIVE"}:
                    raise AuthorityConflict(
                        "durable new-exposure block environment is unsupported"
                    )
                reason = _text(payload.get("reason"), name="reason")
                blocked_at = _text(payload.get("blocked_at"), name="blocked_at")
                _instant(blocked_at, name="blocked_at")
                scope = (account_id, environment)
                existing = self._new_exposure_blocks.get(scope)
                value = {
                    "command_id": command_id,
                    "reason": reason,
                    "blocked_at": blocked_at,
                }
                if existing is not None and existing != value:
                    raise AuthorityConflict(
                        "durable new-exposure block history conflicts"
                    )
                self._new_exposure_blocks[scope] = value
            elif event_type == "AuthorityNewExposureRestored":
                command_id = _text(payload.get("command_id"), name="command_id")
                account_id = _text(payload.get("account_id"), name="account_id")
                environment = _text(
                    payload.get("environment"), name="environment"
                ).upper()
                if environment not in {"SIMULATION", "PAPER", "LIVE"}:
                    raise AuthorityConflict(
                        "durable new-exposure restore environment is unsupported"
                    )
                reason = _text(payload.get("reason"), name="reason")
                restored_at = _text(
                    payload.get("restored_at"), name="restored_at"
                )
                _instant(restored_at, name="restored_at")
                blocked_command_id = _text(
                    payload.get("blocked_command_id"),
                    name="blocked_command_id",
                )
                blocked_reason = _text(
                    payload.get("blocked_reason"),
                    name="blocked_reason",
                )
                blocked_at = _text(
                    payload.get("blocked_at"), name="blocked_at"
                )
                _instant(blocked_at, name="blocked_at")
                scope = (account_id, environment)
                active = self._new_exposure_blocks.get(scope)
                expected = {
                    "command_id": blocked_command_id,
                    "reason": blocked_reason,
                    "blocked_at": blocked_at,
                }
                if active != expected:
                    raise AuthorityConflict(
                        "durable new-exposure restore does not match active block"
                    )
                del self._new_exposure_blocks[scope]
            elif event_type == "AuthorityConfirmationAdded":
                instrument = payload.get("instrument")
                if not isinstance(instrument, dict):
                    raise AuthorityConflict("durable confirmation instrument malformed")
                confirmation = Confirmation(
                    confirmation_id=payload["confirmation_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument_version=InstrumentVersionIdentity(
                        instrument["instrument_id"], instrument["version"]
                    ),
                    action=payload["action"],
                    notional=_decimal(payload["notional"], name="notional"),
                    expires_at=payload["expires_at"],
                )
                if confirmation.policy_id not in self._policies:
                    raise AuthorityConflict("durable confirmation references missing policy")
                existing = self._confirmations.get(confirmation.confirmation_id)
                if existing is not None and existing != confirmation:
                    raise AuthorityConflict("durable confirmation history conflicts")
                self._confirmations[confirmation.confirmation_id] = confirmation
            elif event_type == "AuthorityAdmissionRecorded":
                instrument = payload.get("instrument")
                if not isinstance(instrument, dict):
                    raise AuthorityConflict("durable admission instrument malformed")
                record = AdmissionRecord(
                    admission_id=payload["admission_id"],
                    policy_id=payload["policy_id"],
                    intent_hash=payload["intent_hash"],
                    account_id=payload["account_id"],
                    environment=payload["environment"],
                    instrument_version=InstrumentVersionIdentity(
                        instrument["instrument_id"], instrument["version"]
                    ),
                    action=payload["action"],
                    notional=_decimal(payload["notional"], name="notional"),
                    risk_reducing=payload["risk_reducing"],
                    state_version=payload["state_version"],
                    authority_epoch=payload["authority_epoch"],
                    outcome=payload["outcome"],
                    admitted_at=payload["admitted_at"],
                    confirmation_id=payload["confirmation_id"],
                    reason=payload["reason"],
                    request_fingerprint=payload["request_fingerprint"],
                    intent_id=payload.get("intent_id"),
                    risk_decision_id=payload.get("risk_decision_id"),
                    reservation_id=payload.get("reservation_id"),
                    capability_snapshot_id=payload.get("capability_snapshot_id"),
                    risk_valid_until=payload.get("risk_valid_until"),
                    policy_version=payload.get("policy_version"),
                    financial_command_id=payload.get("financial_command_id"),
                )
                if record.policy_id not in self._policies:
                    raise AuthorityConflict("durable admission references missing policy")
                policy = self._policies[record.policy_id]
                if record.authority_epoch != self._epoch:
                    raise AuthorityConflict(
                        "durable admission authority epoch does not match replay state"
                    )
                if record.outcome == "ADMITTED":
                    active, _ = self._policy_active(policy, record.admitted_at)
                    scope_valid = (
                        active
                        and record.account_id == policy.account_id
                        and record.environment in policy.environments
                        and record.instrument_version in policy.instruments
                        and record.action in policy.actions
                        and record.notional <= policy.max_notional
                        and (
                            (record.account_id, record.environment)
                            not in self._new_exposure_blocks
                            or record.risk_reducing
                        )
                        and (not policy.protection_only or record.risk_reducing)
                    )
                    if not scope_valid:
                        raise AuthorityConflict(
                            "durable admitted record violates policy scope"
                        )
                    if not policy.autonomous and record.confirmation_id is None:
                        raise AuthorityConflict(
                            "durable admitted record is missing required confirmation"
                        )
                    if record.risk_decision_id is None:
                        request_payload = {
                            "policy_id": record.policy_id,
                            "intent_hash": record.intent_hash,
                            "account_id": record.account_id,
                            "environment": record.environment,
                            "instrument_id": record.instrument_version.instrument_id,
                            "instrument_version": record.instrument_version.version,
                            "action": record.action,
                            "notional": _canonical_decimal_text(record.notional),
                            "state_version": record.state_version,
                            "risk_admitted": True,
                            "confirmation_id": record.confirmation_id,
                            "risk_reducing": record.risk_reducing,
                        }
                        expected_fingerprint = sha256(
                            json.dumps(
                                request_payload,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8")
                        ).hexdigest()
                        if record.request_fingerprint != expected_fingerprint:
                            raise AuthorityConflict(
                                "durable admitted record fingerprint is inconsistent"
                            )
                    else:
                        self._validate_durable_financial_evidence(record, policy)
                if record.confirmation_id is not None:
                    if record.confirmation_id not in self._confirmations:
                        raise AuthorityConflict(
                            "durable admission references missing confirmation"
                        )
                    confirmation = self._confirmations[record.confirmation_id]
                    if (
                        confirmation.policy_id != record.policy_id
                        or confirmation.intent_hash != record.intent_hash
                        or confirmation.account_id != record.account_id
                        or confirmation.environment != record.environment
                        or confirmation.instrument_version != record.instrument_version
                        or confirmation.action != record.action
                        or confirmation.notional != record.notional
                        or _instant(record.admitted_at, name="admitted_at")
                        >= _instant(
                            confirmation.expires_at,
                            name="confirmation.expires_at",
                        )
                    ):
                        raise AuthorityConflict(
                            "durable admission does not match confirmation scope"
                        )
                    if record.confirmation_id in self._used_confirmations:
                        raise AuthorityConflict(
                            "durable confirmation was consumed by multiple admissions"
                        )
                existing = self._admissions.get(record.admission_id)
                if existing is not None and existing != record:
                    raise AuthorityConflict("durable admission history conflicts")
                self._admissions[record.admission_id] = record
                if record.outcome == "ADMITTED" and record.confirmation_id is not None:
                    self._used_confirmations.add(record.confirmation_id)
            else:
                raise AuthorityConflict(f"unknown durable authority event: {event_type}")
            self._journal_version = event_version

    def _validate_historical_financial_retry_evidence(
        self,
        record: AdmissionRecord,
        policy: AuthorityPolicy,
    ) -> dict[str, Any]:
        """Validate the immutable original financial transaction for idempotent replay.

        This deliberately does not ask whether the admission is still current.
        Later reconciliation, settlement, lifecycle, policy-revocation, market,
        or provider facts are dispatch/currentness concerns. Historical replay
        proves only that the exact accepted/rejected command and its original
        evidence are durably self-consistent.
        """

        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "historical financial retry requires a JournalStore"
            )
        required = (
            record.intent_id,
            record.risk_decision_id,
            record.capability_snapshot_id,
            record.risk_valid_until,
            record.policy_version,
            record.financial_command_id,
        )
        if any(value is None for value in required):
            raise AuthorityConflict(
                "historical financial retry record has incomplete evidence"
            )
        if record.policy_id != policy.policy_id or record.policy_version != policy.version:
            raise AuthorityConflict(
                "historical financial retry policy identity is inconsistent"
            )

        risk_events = _authority_store_call(self, "load_events",
            "risk_decision", record.risk_decision_id
        )
        if (
            len(risk_events) != 1
            or risk_events[0]["event_type"] != "RiskDecisionRecorded"
            or type(risk_events[0].get("payload")) is not dict
        ):
            raise AuthorityConflict(
                "historical financial retry risk evidence is missing or ambiguous"
            )
        risk_event = risk_events[0]
        risk_payload = risk_event["payload"]
        historical_arithmetic_policy_id = risk_payload.get(
            "arithmetic_policy_id"
        )
        if (
            historical_arithmetic_policy_id is not None
            and historical_arithmetic_policy_id != RISK_ARITHMETIC_POLICY_ID
        ):
            raise AuthorityConflict(
                "historical risk arithmetic policy is unsupported"
            )

        durable_risk_intent = risk_payload.get("risk_intent")
        durable_idempotency_key = risk_payload.get(
            "financial_idempotency_key"
        )
        durable_reservation_id = risk_payload.get(
            "financial_reservation_id"
        )
        if (
            durable_risk_intent is None
            or durable_idempotency_key is None
            or durable_reservation_id is None
        ):
            raise AuthorityConflict(
                "existing admission lacks exact durable retry identity"
            )
        durable_risk_intent = _durable_risk_intent_payload(
            durable_risk_intent
        )
        durable_idempotency_key = _text(
            durable_idempotency_key,
            name="durable financial idempotency key",
        )
        durable_reservation_id = _text(
            durable_reservation_id,
            name="durable financial reservation_id",
        )

        journal_sequence_cut = risk_payload.get("journal_sequence_cut")
        risk_journal_sequence = risk_event.get("journal_sequence")
        if (
            type(journal_sequence_cut) is not int
            or journal_sequence_cut < 0
            or type(risk_journal_sequence) is not int
            or risk_journal_sequence <= journal_sequence_cut
        ):
            raise AuthorityConflict(
                "historical financial retry journal cut is invalid"
            )

        authoritative_snapshot = risk_payload.get(
            "authoritative_risk_snapshot"
        )
        if not isinstance(authoritative_snapshot, Mapping):
            raise AuthorityConflict(
                "historical financial retry lacks authoritative risk snapshot"
            )
        snapshot_id = _text(
            authoritative_snapshot.get("snapshot_id"),
            name="authoritative risk snapshot_id",
        )
        snapshot_identity = dict(authoritative_snapshot)
        snapshot_identity.pop("snapshot_id", None)
        expected_snapshot_id = "risk-snapshot:sha256:" + sha256(
            json.dumps(
                snapshot_identity,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        if snapshot_id != expected_snapshot_id:
            raise AuthorityConflict(
                "historical authoritative risk snapshot digest is invalid"
            )
        instrument = authoritative_snapshot.get("instrument")
        if (
            authoritative_snapshot.get("account_id") != record.account_id
            or authoritative_snapshot.get("environment") != record.environment
            or authoritative_snapshot.get("capability_snapshot_id")
            != record.capability_snapshot_id
            or authoritative_snapshot.get("authority_policy_id")
            != record.policy_id
            or authoritative_snapshot.get("authority_policy_version")
            != record.policy_version
            or authoritative_snapshot.get("context_state_version")
            != record.state_version
            or authoritative_snapshot.get("valid_until")
            != record.risk_valid_until
            or not isinstance(instrument, Mapping)
            or instrument.get("instrument_id")
            != record.instrument_version.instrument_id
            or instrument.get("version")
            != record.instrument_version.version
        ):
            raise AuthorityConflict(
                "historical authoritative risk snapshot scope is inconsistent"
            )

        risk_digest = record.risk_decision_id.removeprefix(
            "risk:sha256:"
        )
        expected_verdict = (
            "ALLOW" if record.outcome == "ADMITTED" else "REJECT"
        )
        if (
            risk_payload.get("decision_id") != record.risk_decision_id
            or risk_payload.get("fingerprint") != risk_digest
            or risk_payload.get("intent_hash") != record.intent_hash
            or risk_payload.get("state_version") != record.state_version
            or risk_payload.get("policy_version") != record.policy_version
            or risk_payload.get("capability_snapshot_id")
            != record.capability_snapshot_id
            or risk_payload.get("valid_until") != record.risk_valid_until
            or risk_payload.get("verdict") != expected_verdict
        ):
            raise AuthorityConflict(
                "historical risk decision does not match admission"
            )
        if _instant(
            risk_payload.get("evaluated_at"),
            name="risk.evaluated_at",
        ) > _instant(record.admitted_at, name="admitted_at"):
            raise AuthorityConflict(
                "historical risk decision was evaluated after admission"
            )
        if _instant(record.admitted_at, name="admitted_at") >= _instant(
            record.risk_valid_until,
            name="risk_valid_until",
        ):
            raise AuthorityConflict(
                "historical risk decision was expired at admission"
            )

        risk_requirements = risk_payload.get("reservation_requirements")
        if type(risk_requirements) is not dict:
            raise AuthorityConflict(
                "historical risk reservation requirements are malformed"
            )

        reservation_request = None
        availability_evidence = risk_payload.get(
            "reservation_availability_evidence"
        )
        if record.outcome == "ADMITTED":
            if record.reservation_id != durable_reservation_id:
                raise AuthorityConflict(
                    "historical reservation identity is inconsistent"
                )
            reservation_book = DurableReservationBook(
                _authority_service_store(self, required=True),
                environment=record.environment,
                account_id=record.account_id,
            )
            reservation_events = _authority_store_call(self, "load_events",
                "reservation_book",
                reservation_book.scope_id,
            )
            matching_reservations = [
                event
                for event in reservation_events
                if (
                    isinstance(event.get("payload"), dict)
                    and event["payload"].get("operation") == "RESERVE"
                    and isinstance(
                        event["payload"].get("snapshot"),
                        dict,
                    )
                    and event["payload"]["snapshot"].get(
                        "reservation_id"
                    )
                    == record.reservation_id
                )
            ]
            if len(matching_reservations) != 1:
                raise AuthorityConflict(
                    "historical reservation creation evidence is ambiguous"
                )
            reservation_request = matching_reservations[0]["payload"].get(
                "request"
            )
            if (
                type(reservation_request) is not dict
                or reservation_request.get("requirements")
                != risk_requirements
            ):
                raise AuthorityConflict(
                    "historical reservation request differs from risk decision"
                )
            if not isinstance(availability_evidence, Mapping):
                raise AuthorityConflict(
                    "historical admitted command lacks availability evidence"
                )
            snapshot_provider_id, snapshot_provider_environment = (
                _require_provider_scope_matches_authoritative_risk_snapshot(
                    authoritative_snapshot,
                    availability_evidence,
                    evidence_name="historical availability evidence",
                )
            )
            checkpoint_event_id = _text(
                availability_evidence.get("checkpoint_event_id"),
                name="checkpoint_event_id",
            )
            checkpoint = _authority_store_call(self, "get_event", checkpoint_event_id)
            if checkpoint is None:
                raise AuthorityConflict(
                    "historical availability checkpoint is missing"
                )
            if (
                checkpoint.get("event_type") != "AccountReconciled"
                or checkpoint.get("aggregate_type")
                != "account_reconciliation"
                or checkpoint.get("payload_hash")
                != availability_evidence.get(
                    "checkpoint_payload_hash"
                )
                or checkpoint.get("aggregate_id")
                != availability_evidence.get(
                    "checkpoint_aggregate_id"
                )
                or checkpoint.get("aggregate_version")
                != availability_evidence.get(
                    "checkpoint_aggregate_version"
                )
            ):
                raise AuthorityConflict(
                    "historical availability checkpoint identity is inconsistent"
                )
            checkpoint_payload = checkpoint.get("payload")
            if not isinstance(checkpoint_payload, Mapping):
                raise AuthorityConflict(
                    "historical availability checkpoint scope is inconsistent"
                )
            _require_provider_scope_matches_authoritative_risk_snapshot(
                authoritative_snapshot,
                checkpoint_payload,
                evidence_name="historical availability checkpoint",
            )
            if (
                checkpoint_payload.get("provider_id") != snapshot_provider_id
                or checkpoint_payload.get("account_id") != record.account_id
                or checkpoint_payload.get("environment") != record.environment
                or _text(
                    checkpoint_payload.get(
                        "provider_environment",
                        checkpoint_payload.get("environment"),
                    ),
                    name="historical checkpoint provider_environment",
                ).upper()
                != snapshot_provider_environment
            ):
                raise AuthorityConflict(
                    "historical availability checkpoint scope is inconsistent"
                )
        else:
            if record.reservation_id is not None:
                raise AuthorityConflict(
                    "historical rejected command unexpectedly owns a reservation"
                )
            if availability_evidence is not None:
                raise AuthorityConflict(
                    "historical rejected command unexpectedly has availability evidence"
                )

        request = {
            "command_id": record.financial_command_id,
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "policy_version": record.policy_version,
            "intent_id": record.intent_id,
            "intent_hash": record.intent_hash,
            "risk_intent": durable_risk_intent,
            "financial_idempotency_key": durable_idempotency_key,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument_id": record.instrument_version.instrument_id,
            "instrument_version": record.instrument_version.version,
            "action": record.action,
            "notional": _canonical_decimal_text(record.notional),
            "current_state_version": record.state_version,
            "capability_snapshot_id": record.capability_snapshot_id,
            "risk_decision_id": record.risk_decision_id,
            "risk_decision_fingerprint": risk_payload.get("fingerprint"),
            "reservation_id": durable_reservation_id,
            "reservation": reservation_request,
            "reservation_availability_evidence": availability_evidence,
            "authoritative_risk_snapshot": dict(
                authoritative_snapshot
            ),
            "confirmation_id": record.confirmation_id,
            "risk_reducing": record.risk_reducing,
            "journal_sequence_cut": journal_sequence_cut,
        }
        if historical_arithmetic_policy_id is not None:
            request["risk_arithmetic_policy_id"] = (
                historical_arithmetic_policy_id
            )
        if "allocation_evidence" in risk_payload:
            allocation_evidence = risk_payload.get(
                "allocation_evidence"
            )
            if not isinstance(allocation_evidence, Mapping):
                raise AuthorityConflict(
                    "historical allocation evidence binding is malformed"
                )
            request["allocation_evidence"] = dict(
                allocation_evidence
            )

        expected_request_fingerprint = sha256(
            json.dumps(
                request,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if expected_request_fingerprint != record.request_fingerprint:
            raise AuthorityConflict(
                "historical financial request fingerprint is inconsistent"
            )
        return risk_payload


    def _validate_durable_financial_evidence(
        self,
        record: AdmissionRecord,
        policy: AuthorityPolicy,
        *,
        require_transaction_cut: bool = False,
    ) -> None:
        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "durable financial evidence requires a JournalStore"
            )
        required = (
            record.intent_id,
            record.risk_decision_id,
            record.reservation_id,
            record.capability_snapshot_id,
            record.risk_valid_until,
            record.policy_version,
            record.financial_command_id,
        )
        if any(value is None for value in required):
            raise AuthorityConflict(
                "durable admitted financial record has incomplete evidence"
            )
        if record.policy_version != policy.version:
            raise AuthorityConflict(
                "durable admitted record policy version is stale"
            )

        risk_events = _authority_store_call(self, "load_events",
            "risk_decision", record.risk_decision_id
        )
        if len(risk_events) != 1 or risk_events[0]["event_type"] != "RiskDecisionRecorded":
            raise AuthorityConflict(
                "durable admission references missing risk decision evidence"
            )
        risk_event = risk_events[0]
        risk_payload = risk_event["payload"]
        if type(risk_payload) is not dict:
            raise AuthorityConflict("durable risk decision payload is malformed")
        if risk_payload.get("arithmetic_policy_id") != RISK_ARITHMETIC_POLICY_ID:
            raise AuthorityConflict(
                "durable risk arithmetic policy is missing or stale"
            )
        durable_risk_intent = risk_payload.get("risk_intent")
        durable_idempotency_key = risk_payload.get(
            "financial_idempotency_key"
        )
        if (durable_risk_intent is None) != (
            durable_idempotency_key is None
        ):
            raise AuthorityConflict(
                "durable financial retry identity is incomplete"
            )
        if durable_risk_intent is not None:
            durable_risk_intent = _durable_risk_intent_payload(
                durable_risk_intent
            )
            durable_idempotency_key = _text(
                durable_idempotency_key,
                name="durable financial idempotency key",
            )
            durable_reservation_id = risk_payload.get(
                "financial_reservation_id"
            )
            if durable_reservation_id is None:
                raise AuthorityConflict(
                    "durable financial retry identity is incomplete"
                )
            if _text(
                durable_reservation_id,
                name="durable financial reservation_id",
            ) != record.reservation_id:
                raise AuthorityConflict(
                    "durable financial reservation identity is inconsistent"
                )
        journal_sequence_cut = risk_payload.get("journal_sequence_cut")
        if journal_sequence_cut is None:
            if require_transaction_cut:
                raise AuthorityConflict(
                    "durable financial admission lacks transaction journal cut"
                )
        else:
            if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
                raise AuthorityConflict(
                    "durable financial admission journal cut is invalid"
                )
            risk_journal_sequence = risk_event.get("journal_sequence")
            if (
                type(risk_journal_sequence) is not int
                or risk_journal_sequence <= journal_sequence_cut
            ):
                raise AuthorityConflict(
                    "durable risk decision is not after its transaction journal cut"
                )
        authoritative_snapshot = risk_payload.get(
            "authoritative_risk_snapshot"
        )
        if authoritative_snapshot is not None:
            if not isinstance(authoritative_snapshot, Mapping):
                raise AuthorityConflict(
                    "durable authoritative risk snapshot is malformed"
                )
            snapshot_id = _text(
                authoritative_snapshot.get("snapshot_id"),
                name="authoritative risk snapshot_id",
            )
            identity = dict(authoritative_snapshot)
            identity.pop("snapshot_id", None)
            expected_snapshot_id = "risk-snapshot:sha256:" + sha256(
                json.dumps(
                    identity,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ).encode("utf-8")
            ).hexdigest()
            if snapshot_id != expected_snapshot_id:
                raise AuthorityConflict(
                    "durable authoritative risk snapshot digest is invalid"
                )
            instrument = authoritative_snapshot.get("instrument")
            if (
                authoritative_snapshot.get("account_id") != record.account_id
                or authoritative_snapshot.get("environment")
                != record.environment
                or authoritative_snapshot.get("capability_snapshot_id")
                != record.capability_snapshot_id
                or authoritative_snapshot.get("authority_policy_id")
                != record.policy_id
                or authoritative_snapshot.get("authority_policy_version")
                != record.policy_version
                or authoritative_snapshot.get("context_state_version")
                != record.state_version
                or authoritative_snapshot.get("valid_until")
                != record.risk_valid_until
                or not isinstance(instrument, Mapping)
                or instrument.get("instrument_id")
                != record.instrument_version.instrument_id
                or instrument.get("version")
                != record.instrument_version.version
            ):
                raise AuthorityConflict(
                    "durable authoritative risk snapshot scope is inconsistent"
                )

        risk_digest = record.risk_decision_id.removeprefix("risk:sha256:")
        if (
            risk_payload.get("decision_id") != record.risk_decision_id
            or risk_payload.get("fingerprint") != risk_digest
            or risk_payload.get("intent_hash") != record.intent_hash
            or risk_payload.get("state_version") != record.state_version
            or risk_payload.get("policy_version") != record.policy_version
            or risk_payload.get("capability_snapshot_id")
            != record.capability_snapshot_id
            or risk_payload.get("valid_until") != record.risk_valid_until
            or risk_payload.get("verdict") != "ALLOW"
        ):
            raise AuthorityConflict(
                "durable risk decision does not match admitted record"
            )
        if _instant(
            risk_payload.get("evaluated_at"), name="risk.evaluated_at"
        ) > _instant(record.admitted_at, name="admitted_at"):
            raise AuthorityConflict("risk decision was evaluated after admission")
        if _instant(record.admitted_at, name="admitted_at") >= _instant(
            record.risk_valid_until, name="risk_valid_until"
        ):
            raise AuthorityConflict("risk decision was expired at admission")

        reservation_book = DurableReservationBook(
            _authority_service_store(self, required=True),
            environment=record.environment,
            account_id=record.account_id,
        )
        try:
            reservation = reservation_book.get(record.reservation_id)
        except KeyError as error:
            raise AuthorityConflict(
                "durable admission references missing reservation"
            ) from error
        if reservation.intent_id != record.intent_id:
            raise AuthorityConflict(
                "durable reservation intent does not match admission"
            )

        reservation_events = _authority_store_call(self, "load_events",
            "reservation_book", reservation_book.scope_id
        )
        matching_reservations = [
            event
            for event in reservation_events
            if (
                isinstance(event.get("payload"), dict)
                and event["payload"].get("operation") == "RESERVE"
                and isinstance(event["payload"].get("snapshot"), dict)
                and event["payload"]["snapshot"].get("reservation_id")
                == record.reservation_id
            )
        ]
        if len(matching_reservations) != 1:
            raise AuthorityConflict(
                "durable admission reservation creation evidence is ambiguous"
            )
        reservation_event = matching_reservations[0]
        reservation_request = reservation_event["payload"].get("request")
        if not isinstance(reservation_request, dict):
            raise AuthorityConflict(
                "durable reservation request evidence is malformed"
            )
        risk_requirements = risk_payload.get("reservation_requirements")
        if (
            not isinstance(risk_requirements, dict)
            or reservation_request.get("requirements") != risk_requirements
        ):
            raise AuthorityConflict(
                "risk decision reservation delta does not match durable reservation"
            )

        availability_evidence = risk_payload.get(
            "reservation_availability_evidence"
        )
        if not isinstance(availability_evidence, Mapping):
            raise AuthorityConflict(
                "durable admission lacks reservation availability evidence"
            )
        if authoritative_snapshot is not None:
            snapshot_provider_id, snapshot_provider_environment = (
                _require_provider_scope_matches_authoritative_risk_snapshot(
                    authoritative_snapshot,
                    availability_evidence,
                    evidence_name="durable reservation availability evidence",
                )
            )
        else:
            snapshot_provider_id = _text(
                availability_evidence.get("provider_id"),
                name="provider_id",
            ).upper()
            snapshot_provider_environment = _text(
                availability_evidence.get(
                    "provider_environment",
                    availability_evidence.get("environment"),
                ),
                name="provider_environment",
            ).upper()
            if snapshot_provider_id == "BYBIT":
                raise AuthorityConflict(
                    "BYBIT durable admission lacks authoritative risk provider domain"
                )
        try:
            regenerated_availability = (
                load_account_resource_availability_evidence(
                    _authority_service_store(self, required=True),
                    checkpoint_event_id=_text(
                        availability_evidence.get("checkpoint_event_id"),
                        name="checkpoint_event_id",
                    ),
                    provider_id=snapshot_provider_id,
                    account_id=record.account_id,
                    environment=record.environment,
                    provider_environment=snapshot_provider_environment,
                    resources=tuple(sorted(risk_requirements)),
                    now=record.admitted_at,
                    max_age_seconds=availability_evidence.get(
                        "max_age_seconds"
                    ),
                    evidence_artifact_store=self.evidence_artifact_store,
                    journal_sequence_cut=journal_sequence_cut,
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise AuthorityConflict(
                "durable reservation availability evidence is invalid"
            ) from error
        expected_availability_evidence = {
            **regenerated_availability,
            "max_age_seconds": _canonical_decimal_text(
                _decimal(
                    availability_evidence.get("max_age_seconds"),
                    name="reservation_max_age_seconds",
                )
            ),
        }
        scope_latest_event_id = _text(
            availability_evidence.get("scope_latest_checkpoint_event_id"),
            name="scope_latest_checkpoint_event_id",
        )
        scope_latest_aggregate_id = _text(
            availability_evidence.get("scope_latest_checkpoint_aggregate_id"),
            name="scope_latest_checkpoint_aggregate_id",
        )
        scope_latest_version = availability_evidence.get(
            "scope_latest_checkpoint_aggregate_version"
        )
        scope_latest_observed_at = _text(
            availability_evidence.get("scope_latest_checkpoint_observed_at"),
            name="scope_latest_checkpoint_observed_at",
        )
        scope_latest_journal_sequence = availability_evidence.get(
            "scope_latest_checkpoint_journal_sequence"
        )
        if scope_latest_journal_sequence is not None:
            if (
                type(scope_latest_journal_sequence) is not int
                or scope_latest_journal_sequence <= 0
            ):
                raise AuthorityConflict(
                    "durable latest reconciliation journal sequence is invalid"
                )
            scope_checkpoint = _authority_store_call(self, "get_event", scope_latest_event_id)
            if (
                scope_checkpoint is None
                or scope_checkpoint.get("journal_sequence")
                != scope_latest_journal_sequence
            ):
                raise AuthorityConflict(
                    "durable latest reconciliation journal sequence is inconsistent"
                )
        if (
            scope_latest_event_id
            != regenerated_availability.get("checkpoint_event_id")
            or scope_latest_aggregate_id
            != regenerated_availability.get("checkpoint_aggregate_id")
            or type(scope_latest_version) is not int
            or scope_latest_version <= 0
            or scope_latest_version
            != regenerated_availability.get("checkpoint_aggregate_version")
            or _instant(
                scope_latest_observed_at,
                name="scope_latest_checkpoint_observed_at",
            )
            != _instant(
                regenerated_availability.get("observed_at"),
                name="checkpoint.observed_at",
            )
        ):
            raise AuthorityConflict(
                "durable latest reconciliation binding is inconsistent"
            )
        expected_availability_evidence.update(
            {
                "scope_latest_checkpoint_event_id": scope_latest_event_id,
                "scope_latest_checkpoint_aggregate_id": scope_latest_aggregate_id,
                "scope_latest_checkpoint_aggregate_version": scope_latest_version,
                "scope_latest_checkpoint_observed_at": scope_latest_observed_at,
            }
        )
        if scope_latest_journal_sequence is None:
            # Historical durable evidence created before the explicit v6 cursor
            # remains verifiable after migration; newly generated v6 evidence binds it.
            expected_availability_evidence.pop(
                "scope_latest_checkpoint_journal_sequence",
                None,
            )
        else:
            expected_availability_evidence[
                "scope_latest_checkpoint_journal_sequence"
            ] = scope_latest_journal_sequence
        borrow_resources = tuple(
            sorted(
                resource
                for resource in risk_requirements
                if resource.startswith("BORROW:")
            )
        )
        raw_adjustments = availability_evidence.get(
            "borrow_capacity_adjustments"
        )
        reservation_expected_available = expected_availability_evidence.get(
            "availability"
        )
        if not isinstance(reservation_expected_available, Mapping):
            raise AuthorityConflict(
                "regenerated reservation availability is malformed"
            )
        legacy_expected_availability_evidence: dict[str, Any] | None = None
        if borrow_resources:
            if (
                not isinstance(raw_adjustments, Mapping)
                or set(raw_adjustments) != set(borrow_resources)
            ):
                raise AuthorityConflict(
                    "durable borrow capacity adjustments are missing or ambiguous"
                )
            regenerated_available = regenerated_availability.get(
                "availability"
            )
            if not isinstance(regenerated_available, Mapping):
                raise AuthorityConflict(
                    "regenerated reservation availability is malformed"
                )
            adjusted_available = dict(regenerated_available)
            canonical_adjustments: dict[str, dict[str, str]] = {}
            for resource in borrow_resources:
                raw_adjustment = raw_adjustments.get(resource)
                if not isinstance(raw_adjustment, Mapping) or set(
                    raw_adjustment
                ) != {
                    "total_capacity",
                    "current_borrowed_quantity",
                    "reservable_capacity",
                    "required_increment",
                }:
                    raise AuthorityConflict(
                        "durable borrow capacity adjustment is malformed"
                    )
                total_capacity = _decimal(
                    raw_adjustment.get("total_capacity"),
                    name="borrow.total_capacity",
                )
                current_borrowed = _decimal(
                    raw_adjustment.get("current_borrowed_quantity"),
                    name="borrow.current_borrowed_quantity",
                )
                reservable_capacity = _decimal(
                    raw_adjustment.get("reservable_capacity"),
                    name="borrow.reservable_capacity",
                )
                required_increment = _decimal(
                    raw_adjustment.get("required_increment"),
                    name="borrow.required_increment",
                )
                provider_total = _decimal(
                    regenerated_available.get(resource),
                    name="borrow.provider_total_capacity",
                )
                required = _decimal(
                    risk_requirements.get(resource),
                    name="borrow.reservation_requirement",
                )
                if (
                    total_capacity < 0
                    or current_borrowed < 0
                    or reservable_capacity < 0
                    or required_increment < 0
                    or total_capacity != provider_total
                    or current_borrowed > total_capacity
                    or reservable_capacity
                    != exact_subtract(total_capacity, current_borrowed)
                    or required_increment != required
                    or required > reservable_capacity
                ):
                    raise AuthorityConflict(
                        "durable borrow capacity adjustment is inconsistent"
                    )
                adjusted_available[resource] = _canonical_decimal_text(reservable_capacity)
                canonical_adjustments[resource] = {
                    "total_capacity": _canonical_decimal_text(total_capacity),
                    "current_borrowed_quantity": _canonical_decimal_text(current_borrowed),
                    "reservable_capacity": _canonical_decimal_text(reservable_capacity),
                    "required_increment": _canonical_decimal_text(required_increment),
                }
            reservation_expected_available = adjusted_available
            legacy_expected_availability_evidence = {
                **expected_availability_evidence,
                "borrow_capacity_adjustments": canonical_adjustments,
            }
            expected_availability_evidence["availability"] = adjusted_available
            expected_availability_evidence[
                "borrow_capacity_adjustments"
            ] = canonical_adjustments
        elif raw_adjustments is not None:
            raise AuthorityConflict(
                "non-borrow admission carries borrow capacity adjustments"
            )

        raw_capital_adjustment = availability_evidence.get(
            "settlement_capital_adjustment"
        )
        settlement_book, _economic_book = _authority_service_capital_binding(self)
        if (
            require_transaction_cut
            and settlement_book is not None
            and raw_capital_adjustment is None
            and any(resource.startswith("CASH:") for resource in risk_requirements)
        ):
            raise AuthorityConflict(
                "durable admission lacks required settlement capital evidence"
            )
        if raw_capital_adjustment is not None:
            risk_journal_sequence = risk_event.get("journal_sequence")
            if type(risk_journal_sequence) is not int:
                raise AuthorityConflict(
                    "durable risk decision journal sequence is invalid"
                )
            regenerated_provider_available = regenerated_availability.get(
                "availability"
            )
            if not isinstance(regenerated_provider_available, Mapping):
                raise AuthorityConflict(
                    "regenerated provider availability is malformed"
                )
            canonical_capital_adjustment, effective_cash = (
                _canonical_settlement_capital_adjustment(
                    raw_capital_adjustment,
                    provider_available=regenerated_provider_available,
                    required_resources=tuple(sorted(risk_requirements)),
                    provider_id=_text(
                        availability_evidence.get("provider_id"),
                        name="provider_id",
                    ),
                    account_id=record.account_id,
                    environment=record.environment,
                    provider_environment=_text(
                        regenerated_availability.get(
                            "provider_environment",
                            regenerated_availability.get("environment"),
                        ),
                        name="provider_environment",
                    ),
                    risk_journal_sequence=risk_journal_sequence,
                    expected_journal_sequence=journal_sequence_cut,
                )
            )
            reservation_expected_available = dict(reservation_expected_available)
            reservation_expected_available.update(
                {resource: _canonical_decimal_text(amount)
                 for resource, amount in effective_cash.items()}
            )
            expected_availability_evidence[
                "settlement_capital_adjustment"
            ] = canonical_capital_adjustment

        durable_availability_evidence = dict(availability_evidence)
        if durable_availability_evidence != expected_availability_evidence:
            if (
                legacy_expected_availability_evidence is None
                or durable_availability_evidence
                != legacy_expected_availability_evidence
            ):
                raise AuthorityConflict(
                    "durable reservation availability evidence is inconsistent"
                )
        if reservation_request.get("available") != reservation_expected_available:
            raise AuthorityConflict(
                "durable reservation exceeds authoritative account availability"
            )
        reservation_version = risk_payload.get("reservation_version")
        if (
            not isinstance(reservation_version, int)
            or isinstance(reservation_version, bool)
            or reservation_version < 0
            or int(reservation_event["aggregate_version"]) != reservation_version + 1
        ):
            raise AuthorityConflict(
                "risk decision was not bound to the reservation journal cut"
            )

        request = {
            "command_id": record.financial_command_id,
            "admission_id": record.admission_id,
            "policy_id": record.policy_id,
            "policy_version": record.policy_version,
            "intent_id": record.intent_id,
            "intent_hash": record.intent_hash,
            "account_id": record.account_id,
            "environment": record.environment,
            "instrument_id": record.instrument_version.instrument_id,
            "instrument_version": record.instrument_version.version,
            "action": record.action,
            "notional": _canonical_decimal_text(record.notional),
            "current_state_version": record.state_version,
            "capability_snapshot_id": record.capability_snapshot_id,
            "risk_decision_id": record.risk_decision_id,
            "risk_decision_fingerprint": risk_payload.get("fingerprint"),
            "risk_arithmetic_policy_id": RISK_ARITHMETIC_POLICY_ID,
            "reservation_id": record.reservation_id,
            "reservation": reservation_event["payload"].get("request"),
            "reservation_availability_evidence": availability_evidence,
            "confirmation_id": record.confirmation_id,
            "risk_reducing": record.risk_reducing,
        }
        if durable_risk_intent is not None:
            request["risk_intent"] = durable_risk_intent
            request["financial_idempotency_key"] = durable_idempotency_key
        if authoritative_snapshot is not None:
            request["authoritative_risk_snapshot"] = dict(authoritative_snapshot)
        if journal_sequence_cut is not None:
            request["journal_sequence_cut"] = journal_sequence_cut
        if "allocation_evidence" in risk_payload:
            allocation_evidence = risk_payload.get("allocation_evidence")
            if not isinstance(allocation_evidence, Mapping):
                raise AuthorityConflict(
                    "durable allocation evidence binding is malformed"
                )
            request["allocation_evidence"] = dict(allocation_evidence)
        expected_request_fingerprint = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if expected_request_fingerprint != record.request_fingerprint:
            raise AuthorityConflict(
                "durable financial admission request fingerprint is inconsistent"
            )

    @property
    def epoch(self) -> int:
        return self._epoch

    def register_policy(self, policy: AuthorityPolicy, *, simulation_time: str | None = None) -> bool:
        if not isinstance(policy, AuthorityPolicy):
            raise TypeError("policy must be AuthorityPolicy")
        if simulation_time is not None:
            if policy.environments != frozenset({"SIMULATION"}):
                raise ValueError("simulation_time requires a SIMULATION-only policy")
            if type(simulation_time) is not str:
                raise TypeError("simulation_time must be an exact timestamp string")
            committed_at = _instant(simulation_time, name="simulation_time").isoformat().replace("+00:00", "Z")
        else:
            committed_at = datetime.now(timezone.utc).isoformat()
        existing = self._policies.get(policy.policy_id)
        if existing is not None:
            if existing != policy:
                raise AuthorityConflict("policy_id already has different content")
            return False
        self._persist(
            "AuthorityPolicyRegistered",
            policy.policy_id,
            self._policy_payload(policy),
            committed_at=committed_at,
        )
        self._policies[policy.policy_id] = policy
        self._epoch += 1
        return True

    def is_new_exposure_blocked(
        self,
        account_id: str,
        environment: str,
    ) -> bool:
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        if env not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment is unsupported for financial authority")
        return (account, env) in self._new_exposure_blocks

    def block_new_exposure(
        self,
        *,
        account_id: str,
        environment: str,
        reason: str,
        blocked_at: str,
        command_id: str,
    ) -> bool:
        """Durably block new risk for one financial account/environment.

        Existing risk-reducing and protective actions remain eligible for
        their normal checks. Ordinary policy registration deliberately does
        not clear this safety fact; re-authorization requires a separately
        specified and qualified durable transition.
        """

        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "durable new-exposure block requires a JournalStore"
            )
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        if env not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment is unsupported for financial authority")
        normalized_reason = _text(reason, name="reason")
        normalized_at = _text(blocked_at, name="blocked_at")
        _instant(normalized_at, name="blocked_at")
        cid = _text(command_id, name="command_id")
        payload = {
            "command_id": cid,
            "account_id": account,
            "environment": env,
            "reason": normalized_reason,
            "blocked_at": normalized_at,
        }
        scope = (account, env)
        existing_scope = self._new_exposure_blocks.get(scope)
        if existing_scope is not None:
            if existing_scope == {
                "command_id": cid,
                "reason": normalized_reason,
                "blocked_at": normalized_at,
            }:
                return False
            raise AuthorityConflict(
                "new-exposure scope is already durably blocked"
            )
        event_key = payload_digest(
            {
                "command_id": cid,
                "account_id": account,
                "environment": env,
            }
        )
        event_id = _authority_event_id(
            "AuthorityNewExposureBlocked",
            event_key,
        )
        existing = _authority_store_call(self, "get_event", event_id)
        if existing is not None:
            if (
                existing["event_type"] != "AuthorityNewExposureBlocked"
                or existing["payload"] != payload
            ):
                raise AuthorityConflict(
                    "new-exposure block command conflicts with durable content"
                )
            return False
        self._persist(
            "AuthorityNewExposureBlocked",
            event_key,
            payload,
            committed_at=normalized_at,
        )
        self._new_exposure_blocks[(account, env)] = {
            "command_id": cid,
            "reason": normalized_reason,
            "blocked_at": normalized_at,
        }
        return True

    def restore_new_exposure(
        self,
        *,
        account_id: str,
        environment: str,
        reason: str,
        restored_at: str,
        command_id: str,
        expected_block_command_id: str,
        expected_block_reason: str,
        expected_blocked_at: str,
    ) -> bool:
        """Durably restore new-exposure eligibility for one exact active block.

        This transition never grants a policy. It only clears the emergency
        scope block captured by the Owner-authorized restore command; a stale
        command cannot clear a later block because the full block identity must
        still match.
        """

        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "durable new-exposure restore requires a JournalStore"
            )
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        if env not in {"SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("environment is unsupported for financial authority")
        normalized_reason = _text(reason, name="reason")
        normalized_at = _text(restored_at, name="restored_at")
        _instant(normalized_at, name="restored_at")
        cid = _text(command_id, name="command_id")
        blocked_cid = _text(
            expected_block_command_id,
            name="expected_block_command_id",
        )
        blocked_reason = _text(
            expected_block_reason,
            name="expected_block_reason",
        )
        blocked_at = _text(
            expected_blocked_at,
            name="expected_blocked_at",
        )
        _instant(blocked_at, name="expected_blocked_at")
        payload = {
            "command_id": cid,
            "account_id": account,
            "environment": env,
            "reason": normalized_reason,
            "restored_at": normalized_at,
            "blocked_command_id": blocked_cid,
            "blocked_reason": blocked_reason,
            "blocked_at": blocked_at,
        }
        event_key = payload_digest(
            {
                "command_id": cid,
                "account_id": account,
                "environment": env,
                "blocked_command_id": blocked_cid,
                "blocked_at": blocked_at,
            }
        )
        event_id = _authority_event_id(
            "AuthorityNewExposureRestored",
            event_key,
        )
        existing_event = _authority_store_call(self, "get_event", event_id)
        if existing_event is not None:
            if (
                existing_event["event_type"] != "AuthorityNewExposureRestored"
                or existing_event["payload"] != payload
            ):
                raise AuthorityConflict(
                    "new-exposure restore command conflicts with durable content"
                )
            return False

        scope = (account, env)
        expected_block = {
            "command_id": blocked_cid,
            "reason": blocked_reason,
            "blocked_at": blocked_at,
        }
        if self._new_exposure_blocks.get(scope) != expected_block:
            raise AuthorityConflict(
                "new-exposure restore does not match active block"
            )

        self._persist(
            "AuthorityNewExposureRestored",
            event_key,
            payload,
            committed_at=normalized_at,
        )
        del self._new_exposure_blocks[scope]
        return True

    def revoke_policy(self, policy_id: str, *, reason: str, revoked_at: str) -> bool:
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        normalized = (_text(reason, name="reason"), revoked_at)
        _instant(revoked_at, name="revoked_at")
        existing = self._revocations.get(pid)
        if existing is not None:
            if existing != normalized:
                raise AuthorityConflict("policy revocation already recorded differently")
            return False
        self._persist(
            "AuthorityPolicyRevoked",
            pid,
            {"policy_id": pid, "reason": normalized[0], "revoked_at": normalized[1]},
            committed_at=normalized[1],
        )
        self._revocations[pid] = normalized
        self._epoch += 1
        return True

    def add_confirmation(
        self,
        *,
        confirmation_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        expires_at: str,
    ) -> bool:
        cid = _text(confirmation_id, name="confirmation_id")
        pid = _text(policy_id, name="policy_id")
        if pid not in self._policies:
            raise KeyError(pid)
        confirmation_notional = _decimal(notional, name="notional")
        if confirmation_notional < 0:
            raise ValueError("notional must be non-negative")
        confirmation = Confirmation(
            confirmation_id=cid,
            policy_id=pid,
            intent_hash=_text(intent_hash, name="intent_hash"),
            account_id=_text(account_id, name="account_id"),
            environment=_text(environment, name="environment").upper(),
            instrument_version=InstrumentVersionIdentity(
                instrument_id, instrument_version
            ),
            action=_text(action, name="action").upper(),
            notional=confirmation_notional,
            expires_at=expires_at,
        )
        _instant(expires_at, name="expires_at")
        existing = self._confirmations.get(cid)
        if existing is not None:
            if existing != confirmation:
                raise AuthorityConflict("confirmation_id already has different content")
            return False
        self._persist(
            "AuthorityConfirmationAdded",
            cid,
            self._confirmation_payload(confirmation),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )
        self._confirmations[cid] = confirmation
        return True

    def _policy_active(self, policy: AuthorityPolicy, now: str) -> tuple[bool, str]:
        current = _instant(now, name="now")
        if current < _instant(policy.valid_from, name="policy.valid_from"):
            return False, "policy_not_yet_active"
        revocation = self._revocations.get(policy.policy_id)
        if revocation is not None:
            _, revoked_at = revocation
            if current >= _instant(revoked_at, name="revoked_at"):
                return False, "policy_revoked"
        if current >= _instant(policy.expires_at, name="policy.expires_at"):
            return False, "policy_expired"
        return True, "active"

    def _admit_unverified(
        self,
        *,
        admission_id: str,
        policy_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        state_version: int,
        risk_admitted: bool,
        now: str,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
    ) -> AdmissionRecord:
        aid = _text(admission_id, name="admission_id")
        pid = _text(policy_id, name="policy_id")
        ihash = _text(intent_hash, name="intent_hash")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        if not isinstance(risk_admitted, bool) or not isinstance(risk_reducing, bool):
            raise TypeError("risk_admitted and risk_reducing must be booleans")
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        amount = _decimal(notional, name="notional")
        if amount < 0:
            raise ValueError("notional must be non-negative")
        request_payload = {
            "policy_id": pid,
            "intent_hash": ihash,
            "account_id": account,
            "environment": env,
            "instrument_id": identity.instrument_id,
            "instrument_version": identity.version,
            "action": normalized_action,
            "notional": _canonical_decimal_text(amount),
            "state_version": state_version,
            "risk_admitted": risk_admitted,
            "confirmation_id": confirmation_id,
            "risk_reducing": risk_reducing,
        }
        request_fingerprint = sha256(
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        existing_admission = self._admissions.get(aid)
        if existing_admission is not None:
            if existing_admission.request_fingerprint != request_fingerprint:
                raise AuthorityConflict("admission_id already has different request content")
            return existing_admission

        active, reason = self._policy_active(policy, now)
        checks = [
            (active, reason),
            (risk_admitted, "risk_rejected"),
            (account == policy.account_id, "account_out_of_scope"),
            (env in policy.environments, "environment_out_of_scope"),
            (identity in policy.instruments, "instrument_version_out_of_scope"),
            (normalized_action in policy.actions, "action_out_of_scope"),
            (amount <= policy.max_notional, "notional_out_of_scope"),
            (
                not self.is_new_exposure_blocked(account, env) or risk_reducing,
                "new_exposure_blocked",
            ),
            (not policy.protection_only or risk_reducing, "protection_policy_requires_risk_reduction"),
        ]
        outcome = "ADMITTED"
        failure_reason = "admitted"
        for passed, failed_reason in checks:
            if not passed:
                outcome = "REJECTED"
                failure_reason = failed_reason
                break

        used_confirmation: str | None = None
        if outcome == "ADMITTED" and not policy.autonomous:
            if confirmation_id is None:
                outcome, failure_reason = "REJECTED", "confirmation_required"
            else:
                confirmation = self._confirmations.get(confirmation_id)
                if confirmation is None:
                    outcome, failure_reason = "REJECTED", "confirmation_missing"
                elif confirmation.confirmation_id in self._used_confirmations:
                    outcome, failure_reason = "REJECTED", "confirmation_already_used"
                elif confirmation.policy_id != pid:
                    outcome, failure_reason = "REJECTED", "confirmation_policy_mismatch"
                elif confirmation.intent_hash != ihash:
                    outcome, failure_reason = "REJECTED", "confirmation_intent_mismatch"
                elif (
                    confirmation.account_id != account
                    or confirmation.environment != env
                    or confirmation.instrument_version != identity
                    or confirmation.action != normalized_action
                    or confirmation.notional != amount
                ):
                    outcome, failure_reason = "REJECTED", "confirmation_scope_mismatch"
                elif _instant(now, name="now") >= _instant(confirmation.expires_at, name="confirmation.expires_at"):
                    outcome, failure_reason = "REJECTED", "confirmation_expired"
                else:
                    used_confirmation = confirmation.confirmation_id

        if (
            outcome == "ADMITTED"
            and _authority_service_store(self) is not None
            and env in {"PAPER", "LIVE"}
        ):
            raise AuthorityConflict(
                "unverified PAPER/LIVE admission cannot create durable dispatch authority"
            )

        record = AdmissionRecord(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            account_id=account,
            environment=env,
            instrument_version=identity,
            action=normalized_action,
            notional=amount,
            risk_reducing=risk_reducing,
            state_version=state_version,
            authority_epoch=self._epoch,
            outcome=outcome,
            admitted_at=now,
            confirmation_id=used_confirmation,
            reason=failure_reason,
            request_fingerprint=request_fingerprint,
        )
        self._persist(
            "AuthorityAdmissionRecorded",
            aid,
            self._admission_payload(record),
            committed_at=now,
        )
        self._admissions[aid] = record
        if outcome == "ADMITTED" and used_confirmation is not None:
            self._used_confirmations.add(used_confirmation)
        return record

    def _allocation_binding_for_admission(
        self,
        *,
        allocation_result: EvidenceBoundObjectiveAllocationResult,
        existing: AdmissionRecord | None,
        risk_intent: RiskIntent,
        risk_context: RiskContext,
        reservation_book: DurableReservationBook,
        reservation_provider_id: str,
        account_id: str,
        environment: str,
        capability_snapshot_id: str,
        instrument_id: str,
        instrument_version: int,
        now: str,
    ) -> dict[str, Any]:
        """Re-resolve and bind one allocation immediately before Transaction A."""

        if type(allocation_result) is not EvidenceBoundObjectiveAllocationResult:
            raise TypeError(
                "allocation_result must be exact EvidenceBoundObjectiveAllocationResult"
            )

        if existing is not None:
            if existing.risk_decision_id is None or _authority_service_store(self) is None:
                raise AuthorityConflict(
                    "existing allocation admission lacks durable risk evidence"
                )
            events = _authority_store_call(self, "load_events",
                "risk_decision",
                existing.risk_decision_id,
            )
            if (
                len(events) != 1
                or not isinstance(events[0].get("payload"), Mapping)
            ):
                raise AuthorityConflict(
                    "existing allocation admission evidence is missing"
                )
            persisted = events[0]["payload"].get("allocation_evidence")
            if not isinstance(persisted, Mapping):
                raise AuthorityConflict(
                    "existing admission was not allocation-bound"
                )
            expected_refs = [
                {"evidence_id": evidence_id, "digest": digest}
                for evidence_id, digest in allocation_result.evidence_refs
            ]
            if (
                persisted.get("decision_digest")
                != allocation_result.decision_digest
                or persisted.get("policy_config_digest")
                != allocation_result.policy_config_digest
                or persisted.get("objective_search_config_digest")
                != allocation_result.objective_search_config_digest
                or persisted.get("evidence_refs") != expected_refs
            ):
                raise AuthorityConflict(
                    "allocation result changed for an existing financial command"
                )
            return dict(persisted)

        env = _text(environment, name="environment").upper()
        if env != "SIMULATION":
            raise AuthorityConflict(
                "PAPER/LIVE allocation admission requires a product-owned "
                "allocation authority resolver; injected callable resolvers are "
                "non-production"
            )
        resolver = self.allocation_authority_resolver
        if resolver is None:
            raise AuthorityConflict(
                "allocation admission requires an AuthorityService-owned resolver"
            )
        try:
            current = resolver(allocation_result)
        except Exception as error:
            raise AuthorityConflict(
                "allocation authority resolver could not resolve canonical state"
            ) from error
        if type(current) is not AllocationAuthoritySnapshot:
            raise AuthorityConflict(
                "allocation authority resolver returned a non-canonical snapshot type"
            )

        expected_ids = {
            evidence_id
            for evidence_id, _digest in allocation_result.evidence_refs
        }
        if set(current.resolved_evidence) != expected_ids:
            raise AuthorityConflict(
                "allocation authority evidence set is incomplete or ambiguous"
            )

        provider = _text(
            reservation_provider_id,
            name="reservation_provider_id",
        ).upper()
        account = _text(account_id, name="account_id")
        if current.provider_id.upper() != provider:
            raise AuthorityConflict(
                "allocation provider does not match admission provider"
            )
        if current.account_id != account:
            raise AuthorityConflict(
                "allocation account does not match admission account"
            )

        try:
            revalidate_evidence_bound_allocation(
                allocation_result,
                resolved_evidence=current.resolved_evidence,
                environment=env,
                as_of=now,
                current_policy_version=current.policy_version,
                current_policy=current.allocation_policy,
                current_max_candidate_sets=current.max_candidate_sets,
                current_provider_id=current.provider_id,
                current_instrument_versions=current.instrument_versions,
                current_capability_snapshot_ids=current.capability_snapshot_ids,
                current_account_id=current.account_id,
                current_account_snapshot_id=current.account_snapshot_id,
                current_reconciliation_run_id=current.reconciliation_run_id,
                current_account_state_version=current.account_state_version,
                current_reservation_state_version=reservation_book.version,
                current_reservation_state_digest=reservation_book.state_digest,
            )
        except (TypeError, ValueError) as error:
            raise AuthorityConflict(
                "allocation evidence is stale, mismatched, or non-authoritative"
            ) from error

        admission_instrument = InstrumentVersionIdentity(
            instrument_id,
            instrument_version,
        )
        bound_financial_instrument = current.financial_instruments.get(
            risk_intent.symbol
        )
        if bound_financial_instrument != admission_instrument:
            raise AuthorityConflict(
                "allocation instrument does not match admitted instrument version"
            )

        capability = dict(
            allocation_result.capability_snapshot_ids
        ).get(risk_intent.symbol)
        if (
            capability is None
            or capability
            != _text(
                capability_snapshot_id,
                name="capability_snapshot_id",
            )
        ):
            raise AuthorityConflict(
                "allocation capability does not match admitted instrument"
            )

        targets = [
            target
            for target in allocation_result.objective.allocation.targets
            if target.symbol == risk_intent.symbol
        ]
        if (
            allocation_result.objective.allocation.status != "ALLOCATED"
            or risk_intent.symbol
            not in allocation_result.objective.selected_symbols
            or len(targets) != 1
        ):
            raise AuthorityConflict(
                "allocation does not authorize this trading symbol"
            )
        target = targets[0]
        current_position = risk_context.positions.get(
            risk_intent.symbol,
            Decimal("0"),
        )
        reserved_delta = risk_context.reserved_position_delta.get(
            risk_intent.symbol,
            Decimal("0"),
        )
        effective_position = exact_add(current_position, reserved_delta)
        remaining = exact_subtract(target.quantity, effective_position)
        if remaining == 0:
            raise AuthorityConflict(
                "allocation target already has no remaining quantity"
            )
        expected_side = "BUY" if remaining > 0 else "SELL"
        if risk_intent.side != expected_side:
            raise AuthorityConflict(
                "risk intent side moves away from allocation target"
            )
        if risk_intent.quantity > exact_abs(remaining):
            raise AuthorityConflict(
                "risk intent quantity exceeds remaining allocation target"
            )

        allocation_valid_until = min(
            (item.valid_until for item in current.resolved_evidence.values()),
            key=lambda value: _instant(
                value,
                name="allocation evidence valid_until",
            ),
        )

        return {
            "decision_digest": allocation_result.decision_digest,
            "evidence_refs": [
                {"evidence_id": evidence_id, "digest": digest}
                for evidence_id, digest in allocation_result.evidence_refs
            ],
            "environment": allocation_result.environment,
            "provider_id": allocation_result.provider_id,
            "account_id": allocation_result.account_id,
            "policy_version": allocation_result.policy_version,
            "policy_config_digest": allocation_result.policy_config_digest,
            "objective_search_config_digest": (
                allocation_result.objective_search_config_digest
            ),
            "valid_until": allocation_valid_until,
            "account_snapshot_id": allocation_result.account_snapshot_id,
            "reconciliation_run_id": allocation_result.reconciliation_run_id,
            "account_state_version": current.account_state_version,
            "financial_instrument": {
                "instrument_id": admission_instrument.instrument_id,
                "version": admission_instrument.version,
            },
            "reservation_state_version": reservation_book.version,
            "reservation_state_digest": reservation_book.state_digest,
            "target": {
                "symbol": target.symbol,
                "target_quantity": _canonical_decimal_text(target.quantity),
                "effective_position_before": _canonical_decimal_text(
                    effective_position
                ),
                "remaining_quantity_before": _canonical_decimal_text(remaining),
                "admitted_side": risk_intent.side,
                "admitted_quantity": _canonical_decimal_text(
                    risk_intent.quantity
                ),
            },
        }

    def admit(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        admission_id: str,
        policy_id: str,
        intent_id: str,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        capability_snapshot_id: str,
        risk_intent: RiskIntent,
        risk_context: RiskContext,
        risk_policy: RiskPolicy,
        risk_valid_until: str,
        reservation_book: DurableReservationBook,
        reservation_id: str,
        reservation_requirements,
        reservation_available,
        reservation_checkpoint_event_id: str,
        reservation_provider_id: str,
        reservation_max_age_seconds,
        now: str,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
        allocation_result: EvidenceBoundObjectiveAllocationResult | None = None,
    ) -> AdmissionRecord:
        """Evaluate final risk inside the financial-writer boundary, then commit.

        Callers provide typed intent/context/policy inputs, never an ALLOW boolean
        or a pre-approved RiskDecision. A new command resolves and evaluates one
        immutable authority cut. Exact retries validate the original durable
        request/evidence identity and return its historical result without a
        second risk verdict, provider-state read, reservation, or publication.
        """

        risk_intent = _canonical_risk_intent(risk_intent)
        canonical_caller_risk_context = _canonical_risk_context(risk_context)
        canonical_caller_risk_policy = canonical_risk_policy(risk_policy)
        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "durable financial admission requires a JournalStore"
            )
        if type(reservation_book) is not DurableReservationBook:
            raise TypeError("reservation_book must be exact DurableReservationBook")
        if reservation_book.store is not _authority_service_store(self, required=True):
            raise AuthorityConflict(
                "authority and reservation book must share one JournalStore"
            )

        cid = _authority_text(command_id, name="command_id")
        idem = _authority_text(idempotency_key, name="idempotency_key")
        aid = _authority_text(admission_id, name="admission_id")
        pid = _authority_text(policy_id, name="policy_id")
        iid = _authority_text(intent_id, name="intent_id")
        ihash = _authority_text(intent_hash, name="intent_hash")
        account = _authority_text(account_id, name="account_id")
        env = _authority_text(environment, name="environment").upper()
        normalized_action = _authority_text(action, name="action").upper()
        normalized_notional = _authority_decimal(notional, name="notional")
        rid = _authority_text(reservation_id, name="reservation_id")
        normalized_risk_valid_until = _authority_text(
            risk_valid_until, name="risk_valid_until"
        )
        evaluated_at = _authority_text(now, name="now")
        current_confirmation_id = (
            None
            if confirmation_id is None
            else _authority_text(confirmation_id, name="confirmation_id")
        )
        if type(risk_reducing) is not bool:
            raise TypeError("risk_reducing must be a boolean")
        capability = _authority_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        scoped_idempotency_key = _authority_event_id(
            "FinancialAdmissionIdempotency",
            f"{env}:{account}:{idem}",
        )
        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)

        snapshot_provider_id = _authority_text(
            reservation_provider_id,
            name="reservation_provider_id",
        ).upper()
        snapshot_checkpoint_event_id = _authority_text(
            reservation_checkpoint_event_id,
            name="reservation_checkpoint_event_id",
        )
        canonical_instrument_id = _authority_text(
            instrument_id, name="instrument_id"
        )
        if type(instrument_version) is not int:
            raise TypeError("instrument_version must be an exact integer")
        if instrument_version < 1:
            raise ValueError("instrument_version must be positive")
        snapshot_instrument = InstrumentVersionIdentity(
            canonical_instrument_id,
            instrument_version,
        )

        existing = self._admissions.get(aid)
        if existing is not None:
            if existing.risk_decision_id is None:
                raise AuthorityConflict(
                    "existing admission was not created by financial risk admission"
                )
            if (
                existing.financial_command_id != cid
                or existing.policy_id != pid
                or existing.intent_id != iid
                or existing.intent_hash != ihash
                or existing.account_id != account
                or existing.environment != env
                or existing.instrument_version != snapshot_instrument
                or existing.action != normalized_action
                or existing.notional != normalized_notional
                or existing.state_version != canonical_caller_risk_context.state_version
                or existing.reservation_id
                != (rid if existing.outcome == "ADMITTED" else None)
                or existing.capability_snapshot_id != capability
                or existing.policy_version != policy.version
                or existing.confirmation_id != current_confirmation_id
                or existing.risk_reducing != risk_reducing
            ):
                raise AuthorityConflict(
                    "admission_id already belongs to another financial command"
                )
            if existing.risk_valid_until is None or _instant(
                normalized_risk_valid_until, name="risk_valid_until"
            ) != _instant(
                existing.risk_valid_until,
                name="existing risk_valid_until",
            ):
                raise AuthorityConflict(
                    "risk_valid_until changed for an existing financial command"
                )

            payload = self._validate_historical_financial_retry_evidence(
                existing,
                policy,
            )
            durable_risk_intent = payload.get("risk_intent")
            durable_idempotency_key = payload.get(
                "financial_idempotency_key"
            )
            durable_reservation_id = payload.get(
                "financial_reservation_id"
            )
            if (
                durable_risk_intent is None
                or durable_idempotency_key is None
                or durable_reservation_id is None
            ):
                raise AuthorityConflict(
                    "existing admission lacks exact durable retry identity"
                )
            if (
                _durable_risk_intent_payload(durable_risk_intent)
                != _risk_intent_payload(risk_intent)
            ):
                raise AuthorityConflict(
                    "risk_intent changed for an existing financial command"
                )
            if _text(
                durable_idempotency_key,
                name="durable financial idempotency key",
            ) != scoped_idempotency_key:
                raise AuthorityConflict(
                    "idempotency_key changed for an existing financial command"
                )
            if _text(
                durable_reservation_id,
                name="durable financial reservation_id",
            ) != rid:
                raise AuthorityConflict(
                    "reservation_id changed for an existing financial command"
                )

            durable_snapshot = payload.get("authoritative_risk_snapshot")
            if not isinstance(durable_snapshot, Mapping):
                raise AuthorityConflict(
                    "existing admission lacks authoritative risk snapshot binding"
                )
            durable_instrument = durable_snapshot.get("instrument")
            if (
                not isinstance(durable_instrument, Mapping)
                or durable_snapshot.get("provider_id") != snapshot_provider_id
                or durable_snapshot.get("account_id") != account
                or durable_snapshot.get("environment") != env
                or durable_snapshot.get("capability_snapshot_id") != capability
                or durable_snapshot.get("reconciliation_checkpoint_event_id")
                != snapshot_checkpoint_event_id
                or durable_snapshot.get("authority_policy_id") != policy.policy_id
                or durable_snapshot.get("authority_policy_version") != policy.version
                or durable_instrument.get("instrument_id")
                != snapshot_instrument.instrument_id
                or durable_instrument.get("version")
                != snapshot_instrument.version
            ):
                raise AuthorityConflict(
                    "authoritative risk snapshot scope changed for an existing financial command"
                )
            if durable_snapshot.get(
                "context_fingerprint"
            ) != _risk_context_fingerprint(canonical_caller_risk_context):
                raise AuthorityConflict(
                    "caller risk_context changed for an existing financial command"
                )
            if durable_snapshot.get(
                "risk_policy_fingerprint"
            ) != _risk_policy_fingerprint(canonical_caller_risk_policy):
                raise AuthorityConflict(
                    "caller risk_policy changed for an existing financial command"
                )

            durable_requirements = payload.get("reservation_requirements")
            current_requirements = reservation_requirements_payload(
                normalize_reservation_requirements(reservation_requirements)
            )
            if durable_requirements != current_requirements:
                raise AuthorityConflict(
                    "reservation requirements changed for an existing financial command"
                )
            durable_availability = payload.get(
                "reservation_availability_evidence"
            )
            if existing.outcome == "ADMITTED":
                if not isinstance(durable_availability, Mapping):
                    raise AuthorityConflict(
                        "existing admission availability evidence is missing"
                    )
                if (
                    durable_availability.get("checkpoint_event_id")
                    != snapshot_checkpoint_event_id
                    or durable_availability.get("provider_id")
                    != snapshot_provider_id
                    or durable_availability.get("max_age_seconds")
                    != _canonical_decimal_text(
                        _decimal(
                            reservation_max_age_seconds,
                            name="reservation_max_age_seconds",
                        )
                    )
                ):
                    raise AuthorityConflict(
                        "reservation availability identity changed for an existing financial command"
                    )
            elif durable_availability is not None:
                raise AuthorityConflict(
                    "rejected admission unexpectedly has availability evidence"
                )

            durable_allocation = payload.get("allocation_evidence")
            if allocation_result is None:
                if durable_allocation is not None:
                    raise AuthorityConflict(
                        "allocation evidence changed for an existing financial command"
                    )
            else:
                allocation_binding = self._allocation_binding_for_admission(
                    allocation_result=allocation_result,
                    existing=existing,
                    risk_intent=risk_intent,
                    risk_context=canonical_caller_risk_context,
                    reservation_book=reservation_book,
                    reservation_provider_id=snapshot_provider_id,
                    account_id=account,
                    environment=env,
                    capability_snapshot_id=capability,
                    instrument_id=snapshot_instrument.instrument_id,
                    instrument_version=snapshot_instrument.version,
                    now=evaluated_at,
                )
                if (
                    not isinstance(durable_allocation, Mapping)
                    or allocation_binding != dict(durable_allocation)
                ):
                    raise AuthorityConflict(
                        "allocation evidence changed for an existing financial command"
                    )

            return existing

        if existing is None:
            journal_sequence_cut = _authority_store_call(self, "current_journal_sequence")
            reservation_version = reservation_book.version
            caller_valid_until = normalized_risk_valid_until
            risk_authority_request = RiskAuthorityRequest(
                risk_intent=risk_intent,
                account_id=account,
                environment=env,
                provider_id=snapshot_provider_id,
                instrument_version=snapshot_instrument,
                capability_snapshot_id=capability,
                reconciliation_checkpoint_event_id=(
                    snapshot_checkpoint_event_id
                ),
                journal_sequence_cut=journal_sequence_cut,
                reservation_version=reservation_version,
                reservation_state_digest=reservation_book.state_digest,
                authority_policy_id=policy.policy_id,
                authority_policy_version=policy.version,
                evaluated_at=evaluated_at,
                **self._risk_policy_cut(provider_id=snapshot_provider_id,
                    account_id=account, environment=env,
                    journal_sequence_cut=journal_sequence_cut),
            )
            risk_snapshot = self._resolve_authoritative_risk_snapshot(
                risk_authority_request
            )
            if (
                _risk_context_compatibility_payload(canonical_caller_risk_context)
                != _risk_context_compatibility_payload(risk_snapshot.context)
            ):
                raise AuthorityConflict(
                    "caller risk_context does not match authoritative risk snapshot"
                )
            if (
                _risk_policy_compatibility_payload(canonical_caller_risk_policy)
                != _risk_policy_compatibility_payload(risk_snapshot.risk_policy)
            ):
                raise AuthorityConflict(
                    "caller risk_policy does not match authoritative risk snapshot"
                )
            if _instant(
                caller_valid_until, name="risk_valid_until"
            ) != _instant(
                risk_snapshot.valid_until,
                name="authoritative risk valid_until",
            ):
                raise AuthorityConflict(
                    "caller risk_valid_until does not match authoritative risk snapshot"
                )
            valid_until = risk_snapshot.valid_until
            effective_risk_context = risk_snapshot.context
            effective_risk_policy = risk_snapshot.risk_policy
            risk_snapshot_payload = risk_snapshot.evidence_payload()
            risk_snapshot_id = risk_snapshot.snapshot_id
        decision = evaluate_bound_risk(
            risk_intent,
            effective_risk_context,
            effective_risk_policy,
            intent_hash=ihash,
            policy_version=policy.version,
            reservation_version=reservation_version,
            reservation_requirements=reservation_requirements,
            capability_snapshot_id=capability,
            evaluated_at=evaluated_at,
            valid_until=valid_until,
            authoritative_risk_snapshot_id=risk_snapshot_id,
        )

        normalized_requirements = normalize_reservation_requirements(
            reservation_requirements
        )
        requirement_map = dict(normalized_requirements)
        borrow_resources = tuple(
            resource
            for resource, _amount in normalized_requirements
            if resource.startswith("BORROW:")
        )
        required_borrow_resource: str | None = None
        required_borrow_quantity = Decimal("0")
        current_borrowed_quantity = Decimal("0")
        if decision.admitted:
            if risk_intent.instrument_type == "EQUITY":
                current_position = effective_risk_context.positions.get(
                    risk_intent.symbol,
                    Decimal("0"),
                )
                reserved_delta = effective_risk_context.reserved_position_delta.get(
                    risk_intent.symbol,
                    Decimal("0"),
                )
                required_borrow_quantity = incremental_short_borrow_quantity(
                    side=risk_intent.side,
                    quantity=risk_intent.quantity,
                    current_position=current_position,
                    reserved_position_delta=reserved_delta,
                )
                current_borrowed_quantity = max(
                    Decimal("0"),
                    -current_position,
                )
                if required_borrow_quantity > 0:
                    required_borrow_resource = borrow_resource_key(
                        provider_id=snapshot_provider_id,
                        account_id=account,
                        environment=env,
                        provider_environment=risk_authority_request.provider_environment,
                        instrument_id=snapshot_instrument.instrument_id,
                        instrument_version=snapshot_instrument.version,
                    )
                    if (
                        borrow_resources != (required_borrow_resource,)
                        or requirement_map.get(required_borrow_resource)
                        != required_borrow_quantity
                    ):
                        raise AuthorityConflict(
                            "increased equity short requires exact scoped borrow reservation"
                        )
                elif borrow_resources:
                    raise AuthorityConflict(
                        "non-increasing equity short must not reserve new borrow capacity"
                    )
            elif borrow_resources:
                raise AuthorityConflict(
                    "securities-borrow reservation is valid only for equity short risk"
                )

        availability_evidence: Mapping[str, Any] | None = None
        authoritative_available = reservation_available
        if decision.admitted:
            checkpoint_event_id = snapshot_checkpoint_event_id
            provider_id = snapshot_provider_id
            max_age = _authority_decimal(
                reservation_max_age_seconds,
                name="reservation_max_age_seconds",
            )
            if max_age < 0:
                raise ValueError(
                    "reservation_max_age_seconds must be non-negative"
                )
            normalized_max_age = _canonical_decimal_text(max_age)

            if existing is not None:
                durable_risk_events = _authority_store_call(self, "load_events",
                    "risk_decision", existing.risk_decision_id
                )
                if (
                    len(durable_risk_events) != 1
                    or not isinstance(
                        durable_risk_events[0].get("payload"), Mapping
                    )
                ):
                    raise AuthorityConflict(
                        "existing admission availability evidence is missing"
                    )
                durable_evidence = durable_risk_events[0]["payload"].get(
                    "reservation_availability_evidence"
                )
                if not isinstance(durable_evidence, Mapping):
                    raise AuthorityConflict(
                        "existing admission availability evidence is missing"
                    )
                if (
                    durable_evidence.get("checkpoint_event_id")
                    != checkpoint_event_id
                    or durable_evidence.get("provider_id") != provider_id
                    or durable_evidence.get("max_age_seconds")
                    != normalized_max_age
                ):
                    raise AuthorityConflict(
                        "reservation availability evidence changed for an existing financial command"
                    )
                availability_evidence = dict(durable_evidence)
            else:
                try:
                    loaded = load_account_resource_availability_evidence(
                        _authority_service_store(self, required=True),
                        checkpoint_event_id=checkpoint_event_id,
                        provider_id=provider_id,
                        account_id=account,
                        environment=env,
                        provider_environment=risk_authority_request.provider_environment,
                        resources=tuple(
                            resource
                            for resource, _amount in normalized_requirements
                        ),
                        now=evaluated_at,
                        max_age_seconds=normalized_max_age,
                        evidence_artifact_store=self.evidence_artifact_store,
                        require_latest_scope=True,
                    )
                except ValueError as error:
                    if (
                        str(error)
                        == "selected reconciliation checkpoint is superseded by newer provider truth"
                    ):
                        raise AuthorityConflict(
                            "reservation checkpoint is superseded by newer provider truth"
                        ) from error
                    raise
                availability_evidence = {
                    **loaded,
                    "max_age_seconds": normalized_max_age,
                }

            raw_authoritative = availability_evidence.get("availability")
            if (
                type(raw_authoritative) is not dict
                or any(type(key) is not str for key in raw_authoritative)
            ):
                raise AuthorityConflict(
                    "authoritative reservation availability is malformed"
                )
            authoritative_available = dict.copy(raw_authoritative)

            if (
                type(reservation_available) is not dict
                or any(type(key) is not str for key in reservation_available)
            ):
                raise TypeError(
                    "reservation_available must be a mapping backed by an exact dict"
                )
            caller_available: dict[str, Decimal] = {}
            for raw_resource, raw_amount in dict.copy(reservation_available).items():
                resource = _authority_text(
                    raw_resource,
                    name="reservation_available resource",
                )
                if resource in caller_available:
                    raise ValueError(
                        "reservation_available resources must be unique after normalization"
                    )
                amount = _authority_decimal(
                    raw_amount,
                    name=f"reservation_available[{resource}]",
                )
                if amount < 0:
                    raise ValueError(
                        "reservation_available amounts must be non-negative"
                    )
                caller_available[resource] = amount
            canonical_available = {
                _authority_text(
                    resource,
                    name="authoritative availability resource",
                ): _authority_decimal(
                    amount,
                    name=f"authoritative availability[{resource}]",
                )
                for resource, amount in authoritative_available.items()
            }
            caller_expected_available = dict(canonical_available)
            durable_borrow_adjustment: tuple[Decimal, Decimal] | None = None
            if existing is not None and required_borrow_resource is not None:
                raw_adjustments = availability_evidence.get(
                    "borrow_capacity_adjustments"
                )
                if (
                    not isinstance(raw_adjustments, Mapping)
                    or set(raw_adjustments) != {required_borrow_resource}
                ):
                    raise AuthorityConflict(
                        "existing borrow admission lacks exact capacity adjustment"
                    )
                raw_adjustment = raw_adjustments.get(required_borrow_resource)
                required_adjustment_fields = {
                    "total_capacity",
                    "current_borrowed_quantity",
                    "reservable_capacity",
                    "required_increment",
                }
                if (
                    not isinstance(raw_adjustment, Mapping)
                    or set(raw_adjustment) != required_adjustment_fields
                ):
                    raise AuthorityConflict(
                        "existing borrow capacity adjustment is malformed"
                    )
                total_capacity = _decimal(
                    raw_adjustment.get("total_capacity"),
                    name="borrow.total_capacity",
                )
                durable_current_borrowed = _decimal(
                    raw_adjustment.get("current_borrowed_quantity"),
                    name="borrow.current_borrowed_quantity",
                )
                reservable_capacity = _decimal(
                    raw_adjustment.get("reservable_capacity"),
                    name="borrow.reservable_capacity",
                )
                durable_required_increment = _decimal(
                    raw_adjustment.get("required_increment"),
                    name="borrow.required_increment",
                )
                durable_available = canonical_available.get(
                    required_borrow_resource
                )
                if (
                    durable_available is None
                    or total_capacity < 0
                    or durable_current_borrowed < 0
                    or reservable_capacity < 0
                    or durable_required_increment < 0
                    or durable_current_borrowed != current_borrowed_quantity
                    or durable_required_increment != required_borrow_quantity
                    or durable_current_borrowed > total_capacity
                    or reservable_capacity
                    != exact_subtract(total_capacity, durable_current_borrowed)
                    or durable_available
                    not in {total_capacity, reservable_capacity}
                ):
                    raise AuthorityConflict(
                        "existing borrow capacity adjustment is inconsistent"
                    )
                caller_expected_available[required_borrow_resource] = (
                    total_capacity
                )
                durable_borrow_adjustment = (
                    total_capacity,
                    reservable_capacity,
                )

            if caller_available != caller_expected_available:
                raise AuthorityConflict(
                    "reservation_available does not match authoritative reconciliation checkpoint"
                )

            authoritative_available = dict(canonical_available)
            if required_borrow_resource is not None:
                if durable_borrow_adjustment is not None:
                    total_capacity, reservable_capacity = (
                        durable_borrow_adjustment
                    )
                    authoritative_available[required_borrow_resource] = (
                        reservable_capacity
                    )
                else:
                    total_capacity = canonical_available.get(
                        required_borrow_resource
                    )
                    if total_capacity is None:
                        raise AuthorityConflict(
                            "authoritative checkpoint lacks required borrow capacity"
                        )
                    if total_capacity < current_borrowed_quantity:
                        raise AuthorityConflict(
                            "provider borrow capacity is below current local borrow"
                        )
                    reservable_capacity = (
                        exact_subtract(total_capacity, current_borrowed_quantity)
                    )
                    authoritative_available[required_borrow_resource] = (
                        reservable_capacity
                    )
                    availability_evidence = {
                        **availability_evidence,
                        "availability": {
                            resource: _canonical_decimal_text(amount)
                            for resource, amount in authoritative_available.items()
                        },
                        "borrow_capacity_adjustments": {
                            required_borrow_resource: {
                                "total_capacity": _canonical_decimal_text(
                                    total_capacity
                                ),
                                "current_borrowed_quantity": _canonical_decimal_text(
                                    current_borrowed_quantity
                                ),
                                "reservable_capacity": _canonical_decimal_text(
                                    reservable_capacity
                                ),
                                "required_increment": _canonical_decimal_text(
                                    required_borrow_quantity
                                ),
                            }
                        },
                    }

        capital_journal_sequence_cut: int | None = None
        if decision.admitted and availability_evidence is not None:
            required_resource_names = tuple(
                resource for resource, _amount in normalized_requirements
            )
            raw_capital_adjustment = availability_evidence.get(
                "settlement_capital_adjustment"
            )
            if existing is not None:
                if raw_capital_adjustment is not None:
                    canonical_capital_adjustment, effective_cash = (
                        _canonical_settlement_capital_adjustment(
                            raw_capital_adjustment,
                            provider_available=canonical_available,
                            required_resources=required_resource_names,
                            provider_id=provider_id,
                            account_id=account,
                            environment=env,
                            provider_environment=_authority_text(
                                availability_evidence.get(
                                    "provider_environment",
                                    availability_evidence.get("environment"),
                                ),
                                name="provider_environment",
                            ),
                        )
                    )
                    authoritative_available.update(effective_cash)
                    availability_evidence = {
                        **availability_evidence,
                        "settlement_capital_adjustment": canonical_capital_adjustment,
                    }
            else:
                capital_cut = _resolve_authority_service_capital(
                    self,
                    canonical_available,
                    required_resource_names,
                    provider_evidence=availability_evidence,
                    as_of=_instant(evaluated_at, name="evaluated_at"),
                    required=False,
                )
                if capital_cut is not None:
                    canonical_capital_adjustment, effective_cash = (
                        _canonical_settlement_capital_adjustment(
                            capital_cut,
                            provider_available=canonical_available,
                            required_resources=required_resource_names,
                            provider_id=provider_id,
                            account_id=account,
                            environment=env,
                            provider_environment=_authority_text(
                                availability_evidence.get(
                                    "provider_environment",
                                    availability_evidence.get("environment"),
                                ),
                                name="provider_environment",
                            ),
                        )
                    )
                    authoritative_available.update(effective_cash)
                    availability_evidence = {
                        **availability_evidence,
                        "settlement_capital_adjustment": canonical_capital_adjustment,
                    }
                    capital_journal_sequence_cut = canonical_capital_adjustment[
                        "journal_sequence"
                    ]

        allocation_binding = None
        if allocation_result is not None:
            allocation_binding = self._allocation_binding_for_admission(
                allocation_result=allocation_result,
                existing=existing,
                risk_intent=risk_intent,
                risk_context=effective_risk_context,
                reservation_book=reservation_book,
                reservation_provider_id=snapshot_provider_id,
                account_id=account,
                environment=env,
                capability_snapshot_id=capability,
                instrument_id=snapshot_instrument.instrument_id,
                instrument_version=snapshot_instrument.version,
                now=evaluated_at,
            )

        if existing is None:
            reservation_book.refresh()
            current_cut = _authority_store_call(self, "current_journal_sequence")
            if (
                capital_journal_sequence_cut is not None
                and capital_journal_sequence_cut != current_cut
            ):
                raise AuthorityConflict(
                    "settlement capital cut changed before financial commit"
                )
            current_risk_authority_request = RiskAuthorityRequest(
                risk_intent=risk_intent,
                account_id=account,
                environment=env,
                provider_id=snapshot_provider_id,
                instrument_version=snapshot_instrument,
                capability_snapshot_id=capability,
                reconciliation_checkpoint_event_id=(
                    snapshot_checkpoint_event_id
                ),
                journal_sequence_cut=current_cut,
                reservation_version=reservation_book.version,
                reservation_state_digest=reservation_book.state_digest,
                authority_policy_id=policy.policy_id,
                authority_policy_version=policy.version,
                evaluated_at=evaluated_at,
                **self._risk_policy_cut(provider_id=snapshot_provider_id,
                    account_id=account, environment=env,
                    journal_sequence_cut=current_cut),
            )
            current_risk_snapshot = self._resolve_authoritative_risk_snapshot(
                current_risk_authority_request
            )
            if (
                current_risk_snapshot.snapshot_id != risk_snapshot_id
                or current_risk_snapshot.evidence_payload()
                != risk_snapshot_payload
            ):
                raise AuthorityConflict(
                    "authoritative risk snapshot changed before financial commit"
                )

        return self._admit_bound_risk(
            command_id=cid,
            idempotency_key=idem,
            admission_id=aid,
            policy_id=pid,
            intent_id=iid,
            intent_hash=ihash,
            risk_intent=risk_intent,
            account_id=account,
            environment=env,
            instrument_id=snapshot_instrument.instrument_id,
            instrument_version=snapshot_instrument.version,
            action=normalized_action,
            notional=normalized_notional,
            current_state_version=effective_risk_context.state_version,
            capability_snapshot_id=capability,
            risk_decision=decision,
            reservation_book=reservation_book,
            reservation_id=rid,
            reservation_requirements=reservation_requirements,
            reservation_available=authoritative_available,
            now=evaluated_at,
            reservation_availability_evidence=availability_evidence,
            allocation_binding=allocation_binding,
            authoritative_risk_snapshot=risk_snapshot_payload,
            journal_sequence_cut=journal_sequence_cut,
            confirmation_id=current_confirmation_id,
            risk_reducing=risk_reducing,
        )

    def _admit_bound_risk(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        admission_id: str,
        policy_id: str,
        intent_id: str,
        intent_hash: str,
        risk_intent: RiskIntent,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        notional,
        current_state_version: int,
        capability_snapshot_id: str,
        risk_decision: RiskDecision,
        reservation_book: DurableReservationBook,
        reservation_id: str,
        reservation_requirements,
        reservation_available,
        now: str,
        reservation_availability_evidence: Mapping[str, Any] | None = None,
        allocation_binding: Mapping[str, Any] | None = None,
        authoritative_risk_snapshot: Mapping[str, Any] | None = None,
        journal_sequence_cut: int | None = None,
        confirmation_id: str | None = None,
        risk_reducing: bool = False,
    ) -> AdmissionRecord:
        """Atomically admit verified risk evidence and its worst-case reservation.

        This is the public financial admission boundary. A boolean risk assertion
        is intentionally not accepted. The risk decision, reservation mutation,
        confirmation consumption encoded by the admission record, admission
        event, and execution outbox intent share one JournalStore transaction.
        """

        if _authority_service_store(self) is None:
            raise AuthorityConflict(
                "durable financial admission requires a JournalStore"
            )
        if type(reservation_book) is not DurableReservationBook:
            raise TypeError("reservation_book must be exact DurableReservationBook")
        if reservation_book.store is not _authority_service_store(self, required=True):
            raise AuthorityConflict(
                "authority and reservation book must share one JournalStore"
            )

        cid = _authority_text(command_id, name="command_id")
        idem = _authority_text(idempotency_key, name="idempotency_key")
        aid = _authority_text(admission_id, name="admission_id")
        pid = _authority_text(policy_id, name="policy_id")
        iid = _authority_text(intent_id, name="intent_id")
        ihash = _authority_text(intent_hash, name="intent_hash")
        canonical_risk_intent = _canonical_risk_intent(risk_intent)
        canonical_risk_intent_payload = _risk_intent_payload(
            canonical_risk_intent
        )
        account = _authority_text(account_id, name="account_id")
        env = _authority_text(environment, name="environment").upper()
        capability = _authority_text(
            capability_snapshot_id, name="capability_snapshot_id"
        )
        rid = _authority_text(reservation_id, name="reservation_id")
        canonical_instrument_id = _authority_text(
            instrument_id, name="instrument_id"
        )
        if type(instrument_version) is not int:
            raise TypeError("instrument_version must be an exact integer")
        if instrument_version < 1:
            raise ValueError("instrument_version must be positive")
        normalized_action = _authority_text(action, name="action").upper()
        normalized_notional = _authority_decimal(notional, name="notional")
        normalized_now = _authority_text(now, name="now")
        normalized_confirmation_id = (
            None
            if confirmation_id is None
            else _authority_text(confirmation_id, name="confirmation_id")
        )
        scoped_command_id = _authority_event_id(
            "FinancialAdmissionCommand", f"{env}:{account}:{cid}"
        )
        scoped_idempotency_key = _authority_event_id(
            "FinancialAdmissionIdempotency", f"{env}:{account}:{idem}"
        )
        risk_snapshot_binding: dict[str, Any] | None = None
        if authoritative_risk_snapshot is not None:
            if not isinstance(authoritative_risk_snapshot, Mapping):
                raise TypeError(
                    "authoritative_risk_snapshot must be a mapping or None"
                )
            risk_snapshot_binding = dict(authoritative_risk_snapshot)
            bound_snapshot_id = _authority_text(
                risk_snapshot_binding.get("snapshot_id"),
                name="authoritative risk snapshot_id",
            )
            if (
                risk_decision.authoritative_risk_snapshot_id
                != bound_snapshot_id
            ):
                raise AuthorityConflict(
                    "risk decision does not match authoritative risk snapshot"
                )
        elif risk_decision.authoritative_risk_snapshot_id is not None:
            raise AuthorityConflict(
                "risk decision authoritative snapshot evidence is missing"
            )
        try:
            expected_risk_id = (
                "risk:sha256:" + risk_decision_fingerprint(risk_decision)
            )
        except (TypeError, ValueError) as error:
            raise AuthorityConflict(
                "risk decision binding is invalid"
            ) from error
        if risk_decision.decision_id != expected_risk_id:
            raise AuthorityConflict(
                "risk decision id does not match bound evidence"
            )
        normalized_requirements = normalize_reservation_requirements(
            reservation_requirements
        )
        if risk_decision.reservation_requirements != normalized_requirements:
            raise AuthorityConflict(
                "reservation requirements do not match risk decision"
            )
        if (
            not isinstance(current_state_version, int)
            or isinstance(current_state_version, bool)
            or current_state_version < 0
        ):
            raise ValueError(
                "current_state_version must be a non-negative integer"
            )
        if reservation_book.environment != env or reservation_book.account_id != account:
            raise AuthorityConflict(
                "reservation book scope does not match admission account/environment"
            )
        if not self._durable_authority_state_current():
            raise AuthorityConflict(
                "durable authority journal advanced; reload required"
            )

        policy = self._policies.get(pid)
        if policy is None:
            raise KeyError(pid)
        if (
            journal_sequence_cut is not None
            and (
                type(journal_sequence_cut) is not int
                or journal_sequence_cut < 0
            )
        ):
            raise ValueError(
                "journal_sequence_cut must be a non-negative integer"
            )

        # Exact retries must be replayable after the first transaction advances
        # the reservation aggregate.  Otherwise the risk decision that was
        # correctly bound to reservation version N would look stale at N+1 and
        # a lost response could not be recovered idempotently.  Only an exact
        # immutable admission identity is replayed; any changed scope fails
        # closed before a new reservation or send can occur.
        existing = self._admissions.get(aid)
        if existing is None and journal_sequence_cut is None:
            raise AuthorityConflict(
                "new financial admission requires a transaction journal cut"
            )
        if existing is not None:
            expected_instrument = InstrumentVersionIdentity(
                canonical_instrument_id, instrument_version
            )
            same_command = (
                existing.financial_command_id == cid
                and existing.policy_id == pid
                and existing.intent_id == iid
                and existing.intent_hash == ihash
                and existing.account_id == account
                and existing.environment == env
                and existing.instrument_version == expected_instrument
                and existing.action == normalized_action
                and existing.notional == normalized_notional
                and existing.state_version == current_state_version
                and existing.risk_decision_id == risk_decision.decision_id
                and existing.reservation_id == (
                    rid if existing.outcome == "ADMITTED" else None
                )
                and existing.capability_snapshot_id == capability
                and existing.risk_valid_until == risk_decision.valid_until
                and existing.policy_version == policy.version
                and existing.confirmation_id == normalized_confirmation_id
                and existing.risk_reducing == risk_reducing
            )
            if not same_command:
                raise AuthorityConflict(
                    "admission_id already belongs to another financial command"
                )
            durable_risk_events = _authority_store_call(self, "load_events",
                "risk_decision", existing.risk_decision_id
            )
            if (
                len(durable_risk_events) != 1
                or not isinstance(
                    durable_risk_events[0].get("payload"), Mapping
                )
            ):
                raise AuthorityConflict(
                    "existing admission risk evidence is missing"
                )
            durable_risk_payload = durable_risk_events[0]["payload"]
            if (
                reservation_availability_evidence is not None
                and durable_risk_payload.get(
                    "reservation_availability_evidence"
                )
                != reservation_availability_evidence
            ):
                raise AuthorityConflict(
                    "reservation availability evidence changed for an existing financial command"
                )
            if durable_risk_payload.get("allocation_evidence") != (
                None if allocation_binding is None else dict(allocation_binding)
            ):
                raise AuthorityConflict(
                    "allocation evidence changed for an existing financial command"
                )
            if durable_risk_payload.get("authoritative_risk_snapshot") != (
                risk_snapshot_binding
            ):
                raise AuthorityConflict(
                    "authoritative risk snapshot changed for an existing financial command"
                )
            if (
                durable_risk_payload.get("arithmetic_policy_id")
                != RISK_ARITHMETIC_POLICY_ID
                or risk_decision.arithmetic_policy_id
                != RISK_ARITHMETIC_POLICY_ID
            ):
                raise AuthorityConflict(
                    "risk arithmetic policy changed for an existing financial command"
                )
            return existing

        validate_bound_risk_decision(risk_decision, now=normalized_now)
        if risk_decision.intent_hash != ihash:
            raise AuthorityConflict("risk decision intent_hash mismatch")
        if risk_decision.state_version != current_state_version:
            raise AuthorityConflict("risk decision state_version is stale")
        if risk_decision.policy_version != policy.version:
            raise AuthorityConflict("risk decision policy_version is stale")
        current_reservation_version = reservation_book.version
        if risk_decision.reservation_version != current_reservation_version:
            raise AuthorityConflict("risk decision reservation_version is stale")
        if risk_decision.capability_snapshot_id != capability:
            raise AuthorityConflict("risk decision capability snapshot is stale")

        reservation_plan = None
        if risk_decision.admitted:
            reservation_plan = reservation_book.prepare_reserve_mutation(
                event_key=cid,
                idempotency_key=idem,
                reservation_id=rid,
                intent_id=iid,
                requirements=reservation_requirements,
                available=reservation_available,
                committed_at=normalized_now,
            )

        # Evaluate authority/confirmation semantics against an isolated copy.
        # The copy has no JournalStore, so it cannot persist or consume durable
        # confirmation state before the multi-aggregate transaction commits.
        probe = AuthorityService.restore(self.export_state())
        candidate = probe._admit_unverified(
            admission_id=aid,
            policy_id=pid,
            intent_hash=ihash,
            account_id=account,
            environment=env,
            instrument_id=canonical_instrument_id,
            instrument_version=instrument_version,
            action=normalized_action,
            notional=normalized_notional,
            state_version=current_state_version,
            risk_admitted=risk_decision.admitted,
            now=normalized_now,
            confirmation_id=normalized_confirmation_id,
            risk_reducing=risk_reducing,
        )

        if candidate.outcome == "ADMITTED" and reservation_plan is None:
            raise AuthorityConflict(
                "admitted command is missing an atomic reservation plan"
            )

        request = {
            "command_id": cid,
            "admission_id": aid,
            "policy_id": pid,
            "policy_version": policy.version,
            "intent_id": iid,
            "intent_hash": ihash,
            "risk_intent": canonical_risk_intent_payload,
            "financial_idempotency_key": scoped_idempotency_key,
            "account_id": account,
            "environment": env,
            "instrument_id": candidate.instrument_version.instrument_id,
            "instrument_version": candidate.instrument_version.version,
            "action": candidate.action,
            "notional": _canonical_decimal_text(candidate.notional),
            "current_state_version": current_state_version,
            "capability_snapshot_id": capability,
            "risk_decision_id": risk_decision.decision_id,
            "risk_decision_fingerprint": risk_decision_fingerprint(risk_decision),
            "risk_arithmetic_policy_id": risk_decision.arithmetic_policy_id,
            "reservation_id": rid,
            "reservation": (
                reservation_plan.request if reservation_plan is not None else None
            ),
            "reservation_availability_evidence": reservation_availability_evidence,
            "authoritative_risk_snapshot": risk_snapshot_binding,
            "confirmation_id": candidate.confirmation_id,
            "risk_reducing": risk_reducing,
        }
        if journal_sequence_cut is not None:
            request["journal_sequence_cut"] = journal_sequence_cut
        if allocation_binding is not None:
            request["allocation_evidence"] = dict(allocation_binding)
        request_fingerprint = sha256(
            json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        record = replace(
            candidate,
            request_fingerprint=request_fingerprint,
            intent_id=iid,
            risk_decision_id=risk_decision.decision_id,
            reservation_id=(rid if candidate.outcome == "ADMITTED" else None),
            capability_snapshot_id=capability,
            risk_valid_until=risk_decision.valid_until,
            policy_version=policy.version,
            financial_command_id=cid,
        )

        risk_payload = {
            "decision_id": risk_decision.decision_id,
            "fingerprint": risk_decision_fingerprint(risk_decision),
            "arithmetic_policy_id": risk_decision.arithmetic_policy_id,
            "intent_hash": risk_decision.intent_hash,
            "risk_intent": canonical_risk_intent_payload,
            "financial_idempotency_key": scoped_idempotency_key,
            "financial_reservation_id": rid,
            "state_version": risk_decision.state_version,
            "policy_version": risk_decision.policy_version,
            "reservation_version": risk_decision.reservation_version,
            "reservation_requirements": reservation_requirements_payload(
                risk_decision.reservation_requirements
            ),
            "reservation_availability_evidence": reservation_availability_evidence,
            "journal_sequence_cut": journal_sequence_cut,
            "capability_snapshot_id": risk_decision.capability_snapshot_id,
            "authoritative_risk_snapshot": risk_snapshot_binding,
            "evaluated_at": risk_decision.evaluated_at,
            "valid_until": risk_decision.valid_until,
            "verdict": "ALLOW" if risk_decision.admitted else "REJECT",
            "resulting_position": _canonical_decimal_text(risk_decision.resulting_position),
            "gross_leverage": _canonical_decimal_text(risk_decision.gross_leverage),
            "net_leverage": _canonical_decimal_text(risk_decision.net_leverage),
            "worst_stress_loss": _canonical_decimal_text(risk_decision.worst_stress_loss),
            "checks": [
                {
                    "rule_id": item.rule,
                    "passed": item.passed,
                    "measured": item.observed,
                    "limit": item.limit,
                    "evidence": item.reason,
                }
                for item in risk_decision.rules
            ],
        }
        if allocation_binding is not None:
            risk_payload["allocation_evidence"] = dict(allocation_binding)
        risk_event = {
            "event_id": _authority_event_id(
                "RiskDecisionRecorded", risk_decision.decision_id
            ),
            "event_type": "RiskDecisionRecorded",
            "aggregate_type": "risk_decision",
            "aggregate_id": risk_decision.decision_id,
            "aggregate_version": "1",
            "payload": risk_payload,
            "payload_hash": payload_digest(risk_payload),
            "committed_at": normalized_now,
        }
        admission_payload = self._admission_payload(record)
        admission_event = {
            "event_id": _authority_event_id(
                "AuthorityAdmissionRecorded", aid
            ),
            "event_type": "AuthorityAdmissionRecorded",
            "aggregate_type": "authority_state",
            "aggregate_id": "canonical",
            "aggregate_version": str(self._journal_version + 1),
            "payload": admission_payload,
            "payload_hash": payload_digest(admission_payload),
            "committed_at": normalized_now,
        }

        events = [(risk_event, None)]
        if reservation_plan is not None and candidate.outcome == "ADMITTED":
            events.append((reservation_plan.envelope, None))
        events.append(
            (
                admission_event,
                "financial.admission.ready"
                if candidate.outcome == "ADMITTED"
                else None,
            )
        )
        result_payload = {
            "admission": admission_payload,
            "reservation": (
                reservation_plan.snapshot_payload
                if reservation_plan is not None
                and candidate.outcome == "ADMITTED"
                else None
            ),
        }
        try:
            _authority_store_call(self, "commit_command",
                command_id=scoped_command_id,
                actor="autotrade-financial-writer",
                environment=env,
                idempotency_key=scoped_idempotency_key,
                request=request,
                result=result_payload,
                state_version=current_state_version,
                events=events,
                expected_journal_sequence=journal_sequence_cut,
            )
        except Exception:
            reservation_book.refresh()
            raise

        self._journal_version += 1
        self._admissions[aid] = record
        if record.outcome == "ADMITTED" and record.confirmation_id is not None:
            self._used_confirmations.add(record.confirmation_id)
        reservation_book.refresh()
        return record

    def _durable_authority_state_current(self) -> bool:
        if _authority_service_store(self) is None:
            return True
        durable_version = (
            _authority_store_call(self, "next_aggregate_version", "authority_state", "canonical") - 1
        )
        return durable_version == self._journal_version

    def dispatch_allowed(
        self,
        admission_id: str,
        *,
        intent_hash: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        now: str,
        capability_snapshot_id: str | None = None,
    ) -> tuple[bool, str]:
        record = self._admissions.get(_text(admission_id, name="admission_id"))
        if record is None:
            return False, "admission_missing"
        if record.outcome != "ADMITTED":
            return False, "admission_not_admitted"
        if not self._durable_authority_state_current():
            return False, "authority_state_stale"
        if record.intent_hash != _text(intent_hash, name="intent_hash"):
            return False, "intent_hash_changed"
        scope = (
            _text(account_id, name="account_id"),
            _text(environment, name="environment").upper(),
            InstrumentVersionIdentity(instrument_id, instrument_version),
            _text(action, name="action").upper(),
        )
        recorded_scope = (
            record.account_id,
            record.environment,
            record.instrument_version,
            record.action,
        )
        if scope != recorded_scope:
            return False, "admission_scope_changed"
        if (
            record.risk_decision_id is not None
            and record.environment in {"PAPER", "LIVE"}
        ):
            return False, "production_risk_authority_unavailable"
        if (
            self.is_new_exposure_blocked(record.account_id, record.environment)
            and not record.risk_reducing
        ):
            return False, "new_exposure_blocked"
        policy = self._policies[record.policy_id]
        active, reason = self._policy_active(policy, now)
        if not active:
            return False, reason
        if record.confirmation_id is not None:
            confirmation = self._confirmations[record.confirmation_id]
            if _instant(now, name="now") >= _instant(
                confirmation.expires_at, name="confirmation.expires_at"
            ):
                return False, "confirmation_expired"

        if (
            record.environment in {"PAPER", "LIVE"}
            and record.risk_decision_id is None
        ):
            return False, "financial_evidence_missing"

        if record.risk_decision_id is not None:
            if record.policy_version != policy.version:
                return False, "policy_version_changed"
            if capability_snapshot_id is None:
                return False, "capability_snapshot_required"
            try:
                current_capability = _text(
                    capability_snapshot_id, name="capability_snapshot_id"
                )
            except ValueError:
                return False, "capability_snapshot_required"
            if current_capability != record.capability_snapshot_id:
                return False, "capability_snapshot_changed"
            if _instant(now, name="now") >= _instant(
                record.risk_valid_until, name="risk_valid_until"
            ):
                return False, "risk_decision_expired"
            try:
                self._validate_durable_financial_evidence(
                    record,
                    policy,
                    require_transaction_cut=True,
                )
                reservation_book = DurableReservationBook(
                    _authority_service_store(self, required=True),
                    environment=record.environment,
                    account_id=record.account_id,
                )
                reservation = reservation_book.get(record.reservation_id)
                risk_event = _authority_store_call(self, "load_events",
                    "risk_decision", record.risk_decision_id
                )[0]
                risk_payload = risk_event["payload"]
                allocation_evidence = risk_payload.get("allocation_evidence")
                if allocation_evidence is not None:
                    if not isinstance(allocation_evidence, Mapping):
                        raise AuthorityConflict(
                            "durable allocation evidence binding is malformed"
                        )
                    allocation_valid_until = _text(
                        allocation_evidence.get("valid_until"),
                        name="allocation evidence valid_until",
                    )
                    if _instant(now, name="now") > _instant(
                        allocation_valid_until,
                        name="allocation evidence valid_until",
                    ):
                        return False, "allocation_evidence_expired"
                admission_reservation_version = (
                    int(risk_payload["reservation_version"]) + 1
                )
                risk_requirements = risk_payload.get("reservation_requirements")
                if not isinstance(risk_requirements, Mapping):
                    raise AuthorityConflict(
                        "durable risk reservation requirements are malformed"
                    )
                availability_evidence = risk_payload.get(
                    "reservation_availability_evidence"
                )
                if not isinstance(availability_evidence, Mapping):
                    raise AuthorityConflict(
                        "durable financial admission lacks availability evidence"
                    )
                authoritative_snapshot = risk_payload.get(
                    "authoritative_risk_snapshot"
                )
                if authoritative_snapshot is not None:
                    (
                        current_provider_id,
                        current_provider_environment,
                    ) = _require_provider_scope_matches_authoritative_risk_snapshot(
                        authoritative_snapshot,
                        availability_evidence,
                        evidence_name="dispatch availability evidence",
                    )
                else:
                    current_provider_id = _text(
                        availability_evidence.get("provider_id"),
                        name="provider_id",
                    ).upper()
                    current_provider_environment = _text(
                        availability_evidence.get(
                            "provider_environment",
                            availability_evidence.get("environment"),
                        ),
                        name="provider_environment",
                    ).upper()
                    if current_provider_id == "BYBIT":
                        raise AuthorityConflict(
                            "BYBIT dispatch lacks authoritative risk provider domain"
                        )
                # Historical evidence above is intentionally regenerated at the
                # admission instant so restart/replay remains deterministic.
                # Sending is a distinct authority boundary: freeze one global
                # journal cut across every final provider/local financial read.
                # A newer reconciliation, settlement or borrow fact appearing
                # between component reads must invalidate the whole decision
                # rather than letting mixed-generation authority reach send.
                financial_validation_sequence = _authority_store_call(
                    self, "current_journal_sequence"
                )
                raw_capital_adjustment = availability_evidence.get(
                    "settlement_capital_adjustment"
                )
                current_provider_evidence = (
                    load_account_resource_availability_evidence(
                        _authority_service_store(self, required=True),
                        checkpoint_event_id=_text(
                            availability_evidence.get("checkpoint_event_id"),
                            name="checkpoint_event_id",
                        ),
                        provider_id=current_provider_id,
                        account_id=record.account_id,
                        environment=record.environment,
                        provider_environment=current_provider_environment,
                        resources=tuple(sorted(risk_requirements)),
                        now=now,
                        max_age_seconds=availability_evidence.get(
                            "max_age_seconds"
                        ),
                        evidence_artifact_store=self.evidence_artifact_store,
                        require_latest_scope=True,
                    )
                )
                if raw_capital_adjustment is not None:
                    current_provider_available = current_provider_evidence.get(
                        "availability"
                    )
                    if not isinstance(current_provider_available, Mapping):
                        raise AuthorityConflict(
                            "current provider availability is malformed"
                        )
                    persisted_capital, _historical_effective = (
                        _canonical_settlement_capital_adjustment(
                            raw_capital_adjustment,
                            provider_available=current_provider_available,
                            required_resources=tuple(sorted(risk_requirements)),
                            provider_id=current_provider_id,
                            account_id=record.account_id,
                            environment=record.environment,
                            provider_environment=current_provider_environment,
                        )
                    )
                    current_capital = _resolve_authority_service_capital(
                        self,
                        current_provider_available,
                        tuple(sorted(risk_requirements)),
                        provider_evidence=current_provider_evidence,
                        as_of=_instant(now, name="now"),
                        required=True,
                    )
                    assert current_capital is not None
                    canonical_current_capital, current_effective = (
                        _canonical_settlement_capital_adjustment(
                            current_capital,
                            provider_available=current_provider_available,
                            required_resources=tuple(sorted(risk_requirements)),
                            provider_id=current_provider_id,
                            account_id=record.account_id,
                            environment=record.environment,
                            provider_environment=current_provider_environment,
                        )
                    )
                    for identity_field in (
                        "provider_id",
                        "account_id",
                        "environment",
                        "provider_environment",
                        "settlement_scope_id",
                        "economic_book_id",
                    ):
                        if (
                            canonical_current_capital.get(identity_field)
                            != persisted_capital.get(identity_field)
                        ):
                            raise AuthorityConflict(
                                "current settlement capital authority changed"
                            )
                    for resource, raw_requirement in risk_requirements.items():
                        if not resource.startswith(("CASH:", "MARGIN_CREDIT:")):
                            continue
                        requirement = _decimal(
                            raw_requirement,
                            name=f"reservation requirement[{resource}]",
                        )
                        if current_effective.get(resource, Decimal("0")) < requirement:
                            raise AuthorityConflict(
                                "current settlement capital no longer covers reservation"
                            )
                borrow_resources = tuple(
                    resource
                    for resource in risk_requirements
                    if resource.startswith("BORROW:")
                )
                if borrow_resources:
                    availability_evidence = risk_payload.get(
                        "reservation_availability_evidence"
                    )
                    if not isinstance(availability_evidence, Mapping):
                        raise AuthorityConflict(
                            "durable short admission lacks availability evidence"
                        )
                    resource_details = availability_evidence.get(
                        "resource_details"
                    )
                    if not isinstance(resource_details, Mapping):
                        raise AuthorityConflict(
                            "durable short admission lacks borrow resource details"
                        )
                    for borrow_resource in borrow_resources:
                        detail = resource_details.get(borrow_resource)
                        if not isinstance(detail, Mapping):
                            raise AuthorityConflict(
                                "durable short admission lacks typed borrow detail"
                            )
                        borrow = BorrowAvailabilityEvidence.from_resource_detail(
                            detail
                        )
                        if (
                            borrow.resource_key != borrow_resource
                            or borrow.account_id != record.account_id
                            or borrow.environment != record.environment
                            or borrow.instrument_id
                            != record.instrument_version.instrument_id
                            or borrow.instrument_version
                            != record.instrument_version.version
                        ):
                            raise AuthorityConflict(
                                "durable borrow dispatch scope mismatch"
                            )
                        if self.evidence_artifact_store is None:
                            raise AuthorityConflict(
                                "borrow dispatch requires trusted ArtifactStore"
                            )
                        projection = DurableBorrowRecallProjection(
                            _authority_service_store(self, required=True),
                            provider_id=borrow.provider_id,
                            account_id=borrow.account_id,
                            environment=borrow.environment,
                            provider_environment=borrow.provider_environment,
                            instrument_id=borrow.instrument_id,
                            instrument_version=borrow.instrument_version,
                            evidence_artifact_store=self.evidence_artifact_store,
                        )
                        if projection.active_quantity_at(now) > 0:
                            return False, "borrow_recall_active"
                if (
                    _authority_store_call(self, "current_journal_sequence")
                    != financial_validation_sequence
                ):
                    raise AuthorityConflict(
                        "financial authority changed during dispatch validation"
                    )
            except Exception:
                return False, "financial_evidence_invalid"
            if reservation.intent_id != record.intent_id:
                return False, "reservation_intent_changed"
            if reservation.state != "WORKING":
                return False, "reservation_not_dispatchable"
            if reservation_book.version != admission_reservation_version:
                return False, "reservation_state_changed"

        # Final linearization barrier for durable authority. A revoke committed
        # by another process after any of the reads above must fail the send.
        if not self._durable_authority_state_current():
            return False, "authority_state_stale"
        return True, "allowed"

    def dispatch_guard(
        self,
        admission_id: str,
        *,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        action: str,
        capability_snapshot_id: str | None = None,
    ) -> Callable[[str, str], tuple[bool, str]]:
        """Bind one admitted versioned scope to the dispatcher's final barrier."""
        aid = _text(admission_id, name="admission_id")
        account = _text(account_id, name="account_id")
        env = _text(environment, name="environment").upper()
        identity = InstrumentVersionIdentity(instrument_id, instrument_version)
        normalized_action = _text(action, name="action").upper()
        capability = (
            None
            if capability_snapshot_id is None
            else _text(capability_snapshot_id, name="capability_snapshot_id")
        )

        def check(intent_hash: str, now: str) -> tuple[bool, str]:
            return self.dispatch_allowed(
                aid,
                intent_hash=intent_hash,
                account_id=account,
                environment=env,
                instrument_id=identity.instrument_id,
                instrument_version=identity.version,
                action=normalized_action,
                now=now,
                capability_snapshot_id=capability,
            )

        return check


    def historical_admission(self, admission_id: str) -> dict[str, Any]:
        """Read a durably validated outcome without granting current authority.

        This narrow replay adapter uses the same original-command validation as
        financial idempotency. It cannot admit, dispatch, refresh risk or resend.
        """
        aid = _text(admission_id, name="admission_id")
        record = self._admissions.get(aid)
        if record is None:
            raise AuthorityConflict("historical admission is not recorded")
        policy = self._policies[record.policy_id]
        self._validate_historical_financial_retry_evidence(record, policy)
        return self._admission_payload(record)

    def export_state(self) -> dict:
        """Return a canonical JSON-compatible snapshot of authority state.

        This is persistence data, not a cryptographic trust boundary.  The
        durable journal is responsible for integrity and ordering; restore()
        revalidates the financial authority identities and references.
        """
        def instrument(value: InstrumentVersionIdentity) -> dict:
            return {"instrument_id": value.instrument_id, "version": value.version}

        policies = []
        for policy_id in sorted(self._policies):
            policy = self._policies[policy_id]
            policies.append(
                {
                    "policy_id": policy.policy_id,
                    "account_id": policy.account_id,
                    "environments": sorted(policy.environments),
                    "instruments": [
                        instrument(item) for item in sorted(policy.instruments)
                    ],
                    "actions": sorted(policy.actions),
                    "max_notional": _canonical_decimal_text(policy.max_notional),
                    "expires_at": policy.expires_at,
                    "autonomous": policy.autonomous,
                    "valid_from": policy.valid_from,
                    "protection_only": policy.protection_only,
                    "version": policy.version,
                }
            )

        confirmations = []
        for confirmation_id in sorted(self._confirmations):
            confirmation = self._confirmations[confirmation_id]
            confirmations.append(
                {
                    "confirmation_id": confirmation.confirmation_id,
                    "policy_id": confirmation.policy_id,
                    "intent_hash": confirmation.intent_hash,
                    "account_id": confirmation.account_id,
                    "environment": confirmation.environment,
                    "instrument": instrument(confirmation.instrument_version),
                    "action": confirmation.action,
                    "notional": _canonical_decimal_text(confirmation.notional),
                    "expires_at": confirmation.expires_at,
                }
            )

        admissions = []
        for admission_id in sorted(self._admissions):
            record = self._admissions[admission_id]
            admissions.append(
                {
                    "admission_id": record.admission_id,
                    "policy_id": record.policy_id,
                    "intent_hash": record.intent_hash,
                    "account_id": record.account_id,
                    "environment": record.environment,
                    "instrument": instrument(record.instrument_version),
                    "action": record.action,
                    "notional": _canonical_decimal_text(record.notional),
                    "risk_reducing": record.risk_reducing,
                    "state_version": record.state_version,
                    "authority_epoch": record.authority_epoch,
                    "outcome": record.outcome,
                    "admitted_at": record.admitted_at,
                    "confirmation_id": record.confirmation_id,
                    "reason": record.reason,
                    "request_fingerprint": record.request_fingerprint,
                    "intent_id": record.intent_id,
                    "risk_decision_id": record.risk_decision_id,
                    "reservation_id": record.reservation_id,
                    "capability_snapshot_id": record.capability_snapshot_id,
                    "risk_valid_until": record.risk_valid_until,
                    "policy_version": record.policy_version,
                    "financial_command_id": record.financial_command_id,
                }
            )

        return {
            "schema_version": 1,
            "epoch": self._epoch,
            "policies": policies,
            "revocations": [
                {
                    "policy_id": policy_id,
                    "reason": value[0],
                    "revoked_at": value[1],
                }
                for policy_id, value in sorted(self._revocations.items())
            ],
            "confirmations": confirmations,
            "used_confirmations": sorted(self._used_confirmations),
            "admissions": admissions,
            "new_exposure_blocks": [
                {
                    "account_id": account_id,
                    "environment": environment,
                    **dict(value),
                }
                for (account_id, environment), value
                in sorted(self._new_exposure_blocks.items())
            ],
        }

    @classmethod
    def restore(cls, state: dict) -> "AuthorityService":
        """Rebuild authority state from one durable snapshot, fail closed."""
        if not isinstance(state, dict) or state.get("schema_version") != 1:
            raise ValueError("unsupported authority state schema")
        service = cls()

        policies = state.get("policies")
        revocations = state.get("revocations")
        confirmations = state.get("confirmations")
        admissions = state.get("admissions")
        used_confirmations = state.get("used_confirmations")
        new_exposure_blocks = state.get("new_exposure_blocks", [])
        if not all(
            isinstance(value, list)
            for value in (
                policies,
                revocations,
                confirmations,
                admissions,
                used_confirmations,
                new_exposure_blocks,
            )
        ):
            raise ValueError("authority state collections must be lists")

        seen_policy_ids: set[str] = set()
        for item in policies:
            if not isinstance(item, dict):
                raise ValueError("policy snapshot entry must be an object")
            instruments = item.get("instruments")
            if not isinstance(instruments, list):
                raise ValueError("policy instruments must be a list")
            policy = AuthorityPolicy.create(
                policy_id=item.get("policy_id"),
                account_id=item.get("account_id"),
                environments=item.get("environments"),
                instruments=[
                    InstrumentVersionIdentity(
                        entry.get("instrument_id"), entry.get("version")
                    )
                    for entry in instruments
                    if isinstance(entry, dict)
                ],
                actions=item.get("actions"),
                max_notional=item.get("max_notional"),
                expires_at=item.get("expires_at"),
                autonomous=item.get("autonomous"),
                valid_from=item.get("valid_from"),
                protection_only=item.get("protection_only"),
                version=item.get("version", 1),
            )
            if policy.policy_id in seen_policy_ids:
                raise AuthorityConflict("duplicate policy in authority snapshot")
            seen_policy_ids.add(policy.policy_id)
            service.register_policy(policy)

        for item in revocations:
            if not isinstance(item, dict):
                raise ValueError("revocation snapshot entry must be an object")
            service.revoke_policy(
                item.get("policy_id"),
                reason=item.get("reason"),
                revoked_at=item.get("revoked_at"),
            )

        seen_confirmation_ids: set[str] = set()
        for item in confirmations:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("confirmation snapshot entry is invalid")
            instrument = item["instrument"]
            cid = item.get("confirmation_id")
            if cid in seen_confirmation_ids:
                raise AuthorityConflict("duplicate confirmation in authority snapshot")
            seen_confirmation_ids.add(cid)
            service.add_confirmation(
                confirmation_id=cid,
                policy_id=item.get("policy_id"),
                intent_hash=item.get("intent_hash"),
                account_id=item.get("account_id"),
                environment=item.get("environment"),
                instrument_id=instrument.get("instrument_id"),
                instrument_version=instrument.get("version"),
                action=item.get("action"),
                notional=item.get("notional"),
                expires_at=item.get("expires_at"),
            )

        restored_admissions: dict[str, AdmissionRecord] = {}
        derived_used: set[str] = set()
        for item in admissions:
            if not isinstance(item, dict) or not isinstance(item.get("instrument"), dict):
                raise ValueError("admission snapshot entry is invalid")
            admission_id = _text(item.get("admission_id"), name="admission_id")
            if admission_id in restored_admissions:
                raise AuthorityConflict("duplicate admission in authority snapshot")
            policy_id = _text(item.get("policy_id"), name="policy_id")
            if policy_id not in service._policies:
                raise ValueError("admission references missing policy")
            instrument = item["instrument"]
            identity = InstrumentVersionIdentity(
                instrument.get("instrument_id"), instrument.get("version")
            )
            amount = _decimal(item.get("notional"), name="notional")
            if amount < 0:
                raise ValueError("admission notional must be non-negative")
            state_version = item.get("state_version")
            authority_epoch = item.get("authority_epoch")
            risk_reducing = item.get("risk_reducing")
            if (
                not isinstance(state_version, int)
                or isinstance(state_version, bool)
                or state_version < 0
            ):
                raise ValueError("admission state_version is invalid")
            if (
                not isinstance(authority_epoch, int)
                or isinstance(authority_epoch, bool)
                or authority_epoch < 0
                or authority_epoch > service._epoch
            ):
                raise ValueError("admission authority_epoch is invalid")
            if not isinstance(risk_reducing, bool):
                raise TypeError("admission risk_reducing must be boolean")
            outcome = _text(item.get("outcome"), name="outcome").upper()
            if outcome not in {"ADMITTED", "REJECTED"}:
                raise ValueError("admission outcome is invalid")
            admitted_at = _text(item.get("admitted_at"), name="admitted_at")
            _instant(admitted_at, name="admitted_at")
            confirmation_id = item.get("confirmation_id")
            if confirmation_id is not None:
                confirmation_id = _text(
                    confirmation_id, name="confirmation_id"
                )
                if confirmation_id not in service._confirmations:
                    raise ValueError("admission references missing confirmation")
                if outcome != "ADMITTED":
                    raise ValueError("rejected admission cannot consume confirmation")
                derived_used.add(confirmation_id)
            record = AdmissionRecord(
                admission_id=admission_id,
                policy_id=policy_id,
                intent_hash=_text(item.get("intent_hash"), name="intent_hash"),
                account_id=_text(item.get("account_id"), name="account_id"),
                environment=_text(item.get("environment"), name="environment").upper(),
                instrument_version=identity,
                action=_text(item.get("action"), name="action").upper(),
                notional=amount,
                risk_reducing=risk_reducing,
                state_version=state_version,
                authority_epoch=authority_epoch,
                outcome=outcome,
                admitted_at=admitted_at,
                confirmation_id=confirmation_id,
                reason=_text(item.get("reason"), name="reason"),
                request_fingerprint=_text(
                    item.get("request_fingerprint"), name="request_fingerprint"
                ),
                intent_id=item.get("intent_id"),
                risk_decision_id=item.get("risk_decision_id"),
                reservation_id=item.get("reservation_id"),
                capability_snapshot_id=item.get("capability_snapshot_id"),
                risk_valid_until=item.get("risk_valid_until"),
                policy_version=item.get("policy_version"),
                financial_command_id=item.get("financial_command_id"),
            )
            restored_admissions[admission_id] = record

        normalized_used = {
            _text(value, name="used_confirmation")
            for value in used_confirmations
        }
        if normalized_used != derived_used:
            raise ValueError("used confirmation set does not match admitted records")
        if not normalized_used <= set(service._confirmations):
            raise ValueError("used confirmation references missing confirmation")

        epoch = state.get("epoch")
        if (
            not isinstance(epoch, int)
            or isinstance(epoch, bool)
            or epoch != service._epoch
        ):
            raise ValueError("authority epoch does not match durable mutations")
        restored_blocks: dict[tuple[str, str], dict[str, str]] = {}
        for item in new_exposure_blocks:
            if not isinstance(item, dict):
                raise ValueError(
                    "new-exposure block snapshot entry must be an object"
                )
            account_id = _text(item.get("account_id"), name="account_id")
            environment = _text(
                item.get("environment"), name="environment"
            ).upper()
            if environment not in {"SIMULATION", "PAPER", "LIVE"}:
                raise ValueError("new-exposure block environment is unsupported")
            command_id = _text(item.get("command_id"), name="command_id")
            reason = _text(item.get("reason"), name="reason")
            blocked_at = _text(item.get("blocked_at"), name="blocked_at")
            _instant(blocked_at, name="blocked_at")
            scope = (account_id, environment)
            if scope in restored_blocks:
                raise AuthorityConflict(
                    "duplicate new-exposure block scope in authority snapshot"
                )
            restored_blocks[scope] = {
                "command_id": command_id,
                "reason": reason,
                "blocked_at": blocked_at,
            }
        service._admissions = restored_admissions
        service._used_confirmations = normalized_used
        service._new_exposure_blocks = restored_blocks
        return service
