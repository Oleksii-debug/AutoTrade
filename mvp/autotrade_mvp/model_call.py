"""Durable production model-call lifecycle over the canonical model budget.

This module composes DurableModelBudget and JournalStore. It does not own a
second router, budget ledger, job scheduler, financial authority, credential
store, or trading sender. Its only authority is the inference-call boundary:
one deterministic semantic attempt may cross that boundary at most once.

Remote/local adapters are injected. A durable ModelCallStarted event is written
before adapter invocation. After that point every adapter exception is
conservatively UNKNOWN and the full reserved ceiling becomes estimated-unbilled
until billing evidence resolves it. In particular, an injected adapter cannot
self-author a NOT_SENT proof after ModelCallStarted merely by raising
ModelCallNotSent.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from .model_budget_journal import DurableModelBudget
from .model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteDecision,
    RouteStatus,
    RoutingPolicy,
    _route_now,
    route_model,
)
from .persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
    require_exact_journal_store_authority,
)
from .exact_decimal import exact_add, parse_bounded_exact_decimal


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_AGGREGATE_TYPE = "model_call_attempt"


class ModelCallError(RuntimeError):
    """Base error for the durable model-call boundary."""


class ModelCallNotSent(ModelCallError):
    """Legacy adapter hint; after durable STARTED it is not independent NOT_SENT proof."""


# Callback code can retain the canonical JournalStore class itself.  Freezing
# instance state and even the live class dictionary at callback entry is not
# enough: __bases__ may be rebound to a layout-compatible hostile subclass,
# changing inherited durable dispatch without changing vars(JournalStore).
# Retain the exact project-owned topology and descriptors from trusted import.
_MODEL_JOURNAL_CLASS_AUTHORITY = tuple(
    (
        cls,
        tuple(cls.__bases__),
        MappingProxyType(dict(vars(cls))),
    )
    for cls in JournalStore.__mro__
    if cls is not object
)


def _model_journal_class_authority_changes(
    *,
    restore: bool,
    authority=_MODEL_JOURNAL_CLASS_AUTHORITY,
) -> list[str]:
    """Detect and optionally restore trusted JournalStore class topology."""

    changes: list[str] = []
    for cls, expected_bases, expected_state in authority:
        current_bases = tuple(cls.__bases__)
        bases_changed = (
            len(current_bases) != len(expected_bases)
            or any(
                current is not expected
                for current, expected in zip(current_bases, expected_bases)
            )
        )
        if bases_changed:
            changes.append(f"budget.journal.class.{cls.__name__}.__bases__")
            if restore:
                try:
                    type.__setattr__(cls, "__bases__", expected_bases)
                except TypeError as error:
                    raise ModelCallError(
                        "journal class base authority could not be restored"
                    ) from error

        current_state = vars(cls)
        current_keys = tuple(current_state)
        if any(type(name) is not str for name in current_keys):
            raise ModelCallError("journal class authority state keys are invalid")
        names = set(current_keys) | set(expected_state)
        for name in sorted(names):
            label = f"budget.journal.class.{cls.__name__}.{name}"
            if name not in expected_state:
                changes.append(label)
                if restore:
                    try:
                        type.__delattr__(cls, name)
                    except (AttributeError, TypeError) as error:
                        raise ModelCallError(
                            "journal class authority could not be restored"
                        ) from error
                continue
            expected = expected_state[name]
            if name not in current_state or current_state[name] is not expected:
                changes.append(label)
                if restore:
                    try:
                        type.__setattr__(cls, name, expected)
                    except TypeError as error:
                        raise ModelCallError(
                            "journal class authority could not be restored"
                        ) from error

        if restore:
            restored_bases = tuple(cls.__bases__)
            if (
                len(restored_bases) != len(expected_bases)
                or any(
                    current is not expected
                    for current, expected in zip(restored_bases, expected_bases)
                )
            ):
                raise ModelCallError(
                    "journal class base authority restore is incomplete"
                )
            restored = vars(cls)
            if set(restored) != set(expected_state):
                raise ModelCallError("journal class authority restore is incomplete")
            if any(restored[name] is not expected_state[name] for name in expected_state):
                raise ModelCallError("journal class authority restore is incomplete")
    return changes


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{name} is required")
    if value != value.strip():
        raise ValueError(f"{name} must be canonical text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _canonical_text(value, name=name)
    if _DIGEST.fullmatch(text) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return text


def _utc_text(value: object, *, name: str) -> str:
    text = _canonical_text(value, name=name)
    try:
        point = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be canonical UTC text") from error
    if point.tzinfo is None or not text.endswith("Z"):
        raise ValueError(f"{name} must be canonical UTC text")
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise ValueError(f"{name} must be canonical UTC text")
    return text


def _exact_decimal(value: object, *, name: str) -> Decimal:
    try:
        result = parse_bounded_exact_decimal(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite exact decimal") from error
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite non-negative exact decimal")
    return result


@dataclass(frozen=True, slots=True)
class ModelCallSpec:
    """Semantic identity and evidence binding for one model-call attempt."""

    job_id: str
    input_digest: str
    policy_id: str
    pricing_evidence_id: str
    pricing_as_of: str
    result_schema_id: str
    cost_currency: str = "USD"
    fallback_parent_attempt_id: str | None = None
    fallback_index: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "job_id", _canonical_text(self.job_id, name="job_id"))
        object.__setattr__(
            self,
            "input_digest",
            _digest(self.input_digest, name="input_digest"),
        )
        object.__setattr__(
            self,
            "policy_id",
            _canonical_text(self.policy_id, name="policy_id"),
        )
        object.__setattr__(
            self,
            "pricing_evidence_id",
            _canonical_text(
                self.pricing_evidence_id,
                name="pricing_evidence_id",
            ),
        )
        object.__setattr__(
            self,
            "pricing_as_of",
            _utc_text(self.pricing_as_of, name="pricing_as_of"),
        )
        object.__setattr__(
            self,
            "result_schema_id",
            _canonical_text(self.result_schema_id, name="result_schema_id"),
        )
        currency = _canonical_text(self.cost_currency, name="cost_currency").upper()
        if currency != self.cost_currency or not currency.isalnum():
            raise ValueError("cost_currency must be canonical uppercase alphanumeric text")
        object.__setattr__(self, "cost_currency", currency)
        if self.fallback_parent_attempt_id is not None:
            object.__setattr__(
                self,
                "fallback_parent_attempt_id",
                _canonical_text(
                    self.fallback_parent_attempt_id,
                    name="fallback_parent_attempt_id",
                ),
            )
        if (
            type(self.fallback_index) is not int
            or self.fallback_index < 0
            or self.fallback_index > 32
        ):
            raise ValueError("fallback_index must be an integer from 0 through 32")
        if self.fallback_index == 0 and self.fallback_parent_attempt_id is not None:
            raise ValueError(
                "fallback_parent_attempt_id requires a positive fallback_index"
            )
        if self.fallback_index > 0 and self.fallback_parent_attempt_id is None:
            raise ValueError(
                "positive fallback_index requires fallback_parent_attempt_id"
            )


@dataclass(frozen=True, slots=True)
class ModelCallBinding:
    """Immutable authority handed to an injected inference adapter."""

    attempt_id: str
    request_id: str
    job_id: str
    input_digest: str
    provider_id: str
    model_id: str
    revision: str | None
    remote: bool
    reserved_cost: Decimal
    pricing_evidence_id: str
    pricing_evidence_digest: str
    pricing_as_of: str
    cost_currency: str
    result_schema_id: str
    fallback_parent_attempt_id: str | None
    fallback_index: int


@dataclass(frozen=True, slots=True)
class PricingQuote:
    provider_id: str
    model_id: str
    revision: str | None
    estimated_cost: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_id", _canonical_text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "model_id", _canonical_text(self.model_id, name="model_id")
        )
        if self.revision is not None:
            object.__setattr__(
                self, "revision", _canonical_text(self.revision, name="revision")
            )
        object.__setattr__(
            self,
            "estimated_cost",
            _exact_decimal(self.estimated_cost, name="estimated_cost"),
        )

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.provider_id, self.model_id, self.revision)


@dataclass(frozen=True, slots=True)
class PricingEvidenceSnapshot:
    evidence_id: str
    evidence_digest: str
    as_of: str
    valid_until: str
    cost_currency: str
    quotes: tuple[PricingQuote, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "evidence_id", _canonical_text(self.evidence_id, name="evidence_id")
        )
        object.__setattr__(
            self,
            "evidence_digest",
            _digest(self.evidence_digest, name="evidence_digest"),
        )
        object.__setattr__(self, "as_of", _utc_text(self.as_of, name="as_of"))
        object.__setattr__(
            self,
            "valid_until",
            _utc_text(self.valid_until, name="valid_until"),
        )
        if datetime.fromisoformat(self.valid_until.replace("Z", "+00:00")) <= datetime.fromisoformat(
            self.as_of.replace("Z", "+00:00")
        ):
            raise ValueError("pricing evidence valid_until must follow as_of")
        currency = _canonical_text(
            self.cost_currency, name="cost_currency"
        ).upper()
        if currency != self.cost_currency or not currency.isalnum():
            raise ValueError(
                "cost_currency must be canonical uppercase alphanumeric text"
            )
        object.__setattr__(self, "cost_currency", currency)
        if isinstance(self.quotes, (str, bytes)):
            raise TypeError("pricing quotes must be a collection")
        quotes = tuple(self.quotes)
        if not quotes or any(type(item) is not PricingQuote for item in quotes):
            raise ValueError("pricing evidence requires PricingQuote values")
        if len({item.key for item in quotes}) != len(quotes):
            raise ValueError("pricing evidence quote identities must be unique")
        object.__setattr__(
            self,
            "quotes",
            tuple(sorted(quotes, key=lambda item: (item.provider_id, item.model_id, item.revision or ""))),
        )

    def quote_for(
        self,
        provider_id: str,
        model_id: str,
        revision: str | None,
    ) -> PricingQuote:
        key = (
            _canonical_text(provider_id, name="provider_id"),
            _canonical_text(model_id, name="model_id"),
            _canonical_text(revision, name="revision") if revision is not None else None,
        )
        matches = [item for item in self.quotes if item.key == key]
        if len(matches) != 1:
            raise ModelCallError(
                "pricing evidence does not uniquely cover provider/model/revision"
            )
        return matches[0]


@dataclass(frozen=True, slots=True)
class ModelObservationEvidence:
    attempt_id: str
    evidence_id: str
    evidence_digest: str
    issuer: str
    observation_digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attempt_id", _canonical_text(self.attempt_id, name="attempt_id")
        )
        object.__setattr__(
            self, "evidence_id", _canonical_text(self.evidence_id, name="evidence_id")
        )
        object.__setattr__(
            self,
            "evidence_digest",
            _digest(self.evidence_digest, name="evidence_digest"),
        )
        object.__setattr__(
            self, "issuer", _canonical_text(self.issuer, name="issuer")
        )
        object.__setattr__(
            self,
            "observation_digest",
            _digest(self.observation_digest, name="observation_digest"),
        )


@dataclass(frozen=True, slots=True)
class BillingEvidence:
    attempt_id: str
    billing_id: str
    provider_id: str
    model_id: str
    revision: str | None
    billed: Decimal
    cost_currency: str
    observed_at: str
    evidence_id: str
    evidence_digest: str
    issuer: str

    def __post_init__(self) -> None:
        for field_name in (
            "attempt_id",
            "billing_id",
            "provider_id",
            "model_id",
            "evidence_id",
            "issuer",
        ):
            object.__setattr__(
                self,
                field_name,
                _canonical_text(getattr(self, field_name), name=field_name),
            )
        if self.revision is not None:
            object.__setattr__(
                self, "revision", _canonical_text(self.revision, name="revision")
            )
        object.__setattr__(
            self, "billed", _exact_decimal(self.billed, name="billed")
        )
        currency = _canonical_text(
            self.cost_currency, name="cost_currency"
        ).upper()
        if currency != self.cost_currency or not currency.isalnum():
            raise ValueError(
                "cost_currency must be canonical uppercase alphanumeric text"
            )
        object.__setattr__(self, "cost_currency", currency)
        object.__setattr__(
            self, "observed_at", _utc_text(self.observed_at, name="observed_at")
        )
        object.__setattr__(
            self,
            "evidence_digest",
            _digest(self.evidence_digest, name="evidence_digest"),
        )


@dataclass(frozen=True, slots=True)
class ModelCallObservation:
    """Observed adapter response and exact cost evidence."""

    provider_id: str
    model_id: str
    revision: str | None
    observed_at: str
    incurred_cost: Decimal
    estimated_unbilled: Decimal
    output: object
    provider_request_id: str | None = None
    provider_response_id: str | None = None
    usage_id: str | None = None
    billing_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _canonical_text(self.provider_id, name="provider_id"),
        )
        object.__setattr__(
            self,
            "model_id",
            _canonical_text(self.model_id, name="model_id"),
        )
        if self.revision is not None:
            object.__setattr__(
                self,
                "revision",
                _canonical_text(self.revision, name="revision"),
            )
        object.__setattr__(
            self,
            "observed_at",
            _utc_text(self.observed_at, name="observed_at"),
        )
        object.__setattr__(
            self,
            "incurred_cost",
            _exact_decimal(self.incurred_cost, name="incurred_cost"),
        )
        object.__setattr__(
            self,
            "estimated_unbilled",
            _exact_decimal(
                self.estimated_unbilled,
                name="estimated_unbilled",
            ),
        )
        for field_name in (
            "provider_request_id",
            "provider_response_id",
            "usage_id",
            "billing_id",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    _canonical_text(value, name=field_name),
                )
        # Result identity is JSON-canonical. Raw output is intentionally not
        # persisted by this module, but an unhashable/non-JSON object cannot
        # become durable evidence.
        canonical_json(self.output)


@dataclass(frozen=True, slots=True)
class ModelCallOutcome:
    status: str
    attempt_id: str
    route: RouteDecision | None
    reason: str
    result_digest: str | None = None
    output: object | None = None
    schema_valid: bool | None = None


InferenceCall = Callable[
    [ModelCallBinding, Callable[[], bool]],
    ModelCallObservation,
]
PricingEvidenceResolver = Callable[
    [ModelCallSpec, tuple[ModelDescriptor, ...]],
    PricingEvidenceSnapshot,
]
ObservationEvidenceResolver = Callable[
    [ModelCallObservation, ModelCallBinding],
    ModelObservationEvidence,
]
BillingEvidenceResolver = Callable[
    [str, str, Mapping[str, object]],
    BillingEvidence,
]
ResultValidator = Callable[[object], bool]
CancelCheck = Callable[[], bool]
RecoveryFence = Callable[[], None]


class DurableModelCallOrchestrator:
    """Single durable call boundary for local and remote inference."""

    def __init__(
        self,
        *,
        budget: DurableModelBudget,
        clock: Callable[[], str],
        pricing_evidence_resolver: PricingEvidenceResolver,
        observation_evidence_resolver: ObservationEvidenceResolver,
        billing_evidence_resolver: BillingEvidenceResolver,
        started_lease_seconds: int = 60,
        owner_token: str | None = None,
    ) -> None:
        if type(budget) is not DurableModelBudget:
            raise TypeError("budget must be DurableModelBudget")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(pricing_evidence_resolver):
            raise TypeError("pricing_evidence_resolver must be callable")
        if not callable(observation_evidence_resolver):
            raise TypeError("observation_evidence_resolver must be callable")
        if not callable(billing_evidence_resolver):
            raise TypeError("billing_evidence_resolver must be callable")
        if (
            type(started_lease_seconds) is not int
            or started_lease_seconds < 1
            or started_lease_seconds > 3600
        ):
            raise ValueError(
                "started_lease_seconds must be an integer from 1 through 3600"
            )
        try:
            require_exact_journal_store_authority(
                budget.journal,
                subject="model budget journal",
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ModelCallError(
                "durable model budget journal authority is invalid"
            ) from error
        self.budget = budget
        self.journal = budget.journal
        self.clock = clock
        self.pricing_evidence_resolver = pricing_evidence_resolver
        self.observation_evidence_resolver = observation_evidence_resolver
        self.billing_evidence_resolver = billing_evidence_resolver
        self.started_lease_seconds = started_lease_seconds
        self.owner_token = (
            _canonical_text(owner_token, name="owner_token")
            if owner_token is not None
            else "model-owner-" + uuid4().hex
        )

    def _now(self) -> str:
        """Capture injected chronology without allowing authority redirection."""
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        utc_text = _utc_text
        module_globals = globals()
        callback_shape = DurableModelCallOrchestrator._callback_shape_snapshot(self)
        clock = callback_shape[1].get("clock")
        if not callable(clock):
            raise ModelCallError("model-call clock authority is invalid")

        clock_error: Exception | None = None
        clock_value: object = None
        clock_changes: list[str] = []
        try:
            clock_value = clock()
        except Exception as error:
            clock_error = error
        finally:
            try:
                clock_changes.extend(
                    restore_callback_shape(
                        self,
                        callback_shape,
                    )
                )
            finally:
                current_utc_text = dict.get(module_globals, "_utc_text")
                if current_utc_text is not utc_text:
                    clock_changes.append("module._utc_text")
                    dict.__setitem__(
                        module_globals,
                        "_utc_text",
                        utc_text,
                    )

        if clock_changes:
            error = ModelCallError(
                "clock mutated orchestrator authority:"
                + ",".join(sorted(set(clock_changes)))
            )
            if clock_error is not None:
                raise error from clock_error
            raise error
        if clock_error is not None:
            raise clock_error
        return utc_text(clock_value, name="clock")

    @staticmethod
    def _safe_instance_snapshot(
        value: object,
        *,
        subject: str,
    ) -> dict[str, object]:
        state = object.__getattribute__(value, "__dict__")
        if type(state) is not dict:
            raise ModelCallError(subject + " authority state is invalid")
        keys = tuple(state)
        if any(type(name) is not str for name in keys):
            raise ModelCallError(subject + " authority state keys are invalid")
        return dict.copy(state)

    @staticmethod
    def _restore_safe_instance_state(
        value: object,
        expected_state: dict[str, object],
        *,
        prefix: str,
    ) -> list[str]:
        current_state = object.__getattribute__(value, "__dict__")
        if type(current_state) is not dict:
            raise ModelCallError(prefix + " authority state is invalid")

        changes: list[str] = []
        current_keys = tuple(current_state)
        if any(type(name) is not str for name in current_keys):
            changes.append(prefix + ".<invalid-state-key>")
        current_names = {name for name in current_keys if type(name) is str}
        expected_names = set(expected_state)
        for name in sorted(current_names | expected_names):
            if name not in expected_state or name not in current_state:
                changes.append(prefix + "." + name)
                continue
            current = current_state[name]
            expected = expected_state[name]
            if type(expected) in (str, int, bool, Decimal, type(None)):
                if type(current) is not type(expected) or current != expected:
                    changes.append(prefix + "." + name)
            elif current is not expected:
                changes.append(prefix + "." + name)

        # Avoid deleting hostile keys one-by-one: dict.clear() does not re-enter
        # attacker-controlled __hash__/__str__ methods, and all restored keys
        # come from the previously validated exact-str snapshot.
        dict.clear(current_state)
        dict.update(current_state, expected_state)
        return changes

    def _callback_shape_snapshot(
        self,
    ) -> tuple[
        type,
        dict[str, object],
        object,
        type,
        dict[str, object],
        object,
        type,
        dict[str, object],
        object | None,
        type | None,
        dict[str, object] | None,
        tuple[tuple[type, str, tuple[type, ...], Mapping[str, object]], ...],
    ]:
        """Freeze exact class and instance authority around caller callbacks."""
        orchestrator_class = object.__getattribute__(self, "__class__")
        if orchestrator_class is not DurableModelCallOrchestrator:
            raise ModelCallError("model orchestrator class authority is invalid")
        self_state = DurableModelCallOrchestrator._safe_instance_snapshot(
            self,
            subject="model orchestrator",
        )
        budget = self_state.get("budget")
        if type(budget) is not DurableModelBudget:
            raise ModelCallError("model budget authority is invalid")
        budget_class = object.__getattribute__(budget, "__class__")
        budget_state = DurableModelCallOrchestrator._safe_instance_snapshot(
            budget,
            subject="model budget",
        )
        journal = self_state.get("journal")
        if journal is None or journal is not budget_state.get("journal"):
            raise ModelCallError("model budget journal authority is inconsistent")
        journal_class = object.__getattribute__(journal, "__class__")
        try:
            require_exact_journal_store_authority(
                journal,
                subject="model budget journal",
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ModelCallError(
                "durable model budget journal authority is invalid"
            ) from error
        if _model_journal_class_authority_changes(restore=False):
            raise ModelCallError(
                "durable model budget journal class authority is invalid"
            )
        journal_state = DurableModelCallOrchestrator._safe_instance_snapshot(
            journal,
            subject="model budget journal",
        )
        identity = journal_state.get("_store_identity")
        identity_class = (
            object.__getattribute__(identity, "__class__")
            if identity is not None
            else None
        )
        identity_state = (
            DurableModelCallOrchestrator._safe_instance_snapshot(
                identity,
                subject="model budget journal identity",
            )
            if identity is not None
            else None
        )

        # Freeze executable class dictionaries as well as each object's
        # __class__ pointer. A callback can otherwise leave every instance
        # dictionary unchanged while rebinding budget, journal, or restore
        # methods that execute before the next durable effect.
        authority_classes: list[type] = []
        seen_class_ids: set[int] = set()
        for selected_class in (
            orchestrator_class,
            budget_class,
            journal_class,
            identity_class,
        ):
            if selected_class is None:
                continue
            for candidate_class in selected_class.__mro__:
                if candidate_class is object:
                    continue
                candidate_id = id(candidate_class)
                if candidate_id in seen_class_ids:
                    continue
                seen_class_ids.add(candidate_id)
                authority_classes.append(candidate_class)
        class_authority = tuple(
            (
                authority_class,
                authority_class.__module__ + "." + authority_class.__qualname__,
                tuple(type.__getattribute__(authority_class, "__bases__")),
                MappingProxyType(dict(vars(authority_class))),
            )
            for authority_class in authority_classes
        )
        return (
            orchestrator_class,
            self_state,
            budget,
            budget_class,
            budget_state,
            journal,
            journal_class,
            journal_state,
            identity,
            identity_class,
            identity_state,
            class_authority,
        )

    @staticmethod
    def _restore_callback_shape(
        self: "DurableModelCallOrchestrator",
        snapshot: tuple[
            type,
            dict[str, object],
            object,
            type,
            dict[str, object],
            object,
            type,
            dict[str, object],
            object | None,
            type | None,
            dict[str, object] | None,
            tuple[tuple[type, str, tuple[type, ...], Mapping[str, object]], ...],
        ],
        module_globals=globals(),
        dataclass_fields=fields,
        json_module=json,
        json_loads=json.loads,
        journal_class_authority_changes=_model_journal_class_authority_changes,
        journal_authority_guard=require_exact_journal_store_authority,
    ) -> list[str]:
        """Restore exact callback authority before any dynamic attribute access."""
        (
            orchestrator_class,
            self_state,
            budget,
            budget_class,
            budget_state,
            journal,
            journal_class,
            journal_state,
            identity,
            identity_class,
            identity_state,
            class_authority,
        ) = snapshot
        changes: list[str] = []
        current_module_alias = dict.get(
            module_globals,
            "DurableModelCallOrchestrator",
        )
        if current_module_alias is not orchestrator_class:
            changes.append("module.DurableModelCallOrchestrator")
            dict.__setitem__(
                module_globals,
                "DurableModelCallOrchestrator",
                orchestrator_class,
            )

        current_budget_alias = dict.get(
            module_globals,
            "DurableModelBudget",
        )
        if current_budget_alias is not budget_class:
            changes.append("module.DurableModelBudget")
            dict.__setitem__(
                module_globals,
                "DurableModelBudget",
                budget_class,
            )

        current_fields = dict.get(module_globals, "fields")
        if current_fields is not dataclass_fields:
            changes.append("module.fields")
            dict.__setitem__(
                module_globals,
                "fields",
                dataclass_fields,
            )

        current_json = dict.get(module_globals, "json")
        if current_json is not json_module:
            changes.append("module.json")
            dict.__setitem__(
                module_globals,
                "json",
                json_module,
            )
        current_json_loads = dict.get(vars(json_module), "loads")
        if current_json_loads is not json_loads:
            changes.append("module.json.loads")
            dict.__setitem__(
                vars(json_module),
                "loads",
                json_loads,
            )

        current_journal_helper = dict.get(
            module_globals,
            "_model_journal_class_authority_changes",
        )
        if current_journal_helper is not journal_class_authority_changes:
            changes.append("module._model_journal_class_authority_changes")
            dict.__setitem__(
                module_globals,
                "_model_journal_class_authority_changes",
                journal_class_authority_changes,
            )

        current_journal_guard = dict.get(
            module_globals,
            "require_exact_journal_store_authority",
        )
        if current_journal_guard is not journal_authority_guard:
            changes.append("module.require_exact_journal_store_authority")
            dict.__setitem__(
                module_globals,
                "require_exact_journal_store_authority",
                journal_authority_guard,
            )

        # Restore the trusted JournalStore class topology before any generic
        # class-dictionary or instance recovery.  In particular, inherited
        # dispatch must not execute through a callback-injected base class.
        changes.extend(journal_class_authority_changes(restore=True))

        # Restore raw class dictionaries first. Descriptor identity comparison
        # avoids invoking attacker-defined equality during recovery.
        for (
            authority_class,
            class_label,
            expected_bases,
            expected_class_state,
        ) in class_authority:
            current_bases = type.__getattribute__(authority_class, "__bases__")
            if len(current_bases) != len(expected_bases) or any(
                current is not expected
                for current, expected in zip(current_bases, expected_bases)
            ):
                label = "class." + class_label + ".__bases__"
                changes.append(label)
                try:
                    type.__setattr__(authority_class, "__bases__", expected_bases)
                except TypeError as error:
                    raise ModelCallError(
                        label + " could not be restored after callback"
                    ) from error
                restored_bases = type.__getattribute__(
                    authority_class,
                    "__bases__",
                )
                if len(restored_bases) != len(expected_bases) or any(
                    current is not expected
                    for current, expected in zip(restored_bases, expected_bases)
                ):
                    raise ModelCallError(
                        label + " could not be restored after callback"
                    )

            current_class_state = vars(authority_class)
            current_names = set(current_class_state)
            expected_names = set(expected_class_state)
            for name in sorted(current_names | expected_names):
                label = "class." + class_label + "." + name
                if name not in expected_class_state:
                    changes.append(label)
                    try:
                        type.__delattr__(authority_class, name)
                    except (AttributeError, TypeError) as error:
                        raise ModelCallError(
                            label + " could not be removed after callback"
                        ) from error
                    continue
                expected = expected_class_state[name]
                if (
                    name not in current_class_state
                    or current_class_state[name] is not expected
                ):
                    changes.append(label)
                    try:
                        type.__setattr__(authority_class, name, expected)
                    except TypeError as error:
                        raise ModelCallError(
                            label + " could not be restored after callback"
                        ) from error

            restored_class_state = vars(authority_class)
            if set(restored_class_state) != expected_names or any(
                restored_class_state[name] is not expected_class_state[name]
                for name in expected_names
            ):
                raise ModelCallError(
                    "class authority restore is incomplete for " + class_label
                )

        def restore_class(
            value: object,
            expected_class: type,
            *,
            label: str,
        ) -> None:
            current_class = object.__getattribute__(value, "__class__")
            if current_class is expected_class:
                return
            changes.append(label)
            try:
                object.__setattr__(value, "__class__", expected_class)
            except (AttributeError, TypeError) as error:
                raise ModelCallError(
                    label + " could not be restored after callback"
                ) from error

        restore_class(self, orchestrator_class, label="orchestrator.__class__")
        restore_class(budget, budget_class, label="budget.__class__")
        restore_class(journal, journal_class, label="journal.__class__")
        if identity is not None and identity_class is not None:
            restore_class(
                identity,
                identity_class,
                label="journal._store_identity.__class__",
            )

        changes.extend(
            DurableModelCallOrchestrator._restore_safe_instance_state(
                self,
                self_state,
                prefix="orchestrator",
            )
        )
        changes.extend(
            DurableModelCallOrchestrator._restore_safe_instance_state(
                budget,
                budget_state,
                prefix="budget",
            )
        )
        changes.extend(
            DurableModelCallOrchestrator._restore_safe_instance_state(
                journal,
                journal_state,
                prefix="journal",
            )
        )
        if identity is not None and identity_state is not None:
            changes.extend(
                DurableModelCallOrchestrator._restore_safe_instance_state(
                    identity,
                    identity_state,
                    prefix="journal._store_identity",
                )
            )

        try:
            journal_authority_guard(
                journal,
                subject="model budget journal",
            )
        except (TypeError, ValueError, RuntimeError) as error:
            raise ModelCallError(
                "durable model budget journal authority changed"
            ) from error
        return changes

    @staticmethod
    def _sealed_spec(spec: ModelCallSpec) -> ModelCallSpec:
        if type(spec) is not ModelCallSpec:
            raise TypeError("spec must be ModelCallSpec")
        return ModelCallSpec(
            **{field.name: getattr(spec, field.name) for field in fields(ModelCallSpec)}
        )

    def attempt_id(self, spec: ModelCallSpec) -> str:
        spec = self._sealed_spec(spec)
        material = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "fallback_parent_attempt_id": spec.fallback_parent_attempt_id,
            "fallback_index": spec.fallback_index,
        }
        return "model-attempt-" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()

    def _aggregate_id(self, attempt_id: str) -> str:
        return "model-call:" + sha256(
            canonical_json(
                [
                    self.budget.environment,
                    self.budget.budget_id,
                    attempt_id,
                ]
            ).encode("utf-8")
        ).hexdigest()

    def _events(self, attempt_id: str) -> list[dict[str, object]]:
        return self.journal.load_events(
            _AGGREGATE_TYPE,
            self._aggregate_id(attempt_id),
        )

    @staticmethod
    def _event_identity(aggregate_id: str, event_type: str, version: int) -> str:
        return "model-call-event-" + sha256(
            canonical_json(
                [aggregate_id, event_type, str(version)]
            ).encode("utf-8")
        ).hexdigest()

    def _append(
        self,
        *,
        attempt_id: str,
        event_type: str,
        version: int,
        payload: dict[str, object],
        unique_claim: bool = False,
    ) -> bool:
        aggregate_id = self._aggregate_id(attempt_id)
        event_id = (
            "model-call-claim-" + uuid4().hex
            if unique_claim
            else self._event_identity(aggregate_id, event_type, version)
        )
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": self._now(),
        }
        try:
            return self.journal.append_event(envelope).inserted
        except ValueError:
            # Aggregate-version contention is an expected single-winner race.
            # Accept it only when durable truth at this version is the same
            # semantic transition; otherwise preserve the conflict.
            events = self._events(attempt_id)
            if len(events) < version:
                raise
            existing = events[version - 1]
            if (
                existing.get("aggregate_version") != version
                or existing.get("event_type") != event_type
            ):
                raise
            if unique_claim:
                return False
            if existing.get("payload") != payload:
                raise
            return False

    @staticmethod
    def _request_identity(request: ModelRequest) -> dict[str, object]:
        if type(request) is not ModelRequest:
            raise TypeError("request must be ModelRequest")
        deadline = request.deadline_utc.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
        return {
            "request_id": request.request_id,
            "allowed_model_ids": list(request.allowed_model_ids),
            "privacy_remote_allowed": request.privacy_remote_allowed,
            "budget_remaining": str(request.budget_remaining),
            "deadline_utc": deadline,
            "cancelled": request.cancelled,
        }

    def _durable_route_input(
        self,
        request_id: str,
    ) -> Mapping[str, object]:
        matches: list[Mapping[str, object]] = []
        for event in self.journal.load_events(
            "model_budget",
            self.budget.budget_id,
        ):
            if event.get("event_type") != "ModelRouteReserved":
                continue
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                continue
            if payload.get("request_id") != request_id:
                continue
            routing_input = payload.get("routing_input")
            if not isinstance(routing_input, Mapping):
                raise ModelCallError(
                    "durable route evidence is malformed"
                )
            matches.append(routing_input)
        if len(matches) != 1:
            raise ModelCallError(
                "request must have exactly one durable route reservation"
            )
        return matches[0]

    def _reservation_context(
        self,
        spec: ModelCallSpec,
        pricing: PricingEvidenceSnapshot | None,
    ) -> dict[str, str]:
        context = {
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "pricing_evidence_id": spec.pricing_evidence_id,
            "pricing_as_of": spec.pricing_as_of,
            "cost_currency": spec.cost_currency,
            "result_schema_id": spec.result_schema_id,
            "fallback_index": str(spec.fallback_index),
        }
        if pricing is not None:
            context["pricing_evidence_digest"] = pricing.evidence_digest
            context["pricing_valid_until"] = pricing.valid_until
        if spec.fallback_parent_attempt_id is not None:
            context["fallback_parent_attempt_id"] = spec.fallback_parent_attempt_id
        return context

    def _pricing_evidence(
        self,
        spec: ModelCallSpec,
        descriptors: tuple[ModelDescriptor, ...],
    ) -> PricingEvidenceSnapshot:
        # Evidence callbacks are not authority over the caller's deterministic
        # attempt identity or the route descriptor graph. Give the resolver
        # detached, revalidated values so object.__setattr__ mutation of frozen
        # dataclasses cannot rewrite the authoritative copies after attempt_id
        # or routing identity has been established.
        resolver_spec = ModelCallSpec(**{
            field.name: getattr(spec, field.name) for field in fields(ModelCallSpec)
        })
        resolver_descriptors = tuple(ModelDescriptor(**{
            field.name: getattr(item, field.name) for field in fields(ModelDescriptor)
        }) for item in descriptors)
        resolver = self.pricing_evidence_resolver
        callback_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        callback_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        callback_shape = self._callback_shape_snapshot()
        budget_callback_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        budget_callback_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        callback_error: Exception | None = None
        callback_changes: list[str] = []
        try:
            try:
                snapshot = resolver(
                    resolver_spec,
                    resolver_descriptors,
                )
            except Exception as error:
                callback_error = error
        finally:
            callback_changes.extend(
                restore_callback_shape(
                    self, callback_shape
                )
            )
            for name, expected in callback_refs.items():
                if getattr(self, name, None) is not expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in callback_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in budget_callback_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in budget_callback_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if callback_changes:
            raise ModelCallError(
                "pricing evidence resolver mutated orchestrator authority:"
                + ",".join(sorted(set(callback_changes)))
            )
        if callback_error is not None:
            raise ModelCallError(
                "pricing evidence could not be resolved before route admission"
            ) from callback_error
        if type(snapshot) is not PricingEvidenceSnapshot:
            raise ModelCallError(
                "pricing evidence resolver did not return PricingEvidenceSnapshot"
            )
        if type(snapshot.quotes) is not tuple or any(type(quote) is not PricingQuote for quote in snapshot.quotes):
            raise ModelCallError("pricing evidence quote graph is invalid")
        try:
            values = {field.name: getattr(snapshot, field.name) for field in fields(PricingEvidenceSnapshot)}
            values["quotes"] = tuple(PricingQuote(**{
                field.name: getattr(quote, field.name) for field in fields(PricingQuote)
            }) for quote in snapshot.quotes)
            snapshot = PricingEvidenceSnapshot(**values)
        except (TypeError, ValueError) as error:
            raise ModelCallError("pricing evidence value graph is invalid") from error
        if snapshot.evidence_id != spec.pricing_evidence_id:
            raise ModelCallError("pricing evidence identity does not match call spec")
        if snapshot.as_of != spec.pricing_as_of:
            raise ModelCallError("pricing evidence as-of does not match call spec")
        if snapshot.cost_currency != spec.cost_currency:
            raise ModelCallError("pricing evidence currency does not match call spec")
        now = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(snapshot.as_of.replace("Z", "+00:00"))
        valid_until = datetime.fromisoformat(
            snapshot.valid_until.replace("Z", "+00:00")
        )
        if as_of > now:
            raise ModelCallError("pricing evidence is from the future")
        if now >= valid_until:
            raise ModelCallError("pricing evidence has expired")
        expected_keys = {
            (item.provider_id, item.model_id, item.revision) for item in descriptors
        }
        evidence_keys = {item.key for item in snapshot.quotes}
        if expected_keys != evidence_keys:
            raise ModelCallError(
                "pricing evidence does not exactly cover routing descriptors"
            )
        for descriptor in descriptors:
            quote = snapshot.quote_for(
                descriptor.provider_id,
                descriptor.model_id,
                descriptor.revision,
            )
            if quote.estimated_cost != descriptor.estimated_cost:
                raise ModelCallError(
                    "descriptor estimated cost conflicts with sealed pricing evidence"
                )
        return snapshot

    @staticmethod
    def _selected_descriptor(
        decision: RouteDecision,
        descriptors: tuple[ModelDescriptor, ...],
    ) -> ModelDescriptor:
        if decision.status is not RouteStatus.ADMITTED:
            raise ModelCallError("route is not admitted")
        matches = [
            item
            for item in descriptors
            if item.model_id == decision.model_id
            and item.provider_id == decision.provider_id
            and item.revision == decision.revision
        ]
        if len(matches) != 1:
            raise ModelCallError(
                "reserved provider/model/revision is not uniquely present"
            )
        chosen = matches[0]
        if chosen.estimated_cost != decision.reserved_cost:
            raise ModelCallError(
                "reserved route cost conflicts with selected descriptor"
            )
        return chosen

    def _prepared_payload(
        self,
        *,
        attempt_id: str,
        spec: ModelCallSpec,
        decision: RouteDecision,
        descriptor: ModelDescriptor,
        pricing: PricingEvidenceSnapshot,
        request: ModelRequest,
    ) -> dict[str, object]:
        context = self._reservation_context(spec, pricing)
        return {
            "attempt_id": attempt_id,
            "request_id": attempt_id,
            "request_identity_digest": payload_digest(self._request_identity(request)),
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "pricing_evidence_id": spec.pricing_evidence_id,
            "pricing_evidence_digest": pricing.evidence_digest,
            "pricing_as_of": spec.pricing_as_of,
            "pricing_valid_until": pricing.valid_until,
            "cost_currency": spec.cost_currency,
            "result_schema_id": spec.result_schema_id,
            "fallback_parent_attempt_id": spec.fallback_parent_attempt_id,
            "fallback_index": spec.fallback_index,
            "provider_id": decision.provider_id,
            "model_id": decision.model_id,
            "revision": decision.revision,
            "remote": descriptor.remote,
            "reserved_cost": str(decision.reserved_cost),
            "reservation_context_hash": payload_digest(context),
        }

    @staticmethod
    def _route_from_prepared(payload: Mapping[str, object]) -> RouteDecision:
        try:
            reserved = _exact_decimal(
                payload["reserved_cost"],
                name="reserved_cost",
            )
            provider = _canonical_text(
                payload["provider_id"],
                name="provider_id",
            )
            model = _canonical_text(payload["model_id"], name="model_id")
            revision_raw = payload.get("revision")
            revision = (
                _canonical_text(revision_raw, name="revision")
                if revision_raw is not None
                else None
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ModelCallError(
                "durable prepared model-call payload is invalid"
            ) from error
        return RouteDecision(
            RouteStatus.ADMITTED,
            model,
            provider,
            revision,
            reserved,
            "durable_model_call",
        )

    def _outcome_from_terminal(
        self,
        event: Mapping[str, object],
        *,
        route: RouteDecision | None,
    ) -> ModelCallOutcome:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ModelCallError("durable terminal model-call payload is invalid")
        attempt_id = _canonical_text(
            payload.get("attempt_id"),
            name="attempt_id",
        )
        event_type = event.get("event_type")
        if event_type == "ModelCallNotSent":
            return ModelCallOutcome(
                "NOT_SENT",
                attempt_id,
                route,
                str(payload.get("reason", "not_sent")),
            )
        if event_type == "ModelCallUnknown":
            return ModelCallOutcome(
                "UNKNOWN",
                attempt_id,
                route,
                str(payload.get("reason", "unknown")),
            )
        if event_type == "ModelCallObserved":
            valid = payload.get("schema_valid")
            if type(valid) is not bool:
                raise ModelCallError(
                    "durable observed schema validation state is invalid"
                )
            digest = _digest(
                payload.get("result_digest"),
                name="result_digest",
            )
            return ModelCallOutcome(
                "OBSERVED_VALID" if valid else "OBSERVED_INVALID",
                attempt_id,
                route,
                "observed_response",
                result_digest=digest,
                output=None,
                schema_valid=valid,
            )
        raise ModelCallError("event is not a terminal model-call state")

    def _ensure_terminal_budget(
        self,
        event: Mapping[str, object],
    ) -> None:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ModelCallError("durable terminal payload is invalid")
        attempt_id = _canonical_text(
            payload.get("attempt_id"),
            name="attempt_id",
        )
        event_type = event.get("event_type")
        if event_type == "ModelCallNotSent":
            self.budget.release(attempt_id)
        elif event_type == "ModelCallUnknown":
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=payload["estimated_unbilled"],
            )
        elif event_type == "ModelCallObserved":
            self.budget.settle(
                attempt_id,
                incurred=payload["incurred_cost"],
                estimated_unbilled=payload["estimated_unbilled"],
            )

    def _recover_existing(
        self,
        *,
        attempt_id: str,
        decision: RouteDecision | None,
    ) -> ModelCallOutcome:
        events = self._events(attempt_id)
        if not events:
            raise ModelCallError("model-call attempt disappeared")
        last = events[-1]
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("event_type")
                in {"ModelCallNotSent", "ModelCallUnknown", "ModelCallObserved"}
            ),
            None,
        )
        if terminal is not None:
            self._ensure_terminal_budget(terminal)
            return self._outcome_from_terminal(terminal, route=decision)
        if last.get("event_type") == "ModelCallPrepared":
            return ModelCallOutcome(
                "PREPARED",
                attempt_id,
                decision,
                "prepared_without_call_boundary",
            )
        if last.get("event_type") != "ModelCallStarted":
            raise ModelCallError("unsupported durable model-call state")
        payload = last.get("payload")
        if not isinstance(payload, Mapping):
            raise ModelCallError("durable started model-call payload is invalid")
        started_at = _utc_text(payload.get("started_at"), name="started_at")
        now = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        age = (now - started).total_seconds()
        if age < 0:
            return ModelCallOutcome(
                "IN_PROGRESS",
                attempt_id,
                decision,
                "clock_before_started_timestamp",
            )
        if age < self.started_lease_seconds:
            return ModelCallOutcome(
                "IN_PROGRESS",
                attempt_id,
                decision,
                "model_call_owner_lease_active",
            )

        reserved = _exact_decimal(
            payload.get("reserved_cost"),
            name="reserved_cost",
        )
        unknown_payload = {
            "attempt_id": attempt_id,
            "reason": "recovered_after_call_boundary_without_terminal_observation",
            "estimated_unbilled": str(reserved),
        }
        self._append(
            attempt_id=attempt_id,
            event_type="ModelCallUnknown",
            version=3,
            payload=unknown_payload,
        )
        events = self._events(attempt_id)
        last = events[-1]
        self._ensure_terminal_budget(last)
        return self._outcome_from_terminal(last, route=decision)

    def _binding(
        self,
        *,
        attempt_id: str,
        spec: ModelCallSpec,
        decision: RouteDecision,
        descriptor: ModelDescriptor,
        pricing_evidence_digest: str,
    ) -> ModelCallBinding:
        return ModelCallBinding(
            attempt_id=attempt_id,
            request_id=attempt_id,
            job_id=spec.job_id,
            input_digest=spec.input_digest,
            provider_id=_canonical_text(
                decision.provider_id,
                name="provider_id",
            ),
            model_id=_canonical_text(decision.model_id, name="model_id"),
            revision=decision.revision,
            remote=descriptor.remote,
            reserved_cost=decision.reserved_cost,
            pricing_evidence_id=spec.pricing_evidence_id,
            pricing_evidence_digest=_digest(
                pricing_evidence_digest,
                name="pricing_evidence_digest",
            ),
            pricing_as_of=spec.pricing_as_of,
            cost_currency=spec.cost_currency,
            result_schema_id=spec.result_schema_id,
            fallback_parent_attempt_id=spec.fallback_parent_attempt_id,
            fallback_index=spec.fallback_index,
        )

    @staticmethod
    def _observation_digest(
        observation: ModelCallObservation,
        binding: ModelCallBinding,
    ) -> str:
        material = {
            "attempt_id": binding.attempt_id,
            "provider_id": observation.provider_id,
            "model_id": observation.model_id,
            "revision": observation.revision,
            "observed_at": observation.observed_at,
            "incurred_cost": str(observation.incurred_cost),
            "estimated_unbilled": str(observation.estimated_unbilled),
            "output_digest": payload_digest(observation.output),
            "provider_request_id": observation.provider_request_id,
            "provider_response_id": observation.provider_response_id,
            "usage_id": observation.usage_id,
            "billing_id": observation.billing_id,
        }
        return payload_digest(material)

    def _verified_observation_evidence(
        self,
        observation: ModelCallObservation,
        binding: ModelCallBinding,
        *,
        resolver: ObservationEvidenceResolver,
    ) -> ModelObservationEvidence:
        expected = self._observation_digest(observation, binding)
        # The authenticator may inspect evidence inputs, but it must not be able
        # to mutate the authoritative observation/binding graph retained by this
        # attempt. Deep-copy JSON output and reseal all scalar dataclass fields.
        resolver_observation_values = {
            field.name: getattr(observation, field.name)
            for field in fields(ModelCallObservation)
        }
        resolver_observation_values["output"] = json.loads(
            canonical_json(resolver_observation_values["output"])
        )
        resolver_observation = ModelCallObservation(**resolver_observation_values)
        resolver_binding = ModelCallBinding(**{
            field.name: getattr(binding, field.name)
            for field in fields(ModelCallBinding)
        })
        callback_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        callback_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        callback_shape = self._callback_shape_snapshot()
        budget_callback_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        budget_callback_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        callback_error: Exception | None = None
        callback_changes: list[str] = []
        try:
            try:
                evidence = resolver(
                    resolver_observation,
                    resolver_binding,
                )
            except Exception as error:
                callback_error = error
        finally:
            callback_changes.extend(
                restore_callback_shape(
                    self, callback_shape
                )
            )
            for name, expected in callback_refs.items():
                if getattr(self, name, None) is not expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in callback_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in budget_callback_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in budget_callback_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if callback_changes:
            raise ModelCallError(
                "observation evidence resolver mutated orchestrator authority:"
                + ",".join(sorted(set(callback_changes)))
            )
        if callback_error is not None:
            raise ModelCallError(
                "model usage/response evidence could not be authenticated"
            ) from callback_error
        if type(evidence) is not ModelObservationEvidence:
            raise ModelCallError(
                "observation evidence resolver did not return ModelObservationEvidence"
            )
        if evidence.attempt_id != binding.attempt_id:
            raise ModelCallError("observation evidence attempt identity mismatch")
        if (evidence.observation_digest != expected
                or self._observation_digest(observation, binding) != expected):
            raise ModelCallError(
                "observation evidence does not bind exact usage/response fields"
            )
        return ModelObservationEvidence(**{
            field.name: getattr(evidence, field.name) for field in fields(ModelObservationEvidence)
        })

    def _validate_fallback_lineage(
        self,
        spec: ModelCallSpec,
        policy: RoutingPolicy,
        request: ModelRequest,
    ) -> None:
        """Require fallback to remain inside one durable, safely terminal envelope."""

        if spec.fallback_index == 0:
            return
        if not isinstance(policy, RoutingPolicy):
            raise TypeError("policy must be RoutingPolicy")
        parent_attempt_id = _canonical_text(
            spec.fallback_parent_attempt_id,
            name="fallback_parent_attempt_id",
        )
        parent_events = self._events(parent_attempt_id)
        if not parent_events:
            raise ModelCallError(
                "fallback parent attempt does not exist in this durable model-call scope"
            )

        prepared = parent_events[0]
        if prepared.get("event_type") != "ModelCallPrepared":
            raise ModelCallError(
                "fallback parent lacks durable prepared model-call identity"
            )
        prepared_payload = prepared.get("payload")
        if not isinstance(prepared_payload, Mapping):
            raise ModelCallError("fallback parent prepared payload is invalid")
        if (
            prepared_payload.get("attempt_id") != parent_attempt_id
            or prepared_payload.get("request_id") != parent_attempt_id
        ):
            raise ModelCallError("fallback parent durable identity is inconsistent")

        expected_parent_scope = {
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "cost_currency": spec.cost_currency,
            "result_schema_id": spec.result_schema_id,
        }
        if any(
            prepared_payload.get(key) != value
            for key, value in expected_parent_scope.items()
        ):
            raise ModelCallError(
                "fallback parent does not match the same semantic request scope"
            )
        parent_index = prepared_payload.get("fallback_index")
        if (
            type(parent_index) is not int
            or parent_index + 1 != spec.fallback_index
        ):
            raise ModelCallError(
                "fallback index must directly follow the durable parent attempt"
            )

        route_events = self.journal.load_events(
            "model_budget",
            self.budget.budget_id,
        )
        matching_routes = [
            event
            for event in route_events
            if event.get("event_type") == "ModelRouteReserved"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("request_id") == parent_attempt_id
        ]
        if len(matching_routes) != 1:
            raise ModelCallError(
                "fallback parent durable route reservation is not unique"
            )
        routing_input = matching_routes[0]["payload"].get("routing_input")
        if not isinstance(routing_input, Mapping):
            raise ModelCallError(
                "fallback parent durable routing envelope is invalid"
            )
        parent_policy = routing_input.get("policy")
        current_policy = {
            "mode": policy.mode.value,
            "allowed_model_ids": list(policy.allowed_model_ids),
            "fixed_model_id": policy.fixed_model_id,
            "allow_remote": policy.allow_remote,
            "maximum_cost": str(policy.maximum_cost),
            "maximum_latency_ms": policy.maximum_latency_ms,
        }
        if parent_policy != current_policy:
            raise ModelCallError(
                "fallback policy must match the durable parent routing policy"
            )

        parent_request = routing_input.get("request")
        if not isinstance(parent_request, Mapping):
            raise ModelCallError(
                "fallback parent durable request envelope is invalid"
            )
        parent_allowed = parent_request.get("allowed_model_ids")
        if (
            not isinstance(parent_allowed, list)
            or any(not isinstance(value, str) for value in parent_allowed)
            or not set(request.allowed_model_ids).issubset(set(parent_allowed))
        ):
            raise ModelCallError(
                "fallback request model allowlist widens the durable parent request"
            )
        parent_privacy_remote = parent_request.get("privacy_remote_allowed")
        if type(parent_privacy_remote) is not bool:
            raise ModelCallError(
                "fallback parent privacy envelope is invalid"
            )
        if request.privacy_remote_allowed and not parent_privacy_remote:
            raise ModelCallError(
                "fallback request cannot widen remote privacy permission"
            )
        try:
            parent_budget_cap = _exact_decimal(
                parent_request.get("budget_cap"),
                name="parent fallback budget_cap",
            )
            parent_deadline = datetime.fromisoformat(
                _canonical_text(
                    parent_request.get("deadline_utc"),
                    name="parent fallback deadline_utc",
                )
            )
        except ValueError as error:
            raise ModelCallError(
                "fallback parent request budget/deadline envelope is invalid"
            ) from error
        if request.budget_remaining > parent_budget_cap:
            raise ModelCallError(
                "fallback request cannot widen the durable parent budget cap"
            )
        if (
            parent_deadline.tzinfo is None
            or request.deadline_utc > parent_deadline
        ):
            raise ModelCallError(
                "fallback request cannot extend the durable parent deadline"
            )

        terminal = next(
            (
                event
                for event in reversed(parent_events)
                if event.get("event_type")
                in {"ModelCallNotSent", "ModelCallUnknown", "ModelCallObserved"}
            ),
            None,
        )
        if terminal is None:
            raise ModelCallError(
                "fallback parent has not reached a durable terminal outcome"
            )
        terminal_payload = terminal.get("payload")
        if (
            not isinstance(terminal_payload, Mapping)
            or terminal_payload.get("attempt_id") != parent_attempt_id
        ):
            raise ModelCallError("fallback parent terminal evidence is invalid")

        event_type = terminal.get("event_type")
        if event_type == "ModelCallNotSent":
            return
        if event_type == "ModelCallUnknown":
            raise ModelCallError(
                "fallback parent outcome is uncertain; blind retry/fallback is forbidden"
            )
        if event_type == "ModelCallObserved":
            schema_valid = terminal_payload.get("schema_valid")
            if type(schema_valid) is not bool:
                raise ModelCallError(
                    "fallback parent observed schema state is invalid"
                )
            if not schema_valid:
                return
            raise ModelCallError(
                "fallback parent already produced a valid observed result"
            )
        raise ModelCallError("fallback parent terminal outcome is unsupported")

    def _temporal_reason(self, prepared: Mapping[str, object], request: ModelRequest) -> str | None:
        now = datetime.fromisoformat(self._now().replace("Z", "+00:00"))
        as_of = datetime.fromisoformat(_utc_text(prepared.get("pricing_as_of"), name="pricing_as_of").replace("Z", "+00:00"))
        expiry = datetime.fromisoformat(_utc_text(prepared.get("pricing_valid_until"), name="pricing_valid_until").replace("Z", "+00:00"))
        if now < as_of:
            return "clock_before_pricing_authority_at_call_boundary"
        if now >= expiry:
            return "pricing_evidence_expired_before_call_boundary"
        if now >= request.deadline_utc:
            return "request_deadline_expired_before_call_boundary"
        return None

    def execute(
        self,
        *,
        spec: ModelCallSpec,
        policy: RoutingPolicy,
        request: ModelRequest,
        descriptors: Iterable[ModelDescriptor],
        call: InferenceCall,
        validate_result: ResultValidator,
        now_utc: datetime | None = None,
        cancel_requested: CancelCheck | None = None,
    ) -> ModelCallOutcome:
        spec = self._sealed_spec(spec)
        if type(policy) is not RoutingPolicy:
            raise TypeError("policy must be RoutingPolicy")
        if type(request) is not ModelRequest:
            raise TypeError("request must be ModelRequest")
        # Retain original scalar authority even if the caller later mutates its
        # DTO during a cancellation, pricing or inference callback.
        request = ModelRequest(**{field.name: getattr(request, field.name) for field in fields(ModelRequest)})
        policy = RoutingPolicy(**{field.name: getattr(policy, field.name) for field in fields(RoutingPolicy)})
        if not callable(call):
            raise TypeError("call must be callable")
        if not callable(validate_result):
            raise TypeError("validate_result must be callable")
        if cancel_requested is not None and not callable(cancel_requested):
            raise TypeError("cancel_requested must be callable or None")

        attempt_id = self.attempt_id(spec)
        if request.request_id != attempt_id:
            raise ValueError(
                "ModelRequest.request_id must equal the deterministic model attempt id"
            )

        self._validate_fallback_lineage(spec, policy, request)
        existing = self._events(attempt_id)
        if any(event.get("event_type") in {
            "ModelCallNotSent", "ModelCallUnknown", "ModelCallObserved",
        } for event in existing):
            return self._recover_existing(
                attempt_id=attempt_id,
                decision=None,
            )

        first = existing[0] if existing else None
        if first is None:
            # Bind all new-attempt routing decisions to one exact time snapshot.
            # Without this, omitted now_utc could be sampled before and after
            # slow inventory/pricing work and cross the request deadline.
            route_now = _route_now(now_utc)
            inventory_free = route_model(
                policy,
                request,
                (),
                now_utc=route_now,
            )
            if inventory_free.reason in {
                "request_cancelled",
                "deadline_expired",
                "zero_model_policy",
            }:
                decision = self.budget.admit_route(
                    policy,
                    request,
                    (),
                    now_utc=route_now,
                )
                if decision.status is RouteStatus.ADMITTED:
                    raise ModelCallError(
                        "inventory-free route unexpectedly admitted a model"
                    )
                return ModelCallOutcome(
                    decision.status.value,
                    attempt_id,
                    decision,
                    decision.reason,
                )

        descriptor_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        descriptor_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        descriptor_shape = self._callback_shape_snapshot()
        descriptor_budget_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        descriptor_budget_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        descriptor_error: Exception | None = None
        descriptor_changes: list[str] = []
        materialized: tuple[ModelDescriptor, ...] | tuple[object, ...] = ()
        try:
            try:
                materialized = tuple(descriptors)
            except Exception as error:
                descriptor_error = error
        finally:
            descriptor_changes.extend(
                restore_callback_shape(
                    self, descriptor_shape
                )
            )
            for name, expected in descriptor_refs.items():
                if getattr(self, name, None) is not expected:
                    descriptor_changes.append(name)
                setattr(self, name, expected)
            for name, expected in descriptor_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    descriptor_changes.append(name)
                setattr(self, name, expected)
            for name, expected in descriptor_budget_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    descriptor_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in descriptor_budget_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    descriptor_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if descriptor_changes:
            raise ModelCallError(
                "descriptor inventory mutated orchestrator authority:"
                + ",".join(sorted(set(descriptor_changes)))
            )
        if descriptor_error is not None:
            raise ModelCallError("descriptor inventory could not be materialized") from descriptor_error
        if any(type(item) is not ModelDescriptor for item in materialized):
            raise TypeError("descriptors must be exact ModelDescriptor values")
        materialized = tuple(ModelDescriptor(**{
            field.name: getattr(item, field.name) for field in fields(ModelDescriptor)
        }) for item in materialized)
        pricing: PricingEvidenceSnapshot | None = None

        if first is not None:
            if first.get("event_type") != "ModelCallPrepared":
                raise ModelCallError(
                    "model-call identity conflicts with durable prepared attempt"
                )
            prepared_payload = first.get("payload")
            if not isinstance(prepared_payload, Mapping):
                raise ModelCallError(
                    "durable prepared model-call payload is invalid"
                )
            expected_spec = {
                "attempt_id": attempt_id,
                "request_id": attempt_id,
                "request_identity_digest": payload_digest(self._request_identity(request)),
                "budget_id": self.budget.budget_id,
                "environment": self.budget.environment,
                "job_id": spec.job_id,
                "input_digest": spec.input_digest,
                "policy_id": spec.policy_id,
                "pricing_evidence_id": spec.pricing_evidence_id,
                "pricing_as_of": spec.pricing_as_of,
                "cost_currency": spec.cost_currency,
                "result_schema_id": spec.result_schema_id,
                "fallback_parent_attempt_id": spec.fallback_parent_attempt_id,
                "fallback_index": spec.fallback_index,
            }
            if any(
                prepared_payload.get(key) != value
                for key, value in expected_spec.items()
            ):
                raise ModelCallError(
                    "model-call identity conflicts with durable prepared attempt"
                )
            routing_input = self._durable_route_input(attempt_id)
            current_policy = {
                "mode": policy.mode.value,
                "allowed_model_ids": list(policy.allowed_model_ids),
                "fixed_model_id": policy.fixed_model_id,
                "allow_remote": policy.allow_remote,
                "maximum_cost": str(policy.maximum_cost),
                "maximum_latency_ms": policy.maximum_latency_ms,
            }
            if routing_input.get("policy") != current_policy:
                raise ModelCallError("routing policy conflicts with durable prepared authority")
            pricing_evidence_digest = _digest(
                prepared_payload.get("pricing_evidence_digest"),
                name="pricing_evidence_digest",
            )
            _utc_text(
                prepared_payload.get("pricing_valid_until"),
                name="pricing_valid_until",
            )
            decision = self._route_from_prepared(prepared_payload)
            descriptor = self._selected_descriptor(decision, materialized)
            if prepared_payload.get("remote") is not descriptor.remote:
                raise ModelCallError(
                    "durable prepared route remote/local identity changed"
                )
            active = self.budget.active_reservation(attempt_id)
            if active is None or active != decision.reserved_cost:
                raise ModelCallError(
                    "exact durable model reservation is not active"
                )
            if len(existing) > 1:
                return self._recover_existing(
                    attempt_id=attempt_id,
                    decision=decision,
                )
        else:
            if getattr(policy.mode, "value", None) != "ZERO":
                pricing = self._pricing_evidence(spec, materialized)
            decision = self.budget.admit_route(
                policy,
                request,
                materialized,
                now_utc=route_now,
                reservation_context=self._reservation_context(spec, pricing),
            )
            if decision.status is not RouteStatus.ADMITTED:
                return ModelCallOutcome(
                    decision.status.value,
                    attempt_id,
                    decision,
                    decision.reason,
                )
            descriptor = self._selected_descriptor(decision, materialized)
            if pricing is None:
                raise ModelCallError(
                    "admitted model route lacks sealed pricing evidence"
                )
            active = self.budget.active_reservation(attempt_id)
            if active is None or active != decision.reserved_cost:
                raise ModelCallError(
                    "exact durable model reservation is not active"
                )
            prepared_payload = self._prepared_payload(
                attempt_id=attempt_id,
                spec=spec,
                decision=decision,
                descriptor=descriptor,
                pricing=pricing,
                request=request,
            )
            pricing_evidence_digest = pricing.evidence_digest
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallPrepared",
                version=1,
                payload=prepared_payload,
            )

        cancelled = (
            cancel_requested if cancel_requested is not None else (lambda: False)
        )
        # The cancellation probe is caller-owned code that runs before the
        # inference boundary. Freeze the exact durable authorities around it so
        # it cannot redirect the NOT_SENT proof, release, chronology, or a later
        # call through a retained orchestrator closure.
        cancellation_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        cancellation_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        cancellation_shape = self._callback_shape_snapshot()
        cancellation_budget_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        cancellation_budget_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        cancellation_error: Exception | None = None
        cancellation_changes: list[str] = []
        cancelled_before_start: object = False
        try:
            try:
                cancelled_before_start = cancelled()
            except Exception as error:
                cancellation_error = error
        finally:
            cancellation_changes.extend(
                restore_callback_shape(
                    self, cancellation_shape
                )
            )
            for name, expected in cancellation_refs.items():
                if getattr(self, name, None) is not expected:
                    cancellation_changes.append(name)
                setattr(self, name, expected)
            for name, expected in cancellation_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    cancellation_changes.append(name)
                setattr(self, name, expected)
            for name, expected in cancellation_budget_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    cancellation_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in cancellation_budget_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    cancellation_changes.append("budget." + name)
                setattr(self.budget, name, expected)

        if cancellation_changes:
            error = ModelCallError(
                "cancellation probe mutated orchestrator authority:"
                + ",".join(sorted(set(cancellation_changes)))
            )
            if cancellation_error is not None:
                raise error from cancellation_error
            raise error
        if cancellation_error is not None:
            raise cancellation_error
        if type(cancelled_before_start) is not bool:
            raise ModelCallError(
                "cancellation probe must return an exact boolean"
            )

        temporal_reason = self._temporal_reason(prepared_payload, request)
        cancellation_reason: str | None = None
        if cancelled_before_start:
            cancellation_reason = "cancelled_before_call_boundary"
        elif temporal_reason is not None:
            cancellation_reason = temporal_reason

        if cancellation_reason is not None:
            payload = {
                "attempt_id": attempt_id,
                "reason": cancellation_reason,
                "released": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallNotSent",
                version=2,
                payload=payload,
            )
            self.budget.release(attempt_id)
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        started_payload = {
            "attempt_id": attempt_id,
            "owner_token": self.owner_token,
            "started_at": self._now(),
            "provider_id": decision.provider_id,
            "model_id": decision.model_id,
            "revision": decision.revision,
            "reserved_cost": str(decision.reserved_cost),
        }
        owns_call = self._append(
            attempt_id=attempt_id,
            event_type="ModelCallStarted",
            version=2,
            payload=started_payload,
            unique_claim=True,
        )
        if not owns_call:
            return self._recover_existing(
                attempt_id=attempt_id,
                decision=decision,
            )

        final_reason = self._temporal_reason(prepared_payload, request)
        if final_reason is not None:
            self._append(
                attempt_id=attempt_id, event_type="ModelCallNotSent", version=3,
                payload={"attempt_id": attempt_id, "reason": final_reason,
                         "released": str(decision.reserved_cost)},
            )
            self.budget.release(attempt_id)
            return self._outcome_from_terminal(self._events(attempt_id)[-1], route=decision)

        binding = self._binding(
            attempt_id=attempt_id,
            spec=spec,
            decision=decision,
            descriptor=descriptor,
            pricing_evidence_digest=pricing_evidence_digest,
        )
        adapter_binding = ModelCallBinding(
            **{field.name: getattr(binding, field.name) for field in fields(ModelCallBinding)}
        )

        # The injected adapter is not an authority over the orchestrator itself.
        # Capture every mutable orchestrator reference that can affect durable
        # evidence, chronology or budget settlement after ModelCallStarted. The
        # adapter may retain a reference to this orchestrator through its closure,
        # so restore these exact authorities before any post-call durable action
        # and classify any attempted replacement as UNKNOWN.
        adapter_boundary_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        adapter_boundary_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        adapter_shape = self._callback_shape_snapshot()
        adapter_budget_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        adapter_budget_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        observation_evidence_resolver = self.observation_evidence_resolver
        adapter_error: Exception | None = None
        authority_changes: list[str] = []
        try:
            try:
                observation = call(adapter_binding, cancelled)
            except Exception as error:
                adapter_error = error
        finally:
            authority_changes.extend(
                restore_callback_shape(
                    self, adapter_shape
                )
            )
            for name, expected in adapter_boundary_refs.items():
                if getattr(self, name, None) is not expected:
                    authority_changes.append(name)
                setattr(self, name, expected)
            for name, expected in adapter_boundary_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    authority_changes.append(name)
                setattr(self, name, expected)
            for name, expected in adapter_budget_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    authority_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in adapter_budget_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    authority_changes.append("budget." + name)
                setattr(self.budget, name, expected)

        if authority_changes:
            payload = {
                "attempt_id": attempt_id,
                "reason": "adapter_mutated_orchestrator_authority:"
                + ",".join(sorted(set(authority_changes))),
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        if isinstance(adapter_error, ModelCallNotSent):
            # Once ModelCallStarted is durable, the injected adapter is inside
            # the possibly-billed boundary. Its own exception type is not an
            # independently authenticated proof that no external/local work
            # crossed that boundary. Preserve the entire reserved ceiling as
            # uncertain until billing evidence reconciles it.
            payload = {
                "attempt_id": attempt_id,
                "reason": "adapter_not_sent_claim_unverified",
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )
        if adapter_error is not None:
            payload = {
                "attempt_id": attempt_id,
                "reason": "call_boundary_result_ambiguous:"
                + type(adapter_error).__name__,
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        if type(observation) is not ModelCallObservation:
            payload = {
                "attempt_id": attempt_id,
                "reason": "adapter_returned_invalid_observation",
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        # Detach the complete accepted observation graph from adapter ownership.
        # Revalidate scalars at use time, including objects modified after DTO construction.
        try:
            values = {field.name: getattr(observation, field.name) for field in fields(ModelCallObservation)}
            values["output"] = json.loads(canonical_json(values["output"]))
            observation = ModelCallObservation(**values)
        except (TypeError, ValueError, RecursionError):
            self._append(
                attempt_id=attempt_id, event_type="ModelCallUnknown", version=3,
                payload={"attempt_id": attempt_id, "reason": "adapter_returned_invalid_observation",
                         "estimated_unbilled": str(decision.reserved_cost)},
            )
            self.budget.settle(attempt_id, incurred="0", estimated_unbilled=decision.reserved_cost)
            return self._outcome_from_terminal(self._events(attempt_id)[-1], route=decision)

        if (
            observation.provider_id != decision.provider_id
            or observation.model_id != decision.model_id
            or observation.revision != decision.revision
        ):
            payload = {
                "attempt_id": attempt_id,
                "reason": "observed_provider_model_revision_mismatch",
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        try:
            observation_evidence = self._verified_observation_evidence(
                observation,
                binding,
                resolver=observation_evidence_resolver,
            )
        except ModelCallError as error:
            payload = {
                "attempt_id": attempt_id,
                "reason": "observation_evidence_invalid:" + str(error),
                "estimated_unbilled": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        try:
            total = exact_add(observation.incurred_cost, observation.estimated_unbilled)
        except ValueError:
            total = None
        if total is None or total > decision.reserved_cost:
            # Do not pretend an over-ceiling observation is safely settled.
            # Preserve the full declared reservation as uncertain and retain the
            # observed overrun in durable diagnostic evidence for qualification.
            payload = {
                "attempt_id": attempt_id,
                "reason": "observed_cost_resource_envelope_exceeded" if total is None else "observed_cost_exceeds_reserved_ceiling",
                "estimated_unbilled": str(decision.reserved_cost),
                "observed_incurred_cost": str(observation.incurred_cost),
                "observed_estimated_unbilled": str(
                    observation.estimated_unbilled
                ),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallUnknown",
                version=3,
                payload=payload,
            )
            self.budget.settle(
                attempt_id,
                incurred="0",
                estimated_unbilled=decision.reserved_cost,
            )
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )

        # Freeze the authenticated result before invoking caller-owned validation.
        # Validators receive a detached JSON graph; mutating their input cannot
        # rewrite retained response identity or the advisory output we return.
        result_json = canonical_json(observation.output)
        result_digest = payload_digest(observation.output)
        observed_payload = {
            "attempt_id": attempt_id,
            "provider_id": observation.provider_id,
            "model_id": observation.model_id,
            "revision": observation.revision,
            "observed_at": observation.observed_at,
            "provider_request_id": observation.provider_request_id,
            "provider_response_id": observation.provider_response_id,
            "usage_id": observation.usage_id,
            "billing_id": observation.billing_id,
            "observation_evidence_id": observation_evidence.evidence_id,
            "observation_evidence_digest": observation_evidence.evidence_digest,
            "observation_evidence_issuer": observation_evidence.issuer,
            "observation_digest": observation_evidence.observation_digest,
            "pricing_evidence_id": spec.pricing_evidence_id,
            "pricing_evidence_digest": pricing_evidence_digest,
            "cost_currency": spec.cost_currency,
            "incurred_cost": str(observation.incurred_cost),
            "estimated_unbilled": str(observation.estimated_unbilled),
            "result_digest": result_digest,
            "result_schema_id": spec.result_schema_id,
            "schema_valid": None,
        }
        validation_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        validation_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        validation_shape = self._callback_shape_snapshot()
        validation_budget_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        validation_budget_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        validation_changes: list[str] = []
        try:
            try:
                schema_valid = validate_result(json.loads(result_json))
            except Exception:
                schema_valid = False
        finally:
            validation_changes.extend(
                restore_callback_shape(
                    self, validation_shape
                )
            )
            for name, expected in validation_refs.items():
                if getattr(self, name, None) is not expected:
                    validation_changes.append(name)
                setattr(self, name, expected)
            for name, expected in validation_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    validation_changes.append(name)
                setattr(self, name, expected)
            for name, expected in validation_budget_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    validation_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in validation_budget_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    validation_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if validation_changes or type(schema_valid) is not bool:
            schema_valid = False
        observed_payload["schema_valid"] = schema_valid
        self._append(
            attempt_id=attempt_id,
            event_type="ModelCallObserved",
            version=3,
            payload=observed_payload,
        )
        self.budget.settle(
            attempt_id,
            incurred=observed_payload["incurred_cost"],
            estimated_unbilled=observed_payload["estimated_unbilled"],
        )
        return ModelCallOutcome(
            "OBSERVED_VALID" if schema_valid else "OBSERVED_INVALID",
            attempt_id,
            decision,
            "observed_response",
            result_digest=result_digest,
            output=json.loads(result_json) if schema_valid else None,
            schema_valid=schema_valid,
        )

    def recover_reserved_not_started(
        self,
        *,
        spec: ModelCallSpec,
        recovery_fence: RecoveryFence,
    ) -> ModelCallOutcome:
        """Prove NOT_SENT after restart and release only under exclusive recovery.

        The caller must provide the existing canonical host/recovery fence. This
        method does not invent a second ownership authority.
        """
        if type(spec) is not ModelCallSpec:
            raise TypeError("spec must be ModelCallSpec")
        # Fence callbacks may capture caller-owned objects in closures. Freeze
        # recovery identity before invoking the fence so it cannot redirect the
        # reservation lookup by mutating a frozen ModelCallSpec.
        spec = ModelCallSpec(**{
            field.name: getattr(spec, field.name) for field in fields(ModelCallSpec)
        })
        if not callable(recovery_fence):
            raise TypeError("recovery_fence must be callable")

        recovery_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        recovery_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        recovery_shape = self._callback_shape_snapshot()
        recovery_budget_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        recovery_budget_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        recovery_error: Exception | None = None
        authority_changes: list[str] = []
        try:
            try:
                recovery_fence()
            except Exception as error:
                recovery_error = error
        finally:
            authority_changes.extend(
                restore_callback_shape(
                    self, recovery_shape
                )
            )
            for name, expected in recovery_refs.items():
                if getattr(self, name, None) is not expected:
                    authority_changes.append(name)
                setattr(self, name, expected)
            for name, expected in recovery_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    authority_changes.append(name)
                setattr(self, name, expected)
            for name, expected in recovery_budget_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    authority_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in recovery_budget_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    authority_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if authority_changes:
            raise ModelCallError(
                "recovery fence mutated orchestrator authority:"
                + ",".join(sorted(set(authority_changes)))
            )
        if recovery_error is not None:
            raise ModelCallError("recovery fence failed") from recovery_error

        attempt_id = self.attempt_id(spec)
        events = self._events(attempt_id)
        if events:
            return self._recover_existing(
                attempt_id=attempt_id,
                decision=None,
            )

        active = self.budget.active_reservation(attempt_id)
        if active is None:
            raise ModelCallError(
                "no active durable model reservation exists for recovery"
            )
        route_events = self.journal.load_events(
            "model_budget",
            self.budget.budget_id,
        )
        expected_static_context = self._reservation_context(spec, None)
        matching = [
            event
            for event in route_events
            if event.get("event_type") == "ModelRouteReserved"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("request_id") == attempt_id
        ]
        if len(matching) != 1:
            raise ModelCallError(
                "durable route reservation evidence is not unique"
            )
        routing_input = matching[0]["payload"].get("routing_input")
        durable_context = (
            routing_input.get("reservation_context")
            if isinstance(routing_input, Mapping)
            else None
        )
        expected_context_keys = set(expected_static_context) | {
            "pricing_evidence_digest",
            "pricing_valid_until",
        }
        if (
            not isinstance(durable_context, Mapping)
            or set(durable_context) != expected_context_keys
            or any(
                durable_context.get(key) != value
                for key, value in expected_static_context.items()
            )
        ):
            raise ModelCallError(
                "durable route reservation context does not match recovery spec"
            )
        _digest(
            durable_context.get("pricing_evidence_digest"),
            name="pricing_evidence_digest",
        )
        _utc_text(
            durable_context.get("pricing_valid_until"),
            name="pricing_valid_until",
        )
        recovered_context = dict(durable_context)
        payload = {
            "attempt_id": attempt_id,
            "reason": "recovered_reserved_without_call_boundary",
            "released": str(active),
            "reservation_context_hash": payload_digest(recovered_context),
        }
        self._append(
            attempt_id=attempt_id,
            event_type="ModelCallNotSent",
            version=1,
            payload=payload,
        )
        self.budget.release(attempt_id)
        return self._outcome_from_terminal(
            self._events(attempt_id)[-1],
            route=None,
        )

    def reconcile_billing(
        self,
        *,
        attempt_id: str,
        billing_id: str,
        expected_billed: object | None = None,
    ) -> bool:
        """Reconcile independently authenticated billing for OBSERVED or UNKNOWN calls.

        The billing resolver, not the caller, is the source of truth for the
        billed amount. expected_billed is assertion-only and is never presented
        to the resolver.
        """

        return self._reconcile_billing(
            attempt_id=attempt_id,
            billing_id=billing_id,
            expected_billed=expected_billed,
            observed_only=False,
        )

    def reconcile_observed_billing(
        self,
        *,
        attempt_id: str,
        billing_id: str,
        expected_billed: object | None = None,
    ) -> bool:
        """Observed-call-only billing reconciliation with evidence-derived amount."""

        return self._reconcile_billing(
            attempt_id=attempt_id,
            billing_id=billing_id,
            expected_billed=expected_billed,
            observed_only=True,
        )

    def _reconcile_billing(
        self,
        *,
        attempt_id: str,
        billing_id: str,
        expected_billed: object | None,
        observed_only: bool,
    ) -> bool:
        attempt = _canonical_text(attempt_id, name="attempt_id")
        billing = _canonical_text(billing_id, name="billing_id")
        if type(observed_only) is not bool:
            raise TypeError("observed_only must be boolean")

        expected: Decimal | None = None
        if expected_billed is not None:
            expected = _exact_decimal(expected_billed, name="expected_billed")

        events = self._events(attempt)
        terminal = next(
            (
                event
                for event in reversed(events)
                if event.get("event_type")
                in {"ModelCallObserved", "ModelCallUnknown", "ModelCallNotSent"}
            ),
            None,
        )
        if terminal is None:
            raise ModelCallError(
                "billing reconciliation requires a terminal model call"
            )

        terminal_type = terminal.get("event_type")
        if terminal_type == "ModelCallNotSent":
            raise ModelCallError(
                "billing reconciliation is forbidden for a proven NOT_SENT call"
            )
        if observed_only and terminal_type != "ModelCallObserved":
            raise ModelCallError(
                "billing reconciliation requires an observed model call"
            )

        terminal_payload = terminal.get("payload")
        if not isinstance(terminal_payload, Mapping):
            raise ModelCallError("durable terminal model-call payload is invalid")

        prepared = next(
            (
                event
                for event in events
                if event.get("event_type") == "ModelCallPrepared"
            ),
            None,
        )
        if prepared is None or not isinstance(prepared.get("payload"), Mapping):
            raise ModelCallError(
                "billing reconciliation lacks durable prepared scope"
            )
        prepared_payload = prepared["payload"]

        if terminal_type == "ModelCallObserved":
            if terminal_payload.get("billing_id") != billing:
                raise ModelCallError(
                    "billing identity does not match the observed model call"
                )
            scope_name = "observed model call"
        else:
            scope_name = "UNKNOWN model call"

        # Give evidence resolvers a detached, immutable scope. A callback may
        # inspect durable authority but cannot mutate the object subsequently
        # used for the acceptance comparison.
        billing_scope = {
            "attempt_id": attempt,
            "request_id": prepared_payload.get("request_id"),
            "request_identity_digest": prepared_payload.get(
                "request_identity_digest"
            ),
            "budget_id": prepared_payload.get("budget_id"),
            "environment": prepared_payload.get("environment"),
            "provider_id": prepared_payload.get("provider_id"),
            "model_id": prepared_payload.get("model_id"),
            "revision": prepared_payload.get("revision"),
            "remote": prepared_payload.get("remote"),
            "pricing_evidence_id": prepared_payload.get("pricing_evidence_id"),
            "pricing_evidence_digest": prepared_payload.get(
                "pricing_evidence_digest"
            ),
            "cost_currency": prepared_payload.get("cost_currency"),
            "terminal_state": (
                "OBSERVED" if terminal_type == "ModelCallObserved" else "UNKNOWN"
            ),
            "provider_request_id": (
                terminal_payload.get("provider_request_id")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "provider_response_id": (
                terminal_payload.get("provider_response_id")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "usage_id": (
                terminal_payload.get("usage_id")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "observation_digest": (
                terminal_payload.get("observation_digest")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "observation_evidence_id": (
                terminal_payload.get("observation_evidence_id")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "observation_evidence_digest": (
                terminal_payload.get("observation_evidence_digest")
                if terminal_type == "ModelCallObserved"
                else None
            ),
            "observation_evidence_issuer": (
                terminal_payload.get("observation_evidence_issuer")
                if terminal_type == "ModelCallObserved"
                else None
            ),
        }
        try:
            frozen_scope = MappingProxyType(
                json.loads(canonical_json(billing_scope))
            )
        except (TypeError, ValueError) as error:
            raise ModelCallError(
                "durable billing scope cannot be canonicalized"
            ) from error

        resolver = self.billing_evidence_resolver
        callback_refs = {
            "budget": self.budget,
            "journal": self.journal,
            "clock": self.clock,
            "pricing_evidence_resolver": self.pricing_evidence_resolver,
            "observation_evidence_resolver": self.observation_evidence_resolver,
            "billing_evidence_resolver": self.billing_evidence_resolver,
        }
        callback_values = {
            "started_lease_seconds": self.started_lease_seconds,
            "owner_token": self.owner_token,
        }
        restore_callback_shape = DurableModelCallOrchestrator._restore_callback_shape
        callback_shape = self._callback_shape_snapshot()
        budget_callback_refs = {
            "journal": self.budget.journal,
            "_clock": self.budget._clock,
        }
        budget_callback_values = {
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "_ceiling": self.budget._ceiling,
        }
        callback_error: Exception | None = None
        callback_changes: list[str] = []
        try:
            try:
                evidence = resolver(
                    attempt,
                    billing,
                    frozen_scope,
                )
            except Exception as error:
                callback_error = error
        finally:
            callback_changes.extend(
                restore_callback_shape(
                    self, callback_shape
                )
            )
            for name, expected in callback_refs.items():
                if getattr(self, name, None) is not expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in callback_values.items():
                current = getattr(self, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append(name)
                setattr(self, name, expected)
            for name, expected in budget_callback_refs.items():
                if getattr(self.budget, name, None) is not expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
            for name, expected in budget_callback_values.items():
                current = getattr(self.budget, name, None)
                if type(current) is not type(expected) or current != expected:
                    callback_changes.append("budget." + name)
                setattr(self.budget, name, expected)
        if callback_changes:
            raise ModelCallError(
                "billing evidence resolver mutated orchestrator authority:"
                + ",".join(sorted(set(callback_changes)))
            )
        if callback_error is not None:
            raise ModelCallError(
                "billing evidence could not be authenticated"
            ) from callback_error
        if type(evidence) is not BillingEvidence:
            raise ModelCallError(
                "billing evidence resolver did not return BillingEvidence"
            )
        evidence = BillingEvidence(**{
            field.name: getattr(evidence, field.name) for field in fields(BillingEvidence)
        })
        if (
            evidence.attempt_id != attempt
            or evidence.billing_id != billing
            or evidence.provider_id != billing_scope.get("provider_id")
            or evidence.model_id != billing_scope.get("model_id")
            or evidence.revision != billing_scope.get("revision")
            or evidence.cost_currency != billing_scope.get("cost_currency")
        ):
            raise ModelCallError(
                f"billing evidence scope does not match the {scope_name}"
            )
        if expected is not None and evidence.billed != expected:
            raise ModelCallError(
                "billing evidence amount does not match caller expectation"
            )

        evidence_payload = {
            "attempt_id": attempt,
            "billing_id": billing,
            "provider_id": evidence.provider_id,
            "model_id": evidence.model_id,
            "revision": evidence.revision,
            "billed": str(evidence.billed),
            "cost_currency": evidence.cost_currency,
            "observed_at": evidence.observed_at,
            "evidence_id": evidence.evidence_id,
            "evidence_digest": evidence.evidence_digest,
            "issuer": evidence.issuer,
        }
        prior = [
            event
            for event in events
            if event.get("event_type") == "ModelBillingEvidenceObserved"
            and isinstance(event.get("payload"), Mapping)
            and event["payload"].get("billing_id") == billing
        ]
        if prior:
            if len(prior) != 1 or prior[0].get("payload") != evidence_payload:
                raise ModelCallError(
                    "billing identity was reused with conflicting immutable evidence"
                )
        else:
            self._append(
                attempt_id=attempt,
                event_type="ModelBillingEvidenceObserved",
                version=len(events) + 1,
                payload=evidence_payload,
            )

        return self.budget.reconcile_unbilled(
            billing_id=billing,
            request_id=attempt,
            billed=evidence.billed,
        )
