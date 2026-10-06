"""Securities-borrow evidence and durable recall projection.

This is not a second risk, reservation, execution, or reconciliation authority.
It supplies typed provider evidence and a journal-backed recall projection for
the existing AutoTrade authorities. Borrow fees remain in financing/accounting.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import json
from threading import RLock
import weakref
from typing import Mapping
from uuid import NAMESPACE_URL, UUID, uuid5

from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_abs,
    exact_add,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
)
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_domain import ProviderDomainError, normalize_provider_environment


_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_AGGREGATE_TYPE = "securities_borrow_recall"
_RECALL_EVENT = "BorrowRecallObserved"
_RESOLUTION_EVENT = "BorrowRecallResolved"
BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE = (
    "application/vnd.autotrade.securities-borrow-evidence+json"
)
BORROW_PROVIDER_EVIDENCE_TYPE = "AUTOTRADE_SECURITIES_BORROW_EVIDENCE"
BORROW_PROVIDER_EVIDENCE_SCHEMA_VERSION = 2


class BorrowEvidenceError(ValueError):
    pass


class BorrowRecallConflict(BorrowEvidenceError):
    pass


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be exact non-empty text")
    return value


def _exact_evidence(evidence: object):
    if type(evidence) not in {
        BorrowAvailabilityEvidence,
        BorrowRecallEvidence,
        BorrowRecallResolutionEvidence,
    }:
        raise TypeError("securities-borrow evidence must use an exact canonical type")
    return replace(evidence)


def _environment(
    value: str,
    _text_fn=_text,
    _environments=_ENVIRONMENTS,
) -> str:
    normalized = _text_fn(value, name="environment").upper()
    if normalized not in _environments:
        raise ValueError("environment must be LIVE, PAPER, REPLAY, or SIMULATION")
    return normalized


def _provider_environment(
    *,
    provider_id: str,
    environment: str,
    provider_environment: str | None,
    _text_fn=_text,
    _environment_fn=_environment,
    _normalize_provider_environment_fn=normalize_provider_environment,
    _provider_domain_error=ProviderDomainError,
) -> str:
    provider = _text_fn(provider_id, name="provider_id").upper()
    runtime = _environment_fn(environment)
    domain = (
        None
        if provider_environment is None
        else _text_fn(provider_environment, name="provider_environment")
    )
    try:
        return _normalize_provider_environment_fn(
            provider_id=provider,
            environment=runtime,
            provider_environment=domain,
        )
    except _provider_domain_error as error:
        raise ValueError(
            "provider_environment is invalid for securities-borrow scope"
        ) from error


def _provider_environment_payload(
    *,
    provider_environment: str,
    environment: str,
) -> dict[str, str]:
    return (
        {}
        if provider_environment == environment
        else {"provider_environment": provider_environment}
    )


def _instrument_id(
    value: str,
    _text_fn=_text,
    _uuid_type=UUID,
) -> str:
    try:
        return str(_uuid_type(_text_fn(value, name="instrument_id")))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("instrument_id must be a UUID") from error


def _version(value: int) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("instrument_version must be an exact positive integer")
    return value


def _exact(operation, *values: Decimal) -> Decimal:
    try:
        return operation(*values)
    except ExactDecimalError as error:
        raise BorrowEvidenceError(
            "securities-borrow arithmetic exceeds exact resource authority"
        ) from error


def _decimal(
    value,
    *,
    name: str,
    positive: bool = False,
    _decimal_type=Decimal,
    _parse_fn=parse_bounded_exact_decimal,
    _exact_error=ExactDecimalError,
) -> Decimal:
    if type(value) not in {_decimal_type, str, int}:
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        result = _parse_fn(value)
    except _exact_error as error:
        raise ValueError(
            f"{name} must be a finite decimal within the exact resource envelope"
        ) from error
    if result < 0 or (positive and result == 0):
        word = "positive" if positive else "non-negative"
        raise ValueError(f"{name} must be a {word} finite decimal")
    return result


def _signed_decimal(
    value,
    *,
    name: str,
    _decimal_type=Decimal,
    _parse_fn=parse_bounded_exact_decimal,
    _exact_error=ExactDecimalError,
) -> Decimal:
    if type(value) not in {_decimal_type, str, int}:
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        return _parse_fn(value)
    except _exact_error as error:
        raise ValueError(
            f"{name} must be a finite decimal within the exact resource envelope"
        ) from error


def incremental_short_borrow_quantity(
    *,
    side: str,
    quantity,
    current_position,
    reserved_position_delta=0,
) -> Decimal:
    """Exact incremental short exposure requiring new borrow reservation."""
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty = _decimal(quantity, name="quantity", positive=True)
    current = _signed_decimal(current_position, name="current_position")
    reserved = _signed_decimal(
        reserved_position_delta,
        name="reserved_position_delta",
    )
    zero = Decimal("0")
    base = _exact(exact_add, current, reserved)
    signed = qty if normalized_side == "BUY" else _exact(exact_subtract, zero, qty)
    resulting = _exact(exact_add, base, signed)
    base_short = _exact(exact_abs, base) if base < 0 else zero
    resulting_short = _exact(exact_abs, resulting) if resulting < 0 else zero
    if resulting_short <= base_short:
        return zero
    return _exact(exact_subtract, resulting_short, base_short)


def _decimal_text(
    value: Decimal,
    _canonical_decimal_text_fn=canonical_decimal_text,
    _exact_error=ExactDecimalError,
    _error_type=BorrowEvidenceError,
) -> str:
    try:
        return _canonical_decimal_text_fn(value)
    except _exact_error as error:
        raise _error_type(
            "securities-borrow decimal exceeds exact rendering authority"
        ) from error


def _instant(
    value: str,
    *,
    name: str,
    _text_fn=_text,
    _datetime_type=datetime,
    _timezone_utc=timezone.utc,
) -> str:
    text = _text_fn(value, name=name)
    try:
        parsed = _datetime_type.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(_timezone_utc).isoformat().replace("+00:00", "Z")


def _dt(
    value: str,
    _datetime_type=datetime,
    _timezone_utc=timezone.utc,
) -> datetime:
    return _datetime_type.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(_timezone_utc)


def _immutable_evidence_ref(value: object) -> tuple[str, str, str]:
    if type(value) is not str or not value or value != value.strip():
        raise BorrowEvidenceError(
            "provider borrow evidence requires exact canonical artifact reference"
        )
    reference = value
    marker = "@sha256:"
    if not reference.startswith("artifact:") or marker not in reference:
        raise BorrowEvidenceError(
            "borrow evidence_ref must bind artifact UUID and SHA-256 digest"
        )
    artifact_id, digest = reference[len("artifact:"):].split(marker, 1)
    try:
        artifact_id = str(UUID(artifact_id))
    except (ValueError, TypeError, AttributeError) as error:
        raise BorrowEvidenceError(
            "borrow evidence artifact identity must be a UUID"
        ) from error
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise BorrowEvidenceError(
            "borrow evidence_ref must use canonical lowercase SHA-256"
        )
    canonical = f"artifact:{artifact_id}@sha256:{digest}"
    if reference != canonical:
        raise BorrowEvidenceError("borrow evidence_ref must be canonical")
    return artifact_id, digest, canonical


def _provider_evidence_kind(evidence: object) -> str:
    if type(evidence) is BorrowAvailabilityEvidence:
        return "AVAILABILITY"
    if type(evidence) is BorrowRecallEvidence:
        return "RECALL"
    if type(evidence) is BorrowRecallResolutionEvidence:
        return "RECALL_RESOLUTION"
    raise TypeError("unsupported securities-borrow evidence type")


def provider_borrow_evidence_receipt(evidence: object) -> dict[str, object]:
    evidence = _exact_evidence(evidence)
    kind = _provider_evidence_kind(evidence)
    if type(evidence) is BorrowAvailabilityEvidence:
        observation = BorrowAvailabilityEvidence.resource_detail(evidence)
    elif type(evidence) is BorrowRecallEvidence:
        observation = BorrowRecallEvidence.payload(evidence)
    else:
        observation = BorrowRecallResolutionEvidence.payload(evidence)
    observation = dict(observation)
    observation.pop("evidence_ref", None)
    return {
        "schema_version": BORROW_PROVIDER_EVIDENCE_SCHEMA_VERSION,
        "evidence_type": BORROW_PROVIDER_EVIDENCE_TYPE,
        "observation_kind": kind,
        "observation": observation,
    }


def provider_borrow_evidence_metadata(evidence: object) -> dict[str, object]:
    evidence = _exact_evidence(evidence)
    kind = _provider_evidence_kind(evidence)
    metadata: dict[str, object] = {
        "evidence_type": BORROW_PROVIDER_EVIDENCE_TYPE,
        "observation_kind": kind,
        "provider_id": evidence.provider_id,
        "account_id": evidence.account_id,
        "environment": evidence.environment,
        **_provider_environment_payload(
            provider_environment=evidence.provider_environment,
            environment=evidence.environment,
        ),
        "instrument_id": evidence.instrument_id,
        "instrument_version": evidence.instrument_version,
        "quantity_unit": evidence.quantity_unit,
        "provider_revision": evidence.provider_revision,
    }
    if type(evidence) is BorrowAvailabilityEvidence:
        metadata["locate_id"] = evidence.locate_id
    elif type(evidence) is BorrowRecallEvidence:
        metadata["recall_id"] = evidence.recall_id
    else:
        metadata["recall_id"] = evidence.recall_id
        metadata["resolution_id"] = evidence.resolution_id
    return metadata


def _build_provider_borrow_evidence_verifier():
    """Freeze the provider-evidence interpretation boundary against late retargets."""

    availability_type = BorrowAvailabilityEvidence
    recall_type = BorrowRecallEvidence
    resolution_type = BorrowRecallResolutionEvidence
    artifact_store_type = ArtifactStore
    artifact_integrity_error = ArtifactIntegrityError
    availability_detail = BorrowAvailabilityEvidence.resource_detail
    recall_payload = BorrowRecallEvidence.payload
    resolution_payload = BorrowRecallResolutionEvidence.payload
    immutable_evidence_ref = _immutable_evidence_ref
    json_loads = strict_json_loads
    canonical_renderer = canonical_json
    evidence_media_type = BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE
    evidence_type = BORROW_PROVIDER_EVIDENCE_TYPE
    schema_version = BORROW_PROVIDER_EVIDENCE_SCHEMA_VERSION
    replace_evidence = replace

    def verify(
        evidence: object,
        artifact_store: ArtifactStore,
    ) -> str:
        evidence_type_obj = type(evidence)
        if evidence_type_obj not in {
            availability_type,
            recall_type,
            resolution_type,
        }:
            raise TypeError(
                "securities-borrow evidence must use an exact canonical type"
            )
        evidence = replace_evidence(evidence)
        if type(artifact_store) is not artifact_store_type:
            raise BorrowEvidenceError(
                "provider borrow evidence requires the exact canonical ArtifactStore"
            )

        if evidence_type_obj is availability_type:
            kind = "AVAILABILITY"
            observation = availability_detail(evidence)
        elif evidence_type_obj is recall_type:
            kind = "RECALL"
            observation = recall_payload(evidence)
        else:
            kind = "RECALL_RESOLUTION"
            observation = resolution_payload(evidence)
        observation = dict(observation)
        observation.pop("evidence_ref", None)

        expected_receipt = {
            "schema_version": schema_version,
            "evidence_type": evidence_type,
            "observation_kind": kind,
            "observation": observation,
        }
        expected_metadata: dict[str, object] = {
            "evidence_type": evidence_type,
            "observation_kind": kind,
            "provider_id": evidence.provider_id,
            "account_id": evidence.account_id,
            "environment": evidence.environment,
            **(
                {}
                if evidence.provider_environment == evidence.environment
                else {"provider_environment": evidence.provider_environment}
            ),
            "instrument_id": evidence.instrument_id,
            "instrument_version": evidence.instrument_version,
            "quantity_unit": evidence.quantity_unit,
            "provider_revision": evidence.provider_revision,
        }
        if evidence_type_obj is availability_type:
            expected_metadata["locate_id"] = evidence.locate_id
        elif evidence_type_obj is recall_type:
            expected_metadata["recall_id"] = evidence.recall_id
        else:
            expected_metadata["recall_id"] = evidence.recall_id
            expected_metadata["resolution_id"] = evidence.resolution_id

        artifact_id, digest, canonical_ref = immutable_evidence_ref(
            evidence.evidence_ref
        )
        try:
            manifest, raw = artifact_store_type.read_authenticated_snapshot(
                artifact_store,
                artifact_id,
            )
            if type(manifest) is not dict or not isinstance(raw, bytes):
                raise artifact_integrity_error(
                    "borrow evidence snapshot has unsupported representation"
                )
            if manifest.get("artifact_id") != artifact_id:
                raise artifact_integrity_error(
                    "borrow evidence artifact identity mismatch"
                )
            manifest_hash = manifest.get("manifest_hash")
            if (
                not isinstance(manifest_hash, str)
                or not manifest_hash.startswith("sha256:")
                or len(manifest_hash) != 71
                or any(
                    ch not in "0123456789abcdef"
                    for ch in manifest_hash[7:]
                )
            ):
                raise artifact_integrity_error(
                    "borrow evidence manifest lacks integrity binding"
                )
            if manifest.get("sha256") != f"sha256:{digest}":
                raise artifact_integrity_error(
                    "borrow evidence digest does not match manifest"
                )
            if manifest.get("media_type") != evidence_media_type:
                raise artifact_integrity_error(
                    "borrow evidence has unsupported media type"
                )
            if manifest.get("metadata") != expected_metadata:
                raise artifact_integrity_error(
                    "borrow evidence metadata differs from financial scope"
                )
            rights = manifest.get("rights")
            if not isinstance(rights, dict) or rights.get("storage") is not True:
                raise artifact_integrity_error(
                    "borrow evidence lacks storage provenance"
                )
            parsed = json_loads(raw.decode("utf-8"))
        except (
            artifact_integrity_error,
            FileNotFoundError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
        ) as error:
            raise BorrowEvidenceError(
                "provider borrow evidence verification failed"
            ) from error
        if parsed != expected_receipt:
            raise BorrowEvidenceError(
                "provider borrow evidence does not match supplied economics"
            )
        if raw != canonical_renderer(expected_receipt).encode("utf-8"):
            raise BorrowEvidenceError(
                "provider borrow evidence must use canonical JSON bytes"
            )
        return canonical_ref

    return verify


verify_provider_borrow_evidence = _build_provider_borrow_evidence_verifier()
del _build_provider_borrow_evidence_verifier

def _borrow_resource_key_from_identity(identity: list[object]) -> str:
    canonical = json.dumps(identity, ensure_ascii=True, separators=(",", ":"))
    return "BORROW:" + str(
        uuid5(
            NAMESPACE_URL,
            "https://resources.autotrade.local/securities-borrow/" + canonical,
        )
    )


def _legacy_borrow_resource_key(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    instrument_id: str,
    instrument_version: int,
) -> str:
    return _borrow_resource_key_from_identity(
        [
            _text(provider_id, name="provider_id").upper(),
            _text(account_id, name="account_id"),
            _environment(environment),
            _instrument_id(instrument_id),
            _version(instrument_version),
        ]
    )


def borrow_resource_key(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    instrument_id: str,
    instrument_version: int,
    provider_environment: str | None = None,
) -> str:
    """Canonical borrow resource including exact provider financial domain."""
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    runtime = _environment(environment)
    domain = _provider_environment(
        provider_id=provider,
        environment=runtime,
        provider_environment=provider_environment,
    )
    identity: list[object] = [provider, account, runtime]
    if domain != runtime:
        identity.append(domain)
    identity.extend([_instrument_id(instrument_id), _version(instrument_version)])
    return _borrow_resource_key_from_identity(identity)


def _borrow_recall_aggregate_id(resource_key: str) -> str:
    return "borrow-recall:" + str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/borrow-recall/" + resource_key,
        )
    )


@dataclass(frozen=True)
class BorrowAvailabilityEvidence:
    """Immutable provider proof of TOTAL approved borrow capacity.

    capacity_quantity includes inventory already borrowed. Adapters that know
    only an ambiguous "available" number must fail closed rather than emit this
    evidence. indicative_rate is informational and is never an economic posting.
    """

    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    locate_id: str
    provider_revision: str
    capacity_quantity: Decimal
    quantity_unit: str
    hard_to_borrow: bool
    observed_at: str
    effective_at: str
    expires_at: str
    evidence_ref: str
    indicative_rate: Decimal | None = None
    provider_environment: str | None = None

    def __post_init__(
        self,
        _text_fn=_text,
        _environment_fn=_environment,
        _provider_environment_fn=_provider_environment,
        _instrument_id_fn=_instrument_id,
        _version_fn=_version,
        _decimal_fn=_decimal,
        _instant_fn=_instant,
        _dt_fn=_dt,
    ) -> None:
        object.__setattr__(self, "provider_id", _text_fn(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text_fn(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment_fn(self.environment))
        object.__setattr__(
            self,
            "provider_environment",
            _provider_environment_fn(
                provider_id=self.provider_id,
                environment=self.environment,
                provider_environment=self.provider_environment,
            ),
        )
        object.__setattr__(self, "instrument_id", _instrument_id_fn(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version_fn(self.instrument_version))
        object.__setattr__(self, "locate_id", _text_fn(self.locate_id, name="locate_id"))
        object.__setattr__(self, "provider_revision", _text_fn(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "capacity_quantity", _decimal_fn(self.capacity_quantity, name="capacity_quantity"))
        object.__setattr__(self, "quantity_unit", _text_fn(self.quantity_unit, name="quantity_unit"))
        if not isinstance(self.hard_to_borrow, bool):
            raise TypeError("hard_to_borrow must be boolean")
        observed = _instant_fn(self.observed_at, name="observed_at")
        effective = _instant_fn(self.effective_at, name="effective_at")
        expires = _instant_fn(self.expires_at, name="expires_at")
        if _dt_fn(effective) > _dt_fn(observed):
            raise ValueError("effective_at must not be after observed_at")
        if _dt_fn(expires) <= _dt_fn(observed):
            raise ValueError("expires_at must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "expires_at", expires)
        object.__setattr__(self, "evidence_ref", _text_fn(self.evidence_ref, name="evidence_ref"))
        if self.indicative_rate is not None:
            object.__setattr__(
                self,
                "indicative_rate",
                _decimal_fn(self.indicative_rate, name="indicative_rate"),
            )

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def resource_detail(
        self,
        _provider_environment_payload_fn=_provider_environment_payload,
        _decimal_text_fn=_decimal_text,
    ) -> dict[str, str]:
        return {
            "resource_type": "SECURITIES_BORROW",
            "capacity_semantics": "TOTAL_APPROVED_CAPACITY",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            **_provider_environment_payload_fn(
                provider_environment=self.provider_environment,
                environment=self.environment,
            ),
            "instrument_id": self.instrument_id,
            "instrument_version": str(self.instrument_version),
            "locate_id": self.locate_id,
            "provider_revision": self.provider_revision,
            "capacity_quantity": _decimal_text_fn(self.capacity_quantity),
            "quantity_unit": self.quantity_unit,
            "hard_to_borrow": "true" if self.hard_to_borrow else "false",
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "expires_at": self.expires_at,
            "evidence_ref": self.evidence_ref,
            "indicative_rate": (
                "" if self.indicative_rate is None else _decimal_text_fn(self.indicative_rate)
            ),
        }

    @classmethod
    def from_resource_detail(cls, detail: Mapping[str, object]) -> "BorrowAvailabilityEvidence":
        if not isinstance(detail, Mapping):
            raise TypeError("borrow resource detail must be a mapping")
        if detail.get("resource_type") != "SECURITIES_BORROW":
            raise ValueError("borrow resource detail has invalid resource_type")
        if detail.get("capacity_semantics") != "TOTAL_APPROVED_CAPACITY":
            raise ValueError("borrow evidence must prove TOTAL_APPROVED_CAPACITY")
        hard = detail.get("hard_to_borrow")
        if hard not in {"true", "false"}:
            raise ValueError("hard_to_borrow must use canonical true/false text")
        try:
            version = int(detail.get("instrument_version"))
        except (TypeError, ValueError) as error:
            raise ValueError("instrument_version must be a positive integer") from error
        raw_rate = detail.get("indicative_rate")
        return cls(
            provider_id=detail.get("provider_id"),
            account_id=detail.get("account_id"),
            environment=detail.get("environment"),
            provider_environment=detail.get("provider_environment"),
            instrument_id=detail.get("instrument_id"),
            instrument_version=version,
            locate_id=detail.get("locate_id"),
            provider_revision=detail.get("provider_revision"),
            capacity_quantity=detail.get("capacity_quantity"),
            quantity_unit=detail.get("quantity_unit"),
            hard_to_borrow=(hard == "true"),
            observed_at=detail.get("observed_at"),
            effective_at=detail.get("effective_at"),
            expires_at=detail.get("expires_at"),
            evidence_ref=detail.get("evidence_ref"),
            indicative_rate=(None if raw_rate in {None, ""} else raw_rate),
        )


@dataclass(frozen=True)
class BorrowRecallEvidence:
    recall_id: str
    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    provider_revision: str
    quantity: Decimal
    quantity_unit: str
    observed_at: str
    effective_at: str
    evidence_ref: str
    deadline: str | None = None
    provider_environment: str | None = None

    def __post_init__(
        self,
        _text_fn=_text,
        _environment_fn=_environment,
        _provider_environment_fn=_provider_environment,
        _instrument_id_fn=_instrument_id,
        _version_fn=_version,
        _decimal_fn=_decimal,
        _instant_fn=_instant,
        _dt_fn=_dt,
    ) -> None:
        object.__setattr__(self, "recall_id", _text_fn(self.recall_id, name="recall_id"))
        object.__setattr__(self, "provider_id", _text_fn(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text_fn(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment_fn(self.environment))
        object.__setattr__(
            self,
            "provider_environment",
            _provider_environment_fn(
                provider_id=self.provider_id,
                environment=self.environment,
                provider_environment=self.provider_environment,
            ),
        )
        object.__setattr__(self, "instrument_id", _instrument_id_fn(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version_fn(self.instrument_version))
        object.__setattr__(self, "provider_revision", _text_fn(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "quantity", _decimal_fn(self.quantity, name="quantity", positive=True))
        object.__setattr__(self, "quantity_unit", _text_fn(self.quantity_unit, name="quantity_unit"))
        observed = _instant_fn(self.observed_at, name="observed_at")
        effective = _instant_fn(self.effective_at, name="effective_at")
        if _dt_fn(effective) > _dt_fn(observed):
            raise ValueError("recall effective_at must not be after observed_at")
        deadline = None if self.deadline is None else _instant_fn(self.deadline, name="deadline")
        if deadline is not None and _dt_fn(deadline) < _dt_fn(effective):
            raise ValueError("recall deadline must not precede effective_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "deadline", deadline)
        object.__setattr__(self, "evidence_ref", _text_fn(self.evidence_ref, name="evidence_ref"))

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def payload(
        self,
        _provider_environment_payload_fn=_provider_environment_payload,
        _decimal_text_fn=_decimal_text,
    ) -> dict[str, object]:
        return {
            "recall_id": self.recall_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            **_provider_environment_payload_fn(
                provider_environment=self.provider_environment,
                environment=self.environment,
            ),
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "provider_revision": self.provider_revision,
            "quantity": _decimal_text_fn(self.quantity),
            "quantity_unit": self.quantity_unit,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "evidence_ref": self.evidence_ref,
            "deadline": self.deadline,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BorrowRecallEvidence":
        return cls(**dict(payload))


@dataclass(frozen=True)
class BorrowRecallResolutionEvidence:
    resolution_id: str
    recall_id: str
    provider_id: str
    account_id: str
    environment: str
    instrument_id: str
    instrument_version: int
    provider_revision: str
    resolved_quantity: Decimal
    quantity_unit: str
    observed_at: str
    effective_at: str
    evidence_ref: str
    provider_environment: str | None = None

    def __post_init__(
        self,
        _text_fn=_text,
        _environment_fn=_environment,
        _provider_environment_fn=_provider_environment,
        _instrument_id_fn=_instrument_id,
        _version_fn=_version,
        _decimal_fn=_decimal,
        _instant_fn=_instant,
        _dt_fn=_dt,
    ) -> None:
        object.__setattr__(self, "resolution_id", _text_fn(self.resolution_id, name="resolution_id"))
        object.__setattr__(self, "recall_id", _text_fn(self.recall_id, name="recall_id"))
        object.__setattr__(self, "provider_id", _text_fn(self.provider_id, name="provider_id").upper())
        object.__setattr__(self, "account_id", _text_fn(self.account_id, name="account_id"))
        object.__setattr__(self, "environment", _environment_fn(self.environment))
        object.__setattr__(
            self,
            "provider_environment",
            _provider_environment_fn(
                provider_id=self.provider_id,
                environment=self.environment,
                provider_environment=self.provider_environment,
            ),
        )
        object.__setattr__(self, "instrument_id", _instrument_id_fn(self.instrument_id))
        object.__setattr__(self, "instrument_version", _version_fn(self.instrument_version))
        object.__setattr__(self, "provider_revision", _text_fn(self.provider_revision, name="provider_revision"))
        object.__setattr__(self, "resolved_quantity", _decimal_fn(self.resolved_quantity, name="resolved_quantity", positive=True))
        object.__setattr__(self, "quantity_unit", _text_fn(self.quantity_unit, name="quantity_unit"))
        observed = _instant_fn(self.observed_at, name="observed_at")
        effective = _instant_fn(self.effective_at, name="effective_at")
        if _dt_fn(effective) > _dt_fn(observed):
            raise ValueError("resolution effective_at must not be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "evidence_ref", _text_fn(self.evidence_ref, name="evidence_ref"))

    @property
    def resource_key(self) -> str:
        return borrow_resource_key(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
            instrument_id=self.instrument_id,
            instrument_version=self.instrument_version,
        )

    def payload(
        self,
        _provider_environment_payload_fn=_provider_environment_payload,
        _decimal_text_fn=_decimal_text,
    ) -> dict[str, object]:
        return {
            "resolution_id": self.resolution_id,
            "recall_id": self.recall_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            **_provider_environment_payload_fn(
                provider_environment=self.provider_environment,
                environment=self.environment,
            ),
            "instrument_id": self.instrument_id,
            "instrument_version": self.instrument_version,
            "provider_revision": self.provider_revision,
            "resolved_quantity": _decimal_text_fn(self.resolved_quantity),
            "quantity_unit": self.quantity_unit,
            "observed_at": self.observed_at,
            "effective_at": self.effective_at,
            "evidence_ref": self.evidence_ref,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> "BorrowRecallResolutionEvidence":
        return cls(**dict(payload))


@dataclass(frozen=True)
class _BorrowProjectionBinding:
    store_ref: weakref.ReferenceType
    store_identity: object
    evidence_artifact_store_ref: weakref.ReferenceType
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    instrument_id: str
    instrument_version: int
    quantity_unit: str
    resource_key: str
    aggregate_id: str


def _build_borrow_projection_binding_accessors():
    """Retain one immutable borrow projection composition outside caller state."""

    bindings: dict[int, tuple[weakref.ReferenceType, _BorrowProjectionBinding]] = {}
    lock = RLock()

    def registered(value: object) -> _BorrowProjectionBinding | None:
        object_id = id(value)
        with lock:
            entry = bindings.get(object_id)
            if entry is None:
                return None
            value_ref, binding = entry
            current = value_ref()
            if current is value:
                return binding
            if current is None:
                bindings.pop(object_id, None)
                return None
            raise BorrowRecallConflict("borrow projection binding identity collision")

    def is_registered(value: object) -> bool:
        return registered(value) is not None

    def initialize(
        value: object,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        quantity_unit: str,
        evidence_artifact_store: ArtifactStore,
        provider_environment: str | None = None,
    ) -> None:
        if type(value) is not DurableBorrowRecallProjection:
            raise TypeError(
                "borrow recall projection must be exact DurableBorrowRecallProjection"
            )
        if registered(value) is not None:
            raise BorrowRecallConflict(
                "borrow projection authority is already established"
            )
        try:
            store_identity = require_exact_journal_store_authority(
                store,
                subject="securities-borrow JournalStore",
            )
        except (TypeError, RuntimeError) as error:
            raise BorrowRecallConflict(
                "securities-borrow JournalStore authority is invalid"
            ) from error
        if type(evidence_artifact_store) is not ArtifactStore:
            raise TypeError(
                "evidence_artifact_store must be the exact canonical ArtifactStore"
            )

        normalized_provider = _text(provider_id, name="provider_id").upper()
        normalized_account = _text(account_id, name="account_id")
        normalized_environment = _environment(environment)
        normalized_provider_environment = _provider_environment(
            provider_id=normalized_provider,
            environment=normalized_environment,
            provider_environment=provider_environment,
        )
        normalized_instrument = _instrument_id(instrument_id)
        normalized_version = _version(instrument_version)
        normalized_quantity_unit = _text(quantity_unit, name="quantity_unit")
        resource_key = borrow_resource_key(
            provider_id=normalized_provider,
            account_id=normalized_account,
            environment=normalized_environment,
            provider_environment=normalized_provider_environment,
            instrument_id=normalized_instrument,
            instrument_version=normalized_version,
        )
        aggregate_id = _borrow_recall_aggregate_id(resource_key)
        if normalized_provider_environment != normalized_environment:
            legacy_resource_key = _legacy_borrow_resource_key(
                provider_id=normalized_provider,
                account_id=normalized_account,
                environment=normalized_environment,
                instrument_id=normalized_instrument,
                instrument_version=normalized_version,
            )
            legacy_aggregate_id = _borrow_recall_aggregate_id(legacy_resource_key)
            with journal_store_authority_scope(store, store_identity):
                legacy_events = JournalStore.load_events(
                    store,
                    _AGGREGATE_TYPE,
                    legacy_aggregate_id,
                )
            if legacy_events:
                raise BorrowRecallConflict(
                    "legacy runtime-only borrow recall history is ambiguous across provider environments"
                )
        for name, item in {
            "store": store,
            "evidence_artifact_store": evidence_artifact_store,
            "provider_id": normalized_provider,
            "account_id": normalized_account,
            "environment": normalized_environment,
            "provider_environment": normalized_provider_environment,
            "instrument_id": normalized_instrument,
            "instrument_version": normalized_version,
            "quantity_unit": normalized_quantity_unit,
            "resource_key": resource_key,
            "aggregate_id": aggregate_id,
            "_recalls": {},
            "_resolved": {},
            "_resolutions": {},
        }.items():
            object.__setattr__(value, name, item)

        object_id = id(value)
        # Callback-free weakrefs are deliberate: a discoverable weakref callback
        # would be a caller-invokable eraser for a live financial trust binding.
        # The live projection itself owns the strong store/artifact references;
        # this registry must not extend either resource lifetime.
        value_ref = weakref.ref(value)
        binding = _BorrowProjectionBinding(
            store_ref=weakref.ref(store),
            store_identity=store_identity,
            evidence_artifact_store_ref=weakref.ref(evidence_artifact_store),
            provider_id=normalized_provider,
            account_id=normalized_account,
            environment=normalized_environment,
            provider_environment=normalized_provider_environment,
            instrument_id=normalized_instrument,
            instrument_version=normalized_version,
            quantity_unit=normalized_quantity_unit,
            resource_key=resource_key,
            aggregate_id=aggregate_id,
        )
        with lock:
            entry = bindings.get(object_id)
            if entry is not None and entry[0]() is not value:
                raise BorrowRecallConflict(
                    "borrow projection binding identity collision"
                )
            bindings[object_id] = (value_ref, binding)
        try:
            DurableBorrowRecallProjection._reload(value)
        except Exception:
            object_id = id(value)
            with lock:
                entry = bindings.get(object_id)
                if entry is not None and entry[0]() is value:
                    bindings.pop(object_id, None)
            raise

    def require(value: object) -> _BorrowProjectionBinding:
        if type(value) is not DurableBorrowRecallProjection:
            raise TypeError(
                "borrow recall projection must be exact DurableBorrowRecallProjection"
            )
        binding = registered(value)
        if binding is None:
            raise BorrowRecallConflict(
                "borrow projection authority is not established"
            )
        state = object.__getattribute__(value, "__dict__")
        if any(type(name) is not str for name in state):
            raise BorrowRecallConflict(
                "borrow projection instance state keys must be exact str"
            )
        class_owned = {
            name
            for base in DurableBorrowRecallProjection.__mro__
            for name in base.__dict__
        }
        if class_owned.intersection(state):
            raise BorrowRecallConflict(
                "borrow projection instance state shadows authority methods"
            )
        store = binding.store_ref()
        evidence_artifact_store = binding.evidence_artifact_store_ref()
        if store is None or evidence_artifact_store is None:
            raise BorrowRecallConflict(
                "borrow projection authority resource was released while projection is live"
            )
        if (
            state.get("store") is not store
            or state.get("evidence_artifact_store") is not evidence_artifact_store
        ):
            raise BorrowRecallConflict(
                "borrow projection authority object changed after construction"
            )
        expected_scalars = {
            "provider_id": binding.provider_id,
            "account_id": binding.account_id,
            "environment": binding.environment,
            "provider_environment": binding.provider_environment,
            "instrument_id": binding.instrument_id,
            "instrument_version": binding.instrument_version,
            "quantity_unit": binding.quantity_unit,
            "resource_key": binding.resource_key,
            "aggregate_id": binding.aggregate_id,
        }
        for name, item in expected_scalars.items():
            actual = state.get(name)
            if type(actual) is not type(item) or actual != item:
                raise BorrowRecallConflict(
                    "borrow projection authority state changed after construction"
                )
        try:
            current_identity = require_exact_journal_store_authority(
                store,
                subject="securities-borrow JournalStore",
            )
        except (TypeError, RuntimeError) as error:
            raise BorrowRecallConflict(
                "securities-borrow JournalStore authority changed"
            ) from error
        if current_identity != binding.store_identity:
            raise BorrowRecallConflict(
                "securities-borrow JournalStore generation changed"
            )
        return binding

    return is_registered, initialize, require


(
    _borrow_projection_binding_registered,
    _initialize_borrow_projection_binding,
    _require_borrow_projection_binding,
) = _build_borrow_projection_binding_accessors()
del _build_borrow_projection_binding_accessors


def _borrow_store_load_events(value: object) -> list[dict[str, object]]:
    binding = _require_borrow_projection_binding(value)
    store = binding.store_ref()
    if store is None:
        raise BorrowRecallConflict("securities-borrow JournalStore was released")
    with journal_store_authority_scope(store, binding.store_identity):
        return JournalStore.load_events(
            store,
            _AGGREGATE_TYPE,
            binding.aggregate_id,
        )


def _borrow_store_get_event(
    value: object,
    event_id: str,
) -> dict[str, object] | None:
    binding = _require_borrow_projection_binding(value)
    store = binding.store_ref()
    if store is None:
        raise BorrowRecallConflict("securities-borrow JournalStore was released")
    with journal_store_authority_scope(store, binding.store_identity):
        return JournalStore.get_event(store, event_id)


def _borrow_store_append_event(
    value: object,
    envelope: Mapping[str, object],
) -> None:
    binding = _require_borrow_projection_binding(value)
    store = binding.store_ref()
    if store is None:
        raise BorrowRecallConflict("securities-borrow JournalStore was released")
    with journal_store_authority_scope(store, binding.store_identity):
        JournalStore.append_event(store, envelope)


class DurableBorrowRecallProjection:
    """Append-only provider recall state for one canonical borrow resource."""

    _BOUND_AUTHORITY_STATE = frozenset(
        {
            "store",
            "evidence_artifact_store",
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "instrument_id",
            "instrument_version",
            "quantity_unit",
            "resource_key",
            "aggregate_id",
            "_recalls",
            "_resolved",
            "_resolutions",
        }
    )

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
        instrument_id: str,
        instrument_version: int,
        quantity_unit: str,
        evidence_artifact_store: ArtifactStore,
        provider_environment: str | None = None,
    ):
        _initialize_borrow_projection_binding(
            self,
            store,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
            instrument_id=instrument_id,
            instrument_version=instrument_version,
            quantity_unit=quantity_unit,
            evidence_artifact_store=evidence_artifact_store,
        )

    def __getattribute__(self, name: str):
        if (
            type(name) is str
            and name != "__dict__"
            and _borrow_projection_binding_registered(self)
        ):
            _require_borrow_projection_binding(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        if _borrow_projection_binding_registered(self):
            class_owned = any(
                name in base.__dict__
                for base in DurableBorrowRecallProjection.__mro__
            )
            if name in DurableBorrowRecallProjection._BOUND_AUTHORITY_STATE or class_owned:
                raise BorrowRecallConflict(
                    "borrow projection authority state is immutable"
                )
        object.__setattr__(self, name, value)

    def _scope_matches(self, evidence) -> bool:
        return (
            evidence.provider_id == self.provider_id
            and evidence.account_id == self.account_id
            and evidence.environment == self.environment
            and evidence.provider_environment == self.provider_environment
            and evidence.instrument_id == self.instrument_id
            and evidence.instrument_version == self.instrument_version
            and evidence.quantity_unit == self.quantity_unit
            and evidence.resource_key == self.resource_key
        )

    def _events(self) -> list[dict[str, object]]:
        return _borrow_store_load_events(self)

    def _reload(self) -> None:
        recalls: dict[str, BorrowRecallEvidence] = {}
        resolved: dict[str, Decimal] = {}
        resolutions: dict[str, BorrowRecallResolutionEvidence] = {}
        expected = 1
        for event in self._events():
            if int(event["aggregate_version"]) != expected:
                raise BorrowRecallConflict("borrow recall aggregate versions are not contiguous")
            expected += 1
            payload = event.get("payload")
            if not isinstance(payload, Mapping) or payload_digest(payload) != event.get("payload_hash"):
                raise BorrowRecallConflict("borrow recall event payload is invalid")
            raw = payload.get("evidence")
            if not isinstance(raw, Mapping):
                raise BorrowRecallConflict("borrow recall evidence is missing")
            if event.get("event_type") == _RECALL_EVENT:
                evidence = BorrowRecallEvidence.from_payload(raw)
                verify_provider_borrow_evidence(
                    evidence,
                    self.evidence_artifact_store,
                )
                if not self._scope_matches(evidence):
                    raise BorrowRecallConflict("borrow recall evidence scope mismatch")
                if evidence.recall_id in recalls:
                    raise BorrowRecallConflict("duplicate borrow recall identity in journal")
                recalls[evidence.recall_id] = evidence
                resolved[evidence.recall_id] = Decimal("0")
            elif event.get("event_type") == _RESOLUTION_EVENT:
                evidence = BorrowRecallResolutionEvidence.from_payload(raw)
                verify_provider_borrow_evidence(
                    evidence,
                    self.evidence_artifact_store,
                )
                if not self._scope_matches(evidence):
                    raise BorrowRecallConflict("borrow recall resolution scope mismatch")
                if evidence.resolution_id in resolutions:
                    raise BorrowRecallConflict("duplicate borrow recall resolution identity")
                recall = recalls.get(evidence.recall_id)
                if recall is None:
                    raise BorrowRecallConflict("resolution references unknown recall")
                if _dt(evidence.effective_at) < _dt(recall.effective_at):
                    raise BorrowRecallConflict("resolution predates recall")
                after = _exact(exact_add, resolved[evidence.recall_id], evidence.resolved_quantity)
                if after > recall.quantity:
                    raise BorrowRecallConflict("resolution exceeds recalled quantity")
                resolved[evidence.recall_id] = after
                resolutions[evidence.resolution_id] = evidence
            else:
                raise BorrowRecallConflict("unsupported borrow recall event type")
        object.__setattr__(self, "_recalls", recalls)
        object.__setattr__(self, "_resolved", resolved)
        object.__setattr__(self, "_resolutions", resolutions)

    @property
    def version(self) -> int:
        return len(self._events())

    def _remaining_from_current_cut(self, recall_id: str) -> Decimal:
        recall = self._recalls.get(recall_id)
        if recall is None:
            raise KeyError(recall_id)
        return _exact(
            exact_subtract,
            recall.quantity,
            self._resolved.get(recall_id, Decimal("0")),
        )

    def _remaining_at_current_cut(
        self,
        recall_id: str,
        point: datetime,
    ) -> Decimal:
        recall = self._recalls.get(recall_id)
        if recall is None:
            raise KeyError(recall_id)
        if _dt(recall.effective_at) > point or _dt(recall.observed_at) > point:
            return Decimal("0")
        resolved = _exact(
            exact_sum,
            (
                evidence.resolved_quantity
                for evidence in self._resolutions.values()
                if evidence.recall_id == recall_id
                and _dt(evidence.effective_at) <= point
                and _dt(evidence.observed_at) <= point
            ),
        )
        if resolved > recall.quantity:
            raise BorrowRecallConflict(
                "decision-cut resolution exceeds recalled quantity"
            )
        return _exact(exact_subtract, recall.quantity, resolved)

    def remaining(self, recall_id: str) -> Decimal:
        DurableBorrowRecallProjection._reload(self)
        rid = _text(recall_id, name="recall_id")
        return self._remaining_from_current_cut(rid)

    def remaining_at(self, recall_id: str, now: str) -> Decimal:
        """Return only recall truth causally visible at one financial cut."""

        DurableBorrowRecallProjection._reload(self)
        rid = _text(recall_id, name="recall_id")
        point = _dt(_instant(now, name="now"))
        return self._remaining_at_current_cut(rid, point)

    @property
    def active_quantity(self) -> Decimal:
        DurableBorrowRecallProjection._reload(self)
        return _exact(
            exact_sum,
            (
                self._remaining_from_current_cut(rid)
                for rid in self._recalls
            ),
        )

    def active_quantity_at(self, now: str) -> Decimal:
        DurableBorrowRecallProjection._reload(self)
        point = _dt(_instant(now, name="now"))
        return _exact(
            exact_sum,
            (
                self._remaining_at_current_cut(rid, point)
                for rid in self._recalls
            ),
        )

    @property
    def active_recall_ids(self) -> tuple[str, ...]:
        DurableBorrowRecallProjection._reload(self)
        return tuple(
            sorted(
                rid
                for rid in self._recalls
                if self._remaining_from_current_cut(rid) > 0
            )
        )

    def active_recall_ids_at(self, now: str) -> tuple[str, ...]:
        DurableBorrowRecallProjection._reload(self)
        point = _dt(_instant(now, name="now"))
        return tuple(
            sorted(
                rid
                for rid in self._recalls
                if self._remaining_at_current_cut(rid, point) > 0
            )
        )

    @property
    def active_blocking_resources(self) -> tuple[str, ...]:
        return (self.resource_key,) if self.active_quantity > 0 else ()

    def active_blocking_resources_at(self, now: str) -> tuple[str, ...]:
        return (self.resource_key,) if self.active_quantity_at(now) > 0 else ()

    def _append(self, *, event_type: str, identity: str, payload: dict[str, object], committed_at: str) -> None:
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/borrow-recall-event/"
                + self.aggregate_id + "/" + event_type + "/" + identity,
            )
        )
        existing = _borrow_store_get_event(self, event_id)
        if existing is not None:
            if existing.get("event_type") == event_type and existing.get("payload") == payload:
                self._reload()
                return
            raise BorrowRecallConflict("borrow recall event identity conflicts")
        envelope = {
            "event_id": event_id,
            "event_type": event_type,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": str(self.version + 1),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        try:
            _borrow_store_append_event(self, envelope)
        except Exception as error:
            self._reload()
            raise BorrowRecallConflict("borrow recall journal changed concurrently") from error
        self._reload()

    def record_recall(self, evidence: BorrowRecallEvidence) -> Decimal:
        DurableBorrowRecallProjection._reload(self)
        if type(evidence) is not BorrowRecallEvidence:
            raise TypeError("evidence must be exact BorrowRecallEvidence")
        evidence = replace(evidence)
        verify_provider_borrow_evidence(
            evidence,
            self.evidence_artifact_store,
        )
        if not self._scope_matches(evidence):
            raise BorrowRecallConflict("borrow recall evidence scope mismatch")
        prior = self._recalls.get(evidence.recall_id)
        if prior is not None:
            if prior != evidence:
                raise BorrowRecallConflict("borrow recall identity has conflicting evidence")
            return self.remaining(evidence.recall_id)
        self._append(
            event_type=_RECALL_EVENT,
            identity=evidence.recall_id,
            payload={"operation": "RECALL", "evidence": evidence.payload()},
            committed_at=evidence.observed_at,
        )
        return self.remaining(evidence.recall_id)

    def resolve_recall(self, evidence: BorrowRecallResolutionEvidence) -> Decimal:
        DurableBorrowRecallProjection._reload(self)
        if type(evidence) is not BorrowRecallResolutionEvidence:
            raise TypeError("evidence must be exact BorrowRecallResolutionEvidence")
        evidence = replace(evidence)
        verify_provider_borrow_evidence(
            evidence,
            self.evidence_artifact_store,
        )
        if not self._scope_matches(evidence):
            raise BorrowRecallConflict("borrow recall resolution scope mismatch")
        prior = self._resolutions.get(evidence.resolution_id)
        if prior is not None:
            if prior != evidence:
                raise BorrowRecallConflict("borrow recall resolution identity conflicts")
            return self.remaining(evidence.recall_id)
        recall = self._recalls.get(evidence.recall_id)
        if recall is None:
            raise BorrowRecallConflict("resolution references unknown recall")
        if _dt(evidence.effective_at) < _dt(recall.effective_at):
            raise BorrowRecallConflict("resolution predates recall")
        if evidence.resolved_quantity > self.remaining(evidence.recall_id):
            raise BorrowRecallConflict("resolution exceeds remaining recall obligation")
        self._append(
            event_type=_RESOLUTION_EVENT,
            identity=evidence.resolution_id,
            payload={"operation": "RESOLVE", "evidence": evidence.payload()},
            committed_at=evidence.observed_at,
        )
        return self.remaining(evidence.recall_id)

    def project_equity_state(self, state):
        from .corporate_actions import EquityState

        if not isinstance(state, EquityState):
            raise TypeError("state must be EquityState")
        active = self.active_quantity
        if active > state.borrowed_quantity:
            raise BorrowRecallConflict("provider recall exceeds locally borrowed quantity")
        return replace(state, recalled_quantity=active)
