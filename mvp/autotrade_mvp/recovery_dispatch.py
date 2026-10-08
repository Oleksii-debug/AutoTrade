"""Construction-bound recovery sender authority for guarded financial dispatch.

This module is intentionally a thin composition seam. It does not create a
second sender state machine, lock, recovery owner, journal, or provider
transport. The existing :class:`GuardedDispatcher` remains the durable send
chronology and the existing shared sender-authority window remains the
cross-process critical section.

The only authority added here is construction provenance: a product-selected
RecoveryController issues one dispatcher for its exact JournalStore generation,
environment/account scope, and current durable owner. The issued dispatcher
always supplies the canonical RecoveryController.validate_sender check at the
irreversible final guard. A legacy per-call ``sender_check`` is accepted only
for call-site compatibility and is deliberately ignored, so caller code cannot
replace recovery readiness/owner authority with an allow-all callback.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .dispatch import (
    AuthorityCheck,
    DispatchOutcome,
    GuardedDispatcher,
    SenderCheck,
    TransportSend,
    _canonical_journal_authority_snapshot,
)
from .persistence import JournalStore, payload_digest
from .reconciliation_journal import load_latest_reconciliation_checkpoint_for_scope
from .recovery import HostState, OwnerFence, RecoveryController


_ISSUANCE_TOKEN = object()
_CANONICAL_VALIDATE_SENDER = RecoveryController.validate_sender
_CANONICAL_RECOVER_DURABLE_UNCERTAINTY = (
    RecoveryController.recover_durable_submission_uncertainty
)
_CANONICAL_LOAD_LATEST_RECONCILIATION = load_latest_reconciliation_checkpoint_for_scope
_CANONICAL_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})


def _scope(environment: str, account_id: str) -> tuple[str, str, str]:
    normalized_environment = (
        environment.strip().upper() if isinstance(environment, str) else ""
    )
    if normalized_environment not in _CANONICAL_ENVIRONMENTS:
        raise ValueError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    if not isinstance(account_id, str) or not account_id.strip():
        raise ValueError("account_id is required")
    normalized_account = account_id.strip()
    return (
        normalized_environment,
        normalized_account,
        f"{normalized_environment}:{normalized_account}",
    )


class RecoveryIssuedDispatcher:
    """Opaque owner-bound facade over the canonical GuardedDispatcher.

    This is trusted-process composition provenance, not a Python sandbox. The
    authority-bearing callback is captured from the canonical RecoveryController
    class at module initialization and is never selected by the dispatch caller.
    """

    __slots__ = (
        "__dispatcher",
        "__recovery",
        "__store",
        "__store_snapshot",
        "__sender_check",
        "__owner",
        "__environment",
        "__account_id",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("RecoveryIssuedDispatcher is sealed")

    def __init__(
        self,
        *,
        recovery: RecoveryController,
        store: JournalStore,
        environment: str,
        account_id: str,
        owner: OwnerFence,
        prepared_lease_seconds: int,
        issuance_token: object | None = None,
    ) -> None:
        if type(self) is not RecoveryIssuedDispatcher or issuance_token is not _ISSUANCE_TOKEN:
            raise PermissionError(
                "recovery-issued dispatcher must be minted by canonical composition"
            )
        if type(recovery) is not RecoveryController:
            raise TypeError("recovery must be exact RecoveryController")
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        if type(owner) is not OwnerFence:
            raise TypeError("owner must be exact OwnerFence")
        normalized_environment, normalized_account, _ = _scope(
            environment, account_id
        )
        self.__recovery = recovery
        self.__store = store
        self.__store_snapshot = _canonical_journal_authority_snapshot(store)
        self.__sender_check = _CANONICAL_VALIDATE_SENDER.__get__(
            recovery, RecoveryController
        )
        self.__owner = owner
        self.__environment = normalized_environment
        self.__account_id = normalized_account
        self.__dispatcher = GuardedDispatcher(
            store,
            environment=normalized_environment,
            account_id=normalized_account,
            owner_token=owner.owner_id,
            owner_epoch=owner.epoch,
            prepared_lease_seconds=prepared_lease_seconds,
        )

    @property
    def environment(self) -> str:
        return self.__environment

    @property
    def account_id(self) -> str:
        return self.__account_id

    @property
    def owner(self) -> OwnerFence:
        return self.__owner

    def _require_issued_authority(self) -> None:
        if type(self) is not RecoveryIssuedDispatcher:
            raise PermissionError("recovery-issued dispatcher type changed")
        if type(self.__recovery) is not RecoveryController:
            raise PermissionError("recovery controller authority changed")
        if type(self.__store) is not JournalStore:
            raise PermissionError("submission journal authority changed")
        if _canonical_journal_authority_snapshot(self.__store) != self.__store_snapshot:
            raise PermissionError("submission journal generation changed")
        if self.__dispatcher.store is not self.__store:
            raise PermissionError("guarded dispatcher journal authority changed")
        if (
            self.__dispatcher.environment != self.__environment
            or self.__dispatcher.account_id != self.__account_id
            or self.__dispatcher.owner_token != self.__owner.owner_id
            or self.__dispatcher.owner_epoch != self.__owner.epoch
        ):
            raise PermissionError("guarded dispatcher sender binding changed")

    def _require_durable_reconciliation_authority(self, provider: str) -> None:
        """Require current owner-bound journal truth before irreversible send."""

        if type(provider) is not str or not provider.strip():
            raise PermissionError("provider identity is required for durable readiness")
        if (
            self.__recovery.state is not HostState.READY
            or self.__recovery.provider_reconciled is not True
            or self.__recovery.unresolved_attempts
        ):
            raise PermissionError(
                "production sender requires accepted durable reconciliation"
            )

        checkpoint = _CANONICAL_LOAD_LATEST_RECONCILIATION(
            self.__store,
            provider_id=provider.strip().upper(),
            account_id=self.__account_id,
            environment=self.__environment,
        )
        if checkpoint is None:
            raise PermissionError(
                "production sender requires a current durable reconciliation checkpoint"
            )
        payload = checkpoint.get("payload")
        if (
            checkpoint.get("event_type") != "AccountReconciled"
            or checkpoint.get("aggregate_type") != "account_reconciliation"
            or not isinstance(payload, dict)
            or payload_digest(payload) != checkpoint.get("payload_hash")
        ):
            raise PermissionError("durable reconciliation payload is invalid")
        checkpoint_owner = payload.get("checkpoint_owner")
        if (
            not isinstance(checkpoint_owner, dict)
            or checkpoint_owner.get("host_id") != self.__owner.owner_id
            or checkpoint_owner.get("owner_epoch") != str(self.__owner.epoch)
        ):
            raise PermissionError(
                "durable reconciliation is not bound to the current sender owner"
            )
        if (
            payload.get("complete") is not True
            or payload.get("snapshot_consistent") is not True
            or payload.get("activity_coverage_complete") is not True
        ):
            raise PermissionError("durable reconciliation is incomplete")

        blocking = payload.get("blocking_resources")
        resolutions = payload.get("submission_resolutions")
        if not isinstance(blocking, list) or not isinstance(resolutions, list):
            raise PermissionError("durable reconciliation readiness fields are invalid")
        if blocking:
            raise PermissionError("durable reconciliation has blocking resources")
        for resolution in resolutions:
            if not isinstance(resolution, dict):
                raise PermissionError("durable reconciliation resolution is invalid")
            outcome = resolution.get("outcome")
            if type(outcome) is not str or outcome.strip().upper() == "UNKNOWN":
                raise PermissionError(
                    "durable reconciliation contains unresolved submission truth"
                )

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
        sender_check: SenderCheck | None = None,
        submission_scope: Mapping[str, Any] | None = None,
    ) -> DispatchOutcome:
        """Dispatch with recovery-owned sender validation at the final guard.

        ``sender_check`` is intentionally ignored. It remains in the signature
        only so legacy callers cannot regain authority by routing around this
        facade while call sites are migrated.
        """

        del sender_check
        self._require_issued_authority()

        def canonical_sender_check(owner_id: str, owner_epoch: int) -> None:
            # Rehydrate same-process and restart-visible ambiguity from the
            # canonical submission journal while the shared sender gate is held.
            # A previous SubmissionSending/SubmissionUnknown must invalidate
            # READY before another attempt can cross the irreversible barrier.
            _CANONICAL_RECOVER_DURABLE_UNCERTAINTY(
                self.__recovery,
                environment=self.__environment,
                account_id=self.__account_id,
            )
            self.__sender_check(owner_id, owner_epoch)
            self._require_durable_reconciliation_authority(provider)

        outcome = self.__dispatcher.dispatch(
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
            sender_check=canonical_sender_check,
            submission_scope=submission_scope,
        )
        if outcome.status == "UNKNOWN":
            # The GuardedDispatcher has already persisted the ambiguous durable
            # chronology. Reflect that fact into recovery immediately so
            # readiness/admission cannot remain optimistically READY until a
            # later send happens to re-enter the final barrier.
            _CANONICAL_RECOVER_DURABLE_UNCERTAINTY(
                self.__recovery,
                environment=self.__environment,
                account_id=self.__account_id,
            )
        return outcome


def build_recovery_issued_dispatcher(
    recovery: RecoveryController,
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    prepared_lease_seconds: int = 60,
) -> RecoveryIssuedDispatcher:
    """Mint one dispatcher from the exact current durable recovery owner.

    Issuance is allowed while the host is RECOVERING so product bootstrap can
    finish before reconciliation. That does not grant send authority: the bound
    ``validate_sender`` is re-executed inside the GuardedDispatcher final sender
    gate and requires the same durable owner to be current and the controller to
    be READY before ``SubmissionSending`` can be persisted or provider bytes can
    be emitted.
    """

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if (
        not isinstance(prepared_lease_seconds, int)
        or isinstance(prepared_lease_seconds, bool)
        or prepared_lease_seconds < 1
    ):
        raise ValueError("prepared_lease_seconds must be a positive integer")

    normalized_environment, normalized_account, expected_scope = _scope(
        environment, account_id
    )
    state = vars(recovery)
    if state.get("_owner_store") is not store:
        raise PermissionError(
            "dispatcher JournalStore must be the exact recovery owner store"
        )
    if recovery.owner_scope != expected_scope:
        raise PermissionError("dispatcher scope does not match recovery owner scope")

    owner = recovery.owner
    if type(owner) is not OwnerFence:
        raise PermissionError("recovery has no active durable owner")
    durable_chain = recovery.durable_owner_chain()
    if not durable_chain or durable_chain[-1] != owner:
        raise PermissionError("recovery owner is not the current durable owner")

    # Snapshot the exact backing generation before creating the inner dispatcher;
    # GuardedDispatcher snapshots and rechecks the same generation independently.
    _canonical_journal_authority_snapshot(store)
    return RecoveryIssuedDispatcher(
        recovery=recovery,
        store=store,
        environment=normalized_environment,
        account_id=normalized_account,
        owner=owner,
        prepared_lease_seconds=prepared_lease_seconds,
        issuance_token=_ISSUANCE_TOKEN,
    )
