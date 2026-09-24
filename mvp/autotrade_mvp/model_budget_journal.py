"""Journal-backed persistence for the canonical model budget projection.

The in-memory :class:`BudgetLedger` remains the single budget rule authority.
This adapter only persists accepted mutations in the canonical JournalStore and
rebuilds that projection after restart. It performs no model/network call.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Any, Callable

from mvp.autotrade_mvp.model_gateway import BudgetLedger, BudgetSnapshot
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


_AGGREGATE_TYPE = "model_budget"


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event_id(aggregate_id: str, idempotency_key: str) -> str:
    material = f"{aggregate_id}|{idempotency_key}".encode("utf-8")
    return "model-budget-" + sha256(material).hexdigest()


def _command_id(aggregate_id: str, idempotency_key: str) -> str:
    material = f"command|{aggregate_id}|{idempotency_key}".encode("utf-8")
    return "model-budget-command-" + sha256(material).hexdigest()


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
        clock: Callable[[], str] | None = None,
    ) -> None:
        if not isinstance(journal, JournalStore):
            raise TypeError("journal must be JournalStore")
        self.journal = journal
        self.budget_id = _text(budget_id, name="budget_id")
        self._clock = clock or _now

        candidate = BudgetLedger(ceiling)
        self._ceiling = candidate.snapshot().ceiling
        existing = self.journal.load_events(_AGGREGATE_TYPE, self.budget_id)
        if not existing:
            payload = {"ceiling": str(self._ceiling)}
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
        if event_type == "ModelCostReserved":
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
                billed=payload["billed"],
            )
            return
        raise ValueError(f"unsupported durable model budget event: {event_type}")

    def _replay(self) -> BudgetLedger:
        events = self._events()
        if not events or events[0]["event_type"] != "ModelBudgetInitialized":
            raise ValueError("durable model budget initialization is missing")
        ceiling = events[0]["payload"].get("ceiling")
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
        idempotency_key = f"model-budget:{self.budget_id}:{action}:{identity}"
        command_id = _command_id(self.budget_id, idempotency_key)

        # Validate on a fresh durable projection so rejected operations never
        # create events or mutate process-only state.
        ledger = self._replay()
        validate(ledger)
        version = len(self._events()) + 1
        envelope = self._envelope(
            event_type=event_type,
            version=version,
            payload=payload,
            event_id=_event_id(self.budget_id, idempotency_key),
        )
        try:
            _, inserted, _ = self.journal.commit_command(
                command_id=command_id,
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
                event_id=_event_id(self.budget_id, idempotency_key),
            )
            _, inserted, _ = self.journal.commit_command(
                command_id=command_id,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=version,
                events=[(envelope, None)],
            )
            return inserted

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
            candidate.release(request_id)

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
        # Canonical BudgetLedger performs exact-decimal validation before write.
        probe = self._replay()
        probe.settle(
            request_id,
            incurred=incurred,
            estimated_unbilled=estimated_unbilled,
        )
        before = self._replay().snapshot()
        after = probe.snapshot()
        normalized_incurred = after.incurred - before.incurred
        normalized_unbilled = after.estimated_unbilled - before.estimated_unbilled
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

    def reconcile_unbilled(self, *, billing_id: str, billed) -> bool:
        billing_id = _text(billing_id, name="billing_id")
        probe = self._replay()
        before = probe.snapshot()
        probe.reconcile_unbilled(billing_id=billing_id, billed=billed)
        after = probe.snapshot()
        normalized = after.incurred - before.incurred
        request = {"billing_id": billing_id, "billed": str(normalized)}

        def validate(ledger: BudgetLedger) -> None:
            ledger.reconcile_unbilled(billing_id=billing_id, billed=normalized)

        return self._commit(
            action="billing",
            identity=billing_id,
            request=request,
            event_type="ModelUnbilledReconciled",
            payload=request,
            result={"reconciled": True, **request},
            validate=validate,
        )
