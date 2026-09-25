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
    published_at: datetime,
) -> str:
    return _canonical_json_digest(
        {
            "subject": subject,
            "predicate": predicate,
            "value": value,
            "passage_hash": passage_hash,
            "published_at": published_at.isoformat(),
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
        if available < published:
            raise ValueError("available_at cannot precede published_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)

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
        rights_basis: str,
        locator: str,
    ) -> "SourceDocument":
        published = _time(published_at, name="published_at")
        available = _time(available_at, name="available_at")
        if available < published:
            raise ValueError("available_at cannot precede published_at")
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
        if available < published:
            raise ValueError("available_at cannot precede published_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)

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
            published_at=self.published_at,
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
        for claim in self.claims:
            if not isinstance(claim, InformationClaim):
                raise ValueError("snapshot claims must be InformationClaim values")
            if claim.available_at > cutoff:
                raise ValueError("snapshot cannot contain future claims")
            if claim.claim_id in seen:
                raise ValueError("snapshot cannot contain duplicate claim identities")
            seen.add(claim.claim_id)

        canonical = tuple(
            sorted(
                self.claims,
                key=lambda item: (item.available_at, item.published_at, item.claim_id),
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
            published_at=document.published_at,
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
                existing_duplicate.available_at,
                existing_duplicate.published_at,
                existing_duplicate.claim_id,
            )
            candidate_key = (claim.available_at, claim.published_at, claim.claim_id)
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
                (item for item in self._claims if item.available_at <= time),
                key=lambda item: (item.available_at, item.published_at, item.claim_id),
            )
        )

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
                key=lambda item: (item.available_at, item.source_revision, item.claim_id),
            )
        )


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
