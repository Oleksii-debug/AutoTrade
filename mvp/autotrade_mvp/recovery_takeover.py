"""Crash-resumable durable recovery takeover issuer.

This module composes the existing recovery owner journal, sender-authority gate,
provider reconciliation checkpoint, ProtectedCredentialVault transition receipt,
and independent JournalStore credential-transition anchor. It never accepts a
caller boolean or caller-authored proof object as takeover authority.

The ordering is deliberately split because real write transports can already
hold a credential lease before invoking terminal ``final_guard``:

1. Under the sender gate, prove the current durable source owner, no unresolved
   send ambiguity, and a reconciliation checkpoint that is not older than any
   durable provider-send state. Append ``RecoveryTakeoverStarted``.
2. Release the sender gate. STARTED now freezes every ordinary PAPER/LIVE sender.
   Only then wait for the credential-vault lock, revoke the exact TRADE
   generation, independently anchor its vault-issued receipt in JournalStore,
   and append sealed ``RecoveryTakeoverEvidenceIssued``.
3. Reacquire the takeover gate, revalidate the frozen transition, credential
   receipt/anchor and send uncertainty, append the next ``RecoveryOwnerChanged``,
   then append ``RecoveryTakeoverOwnerCommitted``.

This freeze -> vault transition -> commit ordering removes the AB/BA cycle where
an old sender holds the vault lease while waiting for final_guard and takeover
holds the sender gate while waiting for the vault. Process death in any gap
leaves ordinary sends frozen and the transition resumable. The target owner is
always RECOVERING and must obtain fresh owner-bound reconciliation before READY.
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from . import credential_transition_receipt as transition
from .credential_transition_journal import (
    record_current_trade_credential_transition_anchor,
    require_current_trade_credential_transition_anchor,
)
from .credential_transition_receipt import (
    CredentialTransitionReceipt,
    revoke_trade_credential_with_receipt,
    verify_trade_credential_transition_receipt,
)
from .persistence import JournalStore, payload_digest
from .recovery import HostState, OwnerFence, RecoveryController
from .sender_authority import SenderAuthorityLease, takeover_authority_window
from .windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    _exclusive_file_lock,
)


class DurableTakeoverError(PermissionError):
    """Raised when durable owner takeover cannot be proven safely."""


_TAKEOVER_AGGREGATE_TYPE = "recovery_takeover"
_STARTED = "RecoveryTakeoverStarted"
_EVIDENCE = "RecoveryTakeoverEvidenceIssued"
_COMPLETED = "RecoveryTakeoverOwnerCommitted"
_EVENT_TYPES = (_STARTED, _EVIDENCE, _COMPLETED)
_IDENTITY_FIELDS = (
    "takeover_id",
    "owner_scope",
    "source_owner_id",
    "source_owner_epoch",
    "target_owner_id",
    "target_owner_epoch",
)
_SEAL_PURPOSE = "AUTOTRADE_DURABLE_TAKEOVER_EVIDENCE_V2"
_EFFECTFUL_SUBMISSION_EVENTS = frozenset(
    {"SubmissionSending", "SubmissionSent", "SubmissionUnknown"}
)


@dataclass(frozen=True)
class DurableTakeoverResult:
    """Detached diagnostic result; the journal remains takeover authority."""

    takeover_id: str
    source_owner: OwnerFence
    target_owner: OwnerFence
    credential_transition_receipt_id: str
    takeover_evidence_event_id: str
    recovery_owner_event_id: str
    completion_event_id: str


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise DurableTakeoverError(f"{name} is required")
    return value.strip()


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise DurableTakeoverError(f"{name} must be a positive integer")
    return value


def _takeover_id(
    *, owner_scope: str, source: OwnerFence, target: OwnerFence
) -> str:
    digest = sha256(
        _canonical_bytes(
            {
                "authority": "AUTOTRADE_RECOVERY_TAKEOVER_V2",
                "owner_scope": owner_scope,
                "source_owner_id": source.owner_id,
                "source_owner_epoch": source.epoch,
                "target_owner_id": target.owner_id,
                "target_owner_epoch": target.epoch,
            }
        )
    ).hexdigest()
    return "recovery-takeover/sha256:" + digest


def _event_id(
    *,
    takeover_id: str,
    version: int,
    event_type: str,
    payload: dict[str, object],
) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://recovery.autotrade.local/takeover/"
            + takeover_id
            + "/"
            + str(version)
            + "/"
            + event_type
            + "/"
            + payload_digest(payload),
        )
    )


def _append_takeover_event(
    store: JournalStore,
    *,
    takeover_id: str,
    version: int,
    event_type: str,
    payload: dict[str, object],
) -> dict[str, object]:
    event = {
        "event_id": _event_id(
            takeover_id=takeover_id,
            version=version,
            event_type=event_type,
            payload=payload,
        ),
        "event_type": event_type,
        "aggregate_type": _TAKEOVER_AGGREGATE_TYPE,
        "aggregate_id": takeover_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": _now(),
    }
    JournalStore.append_event(store, event)
    loaded = JournalStore.load_events(
        store, _TAKEOVER_AGGREGATE_TYPE, takeover_id
    )
    if (
        len(loaded) != version
        or loaded[-1].get("event_id") != event["event_id"]
    ):
        raise DurableTakeoverError(
            "takeover journal version did not advance exactly once"
        )
    return loaded[-1]


def _started_owners(
    payload: dict[str, object], *, takeover_id: str
) -> tuple[OwnerFence, OwnerFence]:
    owner_scope = _text(payload.get("owner_scope"), name="owner_scope")
    source = OwnerFence(
        owner_id=_text(
            payload.get("source_owner_id"), name="source_owner_id"
        ),
        epoch=_positive_int(
            payload.get("source_owner_epoch"), name="source_owner_epoch"
        ),
    )
    target = OwnerFence(
        owner_id=_text(
            payload.get("target_owner_id"), name="target_owner_id"
        ),
        epoch=_positive_int(
            payload.get("target_owner_epoch"), name="target_owner_epoch"
        ),
    )
    # owner_id may be stable across a process restart. Epoch is the durable
    # sender-fence generation, so the successor identity remains distinct only
    # when it advances exactly by one below.
    if target.epoch != source.epoch + 1:
        raise DurableTakeoverError(
            "takeover owner epoch transition is invalid"
        )
    if _takeover_id(
        owner_scope=owner_scope, source=source, target=target
    ) != takeover_id:
        raise DurableTakeoverError(
            "takeover durable identity is not canonical"
        )
    return source, target


def _validate_takeover_events(
    events: list[dict[str, object]], *, takeover_id: str
) -> tuple[dict[str, object], ...]:
    if not events or len(events) > len(_EVENT_TYPES):
        raise DurableTakeoverError(
            "takeover journal cardinality is invalid"
        )
    started_payload: dict[str, object] | None = None
    validated: list[dict[str, object]] = []
    for index, event in enumerate(events, start=1):
        if type(event) is not dict:
            raise DurableTakeoverError(
                "takeover journal event is invalid"
            )
        expected_type = _EVENT_TYPES[index - 1]
        payload = event.get("payload")
        if (
            event.get("aggregate_type") != _TAKEOVER_AGGREGATE_TYPE
            or event.get("aggregate_id") != takeover_id
            or event.get("event_type") != expected_type
            or event.get("aggregate_version") != index
            or type(payload) is not dict
            or payload_digest(payload) != event.get("payload_hash")
            or event.get("event_id")
            != _event_id(
                takeover_id=takeover_id,
                version=index,
                event_type=expected_type,
                payload=payload,
            )
        ):
            raise DurableTakeoverError(
                "takeover journal sequence is invalid"
            )
        if index == 1:
            _started_owners(payload, takeover_id=takeover_id)
            started_payload = payload
        else:
            if started_payload is None:
                raise DurableTakeoverError(
                    "takeover journal start is missing"
                )
            for field_name in _IDENTITY_FIELDS:
                if payload.get(field_name) != started_payload.get(
                    field_name
                ):
                    raise DurableTakeoverError(
                        "takeover identity changed across durable transition"
                    )
        validated.append(event)
    return tuple(validated)


def _takeover_groups(
    store: JournalStore,
) -> dict[str, tuple[dict[str, object], ...]]:
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in JournalStore.load_events_by_aggregate_type(
        store, _TAKEOVER_AGGREGATE_TYPE
    ):
        if type(event) is not dict:
            raise DurableTakeoverError(
                "takeover journal event is invalid"
            )
        aggregate_id = event.get("aggregate_id")
        if type(aggregate_id) is not str or not aggregate_id:
            raise DurableTakeoverError(
                "takeover journal aggregate identity is invalid"
            )
        grouped.setdefault(aggregate_id, []).append(event)
    return {
        aggregate_id: _validate_takeover_events(
            items, takeover_id=aggregate_id
        )
        for aggregate_id, items in grouped.items()
    }


def _pending_for_scope(
    store: JournalStore, *, owner_scope: str
) -> tuple[str, tuple[dict[str, object], ...]] | None:
    pending: list[tuple[str, tuple[dict[str, object], ...]]] = []
    for aggregate_id, events in _takeover_groups(store).items():
        if events[0]["payload"].get("owner_scope") != owner_scope:
            continue
        if events[-1]["event_type"] != _COMPLETED:
            pending.append((aggregate_id, events))
    if len(pending) > 1:
        raise DurableTakeoverError(
            "multiple pending takeovers exist for one owner scope"
        )
    return pending[0] if pending else None


def _vault_snapshot(
    vault: ProtectedCredentialVault,
    *,
    handle_id: str,
) -> tuple[
    PersistentCredentialHandle,
    bool,
    CredentialTransitionReceipt | None,
]:
    """Read vault state. Never call pre-freeze while holding sender gate."""

    with _exclusive_file_lock(vault.lock_path, vault_path=vault.path):
        state = ProtectedCredentialVault._load(vault)
        record = state["records"].get(handle_id)
        if record is None:
            raise DurableTakeoverError(
                "takeover credential record is missing"
            )
        current = ProtectedCredentialVault._handle(record)
        active = record.get("active")
        if type(active) is not bool:
            raise DurableTakeoverError(
                "takeover credential active state is invalid"
            )
        section = state.get(transition._AUTHORITY_KEY)
        receipt = None
        if section is not None:
            parsed = transition._authority_section(
                state, create=False
            )
            item = parsed["latest_by_handle"].get(handle_id)
            if item is not None:
                receipt = transition._parse_receipt(item["receipt"])
    if receipt is not None:
        verify_trade_credential_transition_receipt(vault, receipt)
    return current, active, receipt


def _require_receipt_for_started(
    receipt: CredentialTransitionReceipt,
    *,
    started: dict[str, object],
) -> CredentialTransitionReceipt:
    if type(receipt) is not CredentialTransitionReceipt:
        raise DurableTakeoverError(
            "takeover transition receipt is invalid"
        )
    if receipt.operation != "REVOKED" or receipt.active_after is not False:
        raise DurableTakeoverError(
            "takeover requires an exact TRADE revocation receipt"
        )
    if (
        receipt.handle_id != started.get("credential_handle_id")
        or receipt.account_id != started.get("account_id")
        or receipt.provider != started.get("provider_id")
        or receipt.environment != started.get("environment")
        or receipt.provider_environment
        != started.get("provider_environment")
        or receipt.purpose != "TRADE"
        or receipt.prior_generation
        != started.get("credential_generation")
    ):
        raise DurableTakeoverError(
            "credential transition does not match takeover start"
        )
    return receipt


def _seal_entropy(
    vault: ProtectedCredentialVault,
    subject: dict[str, object],
) -> bytes:
    return sha256(
        _canonical_bytes(
            {
                "purpose": _SEAL_PURPOSE,
                "vault_path": str(vault.path),
                "subject": subject,
            }
        )
    ).digest()


def _seal_evidence(
    vault: ProtectedCredentialVault,
    subject: dict[str, object],
) -> str:
    message = (
        "takeover-evidence:" + payload_digest(subject)
    ).encode("utf-8")
    try:
        sealed = vault._protector.protect(
            message,
            entropy=_seal_entropy(vault, subject),
        )
    except Exception as error:
        raise DurableTakeoverError(
            "takeover evidence seal issuance failed"
        ) from error
    if type(sealed) is not bytes or not sealed:
        raise DurableTakeoverError(
            "takeover evidence seal is invalid"
        )
    return b64encode(sealed).decode("ascii")


def _verify_evidence_seal(
    vault: ProtectedCredentialVault,
    payload: dict[str, object],
) -> None:
    seal_b64 = payload.get("issuer_seal_b64")
    if type(seal_b64) is not str or not seal_b64:
        raise DurableTakeoverError(
            "takeover evidence issuer seal is missing"
        )
    subject = dict(payload)
    subject.pop("issuer_seal_b64", None)
    message = (
        "takeover-evidence:" + payload_digest(subject)
    ).encode("utf-8")
    try:
        sealed = b64decode(seal_b64, validate=True)
        unsealed = vault._protector.unprotect(
            sealed,
            entropy=_seal_entropy(vault, subject),
        )
    except Exception as error:
        raise DurableTakeoverError(
            "takeover evidence issuer seal is invalid"
        ) from error
    if unsealed != message:
        raise DurableTakeoverError(
            "takeover evidence issuer seal payload mismatch"
        )


def _owner_event(
    store: JournalStore,
    *,
    owner_scope: str,
    owner: OwnerFence,
) -> dict[str, object]:
    events = JournalStore.load_events(
        store, "recovery_owner", owner_scope
    )
    if len(events) < owner.epoch:
        raise DurableTakeoverError(
            "recovery owner event is missing"
        )
    event = events[owner.epoch - 1]
    payload = event.get("payload")
    if (
        event.get("event_type") != "RecoveryOwnerChanged"
        or event.get("aggregate_version") != owner.epoch
        or type(payload) is not dict
        or payload.get("owner_id") != owner.owner_id
        or payload.get("owner_epoch") != str(owner.epoch)
        or payload_digest(payload) != event.get("payload_hash")
    ):
        raise DurableTakeoverError(
            "recovery owner event does not match takeover target"
        )
    return event


def _bind_controller_owner(
    controller: RecoveryController,
    owner: OwnerFence,
    *,
    recovering: bool,
) -> None:
    controller.owner = owner
    if recovering:
        controller.provider_reconciled = False
        controller.reason_codes.discard(
            "lease_expired_no_failover"
        )
        controller.reason_codes.add(
            "startup_reconciliation_required"
        )
        controller.state = HostState.RECOVERING


def _require_reconciliation(
    controller: RecoveryController,
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
) -> dict[str, object]:
    controller._recover_scoped_submission_uncertainty_from_owner_scope()
    checkpoint = controller.record_reconciliation_checkpoint(
        reconciliation_id=reconciliation_id,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if controller.unresolved_attempts:
        raise DurableTakeoverError(
            "takeover is blocked by unresolved SubmissionSending/SubmissionUnknown"
        )
    if not controller.provider_reconciled:
        raise DurableTakeoverError(
            "takeover requires current complete reconciliation"
        )
    return checkpoint


def _latest_effectful_submission_sequence(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
) -> int:
    grouped: dict[str, list[dict[str, object]]] = {}
    for event in JournalStore.load_events_by_aggregate_type(
        store, "submission_attempt"
    ):
        aggregate_id = event.get("aggregate_id")
        if type(aggregate_id) is not str or not aggregate_id:
            raise DurableTakeoverError(
                "submission aggregate identity is invalid"
            )
        grouped.setdefault(aggregate_id, []).append(event)
    latest = 0
    for events in grouped.values():
        prepared = events[0]
        if prepared.get("event_type") != "SubmissionPrepared":
            raise DurableTakeoverError(
                "submission aggregate does not start with SubmissionPrepared"
            )
        payload = prepared.get("payload")
        if type(payload) is not dict:
            raise DurableTakeoverError(
                "SubmissionPrepared payload is invalid"
            )
        if (
            str(payload.get("environment", "")).strip().upper()
            != environment
            or str(payload.get("account_id", "")).strip()
            != account_id
        ):
            continue
        for event in events:
            if (
                event.get("event_type")
                not in _EFFECTFUL_SUBMISSION_EVENTS
            ):
                continue
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence < 1:
                raise DurableTakeoverError(
                    "effectful submission journal sequence is invalid"
                )
            latest = max(latest, sequence)
    return latest


def _require_checkpoint_event(
    store: JournalStore,
    started: dict[str, object],
) -> dict[str, object]:
    event_id = started.get("reconciliation_event_id")
    if type(event_id) is not str or not event_id:
        raise DurableTakeoverError(
            "takeover reconciliation event identity is invalid"
        )
    event = JournalStore.get_event(store, event_id)
    if event is None:
        raise DurableTakeoverError(
            "takeover reconciliation event disappeared"
        )
    if (
        event.get("payload_hash")
        != started.get("reconciliation_payload_hash")
        or event.get("journal_sequence")
        != started.get("reconciliation_journal_sequence")
    ):
        raise DurableTakeoverError(
            "takeover reconciliation authority changed"
        )
    return event


def _parse_started(
    started: dict[str, object],
    *,
    takeover_id: str,
    owner_scope: str,
    new_owner_id: str,
    handle: PersistentCredentialHandle,
    reconciliation_id: str,
    provider_id: str,
    lease: SenderAuthorityLease | None = None,
) -> tuple[OwnerFence, OwnerFence]:
    source, target = _started_owners(
        started, takeover_id=takeover_id
    )
    expected = {
        "owner_scope": owner_scope,
        "target_owner_id": new_owner_id,
        "credential_handle_id": handle.handle_id,
        "credential_generation": handle.generation,
        "account_id": handle.account_id,
        "provider_id": provider_id,
        "environment": handle.environment,
        "provider_environment": handle.provider_environment,
        "reconciliation_id": reconciliation_id,
    }
    if lease is not None:
        expected.update(
            {
                "journal_path": lease.journal_path,
                "gate_path": lease.gate_path,
            }
        )
    for key, value in expected.items():
        if started.get(key) != value:
            raise DurableTakeoverError(
                f"pending takeover {key} does not match resume request"
            )
    return source, target


def _common_inputs(
    controller: RecoveryController,
    *,
    new_owner_id: str,
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    execution_identity: str,
    reconciliation_id: str,
    provider_id: str,
) -> tuple[JournalStore, str, str, str, str, str]:
    if type(controller) is not RecoveryController:
        raise TypeError(
            "controller must be an exact RecoveryController"
        )
    if type(vault) is not ProtectedCredentialVault:
        raise TypeError(
            "vault must be an exact ProtectedCredentialVault"
        )
    if type(handle) is not PersistentCredentialHandle:
        raise TypeError(
            "handle must be an exact PersistentCredentialHandle"
        )
    target = _text(new_owner_id, name="new_owner_id")
    identity = _text(
        execution_identity, name="execution_identity"
    )
    reconciliation = _text(
        reconciliation_id, name="reconciliation_id"
    )
    provider = _text(provider_id, name="provider_id").upper()
    if handle.purpose != "TRADE":
        raise DurableTakeoverError(
            "takeover requires a TRADE credential"
        )
    if handle.environment not in {"PAPER", "LIVE"}:
        raise DurableTakeoverError(
            "durable takeover is limited to PAPER/LIVE"
        )
    if handle.provider != provider:
        raise DurableTakeoverError(
            "takeover provider does not match credential"
        )
    store = vars(controller).get("_owner_store")
    if type(store) is not JournalStore:
        raise DurableTakeoverError(
            "durable takeover requires canonical JournalStore"
        )
    owner_scope = controller.owner_scope
    if owner_scope != f"{handle.environment}:{handle.account_id}":
        raise DurableTakeoverError(
            "recovery owner scope does not match credential"
        )
    return (
        store,
        owner_scope,
        target,
        identity,
        reconciliation,
        provider,
    )


def execute_durable_takeover(
    controller: RecoveryController,
    *,
    new_owner_id: str,
    vault: ProtectedCredentialVault,
    handle: PersistentCredentialHandle,
    execution_identity: str,
    reconciliation_id: str,
    provider_id: str,
) -> DurableTakeoverResult:
    """Issue or resume one freeze-first durable owner takeover."""

    (
        store,
        owner_scope,
        target_owner_id,
        execution_identity,
        reconciliation_id,
        provider,
    ) = _common_inputs(
        controller,
        new_owner_id=new_owner_id,
        vault=vault,
        handle=handle,
        execution_identity=execution_identity,
        reconciliation_id=reconciliation_id,
        provider_id=provider_id,
    )

    # Phase 1: durable freeze. Do not touch the vault under this sender gate.
    with takeover_authority_window(
        store, owner_scope=owner_scope
    ) as lease:
        pending = _pending_for_scope(
            store, owner_scope=owner_scope
        )
        if pending is None:
            if controller.owner is None:
                raise DurableTakeoverError(
                    "no active source owner to take over"
                )
            source = controller.owner
            if controller._latest_durable_owner() != source:
                raise DurableTakeoverError(
                    "source owner is not current durable owner"
                )
            # A production host_id is stable configuration, not a process nonce.
            # Same-id restart takeover is valid only through the mandatory next epoch.
            target = OwnerFence(
                target_owner_id, source.epoch + 1
            )
            checkpoint = _require_reconciliation(
                controller,
                reconciliation_id=reconciliation_id,
                provider_id=provider,
                account_id=handle.account_id,
                environment=handle.environment,
            )
            latest_effectful = (
                _latest_effectful_submission_sequence(
                    store,
                    environment=handle.environment,
                    account_id=handle.account_id,
                )
            )
            checkpoint_sequence = checkpoint.get(
                "journal_sequence"
            )
            if (
                type(checkpoint_sequence) is not int
                or checkpoint_sequence < latest_effectful
            ):
                raise DurableTakeoverError(
                    "provider reconciliation predates latest durable send state"
                )
            takeover_id = _takeover_id(
                owner_scope=owner_scope,
                source=source,
                target=target,
            )
            started_payload: dict[str, object] = {
                "takeover_id": takeover_id,
                "owner_scope": owner_scope,
                "source_owner_id": source.owner_id,
                "source_owner_epoch": source.epoch,
                "target_owner_id": target.owner_id,
                "target_owner_epoch": target.epoch,
                "credential_handle_id": handle.handle_id,
                "credential_generation": handle.generation,
                "account_id": handle.account_id,
                "provider_id": provider,
                "environment": handle.environment,
                "provider_environment": handle.provider_environment,
                "reconciliation_id": reconciliation_id,
                "reconciliation_event_id": checkpoint[
                    "event_id"
                ],
                "reconciliation_payload_hash": checkpoint[
                    "payload_hash"
                ],
                "reconciliation_journal_sequence": (
                    checkpoint_sequence
                ),
                "latest_effectful_submission_sequence": (
                    latest_effectful
                ),
                "journal_path": lease.journal_path,
                "gate_path": lease.gate_path,
            }
            started_event = _append_takeover_event(
                store,
                takeover_id=takeover_id,
                version=1,
                event_type=_STARTED,
                payload=started_payload,
            )
            events: tuple[dict[str, object], ...] = (
                started_event,
            )
        else:
            takeover_id, events = pending
            started_payload = events[0]["payload"]
            source, target = _parse_started(
                started_payload,
                takeover_id=takeover_id,
                owner_scope=owner_scope,
                new_owner_id=target_owner_id,
                handle=handle,
                reconciliation_id=reconciliation_id,
                provider_id=provider,
                lease=lease,
            )

        durable_owner = controller._latest_durable_owner()
        if durable_owner == source:
            _bind_controller_owner(
                controller, source, recovering=False
            )
        elif durable_owner == target:
            _bind_controller_owner(
                controller, target, recovering=True
            )
        else:
            raise DurableTakeoverError(
                "durable owner changed outside takeover sequence"
            )

    # Phase 2: STARTED is durable and freezes ordinary sends, so now it is safe
    # to wait for the vault lock without holding the sender gate.
    pending = _pending_for_scope(
        store, owner_scope=owner_scope
    )
    if pending is None:
        raise DurableTakeoverError(
            "durable takeover freeze disappeared before credential transition"
        )
    takeover_id, events = pending
    started_payload = events[0]["payload"]
    source, target = _parse_started(
        started_payload,
        takeover_id=takeover_id,
        owner_scope=owner_scope,
        new_owner_id=target_owner_id,
        handle=handle,
        reconciliation_id=reconciliation_id,
        provider_id=provider,
    )
    _require_checkpoint_event(store, started_payload)

    if len(events) < 2:
        if controller._latest_durable_owner() != source:
            raise DurableTakeoverError(
                "takeover owner committed before credential evidence issuance"
            )
        current_handle, active, current_receipt = (
            _vault_snapshot(
                vault,
                handle_id=handle.handle_id,
            )
        )
        if active is True:
            if current_handle != handle:
                raise DurableTakeoverError(
                    "credential generation changed during pending takeover"
                )
            receipt = revoke_trade_credential_with_receipt(
                vault,
                handle,
                execution_identity=execution_identity,
            )
            verify_trade_credential_transition_receipt(
                vault, receipt
            )
        else:
            if current_receipt is None:
                raise DurableTakeoverError(
                    "inactive credential lacks resumable transition receipt"
                )
            receipt = current_receipt
        receipt = _require_receipt_for_started(
            receipt,
            started=started_payload,
        )
        anchor = record_current_trade_credential_transition_anchor(
            store,
            vault,
            receipt,
        )
        evidence_subject: dict[str, object] = {
            "takeover_id": takeover_id,
            "owner_scope": owner_scope,
            "source_owner_id": source.owner_id,
            "source_owner_epoch": source.epoch,
            "target_owner_id": target.owner_id,
            "target_owner_epoch": target.epoch,
            "started_event_id": events[0]["event_id"],
            "journal_path": started_payload["journal_path"],
            "gate_path": started_payload["gate_path"],
            "reconciliation_event_id": started_payload[
                "reconciliation_event_id"
            ],
            "credential_transition_receipt_id": (
                receipt.receipt_id
            ),
            "credential_transition_sequence": (
                receipt.transition_sequence
            ),
            "credential_transition_operation": receipt.operation,
            "credential_prior_generation": (
                receipt.prior_generation
            ),
            "credential_vault_authority_sha256": (
                receipt.vault_authority_sha256
            ),
            "credential_record_state_sha256": (
                receipt.record_state_sha256
            ),
            "credential_anchor_aggregate_id": (
                anchor.aggregate_id
            ),
            "credential_anchor_event_id": anchor.event_id,
            "credential_anchor_payload_hash": (
                anchor.payload_hash
            ),
            "credential_anchor_journal_sequence": (
                anchor.journal_sequence
            ),
        }
        evidence_payload = dict(evidence_subject)
        evidence_payload["issuer_seal_b64"] = _seal_evidence(
            vault,
            evidence_subject,
        )
        evidence_event = _append_takeover_event(
            store,
            takeover_id=takeover_id,
            version=2,
            event_type=_EVIDENCE,
            payload=evidence_payload,
        )
        events = (events[0], evidence_event)
    else:
        evidence_payload = events[1]["payload"]
        _verify_evidence_seal(vault, evidence_payload)
        _, active, current_receipt = _vault_snapshot(
            vault,
            handle_id=handle.handle_id,
        )
        if active is not False or current_receipt is None:
            raise DurableTakeoverError(
                "credential revocation no longer matches issued takeover evidence"
            )
        receipt = _require_receipt_for_started(
            current_receipt,
            started=started_payload,
        )
        if (
            evidence_payload.get(
                "credential_transition_receipt_id"
            )
            != receipt.receipt_id
        ):
            raise DurableTakeoverError(
                "issued takeover evidence does not match current vault receipt"
            )
        anchor = require_current_trade_credential_transition_anchor(
            store,
            vault,
            receipt,
        )
        if (
            evidence_payload.get("credential_anchor_event_id")
            != anchor.event_id
            or evidence_payload.get(
                "credential_anchor_payload_hash"
            )
            != anchor.payload_hash
        ):
            raise DurableTakeoverError(
                "issued takeover evidence does not match durable credential anchor"
            )

    # Phase 3: old credential is durably inactive; now reacquire the same sender
    # gate, rescan ambiguity, and atomically advance durable sender authority.
    with takeover_authority_window(
        store, owner_scope=owner_scope
    ) as lease:
        pending = _pending_for_scope(
            store, owner_scope=owner_scope
        )
        if pending is None:
            raise DurableTakeoverError(
                "durable takeover disappeared before owner commit"
            )
        pending_id, events = pending
        if pending_id != takeover_id or len(events) < 2:
            raise DurableTakeoverError(
                "takeover evidence is unavailable at owner commit"
            )
        started_payload = events[0]["payload"]
        source, target = _parse_started(
            started_payload,
            takeover_id=takeover_id,
            owner_scope=owner_scope,
            new_owner_id=target_owner_id,
            handle=handle,
            reconciliation_id=reconciliation_id,
            provider_id=provider,
            lease=lease,
        )
        _require_checkpoint_event(store, started_payload)
        controller._recover_scoped_submission_uncertainty_from_owner_scope()
        if controller.unresolved_attempts:
            raise DurableTakeoverError(
                "takeover commit is blocked by durable submission uncertainty"
            )

        evidence_payload = events[1]["payload"]
        _verify_evidence_seal(vault, evidence_payload)
        _, active, current_receipt = _vault_snapshot(
            vault,
            handle_id=handle.handle_id,
        )
        if active is not False or current_receipt is None:
            raise DurableTakeoverError(
                "credential revocation changed before owner commit"
            )
        receipt = _require_receipt_for_started(
            current_receipt,
            started=started_payload,
        )
        if (
            evidence_payload.get(
                "credential_transition_receipt_id"
            )
            != receipt.receipt_id
        ):
            raise DurableTakeoverError(
                "takeover evidence credential identity changed"
            )
        anchor = require_current_trade_credential_transition_anchor(
            store,
            vault,
            receipt,
        )
        if (
            evidence_payload.get("credential_anchor_event_id")
            != anchor.event_id
            or evidence_payload.get(
                "credential_anchor_payload_hash"
            )
            != anchor.payload_hash
        ):
            raise DurableTakeoverError(
                "credential transition anchor changed before owner commit"
            )

        durable_owner = controller._latest_durable_owner()
        if durable_owner == source:
            controller._append_durable_owner(target)
            durable_owner = controller._latest_durable_owner()
        if durable_owner != target:
            raise DurableTakeoverError(
                "target recovery owner was not durably committed"
            )
        recovery_owner_event = _owner_event(
            store,
            owner_scope=owner_scope,
            owner=target,
        )

        if len(events) < 3:
            completion_payload: dict[str, object] = {
                "takeover_id": takeover_id,
                "owner_scope": owner_scope,
                "source_owner_id": source.owner_id,
                "source_owner_epoch": source.epoch,
                "target_owner_id": target.owner_id,
                "target_owner_epoch": target.epoch,
                "takeover_evidence_event_id": events[1][
                    "event_id"
                ],
                "credential_transition_receipt_id": (
                    receipt.receipt_id
                ),
                "recovery_owner_event_id": recovery_owner_event[
                    "event_id"
                ],
                "recovery_owner_payload_hash": recovery_owner_event[
                    "payload_hash"
                ],
                "recovery_owner_journal_sequence": (
                    recovery_owner_event["journal_sequence"]
                ),
            }
            completion_event = _append_takeover_event(
                store,
                takeover_id=takeover_id,
                version=3,
                event_type=_COMPLETED,
                payload=completion_payload,
            )
            events = (
                events[0],
                events[1],
                completion_event,
            )

        _bind_controller_owner(
            controller, target, recovering=True
        )
        return DurableTakeoverResult(
            takeover_id=takeover_id,
            source_owner=source,
            target_owner=target,
            credential_transition_receipt_id=str(
                events[1]["payload"][
                    "credential_transition_receipt_id"
                ]
            ),
            takeover_evidence_event_id=str(
                events[1]["event_id"]
            ),
            recovery_owner_event_id=str(
                recovery_owner_event["event_id"]
            ),
            completion_event_id=str(
                events[2]["event_id"]
            ),
        )
