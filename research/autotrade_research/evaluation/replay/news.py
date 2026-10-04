"""Anonymized causal historical-news replay for product Section 18.

The module prepares structured historical information revisions for the existing
causal feeder and Section 17 blinding boundary. Raw source/entity identities
remain privileged input. Strategy-facing prose is template based: identity
slots are pseudonymized by the canonical BlindingProfile and inserted only
after blinding. This avoids lossy ad-hoc substring redaction and makes leakage
fail closed.

Historical replay remains research evidence only. It does not prove economic
edge and does not replace forward paper evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from string import Formatter
from types import MappingProxyType
from typing import Mapping

from .blinding import (
    BlindedEvent,
    BlindingProfile,
    CalendarField,
    IdentityField,
    _snapshot_blinding_profile,
    _snapshot_source_dataset,
)
from .feeder import CausalDataset, CausalEvent

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SLOT = re.compile(r"^[a-z][a-z0-9_]*$")
_NAMESPACE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_ABSOLUTE_YEAR = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}(?![0-9])")
_ABSOLUTE_ISO_DATE = re.compile(r"(?<![0-9])(?:19|20)[0-9]{2}-[0-9]{2}-[0-9]{2}(?![0-9])")
_ENGLISH_MONTH = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|"
    r"September|October|November|December)\b"
)
_SCHEMA_VERSION = 1
_REVISION_KINDS = frozenset({"ORIGINAL", "UPDATE", "CORRECTION", "RETRACTION"})
_MAPPING_PROXY_TYPE = type(MappingProxyType({}))
_SUPPORTED_TEMPLATE_LANGUAGES = frozenset({"en"})


class NewsReplayError(ValueError):
    """Raised when historical news cannot be represented causally and safely."""


def _text(value: str, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise NewsReplayError(f"{name} is required")
    return value.strip()


def _optional_text(value: str | None, *, name: str) -> str | None:
    if value is None:
        return None
    return _text(value, name=name)


def _digest(value: str, *, name: str) -> str:
    result = _text(value, name=name)
    if _SHA256.fullmatch(result) is None:
        raise NewsReplayError(f"{name} must be canonical sha256:<64 lowercase hex>")
    return result


def _utc(value: datetime | str, *, name: str) -> datetime:
    if type(value) is str:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise NewsReplayError(f"{name} must be an ISO timestamp") from error
    elif type(value) is datetime:
        parsed = value
    else:
        raise TypeError(f"{name} must be an exact datetime or ISO string")
    parsed_tzinfo = object.__getattribute__(parsed, "tzinfo")
    if parsed_tzinfo is None:
        raise NewsReplayError(f"{name} must include a timezone")
    if type(parsed_tzinfo) is not timezone:
        raise NewsReplayError(
            f"{name} timezone must use built-in datetime.timezone"
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _plain(value: object) -> object:
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        result: dict[str, object] = {}
        for key in sorted(value):
            if type(key) is not str:
                raise TypeError("canonical news mappings must use exact str keys")
            result[key] = _plain(value[key])
        return result
    if type(value) in {tuple, list}:
        return [_plain(item) for item in value]
    if value is None or type(value) in {str, bool, int}:
        return value
    raise TypeError(
        f"canonical news content contains unsupported type {type(value).__name__}"
    )


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        _plain(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _freeze(value: object, *, path: str = "value") -> object:
    if value is None or type(value) in {str, bool, int}:
        return value
    if type(value) is float:
        raise TypeError(f"{path} must not contain binary floating-point values")
    if type(value) in {dict, _MAPPING_PROXY_TYPE}:
        result: dict[str, object] = {}
        for raw_key, raw_value in value.items():
            key = _text(raw_key, name=f"{path} key")
            if key in result:
                raise NewsReplayError(f"{path} contains duplicate keys")
            result[key] = _freeze(raw_value, path=f"{path}.{key}")
        return MappingProxyType(result)
    if type(value) is tuple:
        return tuple(_freeze(item, path=f"{path}[]") for item in value)
    raise TypeError(f"{path} contains unsupported type {type(value).__name__}")


def _contains_identity(text: str, raw: str) -> bool:
    folded = text.casefold()
    needle = raw.casefold()
    if len(needle) >= 3:
        return needle in folded
    return re.search(
        rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])",
        folded,
    ) is not None


def _template_fields(template: str, *, name: str) -> tuple[str, ...]:
    template = _text(template, name=name)
    fields: list[str] = []
    try:
        parsed = tuple(Formatter().parse(template))
    except ValueError as error:
        raise NewsReplayError(f"{name} has invalid braces") from error
    for _literal, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name or _SLOT.fullmatch(field_name) is None:
            raise NewsReplayError(
                f"{name} placeholders must be simple lower-case identity slots"
            )
        if format_spec or conversion:
            raise NewsReplayError(f"{name} placeholders cannot use formatting or conversion")
        fields.append(field_name)
    return tuple(fields)


def _validate_template(
    template: str,
    *,
    name: str,
    allowed_slots: frozenset[str],
    raw_identities: frozenset[str],
) -> str:
    result = _text(template, name=name)
    fields = _template_fields(result, name=name)
    unknown = sorted(set(fields) - allowed_slots)
    if unknown:
        raise NewsReplayError(f"{name} uses undeclared identity slots: {unknown}")
    for raw in raw_identities:
        if _contains_identity(result, raw):
            raise NewsReplayError(f"{name} exposes a declared raw identity")
    if _ABSOLUTE_ISO_DATE.search(result) or _ABSOLUTE_YEAR.search(result):
        raise NewsReplayError(f"{name} exposes an absolute calendar year/date")
    if _ENGLISH_MONTH.search(result):
        raise NewsReplayError(f"{name} exposes an obvious absolute calendar month cue")
    return result


@dataclass(frozen=True, slots=True)
class NewsIdentity:
    """One raw identity plus privileged aliases used for leak detection."""

    slot: str
    namespace: str
    raw_value: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        slot = _text(self.slot, name="identity slot")
        if _SLOT.fullmatch(slot) is None:
            raise NewsReplayError("identity slot must match [a-z][a-z0-9_]*")
        namespace = _text(self.namespace, name="identity namespace").upper()
        if _NAMESPACE.fullmatch(namespace) is None:
            raise NewsReplayError("identity namespace must match [A-Z][A-Z0-9_]*")
        raw = _text(self.raw_value, name="identity raw_value")
        if type(self.aliases) is not tuple:
            raise TypeError("identity aliases must be an exact tuple")
        aliases: list[str] = []
        seen = {raw.casefold()}
        for index, value in enumerate(self.aliases):
            alias = _text(value, name=f"identity aliases[{index}]")
            folded = alias.casefold()
            if folded in seen:
                raise NewsReplayError("identity aliases must be unique and non-redundant")
            seen.add(folded)
            aliases.append(alias)
        object.__setattr__(self, "slot", slot)
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "raw_value", raw)
        object.__setattr__(self, "aliases", tuple(aliases))


@dataclass(frozen=True, slots=True)
class NewsClaim:
    """Structured economic/political claim whose quantities survive anonymization."""

    claim_id: str
    subject_slot: str
    predicate: str
    magnitude: str | None
    unit: str | None
    direction: str
    relevance_bps: int
    qualifier_template: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "claim_id", _text(self.claim_id, name="claim_id"))
        subject = _text(self.subject_slot, name="subject_slot")
        if _SLOT.fullmatch(subject) is None:
            raise NewsReplayError("subject_slot must be a simple identity slot")
        object.__setattr__(self, "subject_slot", subject)
        predicate = _text(self.predicate, name="predicate").upper()
        if _CODE.fullmatch(predicate) is None:
            raise NewsReplayError("predicate must be an uppercase semantic code")
        object.__setattr__(self, "predicate", predicate)
        magnitude = _optional_text(self.magnitude, name="magnitude")
        if magnitude is not None and _DECIMAL.fullmatch(magnitude) is None:
            raise NewsReplayError("magnitude must be a canonical decimal string")
        object.__setattr__(self, "magnitude", magnitude)
        unit = _optional_text(self.unit, name="unit")
        object.__setattr__(self, "unit", unit)
        direction = _text(self.direction, name="direction").upper()
        if _CODE.fullmatch(direction) is None:
            raise NewsReplayError("direction must be an uppercase semantic code")
        object.__setattr__(self, "direction", direction)
        if type(self.relevance_bps) is not int or not 0 <= self.relevance_bps <= 10_000:
            raise NewsReplayError("relevance_bps must be an exact integer in [0, 10000]")
        qualifier = _optional_text(self.qualifier_template, name="qualifier_template")
        object.__setattr__(self, "qualifier_template", qualifier)


@dataclass(frozen=True, slots=True)
class NewsRevision:
    """Privileged historical information revision before blinding."""

    information_id: str
    revision: int
    revision_kind: str
    supersedes_revision: int | None
    source_id: str
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    source_event_at: datetime | None
    source_priority: int
    source_sequence: int
    language: str
    rights_id: str
    content_sha256: str
    trust_features_sha256: str
    syndication_sha256: str
    evidence_sha256: tuple[str, ...]
    identities: tuple[NewsIdentity, ...]
    summary_template: str
    claims: tuple[NewsClaim, ...]
    residual_contamination_note: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "information_id", _text(self.information_id, name="information_id")
        )
        if type(self.revision) is not int or self.revision <= 0:
            raise NewsReplayError("revision must be a positive exact integer")
        kind = _text(self.revision_kind, name="revision_kind").upper()
        if kind not in _REVISION_KINDS:
            raise NewsReplayError(f"revision_kind must be one of {sorted(_REVISION_KINDS)}")
        object.__setattr__(self, "revision_kind", kind)
        if self.supersedes_revision is not None:
            if (
                type(self.supersedes_revision) is not int
                or self.supersedes_revision <= 0
                or self.supersedes_revision >= self.revision
            ):
                raise NewsReplayError(
                    "supersedes_revision must be a smaller positive exact integer"
                )

        object.__setattr__(self, "source_id", _text(self.source_id, name="source_id"))
        published = _utc(self.published_at, name="published_at")
        available = _utc(self.available_at, name="available_at")
        ingested = _utc(self.ingested_at, name="ingested_at")
        source_event = (
            None
            if self.source_event_at is None
            else _utc(self.source_event_at, name="source_event_at")
        )
        if available < published:
            raise NewsReplayError("available_at cannot precede published_at")
        if ingested < available:
            raise NewsReplayError("ingested_at cannot precede available_at")
        if source_event is not None and source_event > published:
            raise NewsReplayError("source_event_at cannot be later than published_at")
        object.__setattr__(self, "published_at", published)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "ingested_at", ingested)
        object.__setattr__(self, "source_event_at", source_event)

        for name in ("source_priority", "source_sequence"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise NewsReplayError(f"{name} must be a non-negative exact integer")

        language = _text(self.language, name="language").lower()
        if language not in _SUPPORTED_TEMPLATE_LANGUAGES:
            raise NewsReplayError(
                "unsupported template language for calendar-cue policy; "
                f"supported={sorted(_SUPPORTED_TEMPLATE_LANGUAGES)}"
            )
        object.__setattr__(self, "language", language)
        object.__setattr__(self, "rights_id", _text(self.rights_id, name="rights_id"))
        object.__setattr__(
            self,
            "content_sha256",
            _digest(self.content_sha256, name="content_sha256"),
        )
        object.__setattr__(
            self,
            "trust_features_sha256",
            _digest(self.trust_features_sha256, name="trust_features_sha256"),
        )
        object.__setattr__(
            self,
            "syndication_sha256",
            _digest(self.syndication_sha256, name="syndication_sha256"),
        )
        if type(self.evidence_sha256) is not tuple or not self.evidence_sha256:
            raise NewsReplayError("evidence_sha256 must be a non-empty exact tuple")
        evidence = tuple(
            _digest(item, name=f"evidence_sha256[{index}]")
            for index, item in enumerate(self.evidence_sha256)
        )
        if len(evidence) != len(set(evidence)):
            raise NewsReplayError("evidence_sha256 must not contain duplicates")
        object.__setattr__(self, "evidence_sha256", evidence)

        if type(self.identities) is not tuple or not self.identities:
            raise NewsReplayError("identities must be a non-empty exact tuple")
        identities: list[NewsIdentity] = []
        slots: set[str] = set()
        for item in self.identities:
            if type(item) is not NewsIdentity:
                raise TypeError("identities must contain exact NewsIdentity")
            normalized = NewsIdentity(
                item.slot,
                item.namespace,
                item.raw_value,
                item.aliases,
            )
            if normalized.slot in slots:
                raise NewsReplayError(f"duplicate identity slot: {normalized.slot}")
            slots.add(normalized.slot)
            identities.append(normalized)
        object.__setattr__(self, "identities", tuple(identities))

        if type(self.claims) is not tuple or not self.claims:
            raise NewsReplayError("claims must be a non-empty exact tuple")
        claims: list[NewsClaim] = []
        claim_ids: set[str] = set()
        for item in self.claims:
            if type(item) is not NewsClaim:
                raise TypeError("claims must contain exact NewsClaim")
            normalized = NewsClaim(
                claim_id=item.claim_id,
                subject_slot=item.subject_slot,
                predicate=item.predicate,
                magnitude=item.magnitude,
                unit=item.unit,
                direction=item.direction,
                relevance_bps=item.relevance_bps,
                qualifier_template=item.qualifier_template,
            )
            if normalized.claim_id in claim_ids:
                raise NewsReplayError(f"duplicate claim_id: {normalized.claim_id}")
            if normalized.subject_slot not in slots:
                raise NewsReplayError(
                    f"claim {normalized.claim_id} refers to undeclared subject slot"
                )
            claim_ids.add(normalized.claim_id)
            claims.append(normalized)
        object.__setattr__(self, "claims", tuple(claims))

        raw_identities = frozenset(
            [self.source_id]
            + [item.raw_value for item in identities]
            + [alias for item in identities for alias in item.aliases]
        )
        allowed_slots = frozenset(slots)
        for identity in identities:
            for raw in raw_identities:
                if _contains_identity(identity.slot, raw):
                    raise NewsReplayError(
                        f"identity slot {identity.slot} exposes a declared raw identity"
                    )
        summary = _validate_template(
            self.summary_template,
            name="summary_template",
            allowed_slots=allowed_slots,
            raw_identities=raw_identities,
        )
        object.__setattr__(self, "summary_template", summary)
        for claim in claims:
            for field_name, value in (
                ("claim_id", claim.claim_id),
                ("predicate", claim.predicate),
                ("magnitude", claim.magnitude),
                ("unit", claim.unit),
                ("direction", claim.direction),
            ):
                if value is None:
                    continue
                for raw in raw_identities:
                    if _contains_identity(value, raw):
                        raise NewsReplayError(
                            f"claim {claim.claim_id} {field_name} exposes a declared raw identity"
                        )
            if claim.qualifier_template is not None:
                _validate_template(
                    claim.qualifier_template,
                    name=f"claim {claim.claim_id} qualifier_template",
                    allowed_slots=allowed_slots,
                    raw_identities=raw_identities,
                )

        note = _text(
            self.residual_contamination_note,
            name="residual_contamination_note",
        )
        object.__setattr__(self, "residual_contamination_note", note)

    @property
    def privileged_digest(self) -> str:
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "information_id": self.information_id,
            "revision": self.revision,
            "revision_kind": self.revision_kind,
            "supersedes_revision": self.supersedes_revision,
            "source_id": self.source_id,
            "published_at": _timestamp(self.published_at),
            "available_at": _timestamp(self.available_at),
            "ingested_at": _timestamp(self.ingested_at),
            "source_event_at": (
                None if self.source_event_at is None else _timestamp(self.source_event_at)
            ),
            "source_priority": self.source_priority,
            "source_sequence": self.source_sequence,
            "language": self.language,
            "rights_id": self.rights_id,
            "content_sha256": self.content_sha256,
            "trust_features_sha256": self.trust_features_sha256,
            "syndication_sha256": self.syndication_sha256,
            "evidence_sha256": self.evidence_sha256,
            "identities": [
                {
                    "slot": item.slot,
                    "namespace": item.namespace,
                    "raw_value": item.raw_value,
                    "aliases": item.aliases,
                }
                for item in self.identities
            ],
            "summary_template": self.summary_template,
            "claims": [_claim_payload(item) for item in self.claims],
            "residual_contamination_note": self.residual_contamination_note,
        }
        return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def _claim_payload(claim: NewsClaim) -> dict[str, object]:
    return {
        "claim_id": claim.claim_id,
        "subject_slot": claim.subject_slot,
        "predicate": claim.predicate,
        "magnitude": claim.magnitude,
        "unit": claim.unit,
        "direction": claim.direction,
        "relevance_bps": claim.relevance_bps,
        "qualifier_template": claim.qualifier_template,
    }


def _event_payload(record: NewsRevision) -> Mapping[str, object]:
    identities = {
        item.slot: item.raw_value
        for item in record.identities
    }
    payload: dict[str, object] = {
        "record_kind": "ANONYMIZED_HISTORICAL_NEWS",
        "revision": record.revision,
        "revision_kind": record.revision_kind,
        "supersedes_revision": record.supersedes_revision,
        "story_ref": record.information_id,
        "source_id": record.source_id,
        "published_at": _timestamp(record.published_at),
        "language": record.language,
        "identity_slots": identities,
        "summary_template": record.summary_template,
        "claims": tuple(_claim_payload(item) for item in record.claims),
    }
    if record.source_event_at is not None:
        payload["source_event_at"] = _timestamp(record.source_event_at)
    return _freeze(payload, path="news payload")


def _validate_revision_chains(records: tuple[NewsRevision, ...]) -> None:
    by_information: dict[str, list[NewsRevision]] = {}
    for record in records:
        by_information.setdefault(record.information_id, []).append(record)

    for information_id, series in by_information.items():
        ordered = sorted(series, key=lambda item: item.revision)
        expected = list(range(1, len(ordered) + 1))
        actual = [item.revision for item in ordered]
        if actual != expected:
            raise NewsReplayError(
                f"{information_id} revisions must be contiguous from 1; got {actual}"
            )
        first = ordered[0]
        if first.revision_kind != "ORIGINAL" or first.supersedes_revision is not None:
            raise NewsReplayError(
                f"{information_id} revision 1 must be ORIGINAL and supersede nothing"
            )
        slot_bindings: dict[str, tuple[str, str]] = {}
        for current in ordered:
            for identity in current.identities:
                binding = (identity.namespace, identity.raw_value)
                existing = slot_bindings.setdefault(identity.slot, binding)
                if existing != binding:
                    raise NewsReplayError(
                        f"{information_id} identity slot {identity.slot} changes "
                        "binding across revisions"
                    )
        for current in ordered[1:]:
            if current.source_id != first.source_id:
                raise NewsReplayError(
                    f"{information_id} revisions must retain one source identity"
                )
            if current.syndication_sha256 != first.syndication_sha256:
                raise NewsReplayError(
                    f"{information_id} revisions must retain one syndication identity"
                )
        for prior, current in zip(ordered, ordered[1:]):
            if current.revision_kind == "ORIGINAL":
                raise NewsReplayError(
                    f"{information_id} later revisions cannot be ORIGINAL"
                )
            if current.supersedes_revision != prior.revision:
                raise NewsReplayError(
                    f"{information_id} revision {current.revision} must supersede "
                    f"revision {prior.revision}"
                )
            if current.published_at < prior.published_at:
                raise NewsReplayError(
                    f"{information_id} publication chronology moves backwards"
                )
            if current.available_at < prior.available_at:
                raise NewsReplayError(
                    f"{information_id} correction/update becomes available before "
                    "the revision it supersedes"
                )
            if current.available_at == prior.available_at:
                prior_key = (
                    prior.source_priority,
                    prior.source_sequence,
                    f"NEWS:{information_id}:r{prior.revision}",
                )
                current_key = (
                    current.source_priority,
                    current.source_sequence,
                    f"NEWS:{information_id}:r{current.revision}",
                )
                if current_key <= prior_key:
                    raise NewsReplayError(
                        f"{information_id} same-availability revision ordering "
                        "would expose a later revision before its predecessor"
                    )


@dataclass(frozen=True, slots=True)
class NewsReplayBundle:
    """Privileged Section 18 replay preparation plus canonical blinding profile."""

    dataset: CausalDataset
    blinding_profile: BlindingProfile
    source_records_sha256: str
    contamination_notes_sha256: str

    def __post_init__(self) -> None:
        if type(self.dataset) is not CausalDataset:
            raise TypeError("dataset must be exact CausalDataset")
        if type(self.blinding_profile) is not BlindingProfile:
            raise TypeError("blinding_profile must be exact BlindingProfile")

        try:
            dataset_snapshot = _snapshot_source_dataset(self.dataset)
        except ValueError as error:
            raise NewsReplayError(
                "dataset content no longer matches its committed digest"
            ) from error
        profile_snapshot = _snapshot_blinding_profile(self.blinding_profile)
        object.__setattr__(self, "dataset", dataset_snapshot)
        object.__setattr__(self, "blinding_profile", profile_snapshot)
        object.__setattr__(
            self,
            "source_records_sha256",
            _digest(self.source_records_sha256, name="source_records_sha256"),
        )
        object.__setattr__(
            self,
            "contamination_notes_sha256",
            _digest(
                self.contamination_notes_sha256,
                name="contamination_notes_sha256",
            ),
        )

    @property
    def schema_version(self) -> int:
        return _SCHEMA_VERSION

    @property
    def forward_evidence_required(self) -> bool:
        return True

    @property
    def perfect_anonymization_claimed(self) -> bool:
        return False

    @property
    def digest(self) -> str:
        payload = {
            "schema_version": self.schema_version,
            "dataset_sha256": self.dataset.dataset_sha256,
            "blinding_profile_sha256": self.blinding_profile.digest,
            "source_records_sha256": self.source_records_sha256,
            "contamination_notes_sha256": self.contamination_notes_sha256,
            "forward_evidence_required": True,
            "perfect_anonymization_claimed": False,
        }
        return "sha256:" + sha256(_canonical_bytes(payload)).hexdigest()


def build_news_replay_bundle(
    *,
    manifest_sha256: str,
    records: tuple[NewsRevision, ...],
) -> NewsReplayBundle:
    """Build a causal dataset and Section 17 profile for historical information."""

    manifest = _digest(manifest_sha256, name="manifest_sha256")
    if type(records) is not tuple or not records:
        raise NewsReplayError("records must be a non-empty exact tuple")

    snapshot: list[NewsRevision] = []
    event_ids: set[str] = set()
    slot_namespaces: dict[str, str] = {}
    for item in records:
        if type(item) is not NewsRevision:
            raise TypeError("records must contain exact NewsRevision")
        record = NewsRevision(
            information_id=item.information_id,
            revision=item.revision,
            revision_kind=item.revision_kind,
            supersedes_revision=item.supersedes_revision,
            source_id=item.source_id,
            published_at=item.published_at,
            available_at=item.available_at,
            ingested_at=item.ingested_at,
            source_event_at=item.source_event_at,
            source_priority=item.source_priority,
            source_sequence=item.source_sequence,
            language=item.language,
            rights_id=item.rights_id,
            content_sha256=item.content_sha256,
            trust_features_sha256=item.trust_features_sha256,
            syndication_sha256=item.syndication_sha256,
            evidence_sha256=item.evidence_sha256,
            identities=item.identities,
            summary_template=item.summary_template,
            claims=item.claims,
            residual_contamination_note=item.residual_contamination_note,
        )
        event_id = f"NEWS:{record.information_id}:r{record.revision}"
        if event_id in event_ids:
            raise NewsReplayError(f"duplicate news replay event identity: {event_id}")
        event_ids.add(event_id)
        for identity in record.identities:
            existing = slot_namespaces.setdefault(identity.slot, identity.namespace)
            if existing != identity.namespace:
                raise NewsReplayError(
                    f"identity slot {identity.slot} changes namespace across records"
                )
        snapshot.append(record)

    records_tuple = tuple(
        sorted(snapshot, key=lambda item: (item.information_id, item.revision))
    )
    _validate_revision_chains(records_tuple)

    alias_owner: dict[tuple[str, str], str] = {}
    all_raw_identities: set[str] = {record.source_id for record in records_tuple}
    for record in records_tuple:
        for identity in record.identities:
            all_raw_identities.add(identity.raw_value)
            all_raw_identities.update(identity.aliases)
            for candidate in (identity.raw_value, *identity.aliases):
                key = (identity.namespace, candidate.casefold())
                owner = alias_owner.setdefault(key, identity.raw_value)
                if owner != identity.raw_value:
                    raise NewsReplayError(
                        "one identity or alias cannot refer to multiple canonical entities"
                    )

    global_raw_identities = frozenset(all_raw_identities)
    for record in records_tuple:
        allowed_slots = frozenset(item.slot for item in record.identities)
        for identity in record.identities:
            for raw in global_raw_identities:
                if _contains_identity(identity.slot, raw):
                    raise NewsReplayError(
                        f"identity slot {identity.slot} exposes a declared raw identity"
                    )
        _validate_template(
            record.summary_template,
            name=f"{record.information_id} summary_template",
            allowed_slots=allowed_slots,
            raw_identities=global_raw_identities,
        )
        for claim in record.claims:
            for field_name, value in (
                ("claim_id", claim.claim_id),
                ("predicate", claim.predicate),
                ("magnitude", claim.magnitude),
                ("unit", claim.unit),
                ("direction", claim.direction),
            ):
                if value is None:
                    continue
                for raw in global_raw_identities:
                    if _contains_identity(value, raw):
                        raise NewsReplayError(
                            f"claim {claim.claim_id} {field_name} exposes a declared raw identity"
                        )
            if claim.qualifier_template is not None:
                _validate_template(
                    claim.qualifier_template,
                    name=f"claim {claim.claim_id} qualifier_template",
                    allowed_slots=allowed_slots,
                    raw_identities=global_raw_identities,
                )

    syndication_owner: dict[str, str] = {}
    content_owner: dict[str, str] = {}
    for record in records_tuple:
        if record.revision != 1:
            continue
        owner = syndication_owner.setdefault(
            record.syndication_sha256, record.information_id
        )
        if owner != record.information_id:
            raise NewsReplayError(
                "syndicated duplicate must be collapsed before replay; "
                f"{record.information_id} duplicates {owner}"
            )
        content_owner_id = content_owner.setdefault(
            record.content_sha256, record.information_id
        )
        if content_owner_id != record.information_id:
            raise NewsReplayError(
                "exact-content duplicate must be collapsed before replay; "
                f"{record.information_id} duplicates {content_owner_id}"
            )

    identity_fields = [
        IdentityField(("story_ref",), "ENTITY", required=True),
        IdentityField(("source_id",), "SOURCE", required=True),
    ]
    identity_fields.extend(
        IdentityField(("identity_slots", slot), namespace, required=False)
        for slot, namespace in sorted(slot_namespaces.items())
    )
    profile = BlindingProfile(
        identity_fields=tuple(identity_fields),
        calendar_fields=(
            CalendarField(("published_at",), required=True),
            CalendarField(("source_event_at",), required=False),
        ),
    )

    events = tuple(
        CausalEvent.create(
            event_id=f"NEWS:{record.information_id}:r{record.revision}",
            kind="NEWS",
            event_time=record.published_at,
            available_at=record.available_at,
            ingested_at=record.ingested_at,
            source_priority=record.source_priority,
            source_sequence=record.source_sequence,
            payload=_event_payload(record),
        )
        for record in records_tuple
    )
    dataset = CausalDataset.create(manifest_sha256=manifest, events=events)

    source_records_sha256 = "sha256:" + sha256(
        _canonical_bytes([item.privileged_digest for item in records_tuple])
    ).hexdigest()
    contamination_notes_sha256 = "sha256:" + sha256(
        _canonical_bytes(
            [
                {
                    "information_id": item.information_id,
                    "revision": item.revision,
                    "note": item.residual_contamination_note,
                }
                for item in records_tuple
            ]
        )
    ).hexdigest()
    return NewsReplayBundle(
        dataset=dataset,
        blinding_profile=profile,
        source_records_sha256=source_records_sha256,
        contamination_notes_sha256=contamination_notes_sha256,
    )


def render_blinded_news(event: BlindedEvent) -> str:
    """Render a news summary only from an already-blinded event."""

    if type(event) is not BlindedEvent:
        raise TypeError("event must be exact BlindedEvent")
    kind = object.__getattribute__(event, "kind")
    if type(kind) is not str or kind != "NEWS":
        raise NewsReplayError("event is not a NEWS replay event")
    payload = object.__getattribute__(event, "payload")
    if type(payload) is not _MAPPING_PROXY_TYPE:
        raise NewsReplayError(
            "blinded news payload must remain an exact frozen mapping"
        )
    if payload.get("record_kind") != "ANONYMIZED_HISTORICAL_NEWS":
        raise NewsReplayError("NEWS event does not carry the Section 18 payload")
    template = payload.get("summary_template")
    identities = payload.get("identity_slots")
    if type(template) is not str or type(identities) is not _MAPPING_PROXY_TYPE:
        raise NewsReplayError("blinded news payload is malformed")
    fields = _template_fields(template, name="summary_template")
    keys = frozenset(identities.keys())
    unknown = sorted(set(fields) - keys)
    if unknown:
        raise NewsReplayError(f"blinded summary lacks identity slots: {unknown}")
    values: dict[str, str] = {}
    for key, value in identities.items():
        if type(key) is not str or _SLOT.fullmatch(key) is None:
            raise NewsReplayError("blinded identity slot is malformed")
        if type(value) is not str or not value.strip():
            raise NewsReplayError("blinded identity value is malformed")
        values[key] = value
    return template.format_map(values)
