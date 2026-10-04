"""Construction-bound PAPER/LIVE provider dispatch authority.

``GuardedDispatcher`` remains the low-level persist-before-send state machine.
This module is the recovery-owned product composition boundary: callers supply
one exact durable ``RecoveryController`` and ``JournalStore`` at construction,
and cannot substitute sender validation per submission.
"""

from __future__ import annotations

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
    ``ENVIRONMENT:account`` scope are captured once.  Every terminal send guard
    invokes the class-owned ``RecoveryController.validate_sender`` method, so a
    caller cannot replace sender authority with a permissive callback.

    A durable takeover intentionally invalidates an existing instance.  Product
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
