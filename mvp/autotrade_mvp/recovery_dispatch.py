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
from .persistence import JournalStore
from .recovery import HostState, OwnerFence, RecoveryController


_ISSUANCE_TOKEN = object()
_CANONICAL_VALIDATE_SENDER = RecoveryController.validate_sender
_CANONICAL_VALIDATE_SENDER_CODE = RecoveryController.validate_sender.__code__
_CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT = _canonical_journal_authority_snapshot
_CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE = _canonical_journal_authority_snapshot.__code__
_CANONICAL_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_TAKEOVER_SOURCE_ATTR = "_autotrade_takeover_source_owner"


def _require_executable_authority() -> None:
    if RecoveryController.validate_sender is not _CANONICAL_VALIDATE_SENDER:
        raise PermissionError("recovery sender validator authority changed")
    if _CANONICAL_VALIDATE_SENDER.__code__ is not _CANONICAL_VALIDATE_SENDER_CODE:
        raise PermissionError("recovery sender validator code changed")
    if (
        _canonical_journal_authority_snapshot
        is not _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT
    ):
        raise PermissionError("journal snapshot authority changed")
    if (
        _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT.__code__
        is not _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE
    ):
        raise PermissionError("journal snapshot authority code changed")


def _trusted_journal_authority_snapshot(store: JournalStore):
    _require_executable_authority()
    return _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT(store)


def _scope(environment: str, account_id: str) -> tuple[str, str, str]:
    # Reject executable subclasses before invoking string normalization, but
    # preserve the existing built-in string normalization contract.
    if type(environment) is not str:
        raise TypeError("environment must be exact text")
    normalized_environment = environment.strip().upper()
    if normalized_environment not in _CANONICAL_ENVIRONMENTS:
        raise ValueError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    if type(account_id) is not str:
        raise TypeError("account_id must be exact text")
    normalized_account = account_id.strip()
    if not normalized_account:
        raise ValueError("account_id is required")
    return (
        normalized_environment,
        normalized_account,
        f"{normalized_environment}:{normalized_account}",
    )


def _takeover_source_owner(recovery: RecoveryController) -> OwnerFence | None:
    state = vars(recovery)
    source = state.get(_TAKEOVER_SOURCE_ATTR)
    if source is None:
        return None
    if type(source) is not OwnerFence:
        raise PermissionError("takeover source owner authority changed")
    if type(source.owner_id) is not str or not source.owner_id:
        raise PermissionError("takeover source owner identity is invalid")
    if type(source.epoch) is not int or source.epoch < 1:
        raise PermissionError("takeover source owner epoch is invalid")
    return source


def mark_recovery_takeover_source(
    recovery: RecoveryController,
    source: OwnerFence,
) -> OwnerFence:
    """Mark an attached durable restart owner as takeover-only authority.

    The marker lives on the exact controller already bound to the canonical
    journal.  It does not create another recovery state machine; it only prevents
    the recovery-issued sender seam from reissuing the pre-takeover generation.
    """

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(source) is not OwnerFence:
        raise TypeError("source must be exact OwnerFence")
    if recovery.owner is not source and recovery.owner != source:
        raise PermissionError("takeover source is not the attached recovery owner")
    chain = recovery.durable_owner_chain()
    if not chain or chain[-1] != source:
        raise PermissionError("takeover source is not the current durable owner")
    existing = _takeover_source_owner(recovery)
    if existing is not None and existing != source:
        raise PermissionError("takeover source owner marker already belongs elsewhere")
    vars(recovery)[_TAKEOVER_SOURCE_ATTR] = source
    recovery.provider_reconciled = False
    recovery.reason_codes.add("takeover_source_only")
    recovery.reason_codes.add("startup_reconciliation_required")
    recovery.state = HostState.RECOVERING
    return source


def activate_recovery_takeover_target(
    recovery: RecoveryController,
    *,
    source: OwnerFence,
    target: OwnerFence,
) -> OwnerFence:
    """Release takeover-only fencing only after exact durable N -> N+1 advance."""

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(source) is not OwnerFence or type(target) is not OwnerFence:
        raise TypeError("source and target must be exact OwnerFence values")
    marked_source = _takeover_source_owner(recovery)
    if marked_source is None:
        raise PermissionError("recovery controller is not attached takeover-only")
    if marked_source != source:
        raise PermissionError("durable takeover source does not match attached source")
    if target.epoch != source.epoch + 1:
        raise PermissionError("durable takeover target is not the next owner generation")
    if recovery.owner != target:
        raise PermissionError("recovery controller is not bound to takeover target")
    chain = recovery.durable_owner_chain()
    if not chain or chain[-1] != target:
        raise PermissionError("takeover target is not the current durable owner")
    vars(recovery).pop(_TAKEOVER_SOURCE_ATTR, None)
    recovery.provider_reconciled = False
    recovery.reason_codes.discard("takeover_source_only")
    recovery.reason_codes.add("startup_reconciliation_required")
    recovery.state = HostState.RECOVERING
    return target


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
        "__sender_check_code",
        "__journal_snapshot_reader",
        "__journal_snapshot_reader_code",
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
        if type(owner.owner_id) is not str or not owner.owner_id:
            raise PermissionError("recovery owner identity is not canonical exact text")
        if type(owner.epoch) is not int or owner.epoch < 1:
            raise PermissionError("recovery owner epoch is not a positive exact integer")
        _require_executable_authority()
        normalized_environment, normalized_account, _ = _scope(
            environment, account_id
        )
        source = _takeover_source_owner(recovery)
        if source is not None and owner == source:
            raise PermissionError(
                "takeover source owner cannot receive recovery-issued sender authority"
            )
        sender_function = _CANONICAL_VALIDATE_SENDER
        snapshot_reader = _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT
        self.__recovery = recovery
        self.__store = store
        self.__store_snapshot = snapshot_reader(store)
        self.__sender_check = sender_function.__get__(recovery, RecoveryController)
        self.__sender_check_code = _CANONICAL_VALIDATE_SENDER_CODE
        self.__journal_snapshot_reader = snapshot_reader
        self.__journal_snapshot_reader_code = _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE
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
        if type(self.__owner) is not OwnerFence:
            raise PermissionError("recovery owner authority changed")
        if type(self.__owner.owner_id) is not str or not self.__owner.owner_id:
            raise PermissionError("recovery owner identity is not canonical exact text")
        if type(self.__owner.epoch) is not int or self.__owner.epoch < 1:
            raise PermissionError("recovery owner epoch is not a positive exact integer")
        if (
            type(self.__environment) is not str
            or self.__environment not in _CANONICAL_ENVIRONMENTS
        ):
            raise PermissionError("issued dispatcher environment authority changed")
        if (
            type(self.__account_id) is not str
            or not self.__account_id
            or self.__account_id != self.__account_id.strip()
        ):
            raise PermissionError("issued dispatcher account authority changed")
        source = _takeover_source_owner(self.__recovery)
        if source is not None and self.__owner == source:
            raise PermissionError(
                "takeover source owner cannot retain recovery-issued sender authority"
            )

        # Do not dynamically dispatch through module helper aliases here. This
        # method is the post-composition issuance boundary, so it validates and
        # invokes only the exact executable authorities retained by this object.
        sender_function = self.__sender_check.__func__
        if self.__sender_check.__self__ is not self.__recovery:
            raise PermissionError("bound recovery sender authority changed")
        if RecoveryController.validate_sender is not sender_function:
            raise PermissionError("recovery sender validator authority changed")
        if sender_function.__code__ is not self.__sender_check_code:
            raise PermissionError("recovery sender validator code changed")

        snapshot_reader = self.__journal_snapshot_reader
        if _canonical_journal_authority_snapshot is not snapshot_reader:
            raise PermissionError("journal snapshot authority changed")
        if snapshot_reader.__code__ is not self.__journal_snapshot_reader_code:
            raise PermissionError("journal snapshot authority code changed")
        if snapshot_reader(self.__store) != self.__store_snapshot:
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
        return self.__dispatcher.dispatch(
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
            sender_check=self.__sender_check,
            submission_scope=submission_scope,
        )


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
    finish before reconciliation. A controller attached to a pre-existing
    durable source owner is an exception: it is takeover-only and may not mint
    a sender until exact durable N -> N+1 takeover activation clears that fence.
    """

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if type(prepared_lease_seconds) is not int or prepared_lease_seconds < 1:
        raise ValueError("prepared_lease_seconds must be a positive exact integer")

    _require_executable_authority()
    normalized_environment, normalized_account, expected_scope = _scope(
        environment, account_id
    )
    state = vars(recovery)
    if state.get("_owner_store") is not store:
        raise PermissionError(
            "dispatcher JournalStore must be the exact recovery owner store"
        )
    owner_scope = state.get("_owner_scope")
    if type(owner_scope) is not str or owner_scope != expected_scope:
        raise PermissionError("dispatcher scope does not match recovery owner scope")

    owner = recovery.owner
    if type(owner) is not OwnerFence:
        raise PermissionError("recovery has no active durable owner")
    if type(owner.owner_id) is not str or not owner.owner_id:
        raise PermissionError("recovery owner identity is not canonical exact text")
    if type(owner.epoch) is not int or owner.epoch < 1:
        raise PermissionError("recovery owner epoch is not a positive exact integer")
    source = _takeover_source_owner(recovery)
    if source is not None and owner == source:
        raise PermissionError(
            "takeover source owner cannot receive recovery-issued sender authority"
        )
    durable_chain = recovery.durable_owner_chain()
    if not durable_chain or durable_chain[-1] != owner:
        raise PermissionError("recovery owner is not the current durable owner")

    _trusted_journal_authority_snapshot(store)
    return RecoveryIssuedDispatcher(
        recovery=recovery,
        store=store,
        environment=normalized_environment,
        account_id=normalized_account,
        owner=owner,
        prepared_lease_seconds=prepared_lease_seconds,
        issuance_token=_ISSUANCE_TOKEN,
    )
