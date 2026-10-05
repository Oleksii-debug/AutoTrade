"""Journal-backed persistence for the canonical model budget projection.

The in-memory :class:`BudgetLedger` remains the single budget rule authority.
This adapter only persists accepted mutations in the canonical JournalStore and
rebuilds that projection after restart. It performs no model/network call.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

from mvp.autotrade_mvp.model_gateway import (
    BudgetLedger,
    BudgetSnapshot,
    ModelDescriptor,
    ModelRequest,
    RouteDecision,
    RouteStatus,
    RoutingPolicy,
    route_model,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from .exact_decimal import parse_bounded_exact_decimal


_AGGREGATE_TYPE = "model_budget"
_COMMAND_ACTOR = "autotrade-model-budget"
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    if type(value) is not str:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    normalized = value.strip().upper()
    if normalized not in _ENVIRONMENTS:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clock_text(value: object) -> str:
    if type(value) is not str or not value:
        raise ValueError("clock result must be canonical UTC text")
    try:
        point = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("clock result must be canonical UTC text") from error
    if point.tzinfo is None or point.utcoffset() != timedelta(0):
        raise ValueError("clock result must be canonical UTC text")
    canonical = point.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise ValueError("clock result must be canonical UTC text")
    return value


def _route_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if type(value) is not datetime or type(value.tzinfo) not in (timezone, ZoneInfo):
        raise ValueError("now_utc must be an exact timezone-aware datetime")
    return value


def _reservation_context(
    values: Mapping[str, str] | None,
) -> dict[str, str] | None:
    """Canonical immutable context that becomes part of route reservation identity.

    Omitted context preserves the exact pre-WP-39 durable payload shape for
    backward-compatible retries of existing reservations.
    """
    if values is None:
        return None
    if type(values) is not dict:
        raise TypeError("reservation_context must be an exact dict")
    snapshot = dict.copy(values)
    normalized: dict[str, str] = {}
    for raw_key, raw_value in snapshot.items():
        if type(raw_key) is not str:
            raise TypeError("reservation_context keys must be text")
        key = _text(raw_key, name="reservation_context key")
        if raw_key != key:
            raise ValueError("reservation_context keys must be canonical text")
        if type(raw_value) is not str:
            raise TypeError("reservation_context values must be text")
        value = _text(raw_value, name=f"reservation_context[{key}]")
        if raw_value != value:
            raise ValueError("reservation_context values must be canonical text")
        if key in normalized:
            raise ValueError("reservation_context keys must be unique")
        normalized[key] = value
    return dict(sorted(normalized.items()))


def _identity_digest(*parts: str) -> str:
    return sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


def _event_id(aggregate_id: str, idempotency_key: str) -> str:
    return "model-budget-" + _identity_digest(
        "event",
        aggregate_id,
        idempotency_key,
    )


def _command_id(aggregate_id: str, idempotency_key: str) -> str:
    return "model-budget-command-" + _identity_digest(
        "command",
        aggregate_id,
        idempotency_key,
    )


def _rehydrate_committed_event(event: Mapping[str, Any]) -> dict[str, Any]:
    """Restore the exact canonical envelope accepted by commit_command.

    Journal reads expose aggregate_version as an integer and add the derived
    journal_sequence.  Neither representation is part of the immutable event
    envelope hashed into an EVENT_BATCH effect.
    """

    if not isinstance(event, Mapping):
        raise TypeError("durable event must be a mapping")
    version = event.get("aggregate_version")
    if type(version) is not int or version <= 0:
        raise ValueError("durable event aggregate_version must be positive")
    envelope = {
        key: value
        for key, value in event.items()
        if key != "journal_sequence"
    }
    envelope["aggregate_version"] = str(version)
    return envelope


def _idempotency_key(*, budget_id: str, action: str, identity: str) -> str:
    return "model-budget:" + action + ":" + _identity_digest(
        "idempotency",
        budget_id,
        action,
        identity,
    )


class DurableModelBudget:
    """Durable adapter over the canonical model-cost BudgetLedger.

    Mutations are validated against a replayed projection first, then committed
    atomically to JournalStore. The public snapshot is always rebuilt from the
    durable journal, so a process death after commit cannot silently erase cost.
    """

    def __init__(
        self,
        *,
        journal: JournalStore,
        budget_id: str,
        ceiling,
        environment: str,
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        self.journal = journal
        self.budget_id = _text(budget_id, name="budget_id")
        self.environment = _environment(environment)
        if clock is not None and not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = _now if clock is None else clock

        candidate = BudgetLedger(ceiling)
        self._ceiling = candidate.snapshot().ceiling
        existing = self.journal.load_events(_AGGREGATE_TYPE, self.budget_id)
        if not existing:
            payload = {
                "ceiling": str(self._ceiling),
                "environment": self.environment,
            }
            envelope = self._envelope(
                event_type="ModelBudgetInitialized",
                version=1,
                payload=payload,
                event_id=_event_id(self.budget_id, "initialize"),
            )
            try:
                self.journal.append_event(envelope)
            except ValueError:
                concurrent = self.journal.get_event(envelope["event_id"])
                if (
                    concurrent is None
                    or concurrent["event_type"] != "ModelBudgetInitialized"
                    or concurrent["aggregate_type"] != _AGGREGATE_TYPE
                    or concurrent["aggregate_id"] != self.budget_id
                    or concurrent["aggregate_version"] != 1
                    or concurrent["payload"] != payload
                ):
                    raise

        rebuilt = self._replay()
        if rebuilt.snapshot().ceiling != self._ceiling:
            raise ValueError("budget ceiling conflicts with durable model budget")

    def _events(self) -> list[dict[str, Any]]:
        return self.journal.load_events(_AGGREGATE_TYPE, self.budget_id)

    @staticmethod
    def _safe_authority_state(
        value: object,
        *,
        subject: str,
    ) -> dict[str, object]:
        try:
            state = object.__getattribute__(value, "__dict__")
        except (AttributeError, TypeError) as error:
            raise ValueError(subject + " authority state is unavailable") from error
        if type(state) is not dict:
            raise ValueError(subject + " authority state is invalid")
        names = tuple(state)
        if any(type(name) is not str for name in names):
            raise ValueError(subject + " authority state keys are invalid")
        return dict.copy(state)

    def _clock_authority_snapshot(
        self,
    ) -> tuple[
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
        """Freeze budget/journal authority before executing the injected clock."""
        budget_class = object.__getattribute__(self, "__class__")
        budget_state = DurableModelBudget._safe_authority_state(
            self,
            subject="model budget",
        )
        journal = budget_state.get("journal")
        if not isinstance(journal, JournalStore):
            raise ValueError("model budget journal authority is invalid")
        journal_class = object.__getattribute__(journal, "__class__")
        journal_state = DurableModelBudget._safe_authority_state(
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
            DurableModelBudget._safe_authority_state(
                identity,
                subject="model budget journal identity",
            )
            if identity is not None
            else None
        )
        authority_classes: list[type] = []
        seen_class_ids: set[int] = set()
        for selected_class in (budget_class, journal_class, identity_class):
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
                tuple(authority_class.__bases__),
                MappingProxyType(dict(vars(authority_class))),
            )
            for authority_class in authority_classes
        )
        return (
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
    def _restore_clock_authority(
        self: "DurableModelBudget",
        snapshot: tuple[
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
    ) -> list[str]:
        """Restore budget/journal authority without rebound-class dispatch."""
        (
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
            "DurableModelBudget",
        )
        if current_module_alias is not budget_class:
            changes.append("module.DurableModelBudget")
            dict.__setitem__(
                module_globals,
                "DurableModelBudget",
                budget_class,
            )

        for (
            authority_class,
            class_label,
            expected_bases,
            expected_class_state,
        ) in class_authority:
            current_bases = tuple(authority_class.__bases__)
            bases_changed = (
                len(current_bases) != len(expected_bases)
                or any(
                    current is not expected
                    for current, expected in zip(current_bases, expected_bases)
                )
            )
            if bases_changed:
                label = "class." + class_label + ".__bases__"
                changes.append(label)
                try:
                    type.__setattr__(authority_class, "__bases__", expected_bases)
                except TypeError as error:
                    raise ValueError(
                        label + " could not be restored after model budget clock"
                    ) from error

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
                        raise ValueError(
                            label + " could not be removed after model budget clock"
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
                        raise ValueError(
                            label + " could not be restored after model budget clock"
                        ) from error
            restored_bases = tuple(authority_class.__bases__)
            if (
                len(restored_bases) != len(expected_bases)
                or any(
                    current is not expected
                    for current, expected in zip(restored_bases, expected_bases)
                )
            ):
                raise ValueError(
                    "model budget class base authority restore is incomplete for "
                    + class_label
                )
            restored_class_state = vars(authority_class)
            if set(restored_class_state) != expected_names or any(
                restored_class_state[name] is not expected_class_state[name]
                for name in expected_names
            ):
                raise ValueError(
                    "model budget class authority restore is incomplete for "
                    + class_label
                )

        def restore_class(value: object, expected: type, label: str) -> None:
            current = object.__getattribute__(value, "__class__")
            if current is expected:
                return
            changes.append(label)
            try:
                object.__setattr__(value, "__class__", expected)
            except (AttributeError, TypeError) as error:
                raise ValueError(label + " could not be restored") from error

        def restore_state(
            value: object,
            expected: dict[str, object],
            prefix: str,
        ) -> None:
            current = object.__getattribute__(value, "__dict__")
            if type(current) is not dict:
                raise ValueError(prefix + " authority state is invalid")
            keys = tuple(current)
            if any(type(name) is not str for name in keys):
                changes.append(prefix + ".<invalid-state-key>")
            current_names = {name for name in keys if type(name) is str}
            expected_names = set(expected)
            for name in sorted(current_names | expected_names):
                label = prefix + "." + name
                if name not in current or name not in expected:
                    changes.append(label)
                    continue
                current_value = current[name]
                expected_value = expected[name]
                if type(expected_value) in (str, int, bool, Decimal, type(None)):
                    changed = (
                        type(current_value) is not type(expected_value)
                        or current_value != expected_value
                    )
                else:
                    changed = current_value is not expected_value
                if changed:
                    changes.append(label)
            dict.clear(current)
            dict.update(current, expected)

        restore_class(self, budget_class, "budget.__class__")
        restore_class(journal, journal_class, "journal.__class__")
        if identity is not None and identity_class is not None:
            restore_class(
                identity,
                identity_class,
                "journal._store_identity.__class__",
            )

        if identity is not None and identity_state is not None:
            restore_state(
                identity,
                identity_state,
                "journal._store_identity",
            )
        restore_state(journal, journal_state, "journal")
        restore_state(self, budget_state, "budget")
        return changes

    def _clock_now(self) -> str:
        restore_clock_authority = DurableModelBudget._restore_clock_authority
        snapshot = DurableModelBudget._clock_authority_snapshot(self)
        clock = snapshot[1].get("_clock")
        if not callable(clock):
            raise ValueError("model budget clock authority is invalid")

        clock_error: Exception | None = None
        clock_value: object = None
        try:
            clock_value = clock()
        except Exception as error:
            clock_error = error
        finally:
            changes = restore_clock_authority(
                self,
                snapshot,
            )

        if changes:
            error = ValueError(
                "model budget clock mutated authority:"
                + ",".join(sorted(set(changes)))
            )
            if clock_error is not None:
                raise error from clock_error
            raise error
        if clock_error is not None:
            raise clock_error
        return _clock_text(clock_value)

    def _envelope(
        self,
        *,
        event_type: str,
        version: int,
        payload: dict[str, Any],
        event_id: str,
    ) -> dict[str, Any]:
        return {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.budget_id,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": self._clock_now(),
        }

    @staticmethod
    def _apply(ledger: BudgetLedger, event: dict[str, Any]) -> None:
        event_type = event["event_type"]
        payload = event["payload"]
        if event_type == "ModelBudgetInitialized":
            return
        if event_type in {"ModelCostReserved", "ModelRouteReserved"}:
            ledger.reserve(payload["request_id"], payload["amount"])
            return
        if event_type == "ModelCostReleased":
            released = ledger.release(payload["request_id"])
            if str(released) != payload["released"]:
                raise ValueError("durable model budget release evidence is inconsistent")
            return
        if event_type == "ModelCostSettled":
            ledger.settle(
                payload["request_id"],
                incurred=payload["incurred"],
                estimated_unbilled=payload["estimated_unbilled"],
            )
            return
        if event_type == "ModelUnbilledReconciled":
            ledger.reconcile_unbilled(
                billing_id=payload["billing_id"],
                request_id=payload["request_id"],
                billed=payload["billed"],
            )
            return
        raise ValueError(f"unsupported durable model budget event: {event_type}")

    def _replay(self) -> BudgetLedger:
        return self._replay_events(self._events())

    def _replay_events(self, events: list[dict[str, Any]]) -> BudgetLedger:
        if not events or events[0]["event_type"] != "ModelBudgetInitialized":
            raise ValueError("durable model budget initialization is missing")
        initialization = events[0].get("payload")
        if not isinstance(initialization, dict):
            raise ValueError("durable model budget initialization payload is invalid")
        durable_environment = initialization.get("environment")
        if durable_environment is None:
            raise ValueError(
                "legacy model budget lacks durable environment binding"
            )
        if _environment(durable_environment) != self.environment:
            raise ValueError(
                "budget environment conflicts with durable model budget"
            )
        ceiling = initialization.get("ceiling")
        ledger = BudgetLedger(ceiling)
        for expected_version, event in enumerate(events, start=1):
            if event["aggregate_version"] != expected_version:
                raise ValueError("durable model budget event sequence is not contiguous")
            self._apply(ledger, event)
        return ledger

    def snapshot(self) -> BudgetSnapshot:
        return self._replay().snapshot()

    def active_reservation(self, request_id: str) -> Decimal | None:
        request = _text(request_id, name="request_id")
        active: Decimal | None = None
        for event in self._events():
            payload = event.get("payload")
            if not isinstance(payload, dict) or payload.get("request_id") != request:
                continue
            event_type = event.get("event_type")
            if event_type in {"ModelCostReserved", "ModelRouteReserved"}:
                try:
                    amount = parse_bounded_exact_decimal(payload["amount"])
                except (KeyError, ValueError, TypeError) as error:
                    raise ValueError(
                        "durable model reservation amount is invalid"
                    ) from error
                if not amount.is_finite() or amount < 0:
                    raise ValueError(
                        "durable model reservation amount is invalid"
                    )
                active = amount
            elif event_type in {"ModelCostReleased", "ModelCostSettled"}:
                active = None
        return active

    def _commit(
        self,
        *,
        action: str,
        identity: str,
        request: dict[str, Any],
        event_type: str,
        payload: dict[str, Any],
        result: dict[str, Any],
        validate: Callable[[BudgetLedger], None],
    ) -> bool:
        identity = _text(identity, name="identity")
        idempotency_key = _idempotency_key(
            budget_id=self.budget_id,
            action=action,
            identity=identity,
        )
        command_id = _command_id(self.budget_id, idempotency_key)
        event_id = _event_id(self.budget_id, idempotency_key)

        existing = self.journal.get_event(event_id)
        if existing is not None:
            if (
                existing["event_type"] != event_type
                or existing["aggregate_type"] != _AGGREGATE_TYPE
                or existing["aggregate_id"] != self.budget_id
                or existing["payload"] != payload
            ):
                raise ValueError(
                    "model budget idempotency identity conflicts with durable event"
                )
            original_envelope = _rehydrate_committed_event(existing)
            saved_result, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=int(existing["aggregate_version"]),
                events=[(original_envelope, None)],
            )
            if inserted:
                raise ValueError(
                    "existing model budget event unexpectedly inserted on replay"
                )
            if saved_result != result:
                raise ValueError(
                    "model budget command result conflicts with durable event"
                )
            return False

        frozen_events = self._events()
        ledger = self._replay_events(frozen_events)
        validate(ledger)
        # Validation and aggregate CAS must describe one immutable journal cut.
        # Rereading the version after validation could accept a stale budget
        # projection under a newer version and permanently overbook the ceiling.
        version = len(frozen_events) + 1
        envelope = self._envelope(
            event_type=event_type,
            version=version,
            payload=payload,
            event_id=event_id,
        )
        try:
            saved_result, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=version,
                events=[(envelope, None)],
            )
            if not inserted and saved_result != result:
                raise ValueError(
                    "model budget command result conflicts with durable event"
                )
            return inserted
        except ValueError as error:
            if "aggregate_version must be" not in str(error):
                raise
            frozen_events = self._events()
            ledger = self._replay_events(frozen_events)
            validate(ledger)
            version = len(frozen_events) + 1
            envelope = self._envelope(
                event_type=event_type,
                version=version,
                payload=payload,
                event_id=event_id,
            )
            saved_result, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=version,
                events=[(envelope, None)],
            )
            if not inserted and saved_result != result:
                raise ValueError(
                    "model budget command result conflicts with durable event"
                )
            return inserted

    @staticmethod
    def _routing_input(
        policy: RoutingPolicy,
        request: ModelRequest,
        descriptors: tuple[ModelDescriptor, ...],
        reservation_context: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        if type(policy) is not RoutingPolicy:
            raise TypeError("policy must be exact RoutingPolicy")
        if type(request) is not ModelRequest:
            raise TypeError("request must be exact ModelRequest")
        if not all(type(item) is ModelDescriptor for item in descriptors):
            raise TypeError("descriptors must contain exact ModelDescriptor values")
        material = {
            "policy": {
                "mode": policy.mode.value,
                "allowed_model_ids": list(policy.allowed_model_ids),
                "fixed_model_id": policy.fixed_model_id,
                "allow_remote": policy.allow_remote,
                "maximum_cost": str(policy.maximum_cost),
                "maximum_latency_ms": policy.maximum_latency_ms,
            },
            "request": {
                "request_id": request.request_id,
                "allowed_model_ids": list(request.allowed_model_ids),
                "privacy_remote_allowed": request.privacy_remote_allowed,
                "budget_cap": str(request.budget_remaining),
                "deadline_utc": request.deadline_utc.isoformat(),
                "cancelled": request.cancelled,
            },
            "descriptors": [
                {
                    "model_id": item.model_id,
                    "provider_id": item.provider_id,
                    "revision": item.revision,
                    "remote": item.remote,
                    "estimated_cost": str(item.estimated_cost),
                    "latency_ms": item.latency_ms,
                    "quality_score": str(item.quality_score),
                }
                for item in sorted(descriptors, key=lambda value: value.model_id)
            ],
        }
        context = _reservation_context(reservation_context)
        if context is not None:
            material["reservation_context"] = context
        return material

    @staticmethod
    def _decision_from_route_payload(payload: dict[str, Any]) -> RouteDecision:
        try:
            status = RouteStatus(payload["route_status"])
            amount = parse_bounded_exact_decimal(payload["amount"])
        except (KeyError, ValueError, TypeError) as error:
            raise ValueError("durable model route payload is invalid") from error
        if status is not RouteStatus.ADMITTED:
            raise ValueError("durable model route reservation must be ADMITTED")
        return RouteDecision(
            status=status,
            model_id=_text(payload.get("model_id"), name="model_id"),
            provider_id=_text(payload.get("provider_id"), name="provider_id"),
            revision=(
                _text(payload["revision"], name="revision")
                if payload.get("revision") is not None
                else None
            ),
            reserved_cost=amount,
            reason="admitted_durable_budget",
        )

    def admit_route(
        self,
        policy: RoutingPolicy,
        request: ModelRequest,
        descriptors: Iterable[ModelDescriptor],
        *,
        now_utc: datetime | None = None,
        reservation_context: Mapping[str, str] | None = None,
    ) -> RouteDecision:
        if type(policy) is not RoutingPolicy:
            raise TypeError("policy must be exact RoutingPolicy")
        if type(request) is not ModelRequest:
            raise TypeError("request must be exact ModelRequest")

        # Caller-owned frozen dataclasses remain mutable through object.__setattr__
        # and may be changed by another thread while journal reads occur below.
        # Detach scalar routing authority before touching optional model inventory.
        policy = RoutingPolicy(
            mode=policy.mode,
            allowed_model_ids=policy.allowed_model_ids,
            fixed_model_id=policy.fixed_model_id,
            allow_remote=policy.allow_remote,
            maximum_cost=policy.maximum_cost,
            maximum_latency_ms=policy.maximum_latency_ms,
        )
        request = ModelRequest(
            request_id=request.request_id,
            allowed_model_ids=request.allowed_model_ids,
            privacy_remote_allowed=request.privacy_remote_allowed,
            budget_remaining=request.budget_remaining,
            deadline_utc=request.deadline_utc,
            cancelled=request.cancelled,
        )
        now = _route_now(now_utc)

        # Preserve the pure router's inventory-free short circuits end-to-end.
        # These outcomes cannot reserve cost, so descriptor and reservation
        # context are semantically irrelevant and must not be enumerated.
        if (
            request.cancelled
            or now >= request.deadline_utc
            or getattr(policy.mode, "value", None) == "ZERO"
        ):
            return route_model(policy, request, (), now_utc=now)

        materialized = tuple(descriptors)
        if any(type(item) is not ModelDescriptor for item in materialized):
            raise TypeError("descriptors must contain exact ModelDescriptor values")
        materialized = tuple(
            ModelDescriptor(
                model_id=item.model_id,
                provider_id=item.provider_id,
                revision=item.revision,
                remote=item.remote,
                estimated_cost=item.estimated_cost,
                latency_ms=item.latency_ms,
                quality_score=item.quality_score,
            )
            for item in materialized
        )
        routing_input = self._routing_input(
            policy,
            request,
            materialized,
            reservation_context=reservation_context,
        )
        idempotency_key = _idempotency_key(
            budget_id=self.budget_id,
            action="route_reserve",
            identity=request.request_id,
        )
        event_id = _event_id(self.budget_id, idempotency_key)
        existing = self.journal.get_event(event_id)
        if existing is not None:
            payload = existing.get("payload")
            if (
                existing.get("event_type") != "ModelRouteReserved"
                or not isinstance(payload, dict)
                or payload.get("routing_input") != routing_input
            ):
                raise ValueError(
                    "model route identity conflicts with durable admission"
                )
            decision = self._decision_from_route_payload(payload)
            self._commit(
                action="route_reserve",
                identity=request.request_id,
                request={"routing_input": routing_input},
                event_type="ModelRouteReserved",
                payload=payload,
                result={
                    "status": decision.status.value,
                    "model_id": decision.model_id,
                    "provider_id": decision.provider_id,
                    "revision": decision.revision,
                    "reserved_cost": str(decision.reserved_cost),
                },
                validate=lambda _ledger: None,
            )
            return decision

        durable_available = self.snapshot().available
        effective_budget = min(request.budget_remaining, durable_available)
        authoritative_request = ModelRequest(
            request_id=request.request_id,
            allowed_model_ids=request.allowed_model_ids,
            privacy_remote_allowed=request.privacy_remote_allowed,
            budget_remaining=effective_budget,
            deadline_utc=request.deadline_utc,
            cancelled=request.cancelled,
        )
        decision = route_model(
            policy,
            authoritative_request,
            materialized,
            now_utc=now,
        )
        if decision.status is not RouteStatus.ADMITTED:
            return decision

        payload = {
            "request_id": request.request_id,
            "amount": str(decision.reserved_cost),
            "route_status": decision.status.value,
            "model_id": decision.model_id,
            "provider_id": decision.provider_id,
            "revision": decision.revision,
            "routing_input": routing_input,
        }

        def validate(ledger: BudgetLedger) -> None:
            ledger.reserve(request.request_id, decision.reserved_cost)

        try:
            self._commit(
                action="route_reserve",
                identity=request.request_id,
                request={"routing_input": routing_input},
                event_type="ModelRouteReserved",
                payload=payload,
                result={
                    "status": decision.status.value,
                    "model_id": decision.model_id,
                    "provider_id": decision.provider_id,
                    "revision": decision.revision,
                    "reserved_cost": str(decision.reserved_cost),
                },
                validate=validate,
            )
        except ValueError as error:
            if "budget exhausted" not in str(error):
                raise
            return RouteDecision(
                RouteStatus.NO_MODEL,
                None,
                None,
                None,
                Decimal("0"),
                "durable_budget_exhausted",
            )
        return RouteDecision(
            decision.status,
            decision.model_id,
            decision.provider_id,
            decision.revision,
            decision.reserved_cost,
            "admitted_durable_budget",
        )

    def reserve(self, request_id: str, amount) -> bool:
        request_id = _text(request_id, name="request_id")
        probe = BudgetLedger(self._ceiling)
        probe.reserve("probe", amount)
        normalized_amount = probe.snapshot().reserved
        request = {"request_id": request_id, "amount": str(normalized_amount)}

        def validate(ledger: BudgetLedger) -> None:
            ledger.reserve(request_id, normalized_amount)

        return self._commit(
            action="reserve",
            identity=request_id,
            request=request,
            event_type="ModelCostReserved",
            payload=request,
            result={"reserved": True, **request},
            validate=validate,
        )

    def release(self, request_id: str) -> bool:
        request_id = _text(request_id, name="request_id")
        request = {"request_id": request_id}
        idempotency_key = _idempotency_key(
            budget_id=self.budget_id,
            action="release",
            identity=request_id,
        )
        existing = self.journal.get_event(
            _event_id(self.budget_id, idempotency_key)
        )
        if existing is not None:
            payload = existing.get("payload")
            if (
                existing.get("event_type") != "ModelCostReleased"
                or existing.get("aggregate_type") != _AGGREGATE_TYPE
                or existing.get("aggregate_id") != self.budget_id
                or not isinstance(payload, dict)
                or payload.get("request_id") != request_id
            ):
                raise ValueError(
                    "model budget release identity conflicts with durable event"
                )
            released_text = payload.get("released")
            try:
                released = parse_bounded_exact_decimal(released_text)
            except (ValueError, TypeError) as error:
                raise ValueError(
                    "durable model budget release evidence is inconsistent"
                ) from error
            if (
                not released.is_finite()
                or released <= 0
                or str(released) != released_text
            ):
                raise ValueError(
                    "durable model budget release evidence is inconsistent"
                )
            return self._commit(
                action="release",
                identity=request_id,
                request=request,
                event_type="ModelCostReleased",
                payload=payload,
                result={"released": released_text},
                validate=lambda _ledger: None,
            )

        ledger = self._replay()
        released = ledger.release(request_id)
        if released == 0:
            return False

        def validate(candidate: BudgetLedger) -> None:
            candidate_released = candidate.release(request_id)
            if candidate_released != released:
                raise ValueError(
                    "model budget release amount changed before commit"
                )

        return self._commit(
            action="release",
            identity=request_id,
            request=request,
            event_type="ModelCostReleased",
            payload={"request_id": request_id, "released": str(released)},
            result={"released": str(released)},
            validate=validate,
        )

    def settle(self, request_id: str, *, incurred, estimated_unbilled="0") -> bool:
        request_id = _text(request_id, name="request_id")
        normalized_incurred = BudgetLedger(incurred).snapshot().ceiling
        normalized_unbilled = BudgetLedger(estimated_unbilled).snapshot().ceiling
        request = {
            "request_id": request_id,
            "incurred": str(normalized_incurred),
            "estimated_unbilled": str(normalized_unbilled),
        }

        def validate(ledger: BudgetLedger) -> None:
            ledger.settle(
                request_id,
                incurred=normalized_incurred,
                estimated_unbilled=normalized_unbilled,
            )

        return self._commit(
            action="settle",
            identity=request_id,
            request=request,
            event_type="ModelCostSettled",
            payload=request,
            result={"settled": True, **request},
            validate=validate,
        )

    def reconcile_unbilled(
        self,
        *,
        billing_id: str,
        request_id: str,
        billed,
    ) -> bool:
        billing_id = _text(billing_id, name="billing_id")
        request_id = _text(request_id, name="request_id")
        normalized = BudgetLedger(billed).snapshot().ceiling
        request = {
            "billing_id": billing_id,
            "request_id": request_id,
            "billed": str(normalized),
        }

        def validate(ledger: BudgetLedger) -> None:
            ledger.reconcile_unbilled(
                billing_id=billing_id,
                request_id=request_id,
                billed=normalized,
            )

        return self._commit(
            action="billing",
            identity=billing_id,
            request=request,
            event_type="ModelUnbilledReconciled",
            payload=request,
            result={"reconciled": True, **request},
            validate=validate,
        )
