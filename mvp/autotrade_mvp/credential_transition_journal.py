"""Durable JournalStore anchors for current TRADE credential transition receipts.

This module closes one narrow WP-49 gap: vault-issued transition evidence lives in
one rollbackable vault file.  A canonical JournalStore anchor supplies an
independent monotonic history for the *current* verified receipt so a vault-only
rollback cannot silently make an older receipt current again.

It is deliberately not sender-takeover authority.  It does not prove process
exit, provider-origin credential non-acceptance, UNKNOWN-send reconciliation, or
successor readiness.  Terminal recovery must compose those independent facts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import NAMESPACE_URL, uuid5

from .credential_transition_receipt import (
    CredentialTransitionReceipt,
    CredentialTransitionReceiptError,
    verify_trade_credential_transition_receipt,
)
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .windows_secrets import ProtectedCredentialVault


_AGGREGATE_TYPE = "credential_transition_anchor"
_EVENT_TYPE = "CredentialTransitionAnchored"
_PAYLOAD_FIELDS = frozenset(
    {
        "receipt_id",
        "receipt_schema_version",
        "operation",
        "handle_id",
        "account_id",
        "provider",
        "environment",
        "provider_environment",
        "purpose",
        "prior_generation",
        "successor_generation",
        "active_after",
        "owner_identity_sha256",
        "vault_authority_sha256",
        "record_state_sha256",
        "previous_receipt_id",
        "transition_sequence",
        "completed_time_ns",
    }
)


class CredentialTransitionAnchorError(PermissionError):
    """Raised when durable transition-anchor authority is absent or inconsistent."""


@dataclass(frozen=True)
class CredentialTransitionAnchorWitness:
    """Detached readback identity for one accepted durable anchor.

    The witness is evidence for auditing/composition only.  Possessing it grants
    no provider-send, recovery-owner, or restore-completion authority.
    """

    aggregate_id: str
    event_id: str
    payload_hash: str
    journal_sequence: int
    verified_journal_cut: int
    receipt_id: str
    transition_sequence: int

    def __post_init__(self) -> None:
        for name in ("aggregate_id", "event_id", "receipt_id"):
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise CredentialTransitionAnchorError(
                    f"credential transition anchor {name} is invalid"
                )
        if (
            type(self.payload_hash) is not str
            or not self.payload_hash.startswith("sha256:")
            or len(self.payload_hash) != 71
            or any(
                character not in "0123456789abcdef"
                for character in self.payload_hash[7:]
            )
        ):
            raise CredentialTransitionAnchorError(
                "credential transition anchor payload hash is invalid"
            )
        if (
            type(self.journal_sequence) is not int
            or self.journal_sequence < 1
            or type(self.verified_journal_cut) is not int
            or self.verified_journal_cut < self.journal_sequence
            or type(self.transition_sequence) is not int
            or self.transition_sequence < 1
        ):
            raise CredentialTransitionAnchorError(
                "credential transition anchor sequence is invalid"
            )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _aggregate_id(receipt: CredentialTransitionReceipt) -> str:
    logical_scope = {
        "account_id": receipt.account_id,
        "environment": receipt.environment,
        "provider_environment": receipt.provider_environment,
        "handle_id": receipt.handle_id,
        "provider": receipt.provider,
        "purpose": receipt.purpose,
    }
    digest = sha256(_canonical_bytes(logical_scope)).hexdigest()
    return "credential-transition-anchor/sha256:" + digest


def _event_payload(receipt: CredentialTransitionReceipt) -> dict[str, object]:
    return {
        "receipt_id": receipt.receipt_id,
        "receipt_schema_version": receipt.schema_version,
        "operation": receipt.operation,
        "handle_id": receipt.handle_id,
        "account_id": receipt.account_id,
        "provider": receipt.provider,
        "environment": receipt.environment,
        "provider_environment": receipt.provider_environment,
        "purpose": receipt.purpose,
        "prior_generation": receipt.prior_generation,
        "successor_generation": receipt.successor_generation,
        "active_after": receipt.active_after,
        "owner_identity_sha256": receipt.owner_identity_sha256,
        "vault_authority_sha256": receipt.vault_authority_sha256,
        "record_state_sha256": receipt.record_state_sha256,
        "previous_receipt_id": receipt.previous_receipt_id,
        "transition_sequence": receipt.transition_sequence,
        "completed_time_ns": receipt.completed_time_ns,
    }


def _event_id(aggregate_id: str, receipt: CredentialTransitionReceipt) -> str:
    return str(
        uuid5(
            NAMESPACE_URL,
            "https://recovery.autotrade.local/credential-transition-anchor/"
            + aggregate_id
            + "/"
            + str(receipt.transition_sequence)
            + "/"
            + receipt.receipt_id,
        )
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt_from_payload(
    payload: object,
) -> CredentialTransitionReceipt:
    if type(payload) is not dict or set(payload) != _PAYLOAD_FIELDS:
        raise CredentialTransitionAnchorError(
            "credential transition anchor payload shape is invalid"
        )
    try:
        return CredentialTransitionReceipt(
            schema_version=payload["receipt_schema_version"],
            receipt_id=payload["receipt_id"],
            operation=payload["operation"],
            handle_id=payload["handle_id"],
            account_id=payload["account_id"],
            provider=payload["provider"],
            environment=payload["environment"],
            provider_environment=payload["provider_environment"],
            purpose=payload["purpose"],
            prior_generation=payload["prior_generation"],
            successor_generation=payload["successor_generation"],
            active_after=payload["active_after"],
            owner_identity_sha256=payload["owner_identity_sha256"],
            vault_authority_sha256=payload["vault_authority_sha256"],
            record_state_sha256=payload["record_state_sha256"],
            previous_receipt_id=payload["previous_receipt_id"],
            transition_sequence=payload["transition_sequence"],
            completed_time_ns=payload["completed_time_ns"],
        )
    except (KeyError, TypeError, ValueError, CredentialTransitionReceiptError) as error:
        raise CredentialTransitionAnchorError(
            "credential transition anchor payload is invalid"
        ) from error


def _validate_chain(
    aggregate_id: str,
    events: list[dict[str, object]],
) -> tuple[CredentialTransitionReceipt, ...]:
    parsed: list[CredentialTransitionReceipt] = []
    prior: CredentialTransitionReceipt | None = None

    for expected_version, event in enumerate(events, start=1):
        if event.get("event_type") != _EVENT_TYPE:
            raise CredentialTransitionAnchorError(
                "credential transition anchor event type is invalid"
            )
        if (
            event.get("aggregate_type") != _AGGREGATE_TYPE
            or event.get("aggregate_id") != aggregate_id
            or event.get("aggregate_version") != expected_version
        ):
            raise CredentialTransitionAnchorError(
                "credential transition anchor aggregate chain is invalid"
            )
        payload = event.get("payload")
        receipt = _receipt_from_payload(payload)
        receipt_subject = asdict(receipt)
        receipt_subject.pop("receipt_id")
        expected_receipt_id = (
            "credential-transition/sha256:"
            + sha256(_canonical_bytes(receipt_subject)).hexdigest()
        )
        if receipt.receipt_id != expected_receipt_id:
            raise CredentialTransitionAnchorError(
                "credential transition anchor receipt content identity mismatch"
            )
        if _aggregate_id(receipt) != aggregate_id:
            raise CredentialTransitionAnchorError(
                "credential transition anchor logical scope changed"
            )
        if event.get("payload_hash") != payload_digest(payload):
            raise CredentialTransitionAnchorError(
                "credential transition anchor payload hash mismatch"
            )
        if event.get("event_id") != _event_id(aggregate_id, receipt):
            raise CredentialTransitionAnchorError(
                "credential transition anchor event identity is invalid"
            )
        journal_sequence = event.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence < 1:
            raise CredentialTransitionAnchorError(
                "credential transition anchor journal sequence is invalid"
            )

        if prior is not None:
            if receipt.transition_sequence != prior.transition_sequence + 1:
                raise CredentialTransitionAnchorError(
                    "credential transition anchor receipt sequence is not contiguous"
                )
            if receipt.previous_receipt_id != prior.receipt_id:
                raise CredentialTransitionAnchorError(
                    "credential transition anchor predecessor receipt does not match durable lineage"
                )
            if receipt.vault_authority_sha256 != prior.vault_authority_sha256:
                raise CredentialTransitionAnchorError(
                    "credential transition anchor vault authority changed"
                )
        prior = receipt
        parsed.append(receipt)

    return tuple(parsed)


def _witness(
    aggregate_id: str,
    event: dict[str, object],
    receipt: CredentialTransitionReceipt,
    *,
    verified_journal_cut: int,
) -> CredentialTransitionAnchorWitness:
    return CredentialTransitionAnchorWitness(
        aggregate_id=aggregate_id,
        event_id=event["event_id"],
        payload_hash=event["payload_hash"],
        journal_sequence=event["journal_sequence"],
        verified_journal_cut=verified_journal_cut,
        receipt_id=receipt.receipt_id,
        transition_sequence=receipt.transition_sequence,
    )


def record_current_trade_credential_transition_anchor(
    store: JournalStore,
    vault: ProtectedCredentialVault,
    receipt: CredentialTransitionReceipt,
) -> CredentialTransitionAnchorWitness:
    """Append or idempotently read the current verified receipt's journal anchor.

    A crash after vault transition but before this append is safe: no anchor is
    returned and terminal takeover remains blocked.  A concurrent credential
    transition after verification can only make this anchor stale; the final
    readback re-verifies vault currentness before returning.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be exact ProtectedCredentialVault")
    if type(receipt) is not CredentialTransitionReceipt:
        raise TypeError("receipt must be exact CredentialTransitionReceipt")

    store_identity = require_exact_journal_store_authority(
        store,
        subject="credential transition anchor JournalStore",
    )
    verified = verify_trade_credential_transition_receipt(vault, receipt)
    aggregate_id = _aggregate_id(verified)

    with journal_store_authority_scope(store, store_identity):
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, aggregate_id)
        chain = _validate_chain(aggregate_id, events)

        if chain:
            latest = chain[-1]
            if latest.receipt_id == verified.receipt_id:
                current = verify_trade_credential_transition_receipt(vault, verified)
                if current != latest:
                    raise CredentialTransitionAnchorError(
                        "current vault receipt differs from latest durable anchor"
                    )
                cut = JournalStore.current_journal_sequence(store)
                return _witness(
                    aggregate_id,
                    events[-1],
                    current,
                    verified_journal_cut=cut,
                )
            if verified.transition_sequence != latest.transition_sequence + 1:
                raise CredentialTransitionAnchorError(
                    "new credential transition is not the next durable receipt sequence"
                )
            if verified.previous_receipt_id != latest.receipt_id:
                raise CredentialTransitionAnchorError(
                    "new credential transition does not descend from the latest durable receipt"
                )
            if verified.vault_authority_sha256 != latest.vault_authority_sha256:
                raise CredentialTransitionAnchorError(
                    "new credential transition changed durable vault authority"
                )

        payload = _event_payload(verified)
        version = len(events) + 1
        event = {
            "event_id": _event_id(aggregate_id, verified),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": _now(),
        }
        JournalStore.append_event(store, event)

        readback = JournalStore.load_events(store, _AGGREGATE_TYPE, aggregate_id)
        readback_chain = _validate_chain(aggregate_id, readback)
        if len(readback_chain) != version or readback_chain[-1] != verified:
            raise CredentialTransitionAnchorError(
                "credential transition durable anchor readback mismatch"
            )
        current = verify_trade_credential_transition_receipt(vault, verified)
        if current != verified:
            raise CredentialTransitionAnchorError(
                "credential transition changed during durable anchor publication"
            )
        cut = JournalStore.current_journal_sequence(store)
        return _witness(
            aggregate_id,
            readback[-1],
            verified,
            verified_journal_cut=cut,
        )


def require_current_trade_credential_transition_anchor(
    store: JournalStore,
    vault: ProtectedCredentialVault,
    receipt: CredentialTransitionReceipt,
) -> CredentialTransitionAnchorWitness:
    """Require one stable JournalStore cut whose latest anchor is this receipt.

    This is still a prerequisite witness, not takeover authority.  The accepted
    global journal cut is retained in the witness so terminal composition can
    bind/revalidate it together with process/provider/reconciliation authority.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if type(vault) is not ProtectedCredentialVault:
        raise TypeError("vault must be exact ProtectedCredentialVault")
    if type(receipt) is not CredentialTransitionReceipt:
        raise TypeError("receipt must be exact CredentialTransitionReceipt")

    store_identity = require_exact_journal_store_authority(
        store,
        subject="credential transition anchor JournalStore",
    )
    aggregate_id = _aggregate_id(receipt)

    with journal_store_authority_scope(store, store_identity):
        cut_before = JournalStore.current_journal_sequence(store)
        verified = verify_trade_credential_transition_receipt(vault, receipt)
        events = JournalStore.load_events(store, _AGGREGATE_TYPE, aggregate_id)
        chain = _validate_chain(aggregate_id, events)
        if not chain:
            raise CredentialTransitionAnchorError(
                "credential transition receipt has no durable journal anchor"
            )
        if chain[-1] != verified:
            raise CredentialTransitionAnchorError(
                "credential transition receipt is not the latest durable anchor"
            )
        verified_again = verify_trade_credential_transition_receipt(vault, verified)
        cut_after = JournalStore.current_journal_sequence(store)
        if cut_after != cut_before:
            raise CredentialTransitionAnchorError(
                "credential transition anchor journal cut moved during verification"
            )
        if verified_again != verified:
            raise CredentialTransitionAnchorError(
                "credential transition receipt changed during verification"
            )
        return _witness(
            aggregate_id,
            events[-1],
            verified,
            verified_journal_cut=cut_after,
        )
