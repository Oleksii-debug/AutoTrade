"""Replay-verifiable accepted provider qualification authority for #1082.

`ProviderQualificationIdentity` is deliberately caller-constructible.  This
module never treats construction of that value as qualification authority.
Production acceptance is derived from one canonically verified signed
qualification attestation plus authenticated provider-campaign artifacts and is
then retained as a JournalStore event whose trust evidence is reverified on
terminal reads.

The durable journal is an audit/currentness index, not a signer.  Directly
appending a shape-valid event cannot manufacture Q because every ACCEPT or
SUPERSEDE event is replayed through the canonical signed-attestation verifier
and the independently authenticated campaign bytes before it can become
current authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Callable, Mapping

from research.autotrade_research.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from research.autotrade_research.io.strict_json import strict_json_loads

import mvp.autotrade_mvp.qualification_attestation as qualification_attestation_module
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_domain import ProviderFinancialScope
from .provider_qualification_identity import ProviderQualificationIdentity
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    QualificationAttestation,
    SignedQualificationAttestation,
    parse_signed_qualification_attestation,
)


_AUTHORITY_SCHEMA_VERSION = "1.0.0"
_CAMPAIGN_SCHEMA_VERSION = "1.0.0"
_AGGREGATE_TYPE = "provider_qualification_authority"
_EVENT_ACCEPTED = "ProviderQualificationAccepted"
_EVENT_SUPERSEDED = "ProviderQualificationSuperseded"
_EVENT_REVOKED = "ProviderQualificationRevoked"
_CAMPAIGN_EVIDENCE_KIND = "PROVIDER_QUALIFICATION_CAMPAIGN"
_CAMPAIGN_MEDIA_TYPE = "application/vnd.autotrade.provider-qualification+json"

_QUALIFICATION_DOMAIN = "PROVIDER"
_QUALIFICATION_GATE = "ROUTE_QUALIFICATION"
_QUALIFICATION_PACKAGE = "WP-08"
_QUALIFICATION_PROTOCOL = "provider-route-qualification-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "provider-route-qualification"

_CAMPAIGN_KEYS = frozenset(
    {
        "schema_version",
        "provider_scope",
        "product_family",
        "adapter_source_git_sha",
        "packaged_artifact_id",
        "packaged_artifact_digest",
        "campaign_id",
        "campaign_version",
        "required_case_policy_artifact_id",
        "result_set_artifact_id",
        "route_semantics_artifact_id",
        "documentation_revision_artifact_id",
    }
)


class ProviderQualificationAuthorityError(ValueError):
    """Raised when accepted provider qualification cannot be established."""


@dataclass(frozen=True, slots=True)
class AcceptedProviderQualification:
    """Detached current-Q snapshot reconstructed from durable trusted evidence."""

    identity: ProviderQualificationIdentity
    signed_at: str
    authority_event_id: str
    authority_journal_sequence: int
    store_identity_digest: str

    @property
    def qualification_id(self) -> str:
        return self.identity.content_digest


@dataclass(frozen=True, slots=True)
class _DerivedProviderQualification:
    identity: ProviderQualificationIdentity
    signed_at: str
    receipt_record: Mapping[str, object]
    campaign_artifact_id: str


@dataclass(frozen=True, slots=True)
class _ReplayState:
    current: AcceptedProviderQualification | None
    revoked: bool
    event_count: int


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest_payload(value: object) -> str:
    return "sha256:" + sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderQualificationAuthorityError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 1:
        raise ProviderQualificationAuthorityError(
            f"{name} must be a positive exact integer"
        )
    return value


def _exact_mapping(value: object, *, name: str) -> dict[str, object]:
    if type(value) is not dict:
        raise ProviderQualificationAuthorityError(f"{name} must be an exact object")
    if any(type(key) is not str for key in value):
        raise ProviderQualificationAuthorityError(f"{name} keys must be exact text")
    return dict(value)


def _selected_store_identity(store: JournalStore):
    return require_exact_journal_store_authority(
        store,
        subject="provider qualification JournalStore",
    )


def _store_identity_digest(store: JournalStore) -> str:
    identity = _selected_store_identity(store)
    return payload_digest(
        {
            "canonical_path": identity.canonical_path,
            "filesystem_device": identity.filesystem_device,
            "filesystem_inode": identity.filesystem_inode,
            "identity_source": identity.identity_source,
            "windows_volume_serial": identity.windows_volume_serial,
            "windows_file_index_high": identity.windows_file_index_high,
            "windows_file_index_low": identity.windows_file_index_low,
        }
    )


def _detach_receipt(
    receipt: SignedQualificationAttestation,
) -> tuple[SignedQualificationAttestation, dict[str, object]]:
    if type(receipt) is not SignedQualificationAttestation:
        raise TypeError("receipt must be exact SignedQualificationAttestation")
    attestation = receipt.attestation
    if type(attestation) is not QualificationAttestation:
        raise TypeError("receipt attestation must be exact QualificationAttestation")
    if type(receipt.signature_b64) is not str:
        raise TypeError("receipt signature must be exact text")
    if any(type(ref) is not EvidenceArtifactRef for ref in attestation.evidence_refs):
        raise TypeError("receipt evidence refs must be exact EvidenceArtifactRef values")

    record = {
        "attestation": QualificationAttestation.canonical_payload(attestation),
        "signature_b64": receipt.signature_b64,
    }
    # Round-trip through plain JSON base types so later caller mutation cannot
    # substitute Mapping/list subclasses behind the retained receipt record.
    detached_record = json.loads(canonical_json(record))
    detached = parse_signed_qualification_attestation(detached_record)
    return detached, detached_record


def _campaign_ref(receipt: SignedQualificationAttestation) -> EvidenceArtifactRef:
    matches = tuple(
        ref
        for ref in receipt.attestation.evidence_refs
        if ref.evidence_kind == _CAMPAIGN_EVIDENCE_KIND
    )
    if len(matches) != 1:
        raise ProviderQualificationAuthorityError(
            "signed qualification must contain exactly one provider campaign artifact"
        )
    ref = matches[0]
    if ref.media_type != _CAMPAIGN_MEDIA_TYPE:
        raise ProviderQualificationAuthorityError(
            "provider campaign artifact media type is not canonical"
        )
    return ref


def _read_ref(
    read_snapshot: Callable[[str], tuple[dict[str, object], bytes]],
    ref: EvidenceArtifactRef,
) -> bytes:
    try:
        manifest, data = read_snapshot(ref.artifact_id)
    except (FileNotFoundError, ArtifactIntegrityError, OSError) as error:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact cannot be resolved with integrity"
        ) from error
    if type(manifest) is not dict or type(data) is not bytes:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact snapshot is non-canonical"
        )
    if manifest.get("sha256") != ref.sha256:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact digest conflicts with signed ref"
        )
    if manifest.get("media_type") != ref.media_type:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact media type conflicts with signed ref"
        )
    metadata = manifest.get("metadata")
    if type(metadata) is not dict or metadata.get("evidence_kind") != ref.evidence_kind:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact kind conflicts with signed ref"
        )
    source_refs = manifest.get("source_refs")
    if type(source_refs) is not list or f"git:{ref.source_sha}" not in source_refs:
        raise ProviderQualificationAuthorityError(
            "provider qualification artifact source conflicts with signed ref"
        )
    if "sha256:" + sha256(data).hexdigest() != ref.sha256:
        raise ProviderQualificationAuthorityError(
            "provider qualification returned bytes conflict with signed digest"
        )
    return bytes(data)


def _scope_from_payload(value: object) -> ProviderFinancialScope:
    payload = _exact_mapping(value, name="provider_scope")
    expected = {
        "schema_version",
        "provider_id",
        "runtime_environment",
        "provider_environment",
        "entity_policy_id",
    }
    if set(payload) != expected or payload.get("schema_version") != "1.0.0":
        raise ProviderQualificationAuthorityError(
            "provider campaign scope payload is non-canonical"
        )
    return ProviderFinancialScope(
        provider_id=payload["provider_id"],
        runtime_environment=payload["runtime_environment"],
        provider_environment=payload["provider_environment"],
        entity_policy_id=payload["entity_policy_id"],
    )


def _parse_campaign(data: bytes) -> dict[str, object]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ProviderQualificationAuthorityError(
            "provider campaign bytes must be UTF-8 JSON"
        ) from error
    try:
        value = strict_json_loads(text)
    except (TypeError, ValueError) as error:
        raise ProviderQualificationAuthorityError(
            "provider campaign JSON is malformed or ambiguous"
        ) from error
    payload = _exact_mapping(value, name="provider campaign")
    if set(payload) != _CAMPAIGN_KEYS:
        missing = sorted(_CAMPAIGN_KEYS - set(payload))
        extra = sorted(set(payload) - _CAMPAIGN_KEYS)
        raise ProviderQualificationAuthorityError(
            f"provider campaign fields mismatch: missing={missing}, extra={extra}"
        )
    if payload["schema_version"] != _CAMPAIGN_SCHEMA_VERSION:
        raise ProviderQualificationAuthorityError(
            "provider campaign schema_version is unsupported"
        )
    return payload


def _support_ref(
    refs_by_id: Mapping[str, EvidenceArtifactRef],
    artifact_id: object,
    *,
    name: str,
) -> EvidenceArtifactRef:
    artifact_id = _exact_text(artifact_id, name=name)
    ref = refs_by_id.get(artifact_id)
    if ref is None:
        raise ProviderQualificationAuthorityError(
            f"{name} is not retained by the signed qualification evidence set"
        )
    return ref


def _derive_verified_provider_qualification(
    *,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    expected_provider_scope: ProviderFinancialScope | None = None,
    expected_product_family: str | None = None,
    expected_adapter_source_git_sha: str | None = None,
    expected_packaged_artifact_digest: str | None = None,
) -> _DerivedProviderQualification:
    detached, receipt_record = _detach_receipt(receipt)
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be canonical ArtifactStore")
    try:
        read_snapshot = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
    except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
        raise ProviderQualificationAuthorityError(
            "provider qualification evidence authority cannot be bound"
        ) from error

    campaign_ref = _campaign_ref(detached)
    campaign_bytes = _read_ref(read_snapshot, campaign_ref)
    campaign = _parse_campaign(campaign_bytes)
    scope = _scope_from_payload(campaign["provider_scope"])
    product_family = _exact_text(
        campaign["product_family"], name="product_family"
    ).upper()
    adapter_source_git_sha = _exact_text(
        campaign["adapter_source_git_sha"], name="adapter_source_git_sha"
    )
    packaged_artifact_id = _exact_text(
        campaign["packaged_artifact_id"], name="packaged_artifact_id"
    )
    packaged_artifact_digest = _exact_text(
        campaign["packaged_artifact_digest"], name="packaged_artifact_digest"
    )
    campaign_id = _exact_text(campaign["campaign_id"], name="campaign_id")
    campaign_version = _positive_int(campaign["campaign_version"], name="campaign_version")

    if expected_provider_scope is not None:
        if type(expected_provider_scope) is not ProviderFinancialScope:
            raise TypeError("expected_provider_scope must be exact ProviderFinancialScope")
        if scope != expected_provider_scope:
            raise ProviderQualificationAuthorityError(
                "provider campaign scope does not match product-selected scope"
            )
    if expected_product_family is not None:
        if _exact_text(expected_product_family, name="expected_product_family").upper() != product_family:
            raise ProviderQualificationAuthorityError(
                "provider campaign product family does not match product selection"
            )
    if expected_adapter_source_git_sha is not None:
        if _exact_text(
            expected_adapter_source_git_sha,
            name="expected_adapter_source_git_sha",
        ) != adapter_source_git_sha:
            raise ProviderQualificationAuthorityError(
                "provider campaign adapter source does not match product selection"
            )
    if expected_packaged_artifact_digest is not None:
        if _exact_text(
            expected_packaged_artifact_digest,
            name="expected_packaged_artifact_digest",
        ) != packaged_artifact_digest:
            raise ProviderQualificationAuthorityError(
                "provider campaign packaged artifact does not match product selection"
            )

    if detached.attestation.source_sha != adapter_source_git_sha:
        raise ProviderQualificationAuthorityError(
            "signed qualification source does not match adapter source"
        )

    accepted = qualification_attestation_module.verify_canonical_qualification_attestation(
        detached,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_source_sha=adapter_source_git_sha,
        expected_domain=_QUALIFICATION_DOMAIN,
        expected_gate=_QUALIFICATION_GATE,
        expected_package_id=_QUALIFICATION_PACKAGE,
        expected_protocol_id=_QUALIFICATION_PROTOCOL,
        expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
        expected_requirement_id=_QUALIFICATION_REQUIREMENT,
        expected_release_artifact_id=packaged_artifact_id,
        expected_release_artifact_sha256=packaged_artifact_digest,
    )
    if type(accepted) is not AcceptedQualificationAttestation:
        raise TypeError("canonical verifier returned non-canonical accepted qualification")
    if accepted.result != "PASS":
        raise ProviderQualificationAuthorityError(
            "provider qualification attestation is not PASS"
        )

    refs = tuple(detached.attestation.evidence_refs)
    refs_by_id = {ref.artifact_id: ref for ref in refs}
    if len(refs_by_id) != len(refs):
        raise ProviderQualificationAuthorityError(
            "signed provider qualification evidence identities are not unique"
        )
    required_case_ref = _support_ref(
        refs_by_id,
        campaign["required_case_policy_artifact_id"],
        name="required_case_policy_artifact_id",
    )
    result_set_ref = _support_ref(
        refs_by_id,
        campaign["result_set_artifact_id"],
        name="result_set_artifact_id",
    )
    route_semantics_ref = _support_ref(
        refs_by_id,
        campaign["route_semantics_artifact_id"],
        name="route_semantics_artifact_id",
    )
    documentation_ref = _support_ref(
        refs_by_id,
        campaign["documentation_revision_artifact_id"],
        name="documentation_revision_artifact_id",
    )
    support_refs = (
        required_case_ref,
        result_set_ref,
        route_semantics_ref,
        documentation_ref,
    )
    if len({ref.artifact_id for ref in support_refs}) != len(support_refs):
        raise ProviderQualificationAuthorityError(
            "provider campaign support roles must use distinct retained artifacts"
        )
    if campaign_ref.artifact_id in {ref.artifact_id for ref in support_refs}:
        raise ProviderQualificationAuthorityError(
            "provider campaign artifact cannot self-satisfy a support role"
        )
    for ref in support_refs:
        _read_ref(read_snapshot, ref)

    evidence_set_digest = _digest_payload(
        [ref.canonical() for ref in sorted(refs, key=lambda item: item.artifact_id)]
    )
    issuer_identity_digest = _digest_payload(
        {
            "producer_id": detached.attestation.producer_id,
            "trust_root_id": accepted.trust_root_id,
        }
    )
    verifier_identity_digest = _digest_payload(
        {
            "verifier_id": detached.attestation.verifier_id,
            "trust_root_id": accepted.trust_root_id,
        }
    )
    identity = ProviderQualificationIdentity(
        provider_scope=scope,
        product_family=product_family,
        adapter_source_git_sha=adapter_source_git_sha,
        packaged_artifact_digest=packaged_artifact_digest,
        campaign_id=campaign_id,
        campaign_version=campaign_version,
        required_case_policy_digest=required_case_ref.sha256,
        result_set_digest=result_set_ref.sha256,
        route_semantics_digest=route_semantics_ref.sha256,
        documentation_revision_digest=documentation_ref.sha256,
        evidence_set_digest=evidence_set_digest,
        attestation_digest=accepted.attestation_digest,
        trust_policy_digest=accepted.policy_id,
        issuer_identity_digest=issuer_identity_digest,
        verifier_identity_digest=verifier_identity_digest,
    )
    return _DerivedProviderQualification(
        identity=identity,
        signed_at=detached.attestation.signed_at,
        receipt_record=receipt_record,
        campaign_artifact_id=campaign_ref.artifact_id,
    )


def _aggregate_scope_payload(identity: ProviderQualificationIdentity) -> dict[str, object]:
    return {
        "provider_scope_digest": identity.provider_scope.content_digest,
        "product_family": identity.product_family,
        "adapter_source_git_sha": identity.adapter_source_git_sha,
        "packaged_artifact_digest": identity.packaged_artifact_digest,
    }


def _aggregate_id_for_identity(identity: ProviderQualificationIdentity) -> str:
    digest = sha256(
        canonical_json(_aggregate_scope_payload(identity)).encode("utf-8")
    ).hexdigest()
    return "provider-qualification-current-" + digest


def _aggregate_id_for_expected_scope(
    *,
    provider_scope: ProviderFinancialScope,
    product_family: str,
    adapter_source_git_sha: str,
    packaged_artifact_digest: str,
) -> str:
    probe = {
        "provider_scope_digest": provider_scope.content_digest,
        "product_family": _exact_text(product_family, name="product_family").upper(),
        "adapter_source_git_sha": _exact_text(
            adapter_source_git_sha, name="adapter_source_git_sha"
        ),
        "packaged_artifact_digest": _exact_text(
            packaged_artifact_digest, name="packaged_artifact_digest"
        ),
    }
    return "provider-qualification-current-" + sha256(
        canonical_json(probe).encode("utf-8")
    ).hexdigest()


def _accepted_from_event(
    *,
    identity: ProviderQualificationIdentity,
    signed_at: str,
    event: Mapping[str, object],
    store_identity_digest: str,
) -> AcceptedProviderQualification:
    sequence = event.get("journal_sequence")
    if type(sequence) is not int or sequence < 1:
        raise ProviderQualificationAuthorityError(
            "provider qualification authority event lacks canonical journal sequence"
        )
    event_id = event.get("event_id")
    if type(event_id) is not str or not event_id:
        raise ProviderQualificationAuthorityError(
            "provider qualification authority event id is invalid"
        )
    return AcceptedProviderQualification(
        identity=identity,
        signed_at=signed_at,
        authority_event_id=event_id,
        authority_journal_sequence=sequence,
        store_identity_digest=store_identity_digest,
    )


def _load_authority_events(store: JournalStore, aggregate_id: str):
    selected_identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, selected_identity):
        return JournalStore.load_events(store, _AGGREGATE_TYPE, aggregate_id)


def _replay_authority(
    *,
    store: JournalStore,
    aggregate_id: str,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
) -> _ReplayState:
    expected_store_digest = _store_identity_digest(store)
    events = _load_authority_events(store, aggregate_id)
    current: AcceptedProviderQualification | None = None
    revoked = False
    for expected_version, event in enumerate(events, start=1):
        if event.get("aggregate_version") != expected_version:
            raise ProviderQualificationAuthorityError(
                "provider qualification authority history is non-contiguous"
            )
        payload = event.get("payload")
        if type(payload) is not dict:
            raise ProviderQualificationAuthorityError(
                "provider qualification authority payload is invalid"
            )
        if payload.get("schema_version") != _AUTHORITY_SCHEMA_VERSION:
            raise ProviderQualificationAuthorityError(
                "provider qualification authority schema is unsupported"
            )
        if payload.get("store_identity_digest") != expected_store_digest:
            raise ProviderQualificationAuthorityError(
                "provider qualification authority belongs to a different JournalStore generation"
            )
        event_type = event.get("event_type")
        if event_type in {_EVENT_ACCEPTED, _EVENT_SUPERSEDED}:
            required = {
                "schema_version",
                "store_identity_digest",
                "qualification_id",
                "identity_payload",
                "signed_at",
                "receipt",
                "campaign_artifact_id",
                "supersedes_qualification_id",
            }
            if set(payload) != required:
                raise ProviderQualificationAuthorityError(
                    "provider qualification acceptance payload shape is invalid"
                )
            receipt = parse_signed_qualification_attestation(payload["receipt"])
            derived = _derive_verified_provider_qualification(
                receipt=receipt,
                evidence_store=evidence_store,
                evidence_root=evidence_root,
            )
            identity = derived.identity
            if _aggregate_id_for_identity(identity) != aggregate_id:
                raise ProviderQualificationAuthorityError(
                    "provider qualification event is stored under the wrong currentness scope"
                )
            if payload.get("qualification_id") != identity.content_digest:
                raise ProviderQualificationAuthorityError(
                    "provider qualification durable id conflicts with trusted evidence"
                )
            if payload.get("identity_payload") != identity.payload():
                raise ProviderQualificationAuthorityError(
                    "provider qualification durable identity conflicts with trusted evidence"
                )
            if payload.get("signed_at") != derived.signed_at:
                raise ProviderQualificationAuthorityError(
                    "provider qualification signed chronology conflicts with trusted evidence"
                )
            if payload.get("campaign_artifact_id") != derived.campaign_artifact_id:
                raise ProviderQualificationAuthorityError(
                    "provider qualification campaign binding conflicts with trusted evidence"
                )
            supersedes = payload.get("supersedes_qualification_id")
            if event_type == _EVENT_ACCEPTED:
                if current is not None or supersedes is not None:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification acceptance illegally resets existing history"
                    )
            else:
                if current is None:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification supersession has no predecessor"
                    )
                if supersedes != current.qualification_id:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification supersession predecessor conflicts"
                    )
                if identity.content_digest == current.qualification_id:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification supersession cannot resurrect the same Q"
                    )
                if derived.signed_at <= current.signed_at:
                    raise ProviderQualificationAuthorityError(
                        "provider qualification signed chronology regressed during supersession"
                    )
            current = _accepted_from_event(
                identity=identity,
                signed_at=derived.signed_at,
                event=event,
                store_identity_digest=expected_store_digest,
            )
            revoked = False
        elif event_type == _EVENT_REVOKED:
            required = {
                "schema_version",
                "store_identity_digest",
                "qualification_id",
                "reason",
            }
            if set(payload) != required:
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation payload shape is invalid"
                )
            if current is None or revoked:
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation has no current qualification"
                )
            if payload.get("qualification_id") != current.qualification_id:
                raise ProviderQualificationAuthorityError(
                    "provider qualification revocation targets a different qualification"
                )
            _exact_text(payload.get("reason"), name="revocation reason")
            revoked = True
        else:
            raise ProviderQualificationAuthorityError(
                "provider qualification authority contains unsupported event type"
            )
    return _ReplayState(current=current, revoked=revoked, event_count=len(events))


def _event_id(aggregate_id: str, version: int, action: str, qualification_id: str) -> str:
    material = f"{aggregate_id}\0{version}\0{action}\0{qualification_id}"
    return "provider-qualification-authority-" + sha256(material.encode("utf-8")).hexdigest()


def _append_authority_event(store: JournalStore, envelope: dict[str, object]) -> None:
    selected_identity = _selected_store_identity(store)
    with journal_store_authority_scope(store, selected_identity):
        JournalStore.append_event(store, envelope)


def accept_provider_qualification(
    store: JournalStore,
    *,
    receipt: SignedQualificationAttestation,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
    expected_provider_scope: ProviderFinancialScope,
    expected_product_family: str,
    expected_adapter_source_git_sha: str,
    expected_packaged_artifact_digest: str,
) -> AcceptedProviderQualification:
    """Verify trusted campaign evidence and durably make its exact Q current."""

    _selected_store_identity(store)
    derived = _derive_verified_provider_qualification(
        receipt=receipt,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
        expected_provider_scope=expected_provider_scope,
        expected_product_family=expected_product_family,
        expected_adapter_source_git_sha=expected_adapter_source_git_sha,
        expected_packaged_artifact_digest=expected_packaged_artifact_digest,
    )
    identity = derived.identity
    aggregate_id = _aggregate_id_for_identity(identity)
    state = _replay_authority(
        store=store,
        aggregate_id=aggregate_id,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    if state.current is None:
        event_type = _EVENT_ACCEPTED
        supersedes: str | None = None
    elif not state.revoked and state.current.qualification_id == identity.content_digest:
        return state.current
    else:
        if state.current.qualification_id == identity.content_digest:
            raise ProviderQualificationAuthorityError(
                "revoked provider qualification cannot be reaccepted from the same Q evidence"
            )
        if derived.signed_at <= state.current.signed_at:
            raise ProviderQualificationAuthorityError(
                "provider qualification supersession must advance signed chronology"
            )
        event_type = _EVENT_SUPERSEDED
        supersedes = state.current.qualification_id

    version = state.event_count + 1
    store_digest = _store_identity_digest(store)
    payload = {
        "schema_version": _AUTHORITY_SCHEMA_VERSION,
        "store_identity_digest": store_digest,
        "qualification_id": identity.content_digest,
        "identity_payload": identity.payload(),
        "signed_at": derived.signed_at,
        "receipt": dict(derived.receipt_record),
        "campaign_artifact_id": derived.campaign_artifact_id,
        "supersedes_qualification_id": supersedes,
    }
    event_id = _event_id(aggregate_id, version, event_type, identity.content_digest)
    envelope = {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": _now(),
    }
    try:
        _append_authority_event(store, envelope)
    except ValueError as error:
        raise ProviderQualificationAuthorityError(
            "provider qualification currentness changed during acceptance"
        ) from error

    return require_current_provider_qualification(
        store,
        expected_provider_scope=expected_provider_scope,
        expected_product_family=expected_product_family,
        expected_adapter_source_git_sha=expected_adapter_source_git_sha,
        expected_packaged_artifact_digest=expected_packaged_artifact_digest,
        expected_qualification_id=identity.content_digest,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )


def require_current_provider_qualification(
    store: JournalStore,
    *,
    expected_provider_scope: ProviderFinancialScope,
    expected_product_family: str,
    expected_adapter_source_git_sha: str,
    expected_packaged_artifact_digest: str,
    expected_qualification_id: str,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
) -> AcceptedProviderQualification:
    """Require one exact expected Q to remain current; never substitute a newer Q."""

    _selected_store_identity(store)
    if type(expected_provider_scope) is not ProviderFinancialScope:
        raise TypeError("expected_provider_scope must be exact ProviderFinancialScope")
    expected_qualification_id = _exact_text(
        expected_qualification_id, name="expected_qualification_id"
    )
    aggregate_id = _aggregate_id_for_expected_scope(
        provider_scope=expected_provider_scope,
        product_family=expected_product_family,
        adapter_source_git_sha=expected_adapter_source_git_sha,
        packaged_artifact_digest=expected_packaged_artifact_digest,
    )
    state = _replay_authority(
        store=store,
        aggregate_id=aggregate_id,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    if state.current is None:
        raise ProviderQualificationAuthorityError(
            "no accepted provider qualification exists for the exact scope"
        )
    if state.revoked:
        raise ProviderQualificationAuthorityError(
            "expected provider qualification is revoked"
        )
    current = state.current
    if current.qualification_id != expected_qualification_id:
        raise ProviderQualificationAuthorityError(
            "expected provider qualification is superseded or mismatched"
        )
    identity = current.identity
    if identity.provider_scope != expected_provider_scope:
        raise ProviderQualificationAuthorityError("current provider scope mismatch")
    if identity.product_family != _exact_text(
        expected_product_family, name="expected_product_family"
    ).upper():
        raise ProviderQualificationAuthorityError("current product family mismatch")
    if identity.adapter_source_git_sha != _exact_text(
        expected_adapter_source_git_sha,
        name="expected_adapter_source_git_sha",
    ):
        raise ProviderQualificationAuthorityError("current adapter source mismatch")
    if identity.packaged_artifact_digest != _exact_text(
        expected_packaged_artifact_digest,
        name="expected_packaged_artifact_digest",
    ):
        raise ProviderQualificationAuthorityError("current packaged artifact mismatch")
    return current


def revoke_provider_qualification(
    store: JournalStore,
    *,
    expected_provider_scope: ProviderFinancialScope,
    expected_product_family: str,
    expected_adapter_source_git_sha: str,
    expected_packaged_artifact_digest: str,
    expected_qualification_id: str,
    reason: str,
    evidence_store: ArtifactStore,
    evidence_root: str | Path,
) -> None:
    """Durably revoke the exact current Q; revocation never substitutes another Q."""

    current = require_current_provider_qualification(
        store,
        expected_provider_scope=expected_provider_scope,
        expected_product_family=expected_product_family,
        expected_adapter_source_git_sha=expected_adapter_source_git_sha,
        expected_packaged_artifact_digest=expected_packaged_artifact_digest,
        expected_qualification_id=expected_qualification_id,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    aggregate_id = _aggregate_id_for_identity(current.identity)
    state = _replay_authority(
        store=store,
        aggregate_id=aggregate_id,
        evidence_store=evidence_store,
        evidence_root=evidence_root,
    )
    reason = _exact_text(reason, name="reason")
    version = state.event_count + 1
    store_digest = _store_identity_digest(store)
    payload = {
        "schema_version": _AUTHORITY_SCHEMA_VERSION,
        "store_identity_digest": store_digest,
        "qualification_id": current.qualification_id,
        "reason": reason,
    }
    event_id = _event_id(
        aggregate_id,
        version,
        _EVENT_REVOKED,
        current.qualification_id,
    )
    envelope = {
        "event_id": event_id,
        "event_type": _EVENT_REVOKED,
        "aggregate_type": _AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": _now(),
    }
    try:
        _append_authority_event(store, envelope)
    except ValueError as error:
        raise ProviderQualificationAuthorityError(
            "provider qualification currentness changed during revocation"
        ) from error
