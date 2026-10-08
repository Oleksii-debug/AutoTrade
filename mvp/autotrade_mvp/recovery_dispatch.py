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
from .recovery_takeover import (
    DurableTakeoverResult,
    _latest_effectful_submission_sequence,
    require_committed_durable_takeover,
)
from .windows_secrets import ProtectedCredentialVault


_ISSUANCE_TOKEN = object()
_CANONICAL_VALIDATE_SENDER = RecoveryController.validate_sender
_CANONICAL_VALIDATE_SENDER_CODE = RecoveryController.validate_sender.__code__
_CANONICAL_RECOVER_DURABLE_UNCERTAINTY = (
    RecoveryController.recover_durable_submission_uncertainty
)
_CANONICAL_RECOVER_DURABLE_UNCERTAINTY_CODE = (
    RecoveryController.recover_durable_submission_uncertainty.__code__
)
_CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT = _canonical_journal_authority_snapshot
_CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE = (
    _canonical_journal_authority_snapshot.__code__
)
_CANONICAL_LOAD_LATEST_RECONCILIATION = load_latest_reconciliation_checkpoint_for_scope
_CANONICAL_LOAD_LATEST_RECONCILIATION_CODE = (
    load_latest_reconciliation_checkpoint_for_scope.__code__
)
_CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE = _latest_effectful_submission_sequence
_CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE_CODE = (
    _latest_effectful_submission_sequence.__code__
)
_CANONICAL_PAYLOAD_DIGEST = payload_digest
_CANONICAL_PAYLOAD_DIGEST_CODE = payload_digest.__code__
_CANONICAL_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_TAKEOVER_SOURCE_ATTR = "_autotrade_takeover_source_owner"


def _require_executable_authority() -> None:
    if RecoveryController.validate_sender is not _CANONICAL_VALIDATE_SENDER:
        raise PermissionError("recovery sender validator authority changed")
    if _CANONICAL_VALIDATE_SENDER.__code__ is not _CANONICAL_VALIDATE_SENDER_CODE:
        raise PermissionError("recovery sender validator code changed")
    if (
        RecoveryController.recover_durable_submission_uncertainty
        is not _CANONICAL_RECOVER_DURABLE_UNCERTAINTY
    ):
        raise PermissionError("recovery uncertainty authority changed")
    if (
        _CANONICAL_RECOVER_DURABLE_UNCERTAINTY.__code__
        is not _CANONICAL_RECOVER_DURABLE_UNCERTAINTY_CODE
    ):
        raise PermissionError("recovery uncertainty authority code changed")
    if _canonical_journal_authority_snapshot is not _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT:
        raise PermissionError("journal snapshot authority changed")
    if (
        _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT.__code__
        is not _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE
    ):
        raise PermissionError("journal snapshot authority code changed")
    if (
        load_latest_reconciliation_checkpoint_for_scope
        is not _CANONICAL_LOAD_LATEST_RECONCILIATION
    ):
        raise PermissionError("reconciliation reader authority changed")
    if (
        _CANONICAL_LOAD_LATEST_RECONCILIATION.__code__
        is not _CANONICAL_LOAD_LATEST_RECONCILIATION_CODE
    ):
        raise PermissionError("reconciliation reader authority code changed")
    if (
        _latest_effectful_submission_sequence
        is not _CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE
    ):
        raise PermissionError("effectful submission reader authority changed")
    if (
        _CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE.__code__
        is not _CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE_CODE
    ):
        raise PermissionError("effectful submission reader authority code changed")
    if payload_digest is not _CANONICAL_PAYLOAD_DIGEST:
        raise PermissionError("payload digest authority changed")
    if _CANONICAL_PAYLOAD_DIGEST.__code__ is not _CANONICAL_PAYLOAD_DIGEST_CODE:
        raise PermissionError("payload digest authority code changed")


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
    """Diagnostic reader for the takeover-only source marker.

    Financial issuance paths intentionally do not dispatch through this mutable
    module helper after composition. They read and validate the exact marker
    state directly so rebinding this convenience reader cannot relax authority.
    """

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
    """Mark an attached durable restart owner as takeover-only authority."""

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(source) is not OwnerFence:
        raise TypeError("source must be exact OwnerFence")
    if recovery.owner is not source and recovery.owner != source:
        raise PermissionError("takeover source is not the attached recovery owner")
    chain = recovery.durable_owner_chain()
    if not chain or chain[-1] != source:
        raise PermissionError("takeover source is not the current durable owner")
    state = vars(recovery)
    existing = state.get(_TAKEOVER_SOURCE_ATTR)
    if existing is not None:
        if type(existing) is not OwnerFence:
            raise PermissionError("takeover source owner authority changed")
        if type(existing.owner_id) is not str or not existing.owner_id:
            raise PermissionError("takeover source owner identity is invalid")
        if type(existing.epoch) is not int or existing.epoch < 1:
            raise PermissionError("takeover source owner epoch is invalid")
        if existing != source:
            raise PermissionError(
                "takeover source owner marker already belongs elsewhere"
            )
    state[_TAKEOVER_SOURCE_ATTR] = source
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
    takeover: DurableTakeoverResult,
    vault: ProtectedCredentialVault,
) -> OwnerFence:
    """Release takeover-only fencing only after exact durable N -> N+1 advance."""

    if type(recovery) is not RecoveryController:
        raise TypeError("recovery must be exact RecoveryController")
    if type(source) is not OwnerFence or type(target) is not OwnerFence:
        raise TypeError("source and target must be exact OwnerFence values")
    state = vars(recovery)
    marked_source = state.get(_TAKEOVER_SOURCE_ATTR)
    if marked_source is None:
        raise PermissionError("recovery controller is not attached takeover-only")
    if type(marked_source) is not OwnerFence:
        raise PermissionError("takeover source owner authority changed")
    if type(marked_source.owner_id) is not str or not marked_source.owner_id:
        raise PermissionError("takeover source owner identity is invalid")
    if type(marked_source.epoch) is not int or marked_source.epoch < 1:
        raise PermissionError("takeover source owner epoch is invalid")
    if marked_source != source:
        raise PermissionError("durable takeover source does not match attached source")
    if target.epoch != source.epoch + 1:
        raise PermissionError("durable takeover target is not the next owner generation")
    if recovery.owner != target:
        raise PermissionError("recovery controller is not bound to takeover target")
    chain = recovery.durable_owner_chain()
    if not chain or chain[-1] != target:
        raise PermissionError("takeover target is not the current durable owner")
    verified_target = require_committed_durable_takeover(
        recovery,
        result=takeover,
        vault=vault,
    )
    if verified_target != target:
        raise PermissionError(
            "issued takeover does not match target recovery owner"
        )
    state.pop(_TAKEOVER_SOURCE_ATTR, None)
    recovery.provider_reconciled = False
    recovery.reason_codes.discard("takeover_source_only")
    recovery.reason_codes.add("startup_reconciliation_required")
    recovery.state = HostState.RECOVERING
    return target


def _bybit_environment(value: str | None, runtime: str) -> str | None:
    """Require explicit Bybit provider environment; never infer from PAPER/LIVE.

    It is rechecked against the owner-bound durable reconciliation checkpoint.
    Absence preserves fail-closed BYBIT behavior.
    """
    if value is None:
        return None
    if type(value) is not str:
        raise TypeError("Bybit provider environment must be exact text")
    normalized = value.strip().upper()
    if normalized not in {"MAINNET", "TESTNET", "DEMO"}:
        raise ValueError("unsupported Bybit provider environment")
    expected_runtime = "LIVE" if normalized == "MAINNET" else "PAPER"
    if runtime != expected_runtime:
        raise ValueError("Bybit provider environment conflicts with runtime")
    return normalized


class RecoveryIssuedDispatcher:
    """Opaque owner-bound facade over the canonical GuardedDispatcher."""

    __slots__ = (
        "__dispatcher",
        "__recovery",
        "__store",
        "__store_snapshot",
        "__sender_check",
        "__sender_check_code",
        "__recover_uncertainty",
        "__recover_uncertainty_code",
        "__journal_snapshot_reader",
        "__journal_snapshot_reader_code",
        "__reconciliation_reader",
        "__reconciliation_reader_code",
        "__latest_effectful_submission_sequence",
        "__latest_effectful_submission_sequence_code",
        "__payload_digest",
        "__payload_digest_code",
        "__owner",
        "__environment",
        "__account_id",
        "__bybit_provider_environment",
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
        bybit_provider_environment: str | None = None,
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
        normalized_bybit_environment = _bybit_environment(
            bybit_provider_environment, normalized_environment
        )
        source = vars(recovery).get(_TAKEOVER_SOURCE_ATTR)
        if source is not None:
            if type(source) is not OwnerFence:
                raise PermissionError("takeover source owner authority changed")
            if type(source.owner_id) is not str or not source.owner_id:
                raise PermissionError("takeover source owner identity is invalid")
            if type(source.epoch) is not int or source.epoch < 1:
                raise PermissionError("takeover source owner epoch is invalid")
            if owner == source:
                raise PermissionError(
                    "takeover source owner cannot receive recovery-issued sender authority"
                )

        sender_function = _CANONICAL_VALIDATE_SENDER
        recover_function = _CANONICAL_RECOVER_DURABLE_UNCERTAINTY
        snapshot_reader = _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT
        reconciliation_reader = _CANONICAL_LOAD_LATEST_RECONCILIATION
        effectful_reader = _CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE
        digest_function = _CANONICAL_PAYLOAD_DIGEST

        self.__recovery = recovery
        self.__store = store
        self.__store_snapshot = snapshot_reader(store)
        self.__sender_check = sender_function.__get__(recovery, RecoveryController)
        self.__sender_check_code = _CANONICAL_VALIDATE_SENDER_CODE
        self.__recover_uncertainty = recover_function.__get__(
            recovery, RecoveryController
        )
        self.__recover_uncertainty_code = _CANONICAL_RECOVER_DURABLE_UNCERTAINTY_CODE
        self.__journal_snapshot_reader = snapshot_reader
        self.__journal_snapshot_reader_code = _CANONICAL_JOURNAL_AUTHORITY_SNAPSHOT_CODE
        self.__reconciliation_reader = reconciliation_reader
        self.__reconciliation_reader_code = _CANONICAL_LOAD_LATEST_RECONCILIATION_CODE
        self.__latest_effectful_submission_sequence = effectful_reader
        self.__latest_effectful_submission_sequence_code = (
            _CANONICAL_LATEST_EFFECTFUL_SUBMISSION_SEQUENCE_CODE
        )
        self.__payload_digest = digest_function
        self.__payload_digest_code = _CANONICAL_PAYLOAD_DIGEST_CODE
        self.__owner = owner
        self.__environment = normalized_environment
        self.__account_id = normalized_account
        self.__bybit_provider_environment = normalized_bybit_environment
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
        try:
            if _bybit_environment(
                self.__bybit_provider_environment, self.__environment
            ) != self.__bybit_provider_environment:
                raise PermissionError("issued Bybit provider environment changed")
        except (TypeError, ValueError) as error:
            raise PermissionError("issued Bybit provider environment is invalid") from error
        source = vars(self.__recovery).get(_TAKEOVER_SOURCE_ATTR)
        if source is not None:
            if type(source) is not OwnerFence:
                raise PermissionError("takeover source owner authority changed")
            if type(source.owner_id) is not str or not source.owner_id:
                raise PermissionError("takeover source owner identity is invalid")
            if type(source.epoch) is not int or source.epoch < 1:
                raise PermissionError("takeover source owner epoch is invalid")
            if self.__owner == source:
                raise PermissionError(
                    "takeover source owner cannot retain recovery-issued sender authority"
                )

        sender_function = self.__sender_check.__func__
        if self.__sender_check.__self__ is not self.__recovery:
            raise PermissionError("bound recovery sender authority changed")
        if RecoveryController.validate_sender is not sender_function:
            raise PermissionError("recovery sender validator authority changed")
        if sender_function.__code__ is not self.__sender_check_code:
            raise PermissionError("recovery sender validator code changed")

        recover_function = self.__recover_uncertainty.__func__
        if self.__recover_uncertainty.__self__ is not self.__recovery:
            raise PermissionError("bound recovery uncertainty authority changed")
        if (
            RecoveryController.recover_durable_submission_uncertainty
            is not recover_function
        ):
            raise PermissionError("recovery uncertainty authority changed")
        if recover_function.__code__ is not self.__recover_uncertainty_code:
            raise PermissionError("recovery uncertainty authority code changed")

        snapshot_reader = self.__journal_snapshot_reader
        if _canonical_journal_authority_snapshot is not snapshot_reader:
            raise PermissionError("journal snapshot authority changed")
        if snapshot_reader.__code__ is not self.__journal_snapshot_reader_code:
            raise PermissionError("journal snapshot authority code changed")
        if snapshot_reader(self.__store) != self.__store_snapshot:
            raise PermissionError("submission journal generation changed")

        reconciliation_reader = self.__reconciliation_reader
        if (
            load_latest_reconciliation_checkpoint_for_scope
            is not reconciliation_reader
        ):
            raise PermissionError("reconciliation reader authority changed")
        if reconciliation_reader.__code__ is not self.__reconciliation_reader_code:
            raise PermissionError("reconciliation reader authority code changed")

        effectful_reader = self.__latest_effectful_submission_sequence
        if _latest_effectful_submission_sequence is not effectful_reader:
            raise PermissionError("effectful submission reader authority changed")
        if (
            effectful_reader.__code__
            is not self.__latest_effectful_submission_sequence_code
        ):
            raise PermissionError("effectful submission reader authority code changed")

        digest_function = self.__payload_digest
        if payload_digest is not digest_function:
            raise PermissionError("payload digest authority changed")
        if digest_function.__code__ is not self.__payload_digest_code:
            raise PermissionError("payload digest authority code changed")

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

        checkpoint = self.__reconciliation_reader(
            self.__store,
            provider_id=provider.strip().upper(),
            account_id=self.__account_id,
            environment=self.__environment,
            provider_environment=(
                self.__bybit_provider_environment
                if provider.strip().upper() == "BYBIT" else None
            ),
        )
        if checkpoint is None:
            raise PermissionError(
                "production sender requires a current durable reconciliation checkpoint"
            )
        if type(checkpoint) is not dict:
            raise PermissionError("durable reconciliation checkpoint is invalid")
        payload = checkpoint.get("payload")
        if (
            checkpoint.get("event_type") != "AccountReconciled"
            or checkpoint.get("aggregate_type") != "account_reconciliation"
            or type(payload) is not dict
            or self.__payload_digest(payload) != checkpoint.get("payload_hash")
        ):
            raise PermissionError("durable reconciliation payload is invalid")
        checkpoint_sequence = checkpoint.get("journal_sequence")
        if type(checkpoint_sequence) is not int or checkpoint_sequence < 1:
            raise PermissionError("durable reconciliation journal sequence is invalid")
        latest_effectful_sequence = self.__latest_effectful_submission_sequence(
            self.__store,
            environment=self.__environment,
            account_id=self.__account_id,
        )
        if checkpoint_sequence <= latest_effectful_sequence:
            raise PermissionError(
                "durable reconciliation predates latest durable send state"
            )
        checkpoint_owner = payload.get("checkpoint_owner")
        if (
            type(checkpoint_owner) is not dict
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
        if type(blocking) is not list or type(resolutions) is not list:
            raise PermissionError("durable reconciliation readiness fields are invalid")
        if blocking:
            raise PermissionError("durable reconciliation has blocking resources")
        for resolution in resolutions:
            if type(resolution) is not dict:
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
        """Dispatch with recovery-owned sender validation at the final guard."""

        del sender_check
        self._require_issued_authority()

        def canonical_sender_check(owner_id: str, owner_epoch: int) -> None:
            # Revalidate executable provenance at the actual irreversible cut.
            # This closes callback-time retargeting after the outer dispatch
            # preflight but before SubmissionSending/provider I/O.
            self._require_issued_authority()
            self.__recover_uncertainty(
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
            # Durable UNKNOWN must remain the public outcome even if executable
            # authority is concurrently damaged after the possible provider send.
            # Reflect the blocker into process-local readiness whenever the
            # retained canonical recovery reader is still trustworthy.
            try:
                self._require_issued_authority()
            except PermissionError:
                self.__recovery.provider_reconciled = False
                self.__recovery.reason_codes.add("provider_uncertainty")
                self.__recovery.state = HostState.DEGRADED
            else:
                self.__recover_uncertainty(
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
    bybit_provider_environment: str | None = None,
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
    source = state.get(_TAKEOVER_SOURCE_ATTR)
    if source is not None:
        if type(source) is not OwnerFence:
            raise PermissionError("takeover source owner authority changed")
        if type(source.owner_id) is not str or not source.owner_id:
            raise PermissionError("takeover source owner identity is invalid")
        if type(source.epoch) is not int or source.epoch < 1:
            raise PermissionError("takeover source owner epoch is invalid")
        if owner == source:
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
        bybit_provider_environment=bybit_provider_environment,
        issuance_token=_ISSUANCE_TOKEN,
    )
