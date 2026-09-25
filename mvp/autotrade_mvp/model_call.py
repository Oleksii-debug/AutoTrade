"""Durable production model-call lifecycle over the canonical model budget.

This module composes DurableModelBudget and JournalStore. It does not own a
second router, budget ledger, job scheduler, financial authority, credential
store, or trading sender. Its only authority is the inference-call boundary:
one deterministic semantic attempt may cross that boundary at most once.

Remote/local adapters are injected. A durable ModelCallStarted event is written
before adapter invocation. After that point an exception is conservatively
UNKNOWN and the full reserved ceiling becomes estimated-unbilled until billing
evidence resolves it. A provider adapter may raise ModelCallNotSent only when it
can prove the external/local inference boundary was not crossed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
from typing import Callable, Iterable, Mapping
from uuid import uuid4

from .model_budget_journal import DurableModelBudget
from .model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteDecision,
    RouteStatus,
    RoutingPolicy,
)
from .persistence import canonical_json, payload_digest


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_AGGREGATE_TYPE = "model_call_attempt"


class ModelCallError(RuntimeError):
    """Base error for the durable model-call boundary."""


class ModelCallNotSent(ModelCallError):
    """Adapter proof that no inference boundary was crossed."""


def _canonical_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value:
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
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
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
            not isinstance(self.fallback_index, int)
            or isinstance(self.fallback_index, bool)
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
    pricing_as_of: str
    result_schema_id: str
    fallback_parent_attempt_id: str | None
    fallback_index: int


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
        started_lease_seconds: int = 60,
        owner_token: str | None = None,
    ) -> None:
        if not isinstance(budget, DurableModelBudget):
            raise TypeError("budget must be DurableModelBudget")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if (
            not isinstance(started_lease_seconds, int)
            or isinstance(started_lease_seconds, bool)
            or started_lease_seconds < 1
            or started_lease_seconds > 3600
        ):
            raise ValueError(
                "started_lease_seconds must be an integer from 1 through 3600"
            )
        self.budget = budget
        self.journal = budget.journal
        self.clock = clock
        self.started_lease_seconds = started_lease_seconds
        self.owner_token = (
            _canonical_text(owner_token, name="owner_token")
            if owner_token is not None
            else "model-owner-" + uuid4().hex
        )

    def _now(self) -> str:
        return _utc_text(self.clock(), name="clock")

    def attempt_id(self, spec: ModelCallSpec) -> str:
        if not isinstance(spec, ModelCallSpec):
            raise TypeError("spec must be ModelCallSpec")
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

    def _reservation_context(self, spec: ModelCallSpec) -> dict[str, str]:
        context = {
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "pricing_evidence_id": spec.pricing_evidence_id,
            "pricing_as_of": spec.pricing_as_of,
            "result_schema_id": spec.result_schema_id,
            "fallback_index": str(spec.fallback_index),
        }
        if spec.fallback_parent_attempt_id is not None:
            context["fallback_parent_attempt_id"] = spec.fallback_parent_attempt_id
        return context

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
    ) -> dict[str, object]:
        context = self._reservation_context(spec)
        return {
            "attempt_id": attempt_id,
            "request_id": attempt_id,
            "budget_id": self.budget.budget_id,
            "environment": self.budget.environment,
            "job_id": spec.job_id,
            "input_digest": spec.input_digest,
            "policy_id": spec.policy_id,
            "pricing_evidence_id": spec.pricing_evidence_id,
            "pricing_as_of": spec.pricing_as_of,
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
        if last.get("event_type") in {
            "ModelCallNotSent",
            "ModelCallUnknown",
            "ModelCallObserved",
        }:
            self._ensure_terminal_budget(last)
            return self._outcome_from_terminal(last, route=decision)
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
            pricing_as_of=spec.pricing_as_of,
            result_schema_id=spec.result_schema_id,
            fallback_parent_attempt_id=spec.fallback_parent_attempt_id,
            fallback_index=spec.fallback_index,
        )

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
        if not isinstance(spec, ModelCallSpec):
            raise TypeError("spec must be ModelCallSpec")
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")
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

        existing = self._events(attempt_id)
        if existing and existing[-1].get("event_type") in {
            "ModelCallNotSent",
            "ModelCallUnknown",
            "ModelCallObserved",
        }:
            return self._recover_existing(
                attempt_id=attempt_id,
                decision=None,
            )

        materialized = tuple(descriptors)
        decision = self.budget.admit_route(
            policy,
            request,
            materialized,
            now_utc=now_utc,
            reservation_context=self._reservation_context(spec),
        )
        if decision.status is not RouteStatus.ADMITTED:
            return ModelCallOutcome(
                decision.status.value,
                attempt_id,
                decision,
                decision.reason,
            )
        descriptor = self._selected_descriptor(decision, materialized)
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
        )
        events = self._events(attempt_id)
        if events:
            first = events[0]
            if first.get("event_type") == "ModelCallNotSent":
                return self._recover_existing(
                    attempt_id=attempt_id,
                    decision=decision,
                )
            if (
                first.get("event_type") != "ModelCallPrepared"
                or first.get("payload") != prepared_payload
            ):
                raise ModelCallError(
                    "model-call identity conflicts with durable prepared attempt"
                )
            if len(events) > 1:
                return self._recover_existing(
                    attempt_id=attempt_id,
                    decision=decision,
                )
        else:
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallPrepared",
                version=1,
                payload=prepared_payload,
            )

        cancelled = cancel_requested or (lambda: False)
        if cancelled():
            payload = {
                "attempt_id": attempt_id,
                "reason": "cancelled_before_call_boundary",
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

        binding = self._binding(
            attempt_id=attempt_id,
            spec=spec,
            decision=decision,
            descriptor=descriptor,
        )
        try:
            observation = call(binding, cancelled)
        except ModelCallNotSent as error:
            payload = {
                "attempt_id": attempt_id,
                "reason": "adapter_proved_not_sent:" + str(error),
                "released": str(decision.reserved_cost),
            }
            self._append(
                attempt_id=attempt_id,
                event_type="ModelCallNotSent",
                version=3,
                payload=payload,
            )
            self.budget.release(attempt_id)
            return self._outcome_from_terminal(
                self._events(attempt_id)[-1],
                route=decision,
            )
        except Exception as error:
            payload = {
                "attempt_id": attempt_id,
                "reason": "call_boundary_result_ambiguous:"
                + type(error).__name__,
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

        if not isinstance(observation, ModelCallObservation):
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

        total = observation.incurred_cost + observation.estimated_unbilled
        if total > decision.reserved_cost:
            # Do not pretend an over-ceiling observation is safely settled.
            # Preserve the full declared reservation as uncertain and retain the
            # observed overrun in durable diagnostic evidence for qualification.
            payload = {
                "attempt_id": attempt_id,
                "reason": "observed_cost_exceeds_reserved_ceiling",
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

        try:
            schema_valid = validate_result(observation.output)
        except Exception:
            schema_valid = False
        if type(schema_valid) is not bool:
            schema_valid = False
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
            "incurred_cost": str(observation.incurred_cost),
            "estimated_unbilled": str(observation.estimated_unbilled),
            "result_digest": result_digest,
            "result_schema_id": spec.result_schema_id,
            "schema_valid": schema_valid,
        }
        self._append(
            attempt_id=attempt_id,
            event_type="ModelCallObserved",
            version=3,
            payload=observed_payload,
        )
        self.budget.settle(
            attempt_id,
            incurred=observation.incurred_cost,
            estimated_unbilled=observation.estimated_unbilled,
        )
        return ModelCallOutcome(
            "OBSERVED_VALID" if schema_valid else "OBSERVED_INVALID",
            attempt_id,
            decision,
            "observed_response",
            result_digest=result_digest,
            output=observation.output if schema_valid else None,
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
        if not isinstance(spec, ModelCallSpec):
            raise TypeError("spec must be ModelCallSpec")
        if not callable(recovery_fence):
            raise TypeError("recovery_fence must be callable")
        recovery_fence()
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
        expected_context = self._reservation_context(spec)
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
        if (
            not isinstance(routing_input, Mapping)
            or routing_input.get("reservation_context") != expected_context
        ):
            raise ModelCallError(
                "durable route reservation context does not match recovery spec"
            )
        payload = {
            "attempt_id": attempt_id,
            "reason": "recovered_reserved_without_call_boundary",
            "released": str(active),
            "reservation_context_hash": payload_digest(expected_context),
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

    def reconcile_observed_billing(
        self,
        *,
        attempt_id: str,
        billing_id: str,
        billed: object,
    ) -> bool:
        attempt = _canonical_text(attempt_id, name="attempt_id")
        billing = _canonical_text(billing_id, name="billing_id")
        events = self._events(attempt)
        if not events or events[-1].get("event_type") != "ModelCallObserved":
            raise ModelCallError(
                "billing reconciliation requires an observed model call"
            )
        payload = events[-1].get("payload")
        if not isinstance(payload, Mapping):
            raise ModelCallError("durable observed model-call payload is invalid")
        if payload.get("billing_id") != billing:
            raise ModelCallError(
                "billing identity does not match the observed model call"
            )
        normalized = _exact_decimal(billed, name="billed")
        return self.budget.reconcile_unbilled(
            billing_id=billing,
            request_id=attempt,
            billed=normalized,
        )
