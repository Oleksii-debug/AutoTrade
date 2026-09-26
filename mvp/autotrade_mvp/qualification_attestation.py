from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import binascii
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Iterable, Mapping
from uuid import UUID

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


class QualificationTrustError(ValueError):
    """Raised when qualification evidence cannot cross the trust boundary."""


class QualificationTrustUnavailable(QualificationTrustError):
    """Raised when the separately controlled canonical trust policy is absent."""


_CANONICAL_QUALIFICATION_TRUST_POLICY_PATH = Path(__file__).with_name(
    "qualification_trust_policy.json"
)
_CANONICAL_QUALIFICATION_TRUST_POLICY_GIT_PATH = (
    "mvp/autotrade_mvp/qualification_trust_policy.json"
)

_QUALIFICATION_TRUST_SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _trusted_git_candidate_paths() -> tuple[Path, ...]:
    """Return fail-closed OS-managed Git locations without consulting PATH."""

    if os.name == "nt":
        return (
            Path(r"C:\\Program Files\\Git\\cmd\\git.exe"),
            Path(r"C:\\Program Files\\Git\\bin\\git.exe"),
        )
    return (Path("/usr/bin/git"), Path("/bin/git"))


def _trusted_git_executable(*, source_root: Path) -> str:
    """Resolve Git without caller-controlled PATH or source-checkout authority."""

    resolved_source_root = source_root.resolve()
    for candidate in _trusted_git_candidate_paths():
        try:
            executable = candidate.resolve(strict=True)
        except OSError:
            continue
        if not executable.is_file():
            continue
        try:
            executable.relative_to(resolved_source_root)
        except ValueError:
            return os.fspath(executable)
        raise QualificationTrustUnavailable(
            "qualification trust Git executable originates from trusted source checkout"
        )
    raise QualificationTrustUnavailable(
        "qualification trust Git executable is unavailable at an OS-managed location"
    )


_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")


def _trusted_git_environment() -> dict[str, str]:
    """Run trust-policy Git reads without caller-selected process authority."""

    # Do not inherit the ambient process environment wholesale. An absolute Git
    # executable is still vulnerable to dynamic-loader injection (for example
    # LD_PRELOAD / DYLD_*), user-selected HOME config, and other process-level
    # overrides if those variables are forwarded to the trust-critical child.
    # Git's exact-object reads need only a tiny environment; retain the Windows
    # process bootstrap variables when present and explicitly disable external
    # Git configuration plus replacement-object semantics.
    environment = {
        key: value
        for key in ("SYSTEMROOT", "WINDIR", "COMSPEC")
        if (value := os.environ.get(key))
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["LC_ALL"] = "C"
    environment["LANG"] = "C"
    return environment


_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_RSA_METHOD = "RSA_PKCS1V15_SHA256"
_RESULTS = frozenset({"PASS", "FAIL", "INCONCLUSIVE"})
_SHA256_DER_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_json_object(
    pairs: Iterable[tuple[str, object]],
) -> dict[str, object]:
    """Reject ambiguous trust-policy JSON before schema validation."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise QualificationTrustError(
                "canonical qualification trust policy contains duplicate JSON object key"
            )
        result[key] = value
    return result


def _strict_mapping(
    value: object,
    *,
    name: str,
    keys: frozenset[str],
) -> Mapping[str, object]:
    if type(value) is not dict:
        raise QualificationTrustError(f"{name} must be a JSON object")
    actual = frozenset(value)
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        raise QualificationTrustError(
            f"{name} fields mismatch: missing={missing} extra={extra}"
        )
    return value


def _strict_list(value: object, *, name: str) -> list[object]:
    if type(value) is not list:
        raise QualificationTrustError(f"{name} must be a JSON array")
    return value


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or value != value.strip() or not value:
        raise QualificationTrustError(f"{name} must be a canonical non-empty string")
    return value


def _token(value: str, *, name: str, upper: bool = False) -> str:
    value = _text(value, name=name)
    value = value.upper() if upper else value
    if _TOKEN.fullmatch(value) is None:
        raise QualificationTrustError(f"{name} is not canonical")
    return value


def _uuid(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError, TypeError) as error:
        raise QualificationTrustError(f"{name} must be a canonical UUID") from error
    if canonical != value:
        raise QualificationTrustError(f"{name} must be a canonical UUID")
    return value


def _git_sha(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _GIT_SHA.fullmatch(value) is None:
        raise QualificationTrustError(
            f"{name} must be a lowercase 40-character Git SHA"
        )
    return value


def _digest(value: str, *, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise QualificationTrustError(f"{name} must be sha256:<64 lowercase hex>")
    return value


def _instant(value: str, *, name: str) -> str:
    value = _text(value, name=name)
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as error:
        raise QualificationTrustError(
            f"{name} must use canonical UTC second precision"
        ) from error
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise QualificationTrustError(
            f"{name} must use canonical UTC second precision"
        )
    return value


def _instant_value(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=timezone.utc
    )


@dataclass(frozen=True)
class EvidenceArtifactRef:
    artifact_id: str
    sha256: str
    media_type: str
    evidence_kind: str
    source_sha: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "artifact_id", _uuid(self.artifact_id, name="artifact_id")
        )
        object.__setattr__(self, "sha256", _digest(self.sha256, name="sha256"))
        object.__setattr__(
            self, "media_type", _text(self.media_type, name="media_type")
        )
        object.__setattr__(
            self,
            "evidence_kind",
            _token(self.evidence_kind, name="evidence_kind", upper=True),
        )
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )

    def canonical(self) -> dict[str, str]:
        return {
            "artifact_id": self.artifact_id,
            "evidence_kind": self.evidence_kind,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "source_sha": self.source_sha,
        }


@dataclass(frozen=True, order=True)
class QualificationScope:
    domain: str
    gate: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "domain", _token(self.domain, name="domain", upper=True)
        )
        object.__setattr__(
            self, "gate", _token(self.gate, name="gate", upper=True)
        )

    def canonical(self) -> dict[str, str]:
        return {"domain": self.domain, "gate": self.gate}


@dataclass(frozen=True)
class TrustRoot:
    producer_id: str
    verifier_id: str
    public_modulus_hex: str
    public_exponent: int
    allowed_scopes: tuple[QualificationScope, ...]
    valid_from: str
    valid_until: str | None = None
    revoked_at: str | None = None
    verification_method: str = _RSA_METHOD

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "producer_id", _token(self.producer_id, name="producer_id")
        )
        object.__setattr__(
            self, "verifier_id", _token(self.verifier_id, name="verifier_id")
        )
        method = _token(
            self.verification_method, name="verification_method", upper=True
        )
        if method != _RSA_METHOD:
            raise QualificationTrustError("unsupported verification_method")
        object.__setattr__(self, "verification_method", method)
        modulus_hex = self.public_modulus_hex
        if (
            not isinstance(modulus_hex, str)
            or modulus_hex != modulus_hex.lower()
            or not modulus_hex
            or len(modulus_hex) % 2
            or modulus_hex.startswith("0")
            or any(ch not in "0123456789abcdef" for ch in modulus_hex)
        ):
            raise QualificationTrustError(
                "public_modulus_hex must be canonical lowercase hex"
            )
        modulus = int(modulus_hex, 16)
        if not 2048 <= modulus.bit_length() <= 4096:
            raise QualificationTrustError("RSA trust root must be 2048-4096 bits")
        exponent = self.public_exponent
        if (
            isinstance(exponent, bool)
            or not isinstance(exponent, int)
            or exponent < 3
            or exponent >= 2**32
            or exponent % 2 == 0
        ):
            raise QualificationTrustError("public_exponent is invalid")
        scopes = tuple(self.allowed_scopes)
        if not scopes or not all(
            isinstance(item, QualificationScope) for item in scopes
        ):
            raise QualificationTrustError(
                "allowed_scopes must contain QualificationScope values"
            )
        scopes = tuple(sorted(scopes))
        if len(set(scopes)) != len(scopes):
            raise QualificationTrustError("allowed_scopes must be unique")
        object.__setattr__(self, "allowed_scopes", scopes)
        object.__setattr__(
            self, "valid_from", _instant(self.valid_from, name="valid_from")
        )
        if self.valid_until is not None:
            valid_until = _instant(self.valid_until, name="valid_until")
            if _instant_value(valid_until) <= _instant_value(self.valid_from):
                raise QualificationTrustError(
                    "valid_until must be after valid_from"
                )
            object.__setattr__(self, "valid_until", valid_until)
        if self.revoked_at is not None:
            revoked_at = _instant(self.revoked_at, name="revoked_at")
            if _instant_value(revoked_at) <= _instant_value(self.valid_from):
                raise QualificationTrustError(
                    "revoked_at must be after valid_from"
                )
            object.__setattr__(self, "revoked_at", revoked_at)

    @property
    def root_id(self) -> str:
        payload = {
            "producer_id": self.producer_id,
            "public_exponent": self.public_exponent,
            "public_modulus_hex": self.public_modulus_hex,
            "verification_method": self.verification_method,
            "verifier_id": self.verifier_id,
        }
        return "sha256:" + sha256(_canonical_json(payload)).hexdigest()

    def canonical(self) -> dict[str, object]:
        return {
            "allowed_scopes": [item.canonical() for item in self.allowed_scopes],
            "producer_id": self.producer_id,
            "public_exponent": self.public_exponent,
            "public_modulus_hex": self.public_modulus_hex,
            "revoked_at": self.revoked_at,
            "root_id": self.root_id,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "verification_method": self.verification_method,
            "verifier_id": self.verifier_id,
        }


@dataclass(frozen=True)
class QualificationTrustPolicy:
    policy_version: str
    roots: tuple[TrustRoot, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_version",
            _token(self.policy_version, name="policy_version"),
        )
        roots = tuple(self.roots)
        if not roots or not all(isinstance(item, TrustRoot) for item in roots):
            raise QualificationTrustError("roots must contain TrustRoot values")
        roots = tuple(sorted(roots, key=lambda item: item.root_id))
        if len({item.root_id for item in roots}) != len(roots):
            raise QualificationTrustError(
                "trust roots must have unique public-key identities"
            )
        object.__setattr__(self, "roots", roots)

    @property
    def policy_id(self) -> str:
        payload = {
            "policy_version": self.policy_version,
            "roots": [item.canonical() for item in self.roots],
        }
        return "sha256:" + sha256(_canonical_json(payload)).hexdigest()

    def root(self, root_id: str) -> TrustRoot:
        root_id = _digest(root_id, name="trust_root_id")
        for root in self.roots:
            if root.root_id == root_id:
                return root
        raise QualificationTrustError(
            "trust root is not authorized by qualification policy"
        )


@dataclass(frozen=True)
class QualificationAttestation:
    attestation_id: str
    source_sha: str
    domain: str
    gate: str
    package_id: str
    protocol_id: str
    protocol_version: str
    requirement_ids: tuple[str, ...]
    evidence_refs: tuple[EvidenceArtifactRef, ...]
    producer_id: str
    verifier_id: str
    trust_root_id: str
    runner_id: str
    harness_version: str
    started_at: str
    completed_at: str
    signed_at: str
    result: str
    unresolved_limits: tuple[str, ...] = ()
    release_artifact_id: str | None = None
    release_artifact_sha256: str | None = None
    schema_version: str = "1.0.0"
    verification_method: str = _RSA_METHOD

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise QualificationTrustError(
                "unsupported attestation schema_version"
            )
        object.__setattr__(
            self,
            "attestation_id",
            _uuid(self.attestation_id, name="attestation_id"),
        )
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )
        for name in ("domain", "gate", "package_id"):
            object.__setattr__(
                self, name, _token(getattr(self, name), name=name, upper=True)
            )
        object.__setattr__(
            self, "protocol_id", _token(self.protocol_id, name="protocol_id")
        )
        object.__setattr__(
            self,
            "protocol_version",
            _token(self.protocol_version, name="protocol_version"),
        )
        requirements = tuple(
            _token(item, name="requirement_id")
            for item in self.requirement_ids
        )
        if not requirements or len(set(requirements)) != len(requirements):
            raise QualificationTrustError(
                "requirement_ids must be non-empty and unique"
            )
        object.__setattr__(
            self, "requirement_ids", tuple(sorted(requirements))
        )
        refs = tuple(self.evidence_refs)
        if not refs or not all(
            isinstance(item, EvidenceArtifactRef) for item in refs
        ):
            raise QualificationTrustError(
                "evidence_refs must contain evidence artifacts"
            )
        if len({item.artifact_id for item in refs}) != len(refs):
            raise QualificationTrustError(
                "evidence artifact identities must be unique"
            )
        if any(item.source_sha != self.source_sha for item in refs):
            raise QualificationTrustError(
                "evidence source_sha must match attestation source_sha"
            )
        object.__setattr__(
            self,
            "evidence_refs",
            tuple(sorted(refs, key=lambda item: item.artifact_id)),
        )
        object.__setattr__(
            self, "producer_id", _token(self.producer_id, name="producer_id")
        )
        object.__setattr__(
            self, "verifier_id", _token(self.verifier_id, name="verifier_id")
        )
        method = _token(
            self.verification_method, name="verification_method", upper=True
        )
        if method != _RSA_METHOD:
            raise QualificationTrustError("unsupported verification_method")
        object.__setattr__(self, "verification_method", method)
        object.__setattr__(
            self,
            "trust_root_id",
            _digest(self.trust_root_id, name="trust_root_id"),
        )
        object.__setattr__(
            self, "runner_id", _token(self.runner_id, name="runner_id")
        )
        object.__setattr__(
            self,
            "harness_version",
            _token(self.harness_version, name="harness_version"),
        )
        for name in ("started_at", "completed_at", "signed_at"):
            object.__setattr__(
                self, name, _instant(getattr(self, name), name=name)
            )
        if not (
            _instant_value(self.started_at)
            <= _instant_value(self.completed_at)
            <= _instant_value(self.signed_at)
        ):
            raise QualificationTrustError(
                "qualification timestamps are out of order"
            )
        result = _token(self.result, name="result", upper=True)
        if result not in _RESULTS:
            raise QualificationTrustError("unsupported qualification result")
        object.__setattr__(self, "result", result)
        limits = tuple(
            _text(item, name="unresolved_limit")
            for item in self.unresolved_limits
        )
        if len(set(limits)) != len(limits):
            raise QualificationTrustError(
                "unresolved_limits must be unique"
            )
        limits = tuple(sorted(limits))
        if result == "PASS" and limits:
            raise QualificationTrustError(
                "PASS cannot carry unresolved limits"
            )
        object.__setattr__(self, "unresolved_limits", limits)
        if (self.release_artifact_id is None) != (
            self.release_artifact_sha256 is None
        ):
            raise QualificationTrustError(
                "release artifact identity and digest must be supplied together"
            )
        if self.release_artifact_id is not None:
            object.__setattr__(
                self,
                "release_artifact_id",
                _uuid(self.release_artifact_id, name="release_artifact_id"),
            )
            object.__setattr__(
                self,
                "release_artifact_sha256",
                _digest(
                    self.release_artifact_sha256,
                    name="release_artifact_sha256",
                ),
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "attestation_id": self.attestation_id,
            "completed_at": self.completed_at,
            "domain": self.domain,
            "evidence_refs": [
                item.canonical() for item in self.evidence_refs
            ],
            "gate": self.gate,
            "harness_version": self.harness_version,
            "package_id": self.package_id,
            "producer_id": self.producer_id,
            "protocol_id": self.protocol_id,
            "protocol_version": self.protocol_version,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "requirement_ids": list(self.requirement_ids),
            "result": self.result,
            "runner_id": self.runner_id,
            "schema_version": self.schema_version,
            "signed_at": self.signed_at,
            "source_sha": self.source_sha,
            "started_at": self.started_at,
            "trust_root_id": self.trust_root_id,
            "unresolved_limits": list(self.unresolved_limits),
            "verification_method": self.verification_method,
            "verifier_id": self.verifier_id,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @property
    def content_digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()


@dataclass(frozen=True)
class SignedQualificationAttestation:
    attestation: QualificationAttestation
    signature_b64: str

    def __post_init__(self) -> None:
        if not isinstance(self.attestation, QualificationAttestation):
            raise TypeError(
                "attestation must be QualificationAttestation"
            )
        signature = _text(self.signature_b64, name="signature_b64")
        try:
            decoded = base64.b64decode(signature, validate=True)
        except (binascii.Error, ValueError) as error:
            raise QualificationTrustError(
                "signature_b64 is not canonical base64"
            ) from error
        if base64.b64encode(decoded).decode("ascii") != signature:
            raise QualificationTrustError(
                "signature_b64 is not canonical base64"
            )
        if not decoded:
            raise QualificationTrustError("signature is empty")


def qualification_trust_policy_payload(
    policy: QualificationTrustPolicy,
) -> dict[str, object]:
    if not isinstance(policy, QualificationTrustPolicy):
        raise TypeError("policy must be QualificationTrustPolicy")
    return {
        "policy_version": policy.policy_version,
        "roots": [root.canonical() for root in policy.roots],
    }


def parse_qualification_trust_policy(
    value: object,
) -> QualificationTrustPolicy:
    payload = _strict_mapping(
        value,
        name="qualification trust policy",
        keys=frozenset({"policy_version", "roots"}),
    )
    roots_raw = _strict_list(payload["roots"], name="qualification trust roots")
    roots: list[TrustRoot] = []
    root_keys = frozenset(
        {
            "allowed_scopes",
            "producer_id",
            "public_exponent",
            "public_modulus_hex",
            "revoked_at",
            "root_id",
            "valid_from",
            "valid_until",
            "verification_method",
            "verifier_id",
        }
    )
    scope_keys = frozenset({"domain", "gate"})
    for index, raw_root in enumerate(roots_raw):
        root_payload = _strict_mapping(
            raw_root,
            name=f"qualification trust root[{index}]",
            keys=root_keys,
        )
        scopes_raw = _strict_list(
            root_payload["allowed_scopes"],
            name=f"qualification trust root[{index}].allowed_scopes",
        )
        scopes = tuple(
            QualificationScope(
                **_strict_mapping(
                    raw_scope,
                    name=f"qualification trust root[{index}].allowed_scopes[{scope_index}]",
                    keys=scope_keys,
                )
            )
            for scope_index, raw_scope in enumerate(scopes_raw)
        )
        root = TrustRoot(
            producer_id=root_payload["producer_id"],
            verifier_id=root_payload["verifier_id"],
            public_modulus_hex=root_payload["public_modulus_hex"],
            public_exponent=root_payload["public_exponent"],
            allowed_scopes=scopes,
            valid_from=root_payload["valid_from"],
            valid_until=root_payload["valid_until"],
            revoked_at=root_payload["revoked_at"],
            verification_method=root_payload["verification_method"],
        )
        if root_payload["root_id"] != root.root_id:
            raise QualificationTrustError(
                f"qualification trust root[{index}] root_id mismatch"
            )
        roots.append(root)
    return QualificationTrustPolicy(
        policy_version=payload["policy_version"],
        roots=tuple(roots),
    )


def _canonical_qualification_trust_policy_bytes(
    *, expected_source_sha: str
) -> bytes:
    """Read policy bytes from the exact trusted Git source object."""

    source_sha = _git_sha(expected_source_sha, name="expected_source_sha")
    source_root = _QUALIFICATION_TRUST_SOURCE_ROOT.resolve()
    # The Git object path is a source constant, not a filesystem-derived path.
    # Resolving the working-tree policy path here would let a mutable symlink
    # redirect exact-source lookup to a different blob in the same trusted commit.
    relative_policy = _CANONICAL_QUALIFICATION_TRUST_POLICY_GIT_PATH
    git_executable = _trusted_git_executable(source_root=source_root)
    try:
        head = subprocess.run(
            [git_executable, "rev-parse", "HEAD"],
            cwd=source_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualificationTrustUnavailable(
            "qualification trust source selection is unavailable"
        ) from error
    if head.returncode != 0:
        raise QualificationTrustUnavailable(
            "qualification trust source selection could not be verified"
        )
    if head.stdout.strip() != source_sha:
        raise QualificationTrustError(
            "qualification trust source SHA does not match checkout HEAD"
        )

    try:
        completed = subprocess.run(
            [git_executable, "cat-file", "blob", f"{source_sha}:{relative_policy}"],
            cwd=source_root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            env=_trusted_git_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise QualificationTrustUnavailable(
            "exact-source qualification trust policy is unavailable"
        ) from error
    if completed.returncode != 0:
        raise QualificationTrustUnavailable(
            "canonical qualification trust policy is not present in exact trusted source"
        )
    return bytes(completed.stdout)


def load_canonical_qualification_trust_policy(
    *, expected_source_sha: str
) -> QualificationTrustPolicy:
    """Load the release-controlled policy from the exact trusted source SHA."""

    raw = _canonical_qualification_trust_policy_bytes(
        expected_source_sha=expected_source_sha
    )
    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_object,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QualificationTrustError(
            "canonical qualification trust policy is malformed"
        ) from error
    return parse_qualification_trust_policy(payload)

def parse_signed_qualification_attestation(
    value: object,
) -> SignedQualificationAttestation:
    receipt_payload = _strict_mapping(
        value,
        name="signed qualification attestation",
        keys=frozenset({"attestation", "signature_b64"}),
    )
    attestation_payload = _strict_mapping(
        receipt_payload["attestation"],
        name="qualification attestation",
        keys=frozenset(
            {
                "attestation_id",
                "completed_at",
                "domain",
                "evidence_refs",
                "gate",
                "harness_version",
                "package_id",
                "producer_id",
                "protocol_id",
                "protocol_version",
                "release_artifact_id",
                "release_artifact_sha256",
                "requirement_ids",
                "result",
                "runner_id",
                "schema_version",
                "signed_at",
                "source_sha",
                "started_at",
                "trust_root_id",
                "unresolved_limits",
                "verification_method",
                "verifier_id",
            }
        ),
    )
    evidence_raw = _strict_list(
        attestation_payload["evidence_refs"],
        name="qualification attestation.evidence_refs",
    )
    evidence_keys = frozenset(
        {
            "artifact_id",
            "evidence_kind",
            "media_type",
            "sha256",
            "source_sha",
        }
    )
    evidence_refs = tuple(
        EvidenceArtifactRef(
            **_strict_mapping(
                raw_ref,
                name=f"qualification attestation.evidence_refs[{index}]",
                keys=evidence_keys,
            )
        )
        for index, raw_ref in enumerate(evidence_raw)
    )
    requirements = _strict_list(
        attestation_payload["requirement_ids"],
        name="qualification attestation.requirement_ids",
    )
    limits = _strict_list(
        attestation_payload["unresolved_limits"],
        name="qualification attestation.unresolved_limits",
    )
    attestation = QualificationAttestation(
        attestation_id=attestation_payload["attestation_id"],
        source_sha=attestation_payload["source_sha"],
        domain=attestation_payload["domain"],
        gate=attestation_payload["gate"],
        package_id=attestation_payload["package_id"],
        protocol_id=attestation_payload["protocol_id"],
        protocol_version=attestation_payload["protocol_version"],
        requirement_ids=tuple(requirements),
        evidence_refs=evidence_refs,
        producer_id=attestation_payload["producer_id"],
        verifier_id=attestation_payload["verifier_id"],
        trust_root_id=attestation_payload["trust_root_id"],
        runner_id=attestation_payload["runner_id"],
        harness_version=attestation_payload["harness_version"],
        started_at=attestation_payload["started_at"],
        completed_at=attestation_payload["completed_at"],
        signed_at=attestation_payload["signed_at"],
        result=attestation_payload["result"],
        unresolved_limits=tuple(limits),
        release_artifact_id=attestation_payload["release_artifact_id"],
        release_artifact_sha256=attestation_payload["release_artifact_sha256"],
        schema_version=attestation_payload["schema_version"],
        verification_method=attestation_payload["verification_method"],
    )
    if attestation.canonical_payload() != dict(attestation_payload):
        raise QualificationTrustError(
            "qualification attestation is not in canonical serialized form"
        )
    return SignedQualificationAttestation(
        attestation=attestation,
        signature_b64=receipt_payload["signature_b64"],
    )


@dataclass(frozen=True)
class AcceptedQualificationAttestation:
    attestation_id: str
    attestation_digest: str
    policy_id: str
    policy_version: str
    trust_root_id: str
    result: str
    source_sha: str
    domain: str
    gate: str
    package_id: str
    protocol_id: str
    protocol_version: str
    requirement_id: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None


def _verify_rsa_pkcs1v15_sha256(
    *, payload: bytes, signature: bytes, root: TrustRoot
) -> None:
    modulus = int(root.public_modulus_hex, 16)
    width = (modulus.bit_length() + 7) // 8
    if len(signature) != width:
        raise QualificationTrustError(
            "signature length does not match trust root"
        )
    signature_int = int.from_bytes(signature, "big")
    if signature_int <= 0 or signature_int >= modulus:
        raise QualificationTrustError(
            "signature is outside RSA modulus"
        )
    encoded = pow(
        signature_int, root.public_exponent, modulus
    ).to_bytes(width, "big")
    digest_info = _SHA256_DER_PREFIX + sha256(payload).digest()
    padding_size = width - len(digest_info) - 3
    if padding_size < 8:
        raise QualificationTrustError("RSA trust root is too small")
    expected = (
        b"\x00\x01"
        + (b"\xff" * padding_size)
        + b"\x00"
        + digest_info
    )
    if encoded != expected:
        raise QualificationTrustError(
            "attestation signature verification failed"
        )


def _resolve_evidence(
    store: ArtifactStore, ref: EvidenceArtifactRef
) -> None:
    try:
        manifest = store.load_manifest(ref.artifact_id)
        if "manifest_hash" not in manifest:
            raise QualificationTrustError(
                "evidence manifest lacks integrity binding"
            )
        if manifest.get("sha256") != ref.sha256:
            raise QualificationTrustError(
                "evidence digest does not match immutable manifest"
            )
        if manifest.get("media_type") != ref.media_type:
            raise QualificationTrustError(
                "evidence media type does not match immutable manifest"
            )
        metadata = manifest.get("metadata")
        if (
            type(metadata) is not dict
            or metadata.get("evidence_kind") != ref.evidence_kind
        ):
            raise QualificationTrustError(
                "evidence kind does not match immutable manifest"
            )
        source_refs = manifest.get("source_refs")
        if (
            type(source_refs) is not list
            or f"git:{ref.source_sha}" not in source_refs
        ):
            raise QualificationTrustError(
                "evidence is not bound to the attested source SHA"
            )
        data = store.read_bytes(ref.artifact_id)
    except (FileNotFoundError, ArtifactIntegrityError) as error:
        raise QualificationTrustError(
            "evidence artifact cannot be resolved with integrity"
        ) from error
    if "sha256:" + sha256(data).hexdigest() != ref.sha256:
        raise QualificationTrustError(
            "evidence bytes do not match attested digest"
        )


def verify_qualification_attestation(
    receipt: SignedQualificationAttestation,
    *,
    policy: QualificationTrustPolicy,
    evidence_store: ArtifactStore,
    expected_policy_id: str,
    expected_policy_version: str,
    expected_source_sha: str,
    expected_domain: str,
    expected_gate: str,
    expected_package_id: str,
    expected_protocol_id: str,
    expected_protocol_version: str,
    expected_requirement_id: str,
    expected_release_artifact_id: str | None = None,
    expected_release_artifact_sha256: str | None = None,
) -> AcceptedQualificationAttestation:
    if not isinstance(receipt, SignedQualificationAttestation):
        raise TypeError(
            "receipt must be SignedQualificationAttestation"
        )
    if not isinstance(policy, QualificationTrustPolicy):
        raise TypeError(
            "policy must be QualificationTrustPolicy"
        )
    if not isinstance(evidence_store, ArtifactStore):
        raise TypeError("evidence_store must be ArtifactStore")

    expected_policy_id = _digest(
        expected_policy_id, name="expected_policy_id"
    )
    expected_policy_version = _token(
        expected_policy_version,
        name="expected_policy_version",
    )
    if policy.policy_id != expected_policy_id:
        raise QualificationTrustError(
            "qualification trust policy identity is not the pinned policy"
        )
    if policy.policy_version != expected_policy_version:
        raise QualificationTrustError(
            "qualification trust policy version is stale or mismatched"
        )

    expected_source_sha = _git_sha(
        expected_source_sha, name="expected_source_sha"
    )
    expected_domain = _token(
        expected_domain, name="expected_domain", upper=True
    )
    expected_gate = _token(
        expected_gate, name="expected_gate", upper=True
    )
    expected_package_id = _token(
        expected_package_id, name="expected_package_id", upper=True
    )
    expected_protocol_id = _token(
        expected_protocol_id, name="expected_protocol_id"
    )
    expected_protocol_version = _token(
        expected_protocol_version,
        name="expected_protocol_version",
    )
    expected_requirement_id = _token(
        expected_requirement_id,
        name="expected_requirement_id",
    )
    if (expected_release_artifact_id is None) != (
        expected_release_artifact_sha256 is None
    ):
        raise QualificationTrustError(
            "expected release identity and digest must be supplied together"
        )
    if expected_release_artifact_id is not None:
        expected_release_artifact_id = _uuid(
            expected_release_artifact_id,
            name="expected_release_artifact_id",
        )
        expected_release_artifact_sha256 = _digest(
            expected_release_artifact_sha256,
            name="expected_release_artifact_sha256",
        )

    attestation = receipt.attestation
    if attestation.source_sha != expected_source_sha:
        raise QualificationTrustError(
            "attestation source SHA does not match candidate"
        )
    if (attestation.domain, attestation.gate) != (
        expected_domain,
        expected_gate,
    ):
        raise QualificationTrustError(
            "attestation is not authorized for the requested domain/gate"
        )
    if attestation.package_id != expected_package_id:
        raise QualificationTrustError(
            "attestation package does not match requested package"
        )
    if (
        attestation.protocol_id != expected_protocol_id
        or attestation.protocol_version != expected_protocol_version
    ):
        raise QualificationTrustError(
            "attestation protocol is stale or mismatched"
        )
    if expected_requirement_id not in attestation.requirement_ids:
        raise QualificationTrustError(
            "attestation does not cover the requested requirement"
        )
    if expected_release_artifact_id is not None and (
        attestation.release_artifact_id
        != expected_release_artifact_id
        or attestation.release_artifact_sha256
        != expected_release_artifact_sha256
    ):
        raise QualificationTrustError(
            "attestation is bound to a different release artifact"
        )

    root = policy.root(attestation.trust_root_id)
    if attestation.producer_id != root.producer_id:
        raise QualificationTrustError(
            "attestation producer is not authorized by trust root"
        )
    if attestation.verifier_id != root.verifier_id:
        raise QualificationTrustError(
            "attestation verifier is not authorized by trust root"
        )
    if attestation.verification_method != root.verification_method:
        raise QualificationTrustError(
            "attestation verification method is not authorized"
        )
    if QualificationScope(
        attestation.domain, attestation.gate
    ) not in root.allowed_scopes:
        raise QualificationTrustError(
            "trust root is not authorized for the requested domain/gate"
        )

    signed_at = _instant_value(attestation.signed_at)
    if root.revoked_at is not None:
        raise QualificationTrustError(
            "revoked trust root cannot authorize terminal qualification "
            "without independently authenticated chronology evidence"
        )
    if signed_at < _instant_value(root.valid_from):
        raise QualificationTrustError(
            "attestation predates trust root validity"
        )
    if (
        root.valid_until is not None
        and signed_at >= _instant_value(root.valid_until)
    ):
        raise QualificationTrustError(
            "attestation is outside trust root validity"
        )
    signature = base64.b64decode(
        receipt.signature_b64, validate=True
    )
    _verify_rsa_pkcs1v15_sha256(
        payload=attestation.canonical_bytes(),
        signature=signature,
        root=root,
    )
    for ref in attestation.evidence_refs:
        _resolve_evidence(evidence_store, ref)

    return AcceptedQualificationAttestation(
        attestation_id=attestation.attestation_id,
        attestation_digest=attestation.content_digest,
        policy_id=policy.policy_id,
        policy_version=policy.policy_version,
        trust_root_id=root.root_id,
        result=attestation.result,
        source_sha=attestation.source_sha,
        domain=attestation.domain,
        gate=attestation.gate,
        package_id=attestation.package_id,
        protocol_id=attestation.protocol_id,
        protocol_version=attestation.protocol_version,
        requirement_id=expected_requirement_id,
        release_artifact_id=attestation.release_artifact_id,
        release_artifact_sha256=attestation.release_artifact_sha256,
    )

def verify_canonical_qualification_attestation(
    receipt: SignedQualificationAttestation,
    *,
    evidence_store: ArtifactStore,
    expected_source_sha: str,
    expected_domain: str,
    expected_gate: str,
    expected_package_id: str,
    expected_protocol_id: str,
    expected_protocol_version: str,
    expected_requirement_id: str,
    expected_release_artifact_id: str | None = None,
    expected_release_artifact_sha256: str | None = None,
) -> AcceptedQualificationAttestation:
    """Verify a receipt only against the separately controlled canonical policy.

    Candidate/evidence callers provide no trust policy and no expected pin.
    Policy identity/version are derived only after loading policy bytes from
    the exact expected source commit; mutable working-tree policy bytes are ignored.
    """

    policy = load_canonical_qualification_trust_policy(
        expected_source_sha=expected_source_sha
    )
    return verify_qualification_attestation(
        receipt,
        policy=policy,
        evidence_store=evidence_store,
        expected_policy_id=policy.policy_id,
        expected_policy_version=policy.policy_version,
        expected_source_sha=expected_source_sha,
        expected_domain=expected_domain,
        expected_gate=expected_gate,
        expected_package_id=expected_package_id,
        expected_protocol_id=expected_protocol_id,
        expected_protocol_version=expected_protocol_version,
        expected_requirement_id=expected_requirement_id,
        expected_release_artifact_id=expected_release_artifact_id,
        expected_release_artifact_sha256=expected_release_artifact_sha256,
    )

