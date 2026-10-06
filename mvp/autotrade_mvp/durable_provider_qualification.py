"""Durable currentness authority for accepted provider qualification.

JournalStore is a generic durable primitive, not a qualification ACL.  Therefore
an event hash alone is not Q authority.  Every accepted event stores the exact
signed receipt; both admission and restart replay run the canonical provider
qualification verifier against the immutable ArtifactStore evidence before the
record can participate in currentness.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import re
from typing import Any

from autotrade_runtime.artifacts import ArtifactStore

from .persistence import JournalStore, canonical_json, payload_digest
from .provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationError,
    ProviderQualificationScope,
    _rehydrate_accepted_provider_qualification,
    verify_provider_qualification_campaign,
)
from .provider_qualification_current_scope import (
    ProviderQualificationCurrentScope,
)
from .qualification_attestation import (
    QualificationAttestation,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
)


_AGGREGATE_TYPE = "provider_qualification"
_ACCEPTED_EVENT = "ProviderQualificationAccepted.v1"
_SUPERSEDED_EVENT = "ProviderQualificationSuperseded.v1"
_REVOKED_EVENT = "ProviderQualificationRevoked.v1"
_SCHEMA_VERSION = "1.0.0"
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")


class ProviderQualificationCurrentUnavailable(ProviderQualificationError):
    """No current accepted Q exists for the exact provider/build/protocol scope."""


class ProviderQualificationAuthorityAmbiguous(ProviderQualificationError):
    """More than one accepted Q is current; no implicit latest-wins authority."""


@dataclass(frozen=True, slots=True)
class CurrentProviderQualification:
    qualification: AcceptedProviderQualification
    journal_sequence_cut: int

    def __post_init__(self) -> None:
        if type(self.qualification) is not AcceptedProviderQualification:
            raise TypeError(
                "qualification must be exact AcceptedProviderQualification"
            )
        if (
            type(self.journal_sequence_cut) is not int
            or self.journal_sequence_cut < 0
        ):
            raise ValueError(
                "journal_sequence_cut must be a non-negative exact integer"
            )

    @property
    def qualification_id(self) -> str:
        return self.qualification.qualification_id


@dataclass(frozen=True, slots=True)
class _History:
    accepted: dict[str, AcceptedProviderQualification]
    receipts: dict[str, tuple[str, SignedQualificationAttestation]]
    superseded: dict[str, str]
    journal_sequence_cut: int


def _qid(value: object, *, name: str) -> str:
    if type(value) is not str or _QID_RE.fullmatch(value) is None:
        raise ProviderQualificationError(
            f"{name} must be a canonical provider qualification id"
        )
    return value


def _point(value: datetime, *, name: str) -> datetime:
    # Exact datetime alone is insufficient: its nested tzinfo can still be a
    # caller-defined Python object. Reject executable timezone authority
    # before utcoffset()/astimezone() can dispatch through caller code.
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderQualificationError(
            f"{name} must be an exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _current_scope(
    record: AcceptedProviderQualification,
) -> ProviderQualificationCurrentScope:
    return ProviderQualificationCurrentScope(
        provider_scope=record.scope.provider_scope,
        product_family=record.scope.product_family,
        adapter_source_git_sha=record.scope.adapter_source_git_sha,
        packaged_artifact_digest=record.scope.packaged_artifact_digest,
        protocol_id=record.scope.protocol_id,
        protocol_version=record.scope.protocol_version,
    )


def _event_id(prefix: str, material: object) -> str:
    digest = sha256(canonical_json(material).encode("utf-8")).hexdigest()
    return f"provider-qualification-{prefix}:{digest}"


def _strict_payload(
    value: object,
    *,
    name: str,
    keys: frozenset[str],
) -> dict[str, Any]:
    if type(value) is not dict:
        raise ProviderQualificationError(f"{name} must be an exact object")
    actual = frozenset(value)
    if actual != keys:
        raise ProviderQualificationError(
            f"{name} fields mismatch: missing={sorted(keys-actual)} extra={sorted(actual-keys)}"
        )
    return value


def _receipt_payload(
    receipt: SignedQualificationAttestation,
) -> dict[str, object]:
    if type(receipt) is not SignedQualificationAttestation or type(
        receipt.attestation
    ) is not QualificationAttestation:
        raise TypeError(
            "receipt must be exact SignedQualificationAttestation"
        )
    if type(receipt.signature_b64) is not str:
        raise TypeError("qualification receipt signature must be exact text")
    return {
        "attestation": receipt.attestation.canonical_payload(),
        "signature_b64": receipt.signature_b64,
    }


def _parse_receipt(value: object) -> SignedQualificationAttestation:
    if type(value) is not dict:
        raise ProviderQualificationError(
            "durable provider qualification receipt must be an exact object"
        )
    try:
        receipt = parse_signed_qualification_attestation(value)
    except (TypeError, ValueError) as error:
        raise ProviderQualificationError(
            "durable provider qualification receipt is invalid"
        ) from error
    if type(receipt) is not SignedQualificationAttestation or type(
        receipt.attestation
    ) is not QualificationAttestation:
        raise ProviderQualificationError(
            "durable provider qualification receipt is non-canonical"
        )
    if _receipt_payload(receipt) != value:
        raise ProviderQualificationError(
            "durable provider qualification receipt is not canonical"
        )
    return receipt


class DurableProviderQualificationRegistry:
    """Authenticated journal projection for provider qualification currentness."""

    def __init__(
        self,
        store: JournalStore,
        *,
        evidence_store: ArtifactStore,
        evidence_root: str | Path,
    ) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be the canonical JournalStore")
        if type(evidence_store) is not ArtifactStore:
            raise TypeError(
                "evidence_store must be the canonical ArtifactStore"
            )
        self.store = store
        self.evidence_store = evidence_store
        self.evidence_root = evidence_root

    def _resolved_cut(self, requested: int | None) -> int:
        if requested is not None and (
            type(requested) is not int or requested < 0
        ):
            raise ValueError(
                "journal_sequence_cut must be a non-negative exact integer or None"
            )
        cut = self.store.whole_store_state_cut()
        if type(cut) is not dict:
            raise ProviderQualificationError(
                "whole-store journal cut is non-canonical"
            )
        observed = cut.get("journal_sequence")
        if type(observed) is not int or observed < 0:
            raise ProviderQualificationError(
                "whole-store journal cut lacks canonical sequence"
            )
        if requested is None:
            return observed
        if requested > observed:
            raise ProviderQualificationError(
                "requested provider qualification journal cut is in the future"
            )
        return requested

    def _authenticate_record(
        self,
        *,
        protocol_key: str,
        record: AcceptedProviderQualification,
        receipt: SignedQualificationAttestation,
    ) -> AcceptedProviderQualification:
        verified = verify_provider_qualification_campaign(
            protocol_key=protocol_key,
            receipt=receipt,
            evidence_store=self.evidence_store,
            evidence_root=self.evidence_root,
            expected_scope=record.scope,
            expected_release_artifact_id=record.release_artifact_id,
        )
        if verified != record:
            raise ProviderQualificationError(
                "durable provider qualification record differs from canonical verification"
            )
        return verified

    def _history(
        self,
        *,
        journal_sequence_cut: int | None = None,
    ) -> _History:
        resolved_cut = self._resolved_cut(journal_sequence_cut)
        events = self.store.load_events_by_aggregate_type(_AGGREGATE_TYPE)
        accepted: dict[str, AcceptedProviderQualification] = {}
        receipts: dict[str, tuple[str, SignedQualificationAttestation]] = {}
        superseded: dict[str, str] = {}
        versions: dict[str, int] = {}
        previous_sequence = 0

        for event in events:
            if type(event) is not dict:
                raise ProviderQualificationError(
                    "provider qualification journal event is not canonical"
                )
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence < 1:
                raise ProviderQualificationError(
                    "provider qualification event lacks global journal sequence"
                )
            if sequence > resolved_cut:
                continue
            if sequence <= previous_sequence:
                raise ProviderQualificationError(
                    "provider qualification journal sequence is not strictly increasing"
                )
            previous_sequence = sequence
            if event.get("aggregate_type") != _AGGREGATE_TYPE:
                raise ProviderQualificationError(
                    "provider qualification event uses wrong aggregate type"
                )
            aggregate_id = event.get("aggregate_id")
            if type(aggregate_id) is not str or not aggregate_id:
                raise ProviderQualificationError(
                    "provider qualification aggregate id is invalid"
                )
            expected_version = versions.get(aggregate_id, 0) + 1
            if event.get("aggregate_version") != expected_version:
                raise ProviderQualificationError(
                    "provider qualification aggregate version gap"
                )
            versions[aggregate_id] = expected_version
            payload = event.get("payload")
            if payload_digest(payload) != event.get("payload_hash"):
                raise ProviderQualificationError(
                    "provider qualification payload integrity failure"
                )

            event_type = event.get("event_type")
            if event_type == _ACCEPTED_EVENT:
                accepted_payload = _strict_payload(
                    payload,
                    name=_ACCEPTED_EVENT,
                    keys=frozenset(
                        {"schema_version", "protocol_key", "record", "signed_receipt"}
                    ),
                )
                if accepted_payload["schema_version"] != _SCHEMA_VERSION:
                    raise ProviderQualificationError(
                        "unsupported provider qualification accepted event schema"
                    )
                protocol_key = accepted_payload["protocol_key"]
                if type(protocol_key) is not str or not protocol_key:
                    raise ProviderQualificationError(
                        "accepted provider qualification lacks protocol key"
                    )
                record = _rehydrate_accepted_provider_qualification(
                    accepted_payload["record"]
                )
                receipt = _parse_receipt(accepted_payload["signed_receipt"])
                self._authenticate_record(
                    protocol_key=protocol_key,
                    record=record,
                    receipt=receipt,
                )
                scope = _current_scope(record)
                if scope.content_digest != aggregate_id:
                    raise ProviderQualificationError(
                        "accepted provider qualification aggregate scope mismatch"
                    )
                if record.qualification_id in accepted:
                    raise ProviderQualificationError(
                        "duplicate provider qualification acceptance event"
                    )
                expected_event_id = _event_id("accepted", accepted_payload)
                if event.get("event_id") != expected_event_id:
                    raise ProviderQualificationError(
                        "accepted provider qualification event id mismatch"
                    )
                accepted[record.qualification_id] = record
                receipts[record.qualification_id] = (protocol_key, receipt)

            elif event_type == _SUPERSEDED_EVENT:
                supersede = _strict_payload(
                    payload,
                    name=_SUPERSEDED_EVENT,
                    keys=frozenset(
                        {"schema_version", "old_qualification_id", "new_qualification_id"}
                    ),
                )
                if supersede["schema_version"] != _SCHEMA_VERSION:
                    raise ProviderQualificationError(
                        "unsupported provider qualification supersession schema"
                    )
                old_id = _qid(
                    supersede["old_qualification_id"],
                    name="old_qualification_id",
                )
                new_id = _qid(
                    supersede["new_qualification_id"],
                    name="new_qualification_id",
                )
                if old_id == new_id:
                    raise ProviderQualificationError(
                        "provider qualification cannot supersede itself"
                    )
                old = accepted.get(old_id)
                new = accepted.get(new_id)
                if old is None or new is None:
                    raise ProviderQualificationError(
                        "provider qualification supersession target is not accepted"
                    )
                if _current_scope(old) != _current_scope(new):
                    raise ProviderQualificationError(
                        "provider qualification supersession crosses current scope"
                    )
                if new.supersedes_qualification_id != old_id:
                    raise ProviderQualificationError(
                        "provider qualification supersession is not authorized by signed Q lineage"
                    )
                if old_id in superseded:
                    raise ProviderQualificationError(
                        "provider qualification was superseded more than once"
                    )
                if _current_scope(old).content_digest != aggregate_id:
                    raise ProviderQualificationError(
                        "provider qualification supersession aggregate mismatch"
                    )
                expected_event_id = _event_id(
                    "superseded",
                    {
                        "old_qualification_id": old_id,
                        "new_qualification_id": new_id,
                    },
                )
                if event.get("event_id") != expected_event_id:
                    raise ProviderQualificationError(
                        "provider qualification supersession event id mismatch"
                    )
                superseded[old_id] = new_id

            elif event_type == _REVOKED_EVENT:
                # Revocation needs its own authenticated governance/revocation
                # evidence.  Until that verifier exists, accepting a scalar
                # reason/digest would turn generic JournalStore write access into
                # authority.  Presence of such an event is therefore corruption.
                raise ProviderQualificationError(
                    "provider qualification revocation authority is not implemented"
                )
            else:
                raise ProviderQualificationError(
                    "unsupported provider qualification event type"
                )

        return _History(
            accepted=accepted,
            receipts=receipts,
            superseded=superseded,
            journal_sequence_cut=resolved_cut,
        )

    def _append_accepted(
        self,
        *,
        protocol_key: str,
        record: AcceptedProviderQualification,
        receipt: SignedQualificationAttestation,
    ) -> bool:
        history = self._history()
        existing = history.accepted.get(record.qualification_id)
        if existing is not None:
            if existing != record:
                raise ProviderQualificationError(
                    "provider qualification id conflicts with durable history"
                )
            existing_protocol, existing_receipt = history.receipts[
                record.qualification_id
            ]
            if (
                existing_protocol != protocol_key
                or _receipt_payload(existing_receipt) != _receipt_payload(receipt)
            ):
                raise ProviderQualificationError(
                    "provider qualification retry carries different proof authority"
                )
            return False
        scope = _current_scope(record)
        version = self.store.next_aggregate_version(
            _AGGREGATE_TYPE,
            scope.content_digest,
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "protocol_key": protocol_key,
            "record": record.payload(),
            "signed_receipt": _receipt_payload(receipt),
        }
        envelope = {
            "event_id": _event_id("accepted", payload),
            "event_type": _ACCEPTED_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": scope.content_digest,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": record.signed_at,
        }
        try:
            result = self.store.append_event(envelope)
        except ValueError as error:
            refreshed = self._history()
            persisted = refreshed.accepted.get(record.qualification_id)
            if persisted == record:
                return False
            raise ProviderQualificationError(
                "provider qualification history changed concurrently; fresh decision required"
            ) from error
        return result.inserted

    def _append_supersession(
        self,
        *,
        old_id: str,
        new_id: str,
    ) -> bool:
        old_id = _qid(old_id, name="old_qualification_id")
        new_id = _qid(new_id, name="new_qualification_id")
        history = self._history()
        old = history.accepted.get(old_id)
        new = history.accepted.get(new_id)
        if old is None or new is None:
            raise ProviderQualificationError(
                "provider qualification supersession requires both authenticated records"
            )
        if _current_scope(old) != _current_scope(new):
            raise ProviderQualificationError(
                "provider qualification supersession cannot cross current scope"
            )
        if new.supersedes_qualification_id != old_id:
            raise ProviderQualificationError(
                "new provider qualification does not authorize requested supersession"
            )
        existing = history.superseded.get(old_id)
        if existing is not None:
            if existing == new_id:
                return False
            raise ProviderQualificationError(
                "provider qualification already has a different supersession target"
            )
        scope = _current_scope(old)
        version = self.store.next_aggregate_version(
            _AGGREGATE_TYPE,
            scope.content_digest,
        )
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "old_qualification_id": old_id,
            "new_qualification_id": new_id,
        }
        envelope = {
            "event_id": _event_id(
                "superseded",
                {
                    "old_qualification_id": old_id,
                    "new_qualification_id": new_id,
                },
            ),
            "event_type": _SUPERSEDED_EVENT,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": scope.content_digest,
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": new.signed_at,
        }
        try:
            result = self.store.append_event(envelope)
        except ValueError as error:
            refreshed = self._history()
            if refreshed.superseded.get(old_id) == new_id:
                return False
            raise ProviderQualificationError(
                "provider qualification supersession changed concurrently"
            ) from error
        return result.inserted

    def accept_campaign(
        self,
        *,
        protocol_key: str,
        receipt: SignedQualificationAttestation,
        expected_scope: ProviderQualificationScope,
        expected_release_artifact_id: str | None = None,
    ) -> AcceptedProviderQualification:
        """Verify, persist and (when signed) supersede one provider campaign Q."""

        record = verify_provider_qualification_campaign(
            protocol_key=protocol_key,
            receipt=receipt,
            evidence_store=self.evidence_store,
            evidence_root=self.evidence_root,
            expected_scope=expected_scope,
            expected_release_artifact_id=expected_release_artifact_id,
        )
        self._append_accepted(
            protocol_key=protocol_key,
            record=record,
            receipt=receipt,
        )
        if record.supersedes_qualification_id is not None:
            # Two journal commits are deliberate.  A crash after Q2 acceptance
            # but before supersession leaves two current records, which current()
            # rejects as ambiguity.  Retrying the same signed campaign completes
            # lineage idempotently; there is no latest-wins unsafe window.
            self._append_supersession(
                old_id=record.supersedes_qualification_id,
                new_id=record.qualification_id,
            )
        return record

    def current(
        self,
        *,
        scope: ProviderQualificationCurrentScope,
        at: datetime,
        journal_sequence_cut: int | None = None,
    ) -> CurrentProviderQualification:
        if type(scope) is not ProviderQualificationCurrentScope:
            raise TypeError(
                "scope must be exact ProviderQualificationCurrentScope"
            )
        point = _point(at, name="at")
        history = self._history(journal_sequence_cut=journal_sequence_cut)
        candidates: list[AcceptedProviderQualification] = []
        for qualification_id, record in history.accepted.items():
            if _current_scope(record) != scope:
                continue
            superseding_id = history.superseded.get(qualification_id)
            if superseding_id is not None:
                superseding = history.accepted.get(superseding_id)
                if superseding is None:
                    raise ProviderQualificationError(
                        "provider qualification supersession target disappeared"
                    )
                superseding_signed_at = datetime.strptime(
                    superseding.signed_at,
                    "%Y-%m-%dT%H:%M:%SZ",
                ).replace(tzinfo=timezone.utc)
                # A later journal cut may know that Q2 supersedes Q1, but that
                # knowledge must not retroactively erase Q1 from an evaluation
                # time before Q2's signed authority existed.
                if superseding_signed_at <= point:
                    continue
            signed_at = datetime.strptime(
                record.signed_at,
                "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
            valid_until = datetime.strptime(
                record.valid_until,
                "%Y-%m-%dT%H:%M:%SZ",
            ).replace(tzinfo=timezone.utc)
            if signed_at <= point < valid_until:
                candidates.append(record)
        if not candidates:
            raise ProviderQualificationCurrentUnavailable(
                "no current accepted provider qualification exists for exact scope"
            )
        if len(candidates) != 1:
            raise ProviderQualificationAuthorityAmbiguous(
                "multiple accepted provider qualifications are current for exact scope"
            )
        return CurrentProviderQualification(
            qualification=candidates[0],
            journal_sequence_cut=history.journal_sequence_cut,
        )

    def require_exact_current(
        self,
        *,
        scope: ProviderQualificationCurrentScope,
        at: datetime,
        expected_qualification_id: str,
        journal_sequence_cut: int | None = None,
    ) -> CurrentProviderQualification:
        expected = _qid(
            expected_qualification_id,
            name="expected_qualification_id",
        )
        current = self.current(
            scope=scope,
            at=at,
            journal_sequence_cut=journal_sequence_cut,
        )
        if current.qualification_id != expected:
            raise ProviderQualificationCurrentUnavailable(
                "expected provider qualification is no longer the exact current authority"
            )
        return current

    def qualification(
        self,
        qualification_id: str,
        *,
        journal_sequence_cut: int | None = None,
    ) -> AcceptedProviderQualification:
        qualification_id = _qid(
            qualification_id,
            name="qualification_id",
        )
        history = self._history(journal_sequence_cut=journal_sequence_cut)
        try:
            return history.accepted[qualification_id]
        except KeyError as error:
            raise ProviderQualificationCurrentUnavailable(
                "provider qualification id is not present at requested journal cut"
            ) from error

    def history_cut(
        self,
        *,
        journal_sequence_cut: int | None = None,
    ) -> int:
        return self._history(
            journal_sequence_cut=journal_sequence_cut
        ).journal_sequence_cut
