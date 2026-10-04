"""Construction-bound PAPER/LIVE provider dispatch authority.

``GuardedDispatcher`` remains the low-level persist-before-send state machine.
This module is the recovery-owned product composition boundary: callers supply
one exact durable ``RecoveryController`` and ``JournalStore`` at construction,
and cannot substitute sender validation per submission.
"""

from __future__ import annotations

from threading import Condition, current_thread
from typing import Any, Callable, Mapping

from .dispatch import (
    AuthorityCheck,
    DispatchOutcome,
    GuardedDispatcher,
    TransportSend,
)
from .persistence import JournalStore
from .recovery import RecoveryController


class RecoveryDispatchBindingError(PermissionError):
    """Raised when recovery and dispatch authority are not the same durable scope."""


class RecoveryBoundDispatcher:
    """PAPER/LIVE dispatcher sealed to one recovery owner generation.

    The bound owner id/epoch, exact JournalStore object, and canonical
    ``ENVIRONMENT:account`` scope are captured once. Every terminal send guard
    invokes the class-owned ``RecoveryController.validate_sender`` method, so a
    caller cannot replace sender authority with a permissive callback.

    Product shutdown can call :meth:`stop_and_drain` to make recovery revocation
    linear with this retained dispatch surface. Existing dispatches finish before
    the controller is stopped; new dispatch entry is held until that stop commits,
    after which the retained dispatcher is permanently revoked without touching
    the journal or provider transport. Calling shutdown from inside an active
    dispatch is rejected rather than self-deadlocking.

    A durable takeover intentionally invalidates an existing instance. Product
    composition must construct a new dispatcher from the successor controller;
    silently rebinding an old dispatcher would erase the fencing boundary.
    """

    __slots__ = (
        "_controller",
        "_store",
        "_owner_scope",
        "_owner_id",
        "_owner_epoch",
        "_dispatcher",
        "_lifecycle_condition",
        "_active_dispatches",
        "_active_threads",
        "_revoking",
        "_revoked",
    )

    def __init__(
        self,
        controller: RecoveryController,
        store: JournalStore,
        *,
        environment: str,
        account_id: str,
        prepared_lease_seconds: int = 60,
    ) -> None:
        if type(controller) is not RecoveryController:
            raise TypeError("controller must be the canonical RecoveryController")
        if type(store) is not JournalStore:
            raise TypeError("store must be the canonical JournalStore")

        normalized_environment = (
            environment.strip().upper() if type(environment) is str else ""
        )
        if normalized_environment not in {"PAPER", "LIVE"}:
            raise ValueError(
                "recovery-bound dispatch is only valid for PAPER or LIVE"
            )
        if type(account_id) is not str or not account_id.strip():
            raise ValueError("account_id is required")
        normalized_account = account_id.strip()
        owner_scope = f"{normalized_environment}:{normalized_account}"

        controller_state = vars(controller)
        if controller_state.get("_owner_store") is not store:
            raise RecoveryDispatchBindingError(
                "recovery and dispatch must share the exact JournalStore object"
            )
        if controller.owner_scope != owner_scope:
            raise RecoveryDispatchBindingError(
                "recovery owner scope does not match dispatch environment/account"
            )
        owner = controller.owner
        if owner is None:
            raise RecoveryDispatchBindingError(
                "recovery owner must be started before dispatcher construction"
            )

        self._controller = controller
        self._store = store
        self._owner_scope = owner_scope
        self._owner_id = owner.owner_id
        self._owner_epoch = owner.epoch
        self._dispatcher = GuardedDispatcher(
            store,
            environment=normalized_environment,
            account_id=normalized_account,
            owner_token=self._owner_id,
            owner_epoch=self._owner_epoch,
            prepared_lease_seconds=prepared_lease_seconds,
        )
        self._lifecycle_condition = Condition()
        self._active_dispatches = 0
        self._active_threads: dict[object, int] = {}
        self._revoking = False
        self._revoked = False

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def owner_epoch(self) -> int:
        return self._owner_epoch

    @property
    def owner_scope(self) -> str:
        return self._owner_scope

    def _validate_bound_sender(self, owner_id: str, owner_epoch: int) -> None:
        if (owner_id, owner_epoch) != (self._owner_id, self._owner_epoch):
            raise RecoveryDispatchBindingError(
                "dispatch owner changed after recovery binding"
            )
        controller = self._controller
        state = vars(controller)
        if state.get("_owner_store") is not self._store:
            raise RecoveryDispatchBindingError(
                "recovery JournalStore changed after dispatcher construction"
            )
        if controller.owner_scope != self._owner_scope:
            raise RecoveryDispatchBindingError(
                "recovery owner scope changed after dispatcher construction"
            )
        # Invoke the canonical class method directly rather than an instance
        # attribute supplied or shadowed by a caller.
        RecoveryController.validate_sender(controller, owner_id, owner_epoch)

    def stop_and_drain(self) -> None:
        """Drain this retained send surface, then permanently revoke it.

        Entry to new dispatches is paused while draining. The final controller
        stop happens while that entry gate is still held, so there is no gap in
        which a new caller can validate the old owner after the drain completed.
        Once stopped, this dispatcher rejects all future entry before any durable
        submission or provider-side effect can be attempted.
        """

        thread = current_thread()
        condition = self._lifecycle_condition
        with condition:
            if self._active_threads.get(thread, 0):
                raise RuntimeError(
                    "cannot revoke recovery dispatcher from an active dispatch"
                )
            while self._revoking:
                condition.wait()
                if self._revoked:
                    return
            if self._revoked:
                return
            self._revoking = True
            try:
                while self._active_dispatches:
                    condition.wait()
                RecoveryController.stop(self._controller)
                self._revoked = True
            finally:
                self._revoking = False
                condition.notify_all()

    def dispatch(
        self,
        *,
        attempt_id: str,
        intent_id: str,
        intent_hash: str,
        provider: str,
        request: Mapping[str, Any],
        now: str,
        authority_check: AuthorityCheck,
        transport_send: TransportSend,
        client_id_max_length: int = 32,
        client_id_format: str = "TOKEN",
        final_barrier_clock: Callable[[], str] | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Dispatch without exposing a caller-controlled sender-check seam."""

        thread = current_thread()
        condition = self._lifecycle_condition
        with condition:
            while self._revoking:
                condition.wait()
            if self._revoked:
                raise RecoveryDispatchBindingError(
                    "recovery-bound dispatcher is permanently revoked"
                )
            self._active_dispatches += 1
            self._active_threads[thread] = self._active_threads.get(thread, 0) + 1

        try:
            return self._dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id=intent_id,
                intent_hash=intent_hash,
                provider=provider,
                request=request,
                now=now,
                authority_check=authority_check,
                transport_send=transport_send,
                client_id_max_length=client_id_max_length,
                client_id_format=client_id_format,
                final_barrier_clock=final_barrier_clock,
                sender_check=self._validate_bound_sender,
                submission_scope=submission_scope,
            )
        finally:
            with condition:
                self._active_dispatches -= 1
                remaining = self._active_threads[thread] - 1
                if remaining:
                    self._active_threads[thread] = remaining
                else:
                    self._active_threads.pop(thread, None)
                if self._active_dispatches == 0:
                    condition.notify_all()
