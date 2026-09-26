"""Time/provenance/rights-bound information claim foundation.

Extracted text is untrusted evidence. It can inform research but can never grant
execution permissions, reveal credentials, or expand tool authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Iterable
from urllib.parse import urlsplit
from uuid import UUID


def _time(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _digest(text: str) -> str:
    return "sha256:" + sha256(text.encode("utf-8")).hexdigest()


def _canonical_digest(value: str, *, name: str) -> str:
    normalized = _text(value, name=name)
    if re.fullmatch(r"sha256:[0-9a-f]{64}", normalized) is None:
        raise ValueError(f"{name} must be a canonical SHA-256 digest")
    return normalized


def _canonical_uuid(value: str, *, name: str) -> str:
    normalized = _text(value, name=name)
    try:
        parsed = UUID(normalized)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc
    canonical = str(parsed)
    if normalized.lower() != canonical:
        raise ValueError(f"{name} must be a canonical UUID")
    return canonical


def _utc_text(value: datetime, *, name: str) -> str:
    normalized = _time(value, name=name)
    return normalized.isoformat().replace("+00:00", "Z")


def _uri(value: str, *, name: str) -> str:
    normalized = _text(value, name=name)
    parsed = urlsplit(normalized)
    if not parsed.scheme:
        raise ValueError(f"{name} must be an absolute URI")
    return normalized


def _canonical_json_digest(payload: dict[str, str]) -> str:
    return _digest(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )


def _syndication_digest(
    *,
    subject: str,
    predicate: str,
    value: str,
    passage_hash: str,
) -> str:
    # Syndication identity is semantic-content identity, not an outlet clock.
    # Publication/availability remain committed in each claim's evidence and
    # determine which representative was causally visible first.
    return _canonical_json_digest(
        {
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "passage_hash": passage_hash,
        }
    )


def _conflict_digest(
    *,
    subject: str,
    predicate: str,
    published_at: datetime,
) -> str:
    return _canonical_json_digest(
        {
            "subject": subject,
            "predicate": predicate,
            "published_at": published_at.isoformat(),
        }
    )


def _claim_identity_digest(
    *,
    source_id: str,
    source_revision: str,
    syndication_key: str,
    locator: str,
) -> str:
    return _canonical_json_digest(
        {
            "source_id": source_id,
            "source_revision": source_revision,
            "syndication_key": syndication_key,
            "locator": locator,
        }
    )


@dataclass(frozen=True)
class SourceDocument:
    source_id: str
    source_revision: str
    source_kind: str
    title: str
    passage: str
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    rights_basis: str
    locator: str

    def __post_init__(self) -> None:
        for field in (
            "source_id",
            "source_revision",
            "title",
            "passage",
            "rights_basis",
            "locator",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), name=field))

        kind = _text(self.source_kind, name="source_kind").upper()
        if kind not in {"NEWS", "MACRO", "CORPORATE", "OFFICIAL"}:
            raise ValueError("unsupported source_kind")
        object.__setattr__(self, "source_kind", kind)

        published = _time(self.published_at, name="published_at")
        available = _time(self.available_at, name="available_at")
        ingested = _time(self.ingested_at, name="ingested_at")
        if available < published:
            raise ValueError("available_at cannot precede published_at")
        if ingested < available:
            raise ValueError("ingested_at cannot precede available_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "ingested_at", ingested)

    def to_evidence_ref(
        self,
        *,
        artifact_id: str,
        sha256: str,
        observed_at: datetime,
        source_uri: str | None = None,
    ) -> dict[str, str]:
        """Project source-artifact provenance onto canonical EvidenceRef v1.0.0."""

        observed = _time(observed_at, name="observed_at")
        if observed < self.available_at:
            raise ValueError("observed_at cannot precede source availability")
        evidence = {
            "artifact_id": _canonical_uuid(artifact_id, name="artifact_id"),
            "sha256": _canonical_digest(sha256, name="sha256"),
            "observed_at": _utc_text(observed, name="observed_at"),
            "rights_id": self.rights_basis,
        }
        if source_uri is not None:
            evidence["source_uri"] = _uri(source_uri, name="source_uri")
        return evidence

    @classmethod
    def create(
        cls,
        *,
        source_id: str,
        source_revision: str,
        source_kind: str,
        title: str,
        passage: str,
        published_at: datetime,
        available_at: datetime,
        ingested_at: datetime,
        rights_basis: str,
        locator: str,
    ) -> "SourceDocument":
        published = _time(published_at, name="published_at")
        available = _time(available_at, name="available_at")
        ingested = _time(ingested_at, name="ingested_at")
        if available < published:
            raise ValueError("available_at cannot precede published_at")
        if ingested < available:
            raise ValueError("ingested_at cannot precede available_at")
        kind = _text(source_kind, name="source_kind").upper()
        if kind not in {"NEWS", "MACRO", "CORPORATE", "OFFICIAL"}:
            raise ValueError("unsupported source_kind")
        return cls(
            source_id=_text(source_id, name="source_id"),
            source_revision=_text(source_revision, name="source_revision"),
            source_kind=kind,
            title=_text(title, name="title"),
            passage=_text(passage, name="passage"),
            published_at=published,
            available_at=available,
            ingested_at=ingested,
            rights_basis=_text(rights_basis, name="rights_basis"),
            locator=_text(locator, name="locator"),
        )


@dataclass(frozen=True)
class InformationClaim:
    claim_id: str
    subject: str
    predicate: str
    value: str
    source_id: str
    source_revision: str
    source_kind: str
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    passage_hash: str
    locator: str
    rights_basis: str
    syndication_key: str
    conflict_key: str
    untrusted_content: bool = True
    permission_effect: str = "NONE"

    def __post_init__(self) -> None:
        for field in (
            "subject",
            "predicate",
            "value",
            "source_id",
            "source_revision",
            "locator",
            "rights_basis",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), name=field))

        kind = _text(self.source_kind, name="source_kind").upper()
        if kind not in {"NEWS", "MACRO", "CORPORATE", "OFFICIAL"}:
            raise ValueError("unsupported source_kind")
        object.__setattr__(self, "source_kind", kind)

        published = _time(self.published_at, name="published_at")
        available = _time(self.available_at, name="available_at")
        ingested = _time(self.ingested_at, name="ingested_at")
        if available < published:
            raise ValueError("available_at cannot precede published_at")
        if ingested < available:
            raise ValueError("ingested_at cannot precede available_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "ingested_at", ingested)

        for field in ("claim_id", "passage_hash", "syndication_key", "conflict_key"):
            object.__setattr__(
                self,
                field,
                _canonical_digest(getattr(self, field), name=field),
            )

        expected_syndication_key = _syndication_digest(
            subject=self.subject,
            predicate=self.predicate,
            value=self.value,
            passage_hash=self.passage_hash,
        )
        if self.syndication_key != expected_syndication_key:
            raise ValueError("syndication_key identity mismatch")

        expected_conflict_key = _conflict_digest(
            subject=self.subject,
            predicate=self.predicate,
            published_at=self.published_at,
        )
        if self.conflict_key != expected_conflict_key:
            raise ValueError("conflict_key identity mismatch")

        expected_claim_id = _claim_identity_digest(
            source_id=self.source_id,
            source_revision=self.source_revision,
            syndication_key=self.syndication_key,
            locator=self.locator,
        )
        if self.claim_id != expected_claim_id:
            raise ValueError("claim_id identity mismatch")

        if self.untrusted_content is not True or self.permission_effect != "NONE":
            raise ValueError("information claims cannot grant authority")

    def evidence_digest(self) -> str:
        payload = {
            "claim_id": self.claim_id,
            "subject": self.subject,
            "predicate": self.predicate,
            "value": self.value,
            "source_id": self.source_id,
            "source_revision": self.source_revision,
            "source_kind": self.source_kind,
            "published_at": self.published_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "passage_hash": self.passage_hash,
            "locator": self.locator,
            "rights_basis": self.rights_basis,
            "syndication_key": self.syndication_key,
            "conflict_key": self.conflict_key,
            "untrusted_content": self.untrusted_content,
            "permission_effect": self.permission_effect,
        }
        return _digest(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )


@dataclass(frozen=True)
class InformationSnapshot:
    """Causal, content-addressed view of claims visible at one replay cutoff."""

    cutoff: datetime
    claims: tuple[InformationClaim, ...]

    def __post_init__(self) -> None:
        cutoff = _time(self.cutoff, name="cutoff")
        object.__setattr__(self, "cutoff", cutoff)
        if not isinstance(self.claims, tuple):
            raise ValueError("claims must be a tuple")

        seen: set[str] = set()
        seen_syndication: set[str] = set()
        for claim in self.claims:
            if not isinstance(claim, InformationClaim):
                raise ValueError("snapshot claims must be InformationClaim values")
            if claim.available_at > cutoff or claim.ingested_at > cutoff:
                raise ValueError("snapshot cannot contain future claims")
            if claim.claim_id in seen:
                raise ValueError("snapshot cannot contain duplicate claim identities")
            if claim.syndication_key in seen_syndication:
                raise ValueError("snapshot cannot contain syndicated duplicates")
            seen.add(claim.claim_id)
            seen_syndication.add(claim.syndication_key)

        canonical = tuple(
            sorted(
                self.claims,
                key=lambda item: (
                    max(item.available_at, item.ingested_at),
                    item.published_at,
                    item.claim_id,
                ),
            )
        )
        if canonical != self.claims:
            raise ValueError("snapshot claims must be in canonical order")

    def to_manifest(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "cutoff": self.cutoff.isoformat(),
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "evidence_digest": claim.evidence_digest(),
                }
                for claim in self.claims
            ],
        }

    def digest(self) -> str:
        return _digest(
            json.dumps(
                self.to_manifest(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )


class ClaimStore:
    def __init__(self):
        self._claims: list[InformationClaim] = []
        self._by_id: dict[str, InformationClaim] = {}
        self._history_by_id: dict[str, InformationClaim] = {}
        self._syndication: dict[str, str] = {}

    @property
    def claims(self) -> tuple[InformationClaim, ...]:
        return tuple(self._claims)

    @staticmethod
    def build_claim(
        document: SourceDocument,
        *,
        subject: str,
        predicate: str,
        value: str,
    ) -> InformationClaim:
        if not isinstance(document, SourceDocument):
            raise ValueError("document must be a SourceDocument")
        normalized_subject = _text(subject, name="subject")
        normalized_predicate = _text(predicate, name="predicate")
        normalized_value = _text(value, name="value")
        passage_hash = _digest(document.passage)
        syndication_key = _syndication_digest(
            subject=normalized_subject,
            predicate=normalized_predicate,
            value=normalized_value,
            passage_hash=passage_hash,
        )
        conflict_key = _conflict_digest(
            subject=normalized_subject,
            predicate=normalized_predicate,
            published_at=document.published_at,
        )
        claim_id = _claim_identity_digest(
            source_id=document.source_id,
            source_revision=document.source_revision,
            syndication_key=syndication_key,
            locator=document.locator,
        )
        return InformationClaim(
            claim_id=claim_id,
            subject=normalized_subject,
            predicate=normalized_predicate,
            value=normalized_value,
            source_id=document.source_id,
            source_revision=document.source_revision,
            source_kind=document.source_kind,
            published_at=document.published_at,
            available_at=document.available_at,
            ingested_at=document.ingested_at,
            passage_hash=passage_hash,
            locator=document.locator,
            rights_basis=document.rights_basis,
            syndication_key=syndication_key,
            conflict_key=conflict_key,
        )

    def add(self, claim: InformationClaim) -> tuple[InformationClaim, bool]:
        if claim.permission_effect != "NONE" or claim.untrusted_content is not True:
            raise ValueError("information claims cannot grant authority")
        existing_history = self._history_by_id.get(claim.claim_id)
        if existing_history is not None:
            if existing_history != claim:
                raise ValueError("claim identity conflict")
            canonical_id = self._syndication.get(claim.syndication_key)
            if canonical_id is not None:
                return self._by_id[canonical_id], False
            return existing_history, False

        # Provenance history and the canonical deduplicated claim view have
        # different responsibilities. Every distinct source revision is
        # retained here even when syndicated content is represented only once
        # in snapshots and decision inputs.
        self._history_by_id[claim.claim_id] = claim

        duplicate_id = self._syndication.get(claim.syndication_key)
        if duplicate_id is not None:
            existing_duplicate = self._by_id[duplicate_id]
            existing_key = (
                max(existing_duplicate.available_at, existing_duplicate.ingested_at),
                existing_duplicate.published_at,
                existing_duplicate.claim_id,
            )
            candidate_key = (
                max(claim.available_at, claim.ingested_at),
                claim.published_at,
                claim.claim_id,
            )
            if candidate_key < existing_key:
                # Syndication identity is semantic content identity, but causal replay
                # must retain the earliest observed availability independent of ingest
                # order. Replace only the representative; do not count a duplicate.
                index = self._claims.index(existing_duplicate)
                self._claims[index] = claim
                del self._by_id[duplicate_id]
                self._by_id[claim.claim_id] = claim
                self._syndication[claim.syndication_key] = claim.claim_id
                return claim, False
            return existing_duplicate, False

        self._claims.append(claim)
        self._by_id[claim.claim_id] = claim
        self._syndication[claim.syndication_key] = claim.claim_id
        return claim, True

    def conflicts_for(self, claim: InformationClaim) -> tuple[InformationClaim, ...]:
        return tuple(
            item
            for item in self._claims
            if item.conflict_key == claim.conflict_key and item.value != claim.value
        )

    def available_at(self, cutoff: datetime) -> tuple[InformationClaim, ...]:
        time = _time(cutoff, name="cutoff")
        return tuple(
            sorted(
                (
                    item
                    for item in self._claims
                    if item.available_at <= time and item.ingested_at <= time
                ),
                key=lambda item: (
                    max(item.available_at, item.ingested_at),
                    item.published_at,
                    item.claim_id,
                ),
            )
        )

    def effective_at(self, cutoff: datetime) -> tuple[InformationClaim, ...]:
        """Return causally visible claims after source-revision supersession.

        Provenance history is never deleted. For decision inputs, however, a later
        causally visible revision of the same source/subject/predicate/locator
        supersedes the earlier revision. Syndicated duplicates are then collapsed
        deterministically across the effective source views.
        """

        time = _time(cutoff, name="cutoff")
        visible_history = [
            item
            for item in self._history_by_id.values()
            if item.available_at <= time and item.ingested_at <= time
        ]

        chains: dict[
            tuple[str, str, str, str],
            list[InformationClaim],
        ] = {}
        for item in visible_history:
            key = (
                item.source_id,
                item.subject,
                item.predicate,
                item.locator,
            )
            chains.setdefault(key, []).append(item)

        effective_per_source: list[InformationClaim] = []
        for chain in chains.values():
            by_revision: dict[str, list[InformationClaim]] = {}
            for item in chain:
                by_revision.setdefault(item.source_revision, []).append(item)
            for revision_claims in by_revision.values():
                if len({item.value for item in revision_claims}) > 1:
                    raise ValueError(
                        "one source revision contains contradictory extracted values"
                    )

            latest = max(
                chain,
                key=lambda item: (
                    # Once revisions are causally visible at the cutoff, source
                    # publication order is the supersession authority. A delayed
                    # ingest of an older revision must not roll back a newer
                    # already-visible source fact.
                    item.published_at,
                    item.available_at,
                    item.ingested_at,
                    item.source_revision,
                    item.claim_id,
                ),
            )
            effective_per_source.append(latest)

        representatives: dict[str, InformationClaim] = {}
        for item in effective_per_source:
            existing = representatives.get(item.syndication_key)
            if existing is None:
                representatives[item.syndication_key] = item
                continue
            existing_key = (
                max(existing.available_at, existing.ingested_at),
                existing.published_at,
                existing.claim_id,
            )
            candidate_key = (
                max(item.available_at, item.ingested_at),
                item.published_at,
                item.claim_id,
            )
            if candidate_key < existing_key:
                representatives[item.syndication_key] = item

        return tuple(
            sorted(
                representatives.values(),
                key=lambda item: (
                    max(item.available_at, item.ingested_at),
                    item.published_at,
                    item.claim_id,
                ),
            )
        )

    def contradiction_groups_at(
        self,
        cutoff: datetime,
    ) -> tuple[tuple[InformationClaim, ...], ...]:
        """Return current cross-source contradictions without resolving truth.

        A contradiction group contains effective claims for one subject/predicate
        with at least two distinct asserted values. The store reports disagreement;
        it does not choose which source is correct.
        """

        effective = self.effective_at(cutoff)
        groups: dict[tuple[str, str], list[InformationClaim]] = {}
        for item in effective:
            groups.setdefault((item.subject, item.predicate), []).append(item)
        contradictions = [
            tuple(items)
            for _, items in sorted(groups.items())
            if len({item.value for item in items}) > 1
        ]
        return tuple(contradictions)

    def decision_snapshot_at(self, cutoff: datetime) -> InformationSnapshot:
        """Build a causal snapshot suitable for decision/research inputs.

        Historical snapshot_at() intentionally preserves every visible canonical
        claim for provenance/replay. This method applies revision supersession
        first, while still preserving unresolved cross-source contradictions.
        """

        time = _time(cutoff, name="cutoff")
        return InformationSnapshot(cutoff=time, claims=self.effective_at(time))

    def snapshot_at(self, cutoff: datetime) -> InformationSnapshot:
        time = _time(cutoff, name="cutoff")
        return InformationSnapshot(cutoff=time, claims=self.available_at(time))

    def revisions(self, source_id: str) -> tuple[InformationClaim, ...]:
        identifier = _text(source_id, name="source_id")
        return tuple(
            sorted(
                (
                    item
                    for item in self._history_by_id.values()
                    if item.source_id == identifier
                ),
                key=lambda item: (
                    max(item.available_at, item.ingested_at),
                    item.source_revision,
                    item.claim_id,
                ),
            )
        )


def build_information_event(
    document: SourceDocument,
    claims: Iterable[InformationClaim],
    *,
    information_id: str,
    revision: str,
    language: str,
    extraction_version: str,
    artifact_id: str,
    artifact_sha256: str,
    observed_at: datetime,
    source_uri: str | None = None,
    confidence_by_claim: dict[str, float],
    trust_features: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a canonical InformationEvent v1.0.0 without granting authority."""

    if not isinstance(document, SourceDocument):
        raise ValueError("document must be a SourceDocument")
    canonical_information_id = _canonical_uuid(information_id, name="information_id")
    canonical_revision = _text(revision, name="revision")
    if re.fullmatch(r"0|[1-9][0-9]*", canonical_revision) is None:
        raise ValueError("revision must be a canonical Sequence")
    canonical_language = _text(language, name="language")
    if len(canonical_language) < 2:
        raise ValueError("language must contain at least two characters")
    canonical_extraction_version = _text(
        extraction_version,
        name="extraction_version",
    )

    claim_values = tuple(claims)
    if len({claim.claim_id for claim in claim_values if isinstance(claim, InformationClaim)}) != len(claim_values):
        raise ValueError("claims must have unique identities")

    expected_passage_hash = _digest(document.passage)
    projected_claims: list[dict[str, object]] = []
    for claim in claim_values:
        if not isinstance(claim, InformationClaim):
            raise ValueError("claims must contain only InformationClaim values")
        if (
            claim.source_id != document.source_id
            or claim.source_revision != document.source_revision
            or claim.source_kind != document.source_kind
            or claim.published_at != document.published_at
            or claim.available_at != document.available_at
            or claim.ingested_at != document.ingested_at
            or claim.passage_hash != expected_passage_hash
            or claim.rights_basis != document.rights_basis
        ):
            raise ValueError("claim provenance does not match source document")
        if claim.claim_id not in confidence_by_claim:
            raise ValueError("every claim requires explicit confidence")
        confidence = confidence_by_claim[claim.claim_id]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("claim confidence must be numeric")
        if confidence < 0 or confidence > 1:
            raise ValueError("claim confidence must be between 0 and 1")
        projected_claims.append(
            {
                "claim_id": claim.claim_id,
                "kind": claim.predicate,
                "text": claim.value,
                "confidence": float(confidence),
            }
        )

    expected_confidence_ids = {claim.claim_id for claim in claim_values}
    if set(confidence_by_claim) != expected_confidence_ids:
        raise ValueError("confidence map must match exact claim population")

    reserved_trust_keys = {"source_kind", "source_revision", "extraction_version"}
    features = dict(trust_features or {})
    if reserved_trust_keys.intersection(features):
        raise ValueError("trust_features cannot override canonical provenance")
    features.update(
        {
            "source_kind": document.source_kind,
            "source_revision": document.source_revision,
            "extraction_version": canonical_extraction_version,
        }
    )

    evidence = document.to_evidence_ref(
        artifact_id=artifact_id,
        sha256=artifact_sha256,
        observed_at=observed_at,
        source_uri=source_uri,
    )
    return {
        "information_id": canonical_information_id,
        "source_id": document.source_id,
        "published_at": _utc_text(document.published_at, name="published_at"),
        "available_at": _utc_text(document.available_at, name="available_at"),
        "ingested_at": _utc_text(document.ingested_at, name="ingested_at"),
        "revision": canonical_revision,
        "entities": sorted({claim.subject for claim in claim_values}),
        "claims": projected_claims,
        "content_hash": evidence["sha256"],
        "rights_id": document.rights_basis,
        "trust_features": features,
        "language": canonical_language,
        "evidence": [evidence],
    }


def ingest_claims(
    documents: Iterable[SourceDocument],
    *,
    subject: str,
    predicate: str,
    value_by_source_revision: dict[tuple[str, str], str],
) -> ClaimStore:
    store = ClaimStore()
    for document in documents:
        if not isinstance(document, SourceDocument):
            raise ValueError("documents must contain only SourceDocument values")
        source_revision = (document.source_id, document.source_revision)
        if source_revision not in value_by_source_revision:
            raise ValueError(
                "every source identity and revision requires an explicit extracted value"
            )
        store.add(
            store.build_claim(
                document,
                subject=subject,
                predicate=predicate,
                value=value_by_source_revision[source_revision],
            )
        )
    return store
