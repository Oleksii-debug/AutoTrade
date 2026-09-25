"""Journal-backed persistence for the canonical model budget projection.

The in-memory :class:`BudgetLedger` remains the single budget rule authority.
This adapter only persists accepted mutations in the canonical JournalStore and
rebuilds that projection after restart. It performs no model/network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Any, Callable, Iterable

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


_AGGREGATE_TYPE = "model_budget"
_COMMAND_ACTOR = "autotrade-model-budget"
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    normalized = value.strip().upper() if isinstance(value, str) else ""
    if normalized not in _ENVIRONMENTS:
        raise ValueError("environment must be REPLAY, SIMULATION, PAPER, or LIVE")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._clock = clock or _now

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
                # Another process may have initialized the same aggregate after
                # our read. Replay below determines whether the ceiling agrees.
                pass

        rebuilt = self._replay()
        if rebuilt.snapshot().ceiling != self._ceiling:
            raise ValueError("budget ceiling conflicts with durable model budget")

    def _events(self) -> list[dict[str, Any]]:
        return self.journal.load_events(_AGGREGATE_TYPE, self.budget_id)

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
            "aggregate_version": version,
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": self._clock(),
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
        events = self._events()
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

        # Resolve idempotency before re-applying a mutation to the projection.
        # This matters after restart: a settled reservation is no longer active,
        # but replaying the exact same settlement must remain a safe no-op.
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
            # Do not let the event-id fast path bypass canonical command scope.
            # Exact retries must still prove the same actor/environment/key. For
            # pre-scoped event history this also creates the command dedupe row
            # lazily after the exact event payload has been verified.
            self.journal.record_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=int(existing["aggregate_version"]),
            )
            return False

        # Validate on a fresh durable projection so rejected operations never
        # create events or mutate process-only state.
        ledger = self._replay()
        validate(ledger)
        version = len(self._events()) + 1
        envelope = self._envelope(
            event_type=event_type,
            version=version,
            payload=payload,
            event_id=event_id,
        )
        try:
            _, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=version,
                events=[(envelope, None)],
            )
            return inserted
        except ValueError as error:
            # A concurrent writer can advance aggregate_version between replay
            # and commit. Re-evaluate once from durable truth. We do not loop or
            # invent success; a repeated conflict remains fail-closed.
            if "aggregate_version must be" not in str(error):
                raise
            ledger = self._replay()
            validate(ledger)
            version = len(self._events()) + 1
            envelope = self._envelope(
                event_type=event_type,
                version=version,
                payload=payload,
                event_id=event_id,
            )
            _, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                actor=_COMMAND_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=version,
                events=[(envelope, None)],
            )
            return inserted

    @staticmethod
    def _routing_input(
        policy: RoutingPolicy,
        request: ModelRequest,
        descriptors: tuple[ModelDescriptor, ...],
    ) -> dict[str, Any]:
        if not isinstance(policy, RoutingPolicy):
            raise TypeError("policy must be RoutingPolicy")
        if not isinstance(request, ModelRequest):
            raise TypeError("request must be ModelRequest")
        if not all(isinstance(item, ModelDescriptor) for item in descriptors):
            raise TypeError("descriptors must contain ModelDescriptor values")
        return {
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

    @staticmethod
    def _decision_from_route_payload(payload: dict[str, Any]) -> RouteDecision:
        try:
            status = RouteStatus(payload["route_status"])
            amount = Decimal(payload["amount"])
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
    ) -> RouteDecision:
        """Route and durably reserve worst-case model cost before call authority.

        The caller budget is only an optional tighter request cap. It can never
        enlarge the durable budget. The reservation commit revalidates the
        latest journal state before this method returns ADMITTED.
        """

        materialized = tuple(descriptors)
        routing_input = self._routing_input(policy, request, materialized)
        now = now_utc or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise ValueError("now_utc must be timezone-aware")
        if request.cancelled:
            return RouteDecision(
                RouteStatus.REJECTED,
                None,
                None,
                None,
                Decimal("0"),
                "request_cancelled",
            )
        if now >= request.deadline_utc:
            return RouteDecision(
                RouteStatus.REJECTED,
                None,
                None,
                None,
                Decimal("0"),
                "deadline_expired",
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
        ledger = self._replay()
        released = ledger.release(request_id)
        if released == 0:
            return False
        request = {"request_id": request_id}

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
        # Reuse the canonical ledger parser without mutating durable state.
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
        # The ceiling parser enforces the same exact, finite, non-negative
        # Decimal boundary as the canonical ledger.
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
