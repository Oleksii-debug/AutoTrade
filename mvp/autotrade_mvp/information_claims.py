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

        if self.untrusted_content is not True or self.permission_effect != "NONE":
            raise ValueError("information claims cannot grant authority")


class ClaimStore:
    def __init__(self):
        self._claims: list[InformationClaim] = []
        self._by_id: dict[str, InformationClaim] = {}
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
        normalized_subject = _text(subject, name="subject")
        normalized_predicate = _text(predicate, name="predicate")
        normalized_value = _text(value, name="value")
        passage_hash = _digest(document.passage)
        syndication_payload = {
            "subject": normalized_subject,
            "predicate": normalized_predicate,
            "value": normalized_value,
            "passage_hash": passage_hash,
            "published_at": document.published_at.isoformat(),
        }
        syndication_key = _digest(
            json.dumps(syndication_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
        conflict_key = _digest(
            json.dumps(
                {
                    "subject": normalized_subject,
                    "predicate": normalized_predicate,
                    "published_at": document.published_at.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
        claim_id = _digest(
            json.dumps(
                {
                    "source_id": document.source_id,
                    "source_revision": document.source_revision,
                    "syndication_key": syndication_key,
                    "locator": document.locator,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
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
        existing = self._by_id.get(claim.claim_id)
        if existing is not None:
            if existing != claim:
                raise ValueError("claim identity conflict")
            return existing, False

        duplicate_id = self._syndication.get(claim.syndication_key)
        if duplicate_id is not None:
            return self._by_id[duplicate_id], False

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

    def revisions(self, source_id: str) -> tuple[InformationClaim, ...]:
        identifier = _text(source_id, name="source_id")
        return tuple(
            sorted(
                (item for item in self._claims if item.source_id == identifier),
                key=lambda item: (item.available_at, item.source_revision, item.claim_id),
            )
        )


def ingest_claims(
    documents: Iterable[SourceDocument],
    *,
    subject: str,
    predicate: str,
    value_by_revision: dict[str, str],
) -> ClaimStore:
    store = ClaimStore()
    for document in documents:
        if document.source_revision not in value_by_revision:
            raise ValueError("every source revision requires an explicit extracted value")
        store.add(
            store.build_claim(
                document,
                subject=subject,
                predicate=predicate,
                value=value_by_revision[document.source_revision],
            )
        )
    return store
