"""Fail-closed evidence gate for bounded real-account qualification.

This module never grants trading authority and never sends provider requests.
A PASS requires immutable evidence resolved by an explicit verifier; caller
booleans or syntactically valid references are insufficient on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import FrozenSet, Iterable
import weakref
from uuid import UUID

from research.autotrade_research.artifacts import trusted_authenticated_reader
from research.autotrade_research.artifacts.store import ArtifactStore

from .exact_decimal import ExactDecimalError, canonical_decimal_text
from .qualification_attestation import (
    AcceptedQualificationAttestation,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    verify_qualification_attestation,
)


_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ALLOWED_ACTIONS = frozenset(
    {
        "ORDER.SUBMIT",
        "ORDER.CANCEL",
        "ORDER.REPLACE",
        "PROTECTION.MAINTAIN",
        "PROTECTION.REPAIR",
        "FLATTEN",
    }
)
_FORBIDDEN_ACTIONS = frozenset({"WITHDRAW", "TRANSFER", "CREDENTIAL.ROTATE"})
_QUALIFICATION_DOMAIN = "BOUNDED_REAL"
_QUALIFICATION_GATE = "QUALIFICATION"
_QUALIFICATION_PACKAGE = "WP-58"
_QUALIFICATION_PROTOCOL = "bounded-real-qualification-v1"
_QUALIFICATION_PROTOCOL_VERSION = "1.0.0"
_QUALIFICATION_REQUIREMENT = "bounded-real-terminal-evidence"


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _sha(value: str, *, name: str) -> str:
    result = _text(value, name=name)
    if _SHA_RE.fullmatch(result) is None:
        raise ValueError(
            f"{name} must be an exact 40-character lowercase Git SHA"
        )
    return result


def _artifact_id(value: str) -> str:
    try:
        return str(UUID(_text(value, name="artifact_id")))
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("artifact_id must be a UUID") from error


def _digest(value: str) -> str:
    result = _text(value, name="sha256")
    if _DIGEST_RE.fullmatch(result) is None:
        raise ValueError("sha256 must be canonical lowercase hexadecimal")
    return result


def _decimal(value, *, name: str, allow_zero: bool = False) -> Decimal:
    if type(value) not in {Decimal, str, int}:
        if isinstance(value, Decimal):
            raise TypeError(f"{name} must use an exact built-in Decimal")
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if type(value) is Decimal else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not Decimal.is_finite(result):
        raise ValueError(f"{name} must be a finite decimal")
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(
            f"{name} must be {'non-negative' if allow_zero else 'positive'}"
        )
    try:
        canonical_decimal_text(result)
    except ExactDecimalError as error:
        raise ValueError(f"{name} exceeds exact decimal authority") from error
    return result


def _bool(value, *, name: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"{name} must be boolean")
    return value


def _actions(values: Iterable[str]) -> FrozenSet[str]:
    if type(values) not in {set, frozenset}:
        raise TypeError("allowed_actions must be an exact set or frozenset")
    result = frozenset(_text(value, name="action").upper() for value in values)
    if not result:
        raise ValueError("allowed_actions must be non-empty")
    forbidden = result & _FORBIDDEN_ACTIONS
    if forbidden:
        raise ValueError(
            "bounded-real qualification cannot include forbidden actions: "
            + ", ".join(sorted(forbidden))
        )
    unsupported = result - _ALLOWED_ACTIONS
    if unsupported:
        raise ValueError(
            "bounded-real qualification contains unsupported actions: "
            + ", ".join(sorted(unsupported))
        )
    return result


_BOUNDED_REAL_ENVELOPE_SCHEMA_VERSION = 1


def _canonical_decimal_text(value: Decimal) -> str:
    """Canonical exact numeric identity for bounded-real risk limits."""

    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise ValueError("bounded-real limit exceeds exact decimal authority") from error


def _bounded_real_envelope_digest(
    *,
    envelope_id: str,
    source_sha: str,
    provider_id: str,
    account_id: str,
    policy_id: str,
    allowed_actions: FrozenSet[str],
    max_capital: Decimal,
    max_single_notional: Decimal,
    max_gross_leverage: Decimal,
) -> str:
    payload = {
        "schema_version": _BOUNDED_REAL_ENVELOPE_SCHEMA_VERSION,
        "envelope_id": envelope_id,
        "source_sha": source_sha,
        "provider_id": provider_id,
        "account_id": account_id,
        "policy_id": policy_id,
        "allowed_actions": sorted(allowed_actions),
        "max_capital": _canonical_decimal_text(max_capital),
        "max_single_notional": _canonical_decimal_text(max_single_notional),
        "max_gross_leverage": _canonical_decimal_text(max_gross_leverage),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class BoundedRealEnvelope:
    envelope_id: str
    source_sha: str
    account_id: str
    provider_id: str
    policy_id: str
    allowed_actions: FrozenSet[str]
    max_capital: Decimal
    max_single_notional: Decimal
    max_gross_leverage: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "envelope_id", _text(self.envelope_id, name="envelope_id")
        )
        object.__setattr__(
            self, "source_sha", _sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "policy_id", _text(self.policy_id, name="policy_id")
        )
        object.__setattr__(self, "allowed_actions", _actions(self.allowed_actions))
        object.__setattr__(
            self, "max_capital", _decimal(self.max_capital, name="max_capital")
        )
        object.__setattr__(
            self,
            "max_single_notional",
            _decimal(self.max_single_notional, name="max_single_notional"),
        )
        object.__setattr__(
            self,
            "max_gross_leverage",
            _decimal(self.max_gross_leverage, name="max_gross_leverage"),
        )
        if self.max_single_notional > self.max_capital:
            raise ValueError("max_single_notional cannot exceed max_capital")

    @property
    def envelope_digest(self) -> str:
        snapshot = _snapshot_bounded_real_envelope(self)
        return _bounded_real_envelope_digest(
            envelope_id=snapshot.envelope_id,
            source_sha=snapshot.source_sha,
            provider_id=snapshot.provider_id,
            account_id=snapshot.account_id,
            policy_id=snapshot.policy_id,
            allowed_actions=snapshot.allowed_actions,
            max_capital=snapshot.max_capital,
            max_single_notional=snapshot.max_single_notional,
            max_gross_leverage=snapshot.max_gross_leverage,
        )

    @classmethod
    def create(cls, **values) -> "BoundedRealEnvelope":
        return cls(**values)


def _snapshot_bounded_real_envelope(
    envelope: BoundedRealEnvelope,
) -> BoundedRealEnvelope:
    if type(envelope) is not BoundedRealEnvelope:
        raise TypeError("envelope must be exact BoundedRealEnvelope")
    return BoundedRealEnvelope(
        envelope_id=object.__getattribute__(envelope, "envelope_id"),
        source_sha=object.__getattribute__(envelope, "source_sha"),
        account_id=object.__getattribute__(envelope, "account_id"),
        provider_id=object.__getattribute__(envelope, "provider_id"),
        policy_id=object.__getattribute__(envelope, "policy_id"),
        allowed_actions=object.__getattribute__(envelope, "allowed_actions"),
        max_capital=object.__getattribute__(envelope, "max_capital"),
        max_single_notional=object.__getattribute__(
            envelope,
            "max_single_notional",
        ),
        max_gross_leverage=object.__getattribute__(
            envelope,
            "max_gross_leverage",
        ),
    )


@dataclass(frozen=True)
class ImmutableEvidenceRef:
    artifact_id: str
    sha256: str
    evidence_kind: str
    source_sha: str
    envelope_id: str
    envelope_digest: str
    provider_id: str
    account_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_id", _artifact_id(self.artifact_id))
        object.__setattr__(self, "sha256", _digest(self.sha256))
        object.__setattr__(
            self,
            "evidence_kind",
            _text(self.evidence_kind, name="evidence_kind").upper(),
        )
        object.__setattr__(
            self, "source_sha", _sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self, "envelope_id", _text(self.envelope_id, name="envelope_id")
        )
        object.__setattr__(self, "envelope_digest", _digest(self.envelope_digest))
        object.__setattr__(
            self, "provider_id", _text(self.provider_id, name="provider_id")
        )
        object.__setattr__(
            self, "account_id", _text(self.account_id, name="account_id")
        )


def _snapshot_immutable_evidence_ref(
    ref: ImmutableEvidenceRef,
) -> ImmutableEvidenceRef:
    if type(ref) is not ImmutableEvidenceRef:
        raise TypeError("evidence reference must be exact ImmutableEvidenceRef")
    return ImmutableEvidenceRef(
        artifact_id=object.__getattribute__(ref, "artifact_id"),
        sha256=object.__getattribute__(ref, "sha256"),
        evidence_kind=object.__getattribute__(ref, "evidence_kind"),
        source_sha=object.__getattribute__(ref, "source_sha"),
        envelope_id=object.__getattribute__(ref, "envelope_id"),
        envelope_digest=object.__getattribute__(ref, "envelope_digest"),
        provider_id=object.__getattribute__(ref, "provider_id"),
        account_id=object.__getattribute__(ref, "account_id"),
    )


@dataclass(frozen=True)
class EvidenceVerification:
    valid: bool
    conflicted: bool = False
    reason: str = ""

    def __post_init__(self) -> None:
        _bool(self.valid, name="valid")
        _bool(self.conflicted, name="conflicted")
        if type(self.reason) is not str:
            raise TypeError("reason must be an exact string")
        if self.valid and self.conflicted:
            raise ValueError("evidence cannot be both valid and conflicted")


def _artifact_store_evidence_verifier_operations():
    """Seal verifier state without discoverable weakref callbacks."""

    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            weakref.ReferenceType,
            weakref.ReferenceType,
            Path,
            str,
        ],
    ] = {}
    state_lock = threading.RLock()

    def prune_dead() -> None:
        dead = [key for key, state in states.items() if state[0]() is None]
        for key in dead:
            states.pop(key, None)

    def require_unbound(verifier: object) -> None:
        object_id = id(verifier)
        with state_lock:
            prune_dead()
            current = states.get(object_id)
            if current is None:
                return
            current_verifier = current[0]()
            if current_verifier is verifier:
                raise ValueError(
                    "ArtifactStoreEvidenceVerifier composition is already initialized"
                )
            if current_verifier is not None:
                raise ValueError(
                    "ArtifactStoreEvidenceVerifier binding identity collision"
                )
            states.pop(object_id, None)

    def register(
        verifier: object,
        *,
        store: ArtifactStore,
        evidence_root: Path,
        read_snapshot: object,
        store_identity: str,
    ) -> None:
        object_id = id(verifier)
        with state_lock:
            prune_dead()
            current = states.get(object_id)
            if current is not None:
                current_verifier = current[0]()
                if current_verifier is verifier:
                    raise ValueError(
                        "ArtifactStoreEvidenceVerifier composition is already initialized"
                    )
                if current_verifier is not None:
                    raise ValueError(
                        "ArtifactStoreEvidenceVerifier binding identity collision"
                    )
                states.pop(object_id, None)
            states[object_id] = (
                weakref.ref(verifier),
                weakref.ref(store),
                weakref.ref(read_snapshot),
                evidence_root,
                store_identity,
            )

    def binding(
        verifier: object,
    ) -> tuple[ArtifactStore, Path, object, str]:
        with state_lock:
            state = states.get(id(verifier))
        if state is None or state[0]() is not verifier:
            raise ValueError(
                "ArtifactStoreEvidenceVerifier process binding is unavailable"
            )
        store = state[1]()
        read_snapshot = state[2]()
        evidence_root = state[3]
        store_identity = state[4]
        visible = vars(verifier)
        if (
            visible.get("_store") is not store
            or visible.get("_read_snapshot") is not read_snapshot
            or visible.get("_evidence_root") != evidence_root
            or visible.get("_store_identity") != store_identity
        ):
            raise ValueError(
                "ArtifactStoreEvidenceVerifier composition was modified"
            )
        if store is None or read_snapshot is None:
            raise ValueError(
                "ArtifactStoreEvidenceVerifier selected authority was lost"
            )
        return store, evidence_root, read_snapshot, store_identity

    return require_unbound, register, binding


(
    _require_unbound_artifact_store_evidence_verifier,
    _register_artifact_store_evidence_verifier,
    _artifact_store_evidence_verifier_binding,
) = _artifact_store_evidence_verifier_operations()
del _artifact_store_evidence_verifier_operations

class ArtifactStoreEvidenceVerifier:
    """Immutable-evidence integrity verifier backed by the canonical ArtifactStore.

    ArtifactStore is content/integrity infrastructure, not an independent
    qualification authority. Terminal bounded-real completion additionally
    requires the shared signed qualification-attestation boundary.
    """

    VERIFIER_ID = "AUTOTRADE_ARTIFACT_STORE_BOUNDED_REAL_V1"

    def __init__(
        self,
        store: object,
        *,
        evidence_root: str | Path,
    ):
        if type(store) is not ArtifactStore:
            raise TypeError(
                "bounded-real integrity verification requires canonical ArtifactStore"
            )
        canonical_path_type = type(Path())
        if type(evidence_root) is str:
            if not evidence_root.strip():
                raise ValueError("evidence_root must be non-empty")
        elif type(evidence_root) is not canonical_path_type:
            raise TypeError("evidence_root must be an exact string or Path")
        root = Path(evidence_root).absolute()
        _require_unbound_artifact_store_evidence_verifier(self)
        read_snapshot = trusted_authenticated_reader(
            root,
            publication_store=store,
        )
        store_identity = (
            "sha256:"
            + hashlib.sha256(str(root).encode("utf-8")).hexdigest()
        )
        _register_artifact_store_evidence_verifier(
            self,
            store=store,
            evidence_root=root,
            read_snapshot=read_snapshot,
            store_identity=store_identity,
        )
        # Strong lifetime anchors only. Closure-owned weak references and exact
        # values remain the authority and detect any caller retargeting.
        self._store = store
        self._evidence_root = root
        self._read_snapshot = read_snapshot
        self._store_identity = store_identity

    @property
    def identity(self) -> str:
        _, _, _, store_identity = _artifact_store_evidence_verifier_binding(self)
        return f"{ArtifactStoreEvidenceVerifier.VERIFIER_ID}:{store_identity}"

    @property
    def store(self) -> ArtifactStore:
        store, _, _, _ = _artifact_store_evidence_verifier_binding(self)
        return store

    @property
    def evidence_root(self) -> Path:
        _, evidence_root, _, _ = _artifact_store_evidence_verifier_binding(self)
        return evidence_root

    @staticmethod
    def _manifest_hash(manifest: dict) -> str:
        payload = {
            key: value for key, value in manifest.items() if key != "manifest_hash"
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    def verify(self, ref: ImmutableEvidenceRef) -> EvidenceVerification:
        if type(ref) is not ImmutableEvidenceRef:
            raise TypeError("evidence reference must be exact ImmutableEvidenceRef")
        ref = _snapshot_immutable_evidence_ref(ref)
        _, _, read_snapshot, _ = _artifact_store_evidence_verifier_binding(self)
        if not callable(read_snapshot):
            raise ValueError("trusted immutable evidence reader is unavailable")
        try:
            manifest, payload = read_snapshot(ref.artifact_id)
        except FileNotFoundError:
            return EvidenceVerification(
                valid=False,
                reason="immutable evidence artifact is missing",
            )
        except Exception:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence artifact is unreadable or corrupt",
            )
        if type(manifest) is not dict or type(payload) is not bytes:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence artifact representation is invalid",
            )
        manifest_hash = manifest.get("manifest_hash")
        if (
            type(manifest_hash) is not str
            or manifest_hash != ArtifactStoreEvidenceVerifier._manifest_hash(manifest)
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence manifest integrity binding is missing or invalid",
            )
        actual_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if (
            manifest.get("artifact_id") != ref.artifact_id
            or manifest.get("sha256") != ref.sha256
            or actual_digest != ref.sha256
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence identity or digest mismatch",
            )
        metadata = manifest.get("metadata")
        if type(metadata) is not dict:
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence metadata is missing",
            )
        expected = {
            "artifact_kind": "BOUNDED_REAL_EVIDENCE",
            "schema_version": 1,
            "evidence_kind": ref.evidence_kind,
            "source_sha": ref.source_sha,
            "envelope_id": ref.envelope_id,
            "envelope_digest": ref.envelope_digest,
            "provider_id": ref.provider_id,
            "account_id": ref.account_id,
            "outcome": "PASS",
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence semantics do not match qualification scope",
            )
        if (
            type(metadata.get("producer_id")) is not str
            or not metadata["producer_id"].strip()
            or type(metadata.get("evidence_version")) is not str
            or not metadata["evidence_version"].strip()
        ):
            return EvidenceVerification(
                valid=False,
                conflicted=True,
                reason="immutable evidence producer/version identity is missing",
            )
        return EvidenceVerification(valid=True)


def artifact_store_evidence_verifier(
    store: object,
    *,
    evidence_root: str | Path,
) -> ArtifactStoreEvidenceVerifier:
    """Create the canonical bounded-real immutable-evidence integrity verifier."""

    return ArtifactStoreEvidenceVerifier(
        store,
        evidence_root=evidence_root,
    )


@dataclass(frozen=True)
class QualificationEvidence:
    evidence_id: str
    evidence_kind: str
    source_sha: str
    envelope_id: str
    envelope_digest: str
    passed: bool
    evidence_ref: ImmutableEvidenceRef
    unresolved_blockers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.unresolved_blockers) is not tuple:
            raise TypeError("unresolved_blockers must be an exact tuple")
        blockers = tuple(
            _text(value, name="unresolved_blocker")
            for value in self.unresolved_blockers
        )
        if len(blockers) != len(set(blockers)):
            raise ValueError("unresolved_blockers must be unique")
        evidence_id = _text(self.evidence_id, name="evidence_id")
        evidence_kind = _text(self.evidence_kind, name="evidence_kind").upper()
        source_sha = _sha(self.source_sha, name="source_sha")
        envelope_id = _text(self.envelope_id, name="envelope_id")
        envelope_digest = _digest(self.envelope_digest)
        if type(self.evidence_ref) is not ImmutableEvidenceRef:
            raise TypeError("evidence_ref must be exact ImmutableEvidenceRef")
        evidence_ref = _snapshot_immutable_evidence_ref(self.evidence_ref)
        if evidence_ref.evidence_kind != f"PREREQUISITE:{evidence_kind}":
            raise ValueError("prerequisite evidence_ref kind does not match evidence_kind")
        if (
            evidence_ref.source_sha != source_sha
            or evidence_ref.envelope_id != envelope_id
            or evidence_ref.envelope_digest != envelope_digest
        ):
            raise ValueError("prerequisite evidence_ref scope does not match evidence")
        object.__setattr__(self, "evidence_id", evidence_id)
        object.__setattr__(self, "evidence_kind", evidence_kind)
        object.__setattr__(self, "source_sha", source_sha)
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "envelope_digest", envelope_digest)
        object.__setattr__(self, "passed", _bool(self.passed, name="passed"))
        object.__setattr__(self, "evidence_ref", evidence_ref)
        object.__setattr__(self, "unresolved_blockers", blockers)

    @classmethod
    def create(cls, **values) -> "QualificationEvidence":
        return cls(**values)


def _snapshot_qualification_evidence(
    evidence: QualificationEvidence,
) -> QualificationEvidence:
    if type(evidence) is not QualificationEvidence:
        raise TypeError(
            "prerequisite_evidence must contain exact QualificationEvidence"
        )
    return QualificationEvidence(
        evidence_id=object.__getattribute__(evidence, "evidence_id"),
        evidence_kind=object.__getattribute__(evidence, "evidence_kind"),
        source_sha=object.__getattribute__(evidence, "source_sha"),
        envelope_id=object.__getattribute__(evidence, "envelope_id"),
        envelope_digest=object.__getattribute__(evidence, "envelope_digest"),
        passed=object.__getattribute__(evidence, "passed"),
        evidence_ref=object.__getattribute__(evidence, "evidence_ref"),
        unresolved_blockers=object.__getattribute__(
            evidence,
            "unresolved_blockers",
        ),
    )


@dataclass(frozen=True)
class BoundedRealObservations:
    source_sha: str
    envelope_id: str
    envelope_digest: str
    provider_id: str
    account_id: str
    observed_fill_count: int
    observed_partial_fill: bool
    all_fills_reconciled: bool
    fees_reconciled: bool
    revocation_verified: bool
    protection_verified: bool
    unauthorized_action_count: int
    unresolved_unknown_count: int
    evidence_refs: tuple[ImmutableEvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        source_sha = _sha(self.source_sha, name="source_sha")
        envelope_id = _text(self.envelope_id, name="envelope_id")
        envelope_digest = _digest(self.envelope_digest)
        provider_id = _text(self.provider_id, name="provider_id")
        account_id = _text(self.account_id, name="account_id")
        for value, name in (
            (self.observed_fill_count, "observed_fill_count"),
            (self.unauthorized_action_count, "unauthorized_action_count"),
            (self.unresolved_unknown_count, "unresolved_unknown_count"),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for field_name in (
            "observed_partial_fill",
            "all_fills_reconciled",
            "fees_reconciled",
            "revocation_verified",
            "protection_verified",
        ):
            _bool(getattr(self, field_name), name=field_name)
        if type(self.evidence_refs) is not tuple:
            raise TypeError("evidence_refs must be an exact tuple")
        refs = tuple(
            _snapshot_immutable_evidence_ref(ref)
            for ref in self.evidence_refs
        )
        artifact_ids: set[str] = set()
        digests: set[str] = set()
        kinds: set[str] = set()
        for ref in refs:
            if (
                ref.source_sha != source_sha
                or ref.envelope_id != envelope_id
                or ref.envelope_digest != envelope_digest
                or ref.provider_id != provider_id
                or ref.account_id != account_id
            ):
                raise ValueError("observation evidence_ref scope does not match observations")
            if ref.artifact_id in artifact_ids or ref.sha256 in digests:
                raise ValueError("observation evidence artifacts must be unique")
            if ref.evidence_kind in kinds:
                raise ValueError("observation evidence kinds must be unique")
            artifact_ids.add(ref.artifact_id)
            digests.add(ref.sha256)
            kinds.add(ref.evidence_kind)
        object.__setattr__(self, "source_sha", source_sha)
        object.__setattr__(self, "envelope_id", envelope_id)
        object.__setattr__(self, "envelope_digest", envelope_digest)
        object.__setattr__(self, "provider_id", provider_id)
        object.__setattr__(self, "account_id", account_id)
        object.__setattr__(self, "evidence_refs", refs)

    @classmethod
    def create(cls, **values) -> "BoundedRealObservations":
        return cls(**values)


def _snapshot_bounded_real_observations(
    observations: BoundedRealObservations,
) -> BoundedRealObservations:
    if type(observations) is not BoundedRealObservations:
        raise TypeError("observations must be exact BoundedRealObservations")
    return BoundedRealObservations(
        source_sha=object.__getattribute__(observations, "source_sha"),
        envelope_id=object.__getattribute__(observations, "envelope_id"),
        envelope_digest=object.__getattribute__(
            observations,
            "envelope_digest",
        ),
        provider_id=object.__getattribute__(observations, "provider_id"),
        account_id=object.__getattribute__(observations, "account_id"),
        observed_fill_count=object.__getattribute__(
            observations,
            "observed_fill_count",
        ),
        observed_partial_fill=object.__getattribute__(
            observations,
            "observed_partial_fill",
        ),
        all_fills_reconciled=object.__getattribute__(
            observations,
            "all_fills_reconciled",
        ),
        fees_reconciled=object.__getattribute__(
            observations,
            "fees_reconciled",
        ),
        revocation_verified=object.__getattribute__(
            observations,
            "revocation_verified",
        ),
        protection_verified=object.__getattribute__(
            observations,
            "protection_verified",
        ),
        unauthorized_action_count=object.__getattribute__(
            observations,
            "unauthorized_action_count",
        ),
        unresolved_unknown_count=object.__getattribute__(
            observations,
            "unresolved_unknown_count",
        ),
        evidence_refs=object.__getattribute__(observations, "evidence_refs"),
    )


@dataclass(frozen=True)
class BoundedRealQualificationResult:
    complete: bool
    reason_codes: tuple[str, ...]
    exact_source_sha: str
    envelope_id: str
    envelope_digest: str
    evidence_verifier_identity: str | None
    qualification_attestation_id: str | None = None
    qualification_attestation_digest: str | None = None
    qualification_policy_id: str | None = None
    qualification_trust_root_id: str | None = None

    @property
    def authorizes_trading(self) -> bool:
        """Qualification evidence is never itself an authority token."""
        return False


_REQUIRED_EVIDENCE_KINDS = frozenset(
    {
        "RELEASE_CANDIDATE",
        "SCIENTIFIC_QUALIFICATION",
        "FORWARD_PAPER",
        "PROVIDER_CAPABILITY",
        "ACCESSIBILITY",
        "RECOVERY",
    }
)
_REQUIRED_OBSERVATION_KINDS = frozenset(
    {
        "ACTUAL_FILL",
        "PARTIAL_FILL",
        "FILL_RECONCILIATION",
        "FEE_RECONCILIATION",
        "REVOCATION",
        "PROTECTION",
        "AUTHORITY_AUDIT",
        "UNKNOWN_SUBMISSION_AUDIT",
    }
)


def assess_bounded_real_qualification(
    *,
    envelope: BoundedRealEnvelope,
    prerequisite_evidence: Iterable[QualificationEvidence],
    observations: BoundedRealObservations,
    evidence_verifier: ArtifactStoreEvidenceVerifier | None = None,
    qualification_receipt: SignedQualificationAttestation | None = None,
    qualification_policy: QualificationTrustPolicy | None = None,
    expected_policy_id: str | None = None,
    expected_policy_version: str | None = None,
) -> BoundedRealQualificationResult:
    """Validate a bounded-real evidence bundle without granting authority."""

    if type(envelope) is not BoundedRealEnvelope:
        raise TypeError("envelope must be exact BoundedRealEnvelope")
    if type(observations) is not BoundedRealObservations:
        raise TypeError("observations must be exact BoundedRealObservations")
    if type(prerequisite_evidence) not in {list, tuple}:
        raise TypeError("prerequisite_evidence must be an exact list or tuple")

    envelope = _snapshot_bounded_real_envelope(envelope)
    observations = _snapshot_bounded_real_observations(observations)
    prerequisite_evidence = tuple(
        _snapshot_qualification_evidence(evidence)
        for evidence in prerequisite_evidence
    )

    reasons: list[str] = []
    scope_matches = (
        observations.source_sha == envelope.source_sha
        and observations.envelope_id == envelope.envelope_id
        and observations.envelope_digest == envelope.envelope_digest
        and observations.provider_id == envelope.provider_id
        and observations.account_id == envelope.account_id
    )
    if not scope_matches:
        reasons.append("observation_scope_mismatch")

    evidence_by_kind: dict[str, QualificationEvidence] = {}
    all_refs: list[tuple[str, ImmutableEvidenceRef]] = []
    evidence_ids: set[str] = set()
    for evidence in prerequisite_evidence:
        if type(evidence) is not QualificationEvidence:
            raise TypeError(
                "prerequisite_evidence must contain exact QualificationEvidence"
            )
        if evidence.evidence_id in evidence_ids:
            raise ValueError("evidence_id must be unique")
        evidence_ids.add(evidence.evidence_id)
        if evidence.evidence_kind in evidence_by_kind:
            raise ValueError(
                f"duplicate prerequisite evidence kind: {evidence.evidence_kind}"
            )
        evidence_by_kind[evidence.evidence_kind] = evidence
        all_refs.append(
            (f"PREREQUISITE:{evidence.evidence_kind}", evidence.evidence_ref)
        )

    observation_by_kind = {
        ref.evidence_kind: ref for ref in observations.evidence_refs
    }
    all_refs.extend(
        (f"OBSERVATION:{kind}", ref)
        for kind, ref in observation_by_kind.items()
    )

    artifact_ids: set[str] = set()
    digests: set[str] = set()
    for label, ref in all_refs:
        if ref.artifact_id in artifact_ids:
            raise ValueError("immutable evidence artifact_id must be unique")
        if ref.sha256 in digests:
            raise ValueError("immutable evidence digest must be unique")
        artifact_ids.add(ref.artifact_id)
        digests.add(ref.sha256)
        if (
            ref.source_sha != envelope.source_sha
            or ref.envelope_id != envelope.envelope_id
            or ref.envelope_digest != envelope.envelope_digest
            or ref.provider_id != envelope.provider_id
            or ref.account_id != envelope.account_id
        ):
            reasons.append(f"immutable_evidence_scope_mismatch:{label}")

    missing = sorted(_REQUIRED_EVIDENCE_KINDS - set(evidence_by_kind))
    if missing:
        reasons.append("missing_prerequisite_evidence:" + ",".join(missing))
    missing_observations = sorted(
        _REQUIRED_OBSERVATION_KINDS - set(observation_by_kind)
    )
    if missing_observations:
        reasons.append(
            "missing_observation_evidence:" + ",".join(missing_observations)
        )

    for kind in sorted(_REQUIRED_EVIDENCE_KINDS & set(evidence_by_kind)):
        evidence = evidence_by_kind[kind]
        if evidence.source_sha != envelope.source_sha:
            reasons.append(f"source_sha_mismatch:{kind}")
        if evidence.envelope_id != envelope.envelope_id:
            reasons.append(f"envelope_mismatch:{kind}")
        if evidence.envelope_digest != envelope.envelope_digest:
            reasons.append(f"envelope_digest_mismatch:{kind}")
        if (
            evidence.evidence_ref.provider_id != envelope.provider_id
            or evidence.evidence_ref.account_id != envelope.account_id
        ):
            reasons.append(f"provider_account_mismatch:{kind}")
        if not evidence.passed:
            reasons.append(f"prerequisite_failed:{kind}")
        if evidence.unresolved_blockers:
            reasons.append(f"unresolved_blockers:{kind}")

    verifier_identity: str | None = None
    accepted: AcceptedQualificationAttestation | None = None
    if evidence_verifier is None:
        reasons.append("trusted_immutable_evidence_verifier_required")
    elif type(evidence_verifier) is not ArtifactStoreEvidenceVerifier:
        reasons.append("untrusted_immutable_evidence_verifier")
    else:
        verifier_identity = ArtifactStoreEvidenceVerifier.identity.__get__(
            evidence_verifier,
            ArtifactStoreEvidenceVerifier,
        )
        for label, ref in all_refs:
            try:
                verification = ArtifactStoreEvidenceVerifier.verify(
                    evidence_verifier,
                    ref,
                )
            except Exception:
                verification = EvidenceVerification(
                    valid=False,
                    conflicted=True,
                    reason="trusted immutable evidence verifier raised",
                )
            if type(verification) is not EvidenceVerification:
                raise TypeError(
                    "trusted evidence verifier must return exact EvidenceVerification"
                )
            if not verification.valid:
                suffix = "conflicted" if verification.conflicted else "unverified"
                reasons.append(f"immutable_evidence_{suffix}:{label}")

    trust_inputs = (
        qualification_receipt,
        qualification_policy,
        expected_policy_id,
        expected_policy_version,
    )
    if all(value is None for value in trust_inputs):
        reasons.append("independent_evidence_trust_unavailable")
    elif any(value is None for value in trust_inputs):
        reasons.append("independent_evidence_trust_incomplete")
    elif type(evidence_verifier) is not ArtifactStoreEvidenceVerifier:
        reasons.append("independent_evidence_trust_unavailable")
    elif (
        type(qualification_receipt) is not SignedQualificationAttestation
        or type(qualification_policy) is not QualificationTrustPolicy
    ):
        reasons.append("independent_evidence_trust_invalid")
    else:
        required_scope = f"envelope/{envelope.envelope_digest}"
        signed_requirements = frozenset(
            qualification_receipt.attestation.requirement_ids
        )
        signed_refs = frozenset(
            (ref.artifact_id, ref.sha256, ref.evidence_kind)
            for ref in qualification_receipt.attestation.evidence_refs
        )
        expected_refs = frozenset(
            (ref.artifact_id, ref.sha256, ref.evidence_kind)
            for _, ref in all_refs
        )
        try:
            accepted = verify_qualification_attestation(
                qualification_receipt,
                policy=qualification_policy,
                evidence_store=ArtifactStoreEvidenceVerifier.store.__get__(
                    evidence_verifier,
                    ArtifactStoreEvidenceVerifier,
                ),
                evidence_root=ArtifactStoreEvidenceVerifier.evidence_root.__get__(
                    evidence_verifier,
                    ArtifactStoreEvidenceVerifier,
                ),
                expected_policy_id=expected_policy_id,
                expected_policy_version=expected_policy_version,
                expected_source_sha=envelope.source_sha,
                expected_domain=_QUALIFICATION_DOMAIN,
                expected_gate=_QUALIFICATION_GATE,
                expected_package_id=_QUALIFICATION_PACKAGE,
                expected_protocol_id=_QUALIFICATION_PROTOCOL,
                expected_protocol_version=_QUALIFICATION_PROTOCOL_VERSION,
                expected_requirement_id=_QUALIFICATION_REQUIREMENT,
            )
        except (QualificationTrustError, TypeError, ValueError):
            reasons.append("independent_evidence_trust_invalid")
        else:
            if accepted.result == "FAIL":
                reasons.append("independent_evidence_attestation_failed")
            elif accepted.result != "PASS":
                reasons.append("independent_evidence_attestation_inconclusive")
            if required_scope not in signed_requirements:
                reasons.append("independent_evidence_scope_mismatch")
            if signed_refs != expected_refs:
                reasons.append("independent_evidence_set_mismatch")

    if observations.observed_fill_count < 1:
        reasons.append("no_real_fill_evidence")
    if not observations.observed_partial_fill:
        reasons.append("partial_fill_not_evidenced")
    if not observations.all_fills_reconciled:
        reasons.append("fills_not_fully_reconciled")
    if not observations.fees_reconciled:
        reasons.append("fees_not_reconciled")
    if not observations.revocation_verified:
        reasons.append("revocation_not_verified")
    if not observations.protection_verified:
        reasons.append("protection_not_verified")
    if observations.unauthorized_action_count != 0:
        reasons.append("unauthorized_action_observed")
    if observations.unresolved_unknown_count != 0:
        reasons.append("unresolved_submission_unknown")

    return BoundedRealQualificationResult(
        complete=not reasons,
        reason_codes=tuple(reasons),
        exact_source_sha=envelope.source_sha,
        envelope_id=envelope.envelope_id,
        envelope_digest=envelope.envelope_digest,
        evidence_verifier_identity=verifier_identity,
        qualification_attestation_id=(
            None if accepted is None else accepted.attestation_id
        ),
        qualification_attestation_digest=(
            None if accepted is None else accepted.attestation_digest
        ),
        qualification_policy_id=(
            None if accepted is None else accepted.policy_id
        ),
        qualification_trust_root_id=(
            None if accepted is None else accepted.trust_root_id
        ),
    )
