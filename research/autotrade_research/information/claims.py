"""Causal, provenance-bound news and macro claim primitives.

This module is deliberately a pure information layer. It does not fetch network
content, persist raw licensed documents, grant tool permissions, score trading
authority, or execute model instructions embedded in source text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Iterable, Mapping
from uuid import NAMESPACE_URL, UUID, uuid5


_ALLOWED_USES = frozenset(
    {
        "LIVE_ANALYSIS",
        "HISTORICAL_REPLAY",
        "MODEL_TRAINING",
        "REDISTRIBUTION",
    }
)
_MODES = frozenset({"LIVE", "REPLAY", "TRAINING"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class InformationClaimError(ValueError):
    pass


class RevisionConflict(InformationClaimError):
    pass


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InformationClaimError(f"{name} is required")
    return value.strip()


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InformationClaimError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _decimal(value: object, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise InformationClaimError(f"{name} must use exact decimal input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise InformationClaimError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise InformationClaimError(f"{name} must be a finite decimal")
    return result


def _canonical(value: object) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise InformationClaimError("value must be canonical JSON") from error


def _utc_text(value: datetime) -> str:
    return _time(value, name="time").isoformat().replace("+00:00", "Z")


def _allowed_uses(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise InformationClaimError("allowed_uses must be a collection")
    normalized = frozenset(_text(value, name="allowed_use").upper() for value in values)
    unknown = normalized - _ALLOWED_USES
    if unknown:
        raise InformationClaimError(
            "unsupported allowed_uses: " + ", ".join(sorted(unknown))
        )
    if not normalized:
        raise InformationClaimError("at least one allowed_use is required")
    return normalized


def _evidence_ref(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InformationClaimError("raw_evidence_ref must be an object")
    required = {"artifact_id", "sha256", "observed_at", "source_uri", "rights_id"}
    if set(value) != required:
        raise InformationClaimError(
            "raw_evidence_ref must contain exactly artifact_id, sha256, observed_at, source_uri, rights_id"
        )
    try:
        artifact_id = str(UUID(_text(value["artifact_id"], name="artifact_id")))
    except ValueError as error:
        raise InformationClaimError("artifact_id must be a UUID") from error
    digest = _text(value["sha256"], name="sha256")
    if _SHA256.fullmatch(digest) is None:
        raise InformationClaimError("sha256 must be a canonical SHA-256 digest")
    observed = _text(value["observed_at"], name="observed_at")
    if not observed.endswith("Z"):
        raise InformationClaimError("observed_at must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(observed[:-1] + "+00:00")
    except ValueError as error:
        raise InformationClaimError("observed_at must be an ISO timestamp") from error
    source_uri = _text(value["source_uri"], name="source_uri")
    if "://" not in source_uri:
        raise InformationClaimError("source_uri must be an absolute URI")
    return MappingProxyType(
        {
            "artifact_id": artifact_id,
            "sha256": digest,
            "observed_at": _utc_text(parsed),
            "source_uri": source_uri,
            "rights_id": _text(value["rights_id"], name="rights_id"),
        }
    )


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_id: str
    owner: str
    source_type: str
    endpoint: str
    rights_id: str
    publication_convention: str
    correction_policy: str
    extraction_version: str
    trust_features: tuple[str, ...]

    def __post_init__(self) -> None:
        for field in (
            "source_id",
            "owner",
            "source_type",
            "endpoint",
            "rights_id",
            "publication_convention",
            "correction_policy",
            "extraction_version",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), name=field))
        features = tuple(
            sorted({_text(value, name="trust_feature") for value in self.trust_features})
        )
        object.__setattr__(self, "trust_features", features)


@dataclass(frozen=True, slots=True)
class InformationObservation:
    observation_id: str
    source_id: str
    source_document_id: str
    revision_index: int
    publication_at: datetime
    available_at: datetime
    ingested_at: datetime
    availability_basis: str
    allowed_uses: frozenset[str]
    historical_availability_proven: bool
    revision_lineage_complete: bool
    syndication_root_id: str
    raw_evidence_ref: Mapping[str, object]

    @classmethod
    def create(
        cls,
        *,
        source: SourceDescriptor,
        source_document_id: str,
        revision_index: int,
        publication_at: datetime,
        available_at: datetime,
        ingested_at: datetime,
        availability_basis: str,
        allowed_uses: Iterable[str],
        historical_availability_proven: bool,
        revision_lineage_complete: bool,
        syndication_root_id: str,
        raw_evidence_ref: Mapping[str, object],
    ) -> "InformationObservation":
        if not isinstance(source, SourceDescriptor):
            raise TypeError("source must be SourceDescriptor")
        if (
            not isinstance(revision_index, int)
            or isinstance(revision_index, bool)
            or revision_index < 0
        ):
            raise InformationClaimError("revision_index must be a non-negative integer")
        if type(historical_availability_proven) is not bool:
            raise TypeError("historical_availability_proven must be boolean")
        if type(revision_lineage_complete) is not bool:
            raise TypeError("revision_lineage_complete must be boolean")

        published = _time(publication_at, name="publication_at")
        available = _time(available_at, name="available_at")
        ingested = _time(ingested_at, name="ingested_at")
        if published > available:
            raise InformationClaimError("publication_at cannot be after available_at")
        if available > ingested:
            raise InformationClaimError("available_at cannot be after ingested_at")
        evidence = _evidence_ref(raw_evidence_ref)
        if evidence["rights_id"] != source.rights_id:
            raise InformationClaimError("evidence rights_id does not match source rights")

        document = _text(source_document_id, name="source_document_id")
        root = _text(syndication_root_id, name="syndication_root_id")
        uses = _allowed_uses(allowed_uses)
        identity = {
            "source_id": source.source_id,
            "source_document_id": document,
            "revision_index": revision_index,
            "publication_at": _utc_text(published),
            "available_at": _utc_text(available),
            "availability_basis": _text(availability_basis, name="availability_basis"),
            "raw_sha256": evidence["sha256"],
        }
        observation_id = str(uuid5(NAMESPACE_URL, _canonical(identity)))
        return cls(
            observation_id=observation_id,
            source_id=source.source_id,
            source_document_id=document,
            revision_index=revision_index,
            publication_at=published,
            available_at=available,
            ingested_at=ingested,
            availability_basis=_text(availability_basis, name="availability_basis"),
            allowed_uses=uses,
            historical_availability_proven=historical_availability_proven,
            revision_lineage_complete=revision_lineage_complete,
            syndication_root_id=root,
            raw_evidence_ref=evidence,
        )


@dataclass(frozen=True, slots=True)
class StructuredClaim:
    claim_id: str
    observation_id: str
    source_id: str
    source_document_id: str
    revision_index: int
    syndication_root_id: str
    claim_key: str
    entity_id: str
    claim_type: str
    statement: str
    event_time: datetime | None
    numeric_value: Decimal | None
    unit: str | None
    extractor_confidence: Decimal
    extraction_version: str
    extracted_at: datetime
    historical_available_at: datetime
    ingested_at: datetime
    allowed_uses: frozenset[str]
    historical_availability_proven: bool
    revision_lineage_complete: bool
    supersedes_claim_id: str | None
    evidence_ref: Mapping[str, object]

    @classmethod
    def create(
        cls,
        observation: InformationObservation,
        *,
        claim_key: str,
        entity_id: str,
        claim_type: str,
        statement: str,
        extraction_version: str,
        extracted_at: datetime,
        extractor_confidence,
        event_time: datetime | None = None,
        numeric_value=None,
        unit: str | None = None,
        supersedes_claim_id: str | None = None,
    ) -> "StructuredClaim":
        if not isinstance(observation, InformationObservation):
            raise TypeError("observation must be InformationObservation")
        extracted = _time(extracted_at, name="extracted_at")
        if extracted < observation.ingested_at:
            raise InformationClaimError("extracted_at cannot precede ingested_at")
        confidence = _decimal(extractor_confidence, name="extractor_confidence")
        if confidence < 0 or confidence > 1:
            raise InformationClaimError("extractor_confidence must be in [0,1]")
        event = None if event_time is None else _time(event_time, name="event_time")
        number = None if numeric_value is None else _decimal(numeric_value, name="numeric_value")
        if (number is None) != (unit is None):
            raise InformationClaimError("numeric_value and unit must be supplied together")
        normalized_unit = None if unit is None else _text(unit, name="unit")
        supersedes = (
            None
            if supersedes_claim_id is None
            else _text(supersedes_claim_id, name="supersedes_claim_id")
        )
        key = _text(claim_key, name="claim_key")
        entity = _text(entity_id, name="entity_id")
        kind = _text(claim_type, name="claim_type").upper()
        text = _text(statement, name="statement")
        version = _text(extraction_version, name="extraction_version")
        value_material = {
            "claim_key": key,
            "entity_id": entity,
            "claim_type": kind,
            "statement": text,
            "event_time": None if event is None else _utc_text(event),
            "numeric_value": None if number is None else str(number),
            "unit": normalized_unit,
        }
        identity = {
            "observation_id": observation.observation_id,
            "revision_index": observation.revision_index,
            "extraction_version": version,
            "value": value_material,
        }
        claim_id = str(uuid5(NAMESPACE_URL, _canonical(identity)))
        return cls(
            claim_id=claim_id,
            observation_id=observation.observation_id,
            source_id=observation.source_id,
            source_document_id=observation.source_document_id,
            revision_index=observation.revision_index,
            syndication_root_id=observation.syndication_root_id,
            claim_key=key,
            entity_id=entity,
            claim_type=kind,
            statement=text,
            event_time=event,
            numeric_value=number,
            unit=normalized_unit,
            extractor_confidence=confidence,
            extraction_version=version,
            extracted_at=extracted,
            historical_available_at=observation.available_at,
            ingested_at=observation.ingested_at,
            allowed_uses=observation.allowed_uses,
            historical_availability_proven=observation.historical_availability_proven,
            revision_lineage_complete=observation.revision_lineage_complete,
            supersedes_claim_id=supersedes,
            evidence_ref=observation.raw_evidence_ref,
        )

    @property
    def semantic_value_hash(self) -> str:
        payload = {
            "entity_id": self.entity_id,
            "claim_type": self.claim_type,
            "statement": self.statement,
            "event_time": None if self.event_time is None else _utc_text(self.event_time),
            "numeric_value": None if self.numeric_value is None else str(self.numeric_value),
            "unit": self.unit,
        }
        return "sha256:" + sha256(_canonical(payload).encode("utf-8")).hexdigest()

    @property
    def untrusted_content(self) -> bool:
        return True


def _lineage_key(claim: StructuredClaim) -> tuple[str, str, str]:
    return claim.source_id, claim.source_document_id, claim.claim_key


def _validate_lineages(claims: tuple[StructuredClaim, ...]) -> None:
    by_id = {claim.claim_id: claim for claim in claims}
    if len(by_id) != len(claims):
        raise RevisionConflict("claim_id is duplicated")
    seen_revisions: dict[tuple[str, str, str, int], StructuredClaim] = {}
    for claim in claims:
        key = (*_lineage_key(claim), claim.revision_index)
        previous = seen_revisions.get(key)
        if previous is not None and previous.semantic_value_hash != claim.semantic_value_hash:
            raise RevisionConflict(
                "one source claim revision contains conflicting extracted values"
            )
        seen_revisions[key] = claim
        if claim.supersedes_claim_id is not None:
            parent = by_id.get(claim.supersedes_claim_id)
            if parent is None:
                if claim.revision_lineage_complete:
                    raise RevisionConflict(
                        "complete revision lineage references a missing parent claim"
                    )
                continue
            if _lineage_key(parent) != _lineage_key(claim):
                raise RevisionConflict("correction supersedes a different claim lineage")
            if parent.revision_index >= claim.revision_index:
                raise RevisionConflict("correction must advance source revision")


def causal_claims(
    claims: Iterable[StructuredClaim],
    *,
    information_cutoff: datetime,
    mode: str,
) -> tuple[StructuredClaim, ...]:
    """Return only the latest causally usable source revision at the cutoff.

    REPLAY and TRAINING require evidenced historical availability and complete
    correction lineage. LIVE also requires that ingestion and extraction had
    actually completed by the runtime cutoff. Later corrections never rewrite
    what was visible at an earlier cutoff.
    """

    cutoff = _time(information_cutoff, name="information_cutoff")
    normalized_mode = _text(mode, name="mode").upper()
    if normalized_mode not in _MODES:
        raise InformationClaimError("mode must be LIVE, REPLAY or TRAINING")
    materialized = tuple(claims)
    if any(not isinstance(claim, StructuredClaim) for claim in materialized):
        raise TypeError("claims must contain StructuredClaim values")
    _validate_lineages(materialized)

    required_use = {
        "LIVE": "LIVE_ANALYSIS",
        "REPLAY": "HISTORICAL_REPLAY",
        "TRAINING": "MODEL_TRAINING",
    }[normalized_mode]
    eligible: list[StructuredClaim] = []
    for claim in materialized:
        if required_use not in claim.allowed_uses:
            continue
        if claim.historical_available_at > cutoff:
            continue
        if normalized_mode in {"REPLAY", "TRAINING"}:
            if not claim.historical_availability_proven:
                continue
            if not claim.revision_lineage_complete:
                continue
        if normalized_mode == "LIVE":
            if claim.ingested_at > cutoff or claim.extracted_at > cutoff:
                continue
        eligible.append(claim)

    active: dict[tuple[str, str, str], StructuredClaim] = {}
    for claim in sorted(
        eligible,
        key=lambda item: (
            item.historical_available_at,
            item.revision_index,
            item.claim_id,
        ),
    ):
        key = _lineage_key(claim)
        current = active.get(key)
        if current is None or claim.revision_index > current.revision_index:
            active[key] = claim
        elif (
            claim.revision_index == current.revision_index
            and claim.semantic_value_hash != current.semantic_value_hash
        ):
            raise RevisionConflict(
                "visible source revision has conflicting claim values"
            )

    return tuple(
        sorted(
            active.values(),
            key=lambda item: (
                item.historical_available_at,
                item.source_id,
                item.source_document_id,
                item.claim_key,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class ClaimDisagreement:
    claim_key: str
    value_hashes: tuple[str, ...]
    claim_ids: tuple[str, ...]
    independent_syndication_roots: tuple[str, ...]


def disagreements(claims: Iterable[StructuredClaim]) -> tuple[ClaimDisagreement, ...]:
    """Expose disagreements; never collapse them into an invented consensus."""

    materialized = tuple(claims)
    groups: dict[str, list[StructuredClaim]] = {}
    for claim in materialized:
        if not isinstance(claim, StructuredClaim):
            raise TypeError("claims must contain StructuredClaim values")
        groups.setdefault(claim.claim_key, []).append(claim)

    result: list[ClaimDisagreement] = []
    for claim_key, group in groups.items():
        value_hashes = tuple(sorted({claim.semantic_value_hash for claim in group}))
        if len(value_hashes) <= 1:
            continue
        result.append(
            ClaimDisagreement(
                claim_key=claim_key,
                value_hashes=value_hashes,
                claim_ids=tuple(sorted(claim.claim_id for claim in group)),
                independent_syndication_roots=tuple(
                    sorted({claim.syndication_root_id for claim in group})
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: item.claim_key))


def independent_confirmation_count(
    target: StructuredClaim,
    claims: Iterable[StructuredClaim],
) -> int:
    """Count independent syndication roots agreeing with the target value."""

    if not isinstance(target, StructuredClaim):
        raise TypeError("target must be StructuredClaim")
    roots = {
        claim.syndication_root_id
        for claim in claims
        if isinstance(claim, StructuredClaim)
        and claim.claim_key == target.claim_key
        and claim.semantic_value_hash == target.semantic_value_hash
    }
    return len(roots)
