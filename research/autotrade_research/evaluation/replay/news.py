"""Structured anonymized historical-news replay for product-spec item 18.

Raw historical prose is not passed through and then "cleaned" heuristically.
Instead, privileged preparation accepts a structured template made from neutral
text, declared entity mentions and declared economic facts.  Every entity is
pseudonymized with the same experiment-keyed identity primitive used by
blinded market replay.  Corrections and updates may reference only an earlier
causally published news item.

The returned dataset remains privileged replay input.  Strategy code should
receive it only through the causal blinded feeder/process boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import hmac
from types import MappingProxyType
from typing import Mapping, Sequence

from .blinding import (
    BlindingError,
    _NAMESPACE_LABELS,
    _canonical_bytes,
    _contains_absolute_date,
    _contains_identity,
    _digest,
    _freeze,
    _label_prefix,
    _sort_key,
    _text,
)
from .feeder import CausalDataset, CausalEvent

_SCHEMA_VERSION = 1
_ALLOWED_KINDS = frozenset({"NEWS", "REPORT", "NEWS_UPDATE", "NEWS_CORRECTION"})
_RELATION_KINDS = {
    "NEWS_UPDATE": "UPDATES",
    "NEWS_CORRECTION": "CORRECTS",
}
_NEWS_KEYS = frozenset(
    {
        "news_id",
        "template",
        "entities",
        "facts",
        "magnitude",
        "relevance",
        "corrects_news_id",
    }
)
_ENTITY_KEYS = frozenset({"id", "namespace", "aliases"})


class NewsReplayError(ValueError):
    """Raised when historical news cannot be anonymized causally and losslessly."""


def _exact_mapping(value: object, *, name: str) -> Mapping[str, object]:
    if type(value) is not MappingProxyType:
        raise TypeError(f"{name} must be an exact frozen mapping")
    return value


def _exact_tuple(value: object, *, name: str) -> tuple[object, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{name} must be an exact tuple")
    return value


def _scalar(value: object, *, name: str) -> str | int | bool:
    if type(value) is str:
        return _text(value, name=name)
    if type(value) is bool:
        return value
    if type(value) is int:
        return value
    raise TypeError(f"{name} must be text, integer or boolean")


def _neutral_scalar_text(value: str | int | bool) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _news_token(*, key_digest: str, experiment_id: str, raw_news_id: str) -> str:
    key = bytes.fromhex(key_digest.removeprefix("sha256:"))
    material = "\x1f".join(("NEWS_ID", experiment_id, raw_news_id)).encode("utf-8")
    return "News " + hmac.new(key, material, sha256).hexdigest().upper()


def _entity_token(
    *,
    key_digest: str,
    experiment_id: str,
    namespace: str,
    raw_identity: str,
) -> str:
    namespace = _text(namespace, name="entity namespace").upper()
    if namespace not in _NAMESPACE_LABELS:
        raise NewsReplayError("entity namespace must be a registered neutral namespace")
    raw = _text(raw_identity, name="entity id")
    token = _sort_key(
        shuffle_key=key_digest,
        experiment_id=experiment_id,
        namespace=namespace,
        raw=raw,
    ).hex().upper()
    return f"{_label_prefix(namespace)} {token}"


def _scan_neutral(
    value: object,
    *,
    raw_identities: frozenset[str],
    path: str,
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if type(key) is not str:
                raise TypeError(f"{path} key must be exact text")
            for raw in raw_identities:
                if raw and _contains_identity(key, raw):
                    raise NewsReplayError(f"{path} key contains a declared raw identity")
            if _contains_absolute_date(key):
                raise NewsReplayError(f"{path} key exposes an absolute calendar date")
            _scan_neutral(child, raw_identities=raw_identities, path=f"{path}.{key}")
        return
    if isinstance(value, tuple):
        for index, child in enumerate(value):
            _scan_neutral(
                child,
                raw_identities=raw_identities,
                path=f"{path}[{index}]",
            )
        return
    if type(value) is str:
        for raw in raw_identities:
            if raw and _contains_identity(value, raw):
                raise NewsReplayError(f"{path} repeats a declared raw identity")
        if _contains_absolute_date(value):
            raise NewsReplayError(f"{path} exposes an absolute calendar date")
        return
    if value is None or type(value) in (bool, int):
        return
    raise TypeError(f"{path} contains unsupported value type {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class _Entity:
    role: str
    namespace: str
    canonical_id: str
    aliases: tuple[str, ...]
    pseudonym: str


@dataclass(frozen=True, slots=True)
class _ParsedNews:
    source: CausalEvent
    raw_news_id: str
    public_news_id: str
    entities: Mapping[str, _Entity]
    template: tuple[Mapping[str, object], ...]
    facts: Mapping[str, str | int | bool]
    magnitude: Mapping[str, str | int | bool]
    relevance: tuple[str, ...]
    corrects_news_id: str | None


def _parse_entities(
    value: object,
    *,
    key_digest: str,
    experiment_id: str,
) -> tuple[Mapping[str, _Entity], frozenset[str]]:
    raw = _exact_mapping(value, name="entities")
    parsed: dict[str, _Entity] = {}
    raw_identities: set[str] = set()
    alias_owners: dict[tuple[str, str], str] = {}

    for role_raw, entity_raw in raw.items():
        role = _text(role_raw, name="entity role")
        if role in parsed:
            raise NewsReplayError("duplicate entity role")
        entity = _exact_mapping(entity_raw, name=f"entities.{role}")
        if set(entity) != _ENTITY_KEYS:
            raise NewsReplayError(
                f"entities.{role} keys must be exactly {sorted(_ENTITY_KEYS)}"
            )
        canonical_id = _text(entity["id"], name=f"entities.{role}.id")
        namespace = _text(
            entity["namespace"], name=f"entities.{role}.namespace"
        ).upper()
        if namespace not in _NAMESPACE_LABELS:
            raise NewsReplayError(
                f"entities.{role}.namespace must be a registered neutral namespace"
            )
        aliases_raw = _exact_tuple(
            entity["aliases"], name=f"entities.{role}.aliases"
        )
        aliases: list[str] = []
        for index, alias_value in enumerate(aliases_raw):
            alias = _text(
                alias_value, name=f"entities.{role}.aliases[{index}]"
            )
            if alias in aliases:
                raise NewsReplayError(f"entities.{role}.aliases contains duplicates")
            aliases.append(alias)

        raw_identities.add(canonical_id)
        raw_identities.update(aliases)
        for alias in (canonical_id, *aliases):
            owner_key = (namespace, alias.casefold())
            prior = alias_owners.get(owner_key)
            if prior is not None and prior != canonical_id:
                raise NewsReplayError(
                    "one news identity/alias cannot refer to multiple canonical entities"
                )
            alias_owners[owner_key] = canonical_id

        parsed[role] = _Entity(
            role=role,
            namespace=namespace,
            canonical_id=canonical_id,
            aliases=tuple(aliases),
            pseudonym=_entity_token(
                key_digest=key_digest,
                experiment_id=experiment_id,
                namespace=namespace,
                raw_identity=canonical_id,
            ),
        )

    return MappingProxyType(parsed), frozenset(raw_identities)


def _parse_scalar_mapping(value: object, *, name: str) -> Mapping[str, str | int | bool]:
    raw = _exact_mapping(value, name=name)
    parsed: dict[str, str | int | bool] = {}
    for key_raw, item in raw.items():
        key = _text(key_raw, name=f"{name} key")
        if key in parsed:
            raise NewsReplayError(f"{name} has duplicate normalized key")
        parsed[key] = _scalar(item, name=f"{name}.{key}")
    return MappingProxyType(parsed)


def _parse_template(value: object) -> tuple[Mapping[str, object], ...]:
    raw = _exact_tuple(value, name="template")
    if not raw:
        raise NewsReplayError("template must contain at least one segment")
    result: list[Mapping[str, object]] = []
    for index, segment_raw in enumerate(raw):
        segment = _exact_mapping(segment_raw, name=f"template[{index}]")
        segment_type = _text(
            segment.get("type"), name=f"template[{index}].type"
        ).lower()
        if segment_type == "text":
            if set(segment) != {"type", "text"}:
                raise NewsReplayError("text segment keys must be type,text")
            text_value = _text(
                segment["text"], name=f"template[{index}].text"
            )
            result.append(MappingProxyType({"type": "text", "text": text_value}))
        elif segment_type == "entity":
            if set(segment) != {"type", "role"}:
                raise NewsReplayError("entity segment keys must be type,role")
            role = _text(
                segment["role"], name=f"template[{index}].role"
            )
            result.append(MappingProxyType({"type": "entity", "role": role}))
        elif segment_type == "fact":
            if set(segment) != {"type", "key"}:
                raise NewsReplayError("fact segment keys must be type,key")
            key = _text(segment["key"], name=f"template[{index}].key")
            result.append(MappingProxyType({"type": "fact", "key": key}))
        else:
            raise NewsReplayError(
                f"template[{index}].type must be text, entity or fact"
            )
    return tuple(result)


def _parse_news_event(
    event: CausalEvent,
    *,
    key_digest: str,
    experiment_id: str,
) -> tuple[_ParsedNews, frozenset[str]]:
    if type(event) is not CausalEvent:
        raise TypeError("news dataset must contain exact CausalEvent")
    if event.kind not in _ALLOWED_KINDS:
        raise NewsReplayError(f"unsupported historical news kind: {event.kind}")
    payload = _exact_mapping(event.payload, name="news payload")
    keys = set(payload)
    required = _NEWS_KEYS - {"corrects_news_id"}
    if not required.issubset(keys) or not keys.issubset(_NEWS_KEYS):
        raise NewsReplayError(
            "news payload keys must match structured anonymized-news schema"
        )

    raw_news_id = _text(payload["news_id"], name="news_id")
    entities, raw_identities = _parse_entities(
        payload["entities"],
        key_digest=key_digest,
        experiment_id=experiment_id,
    )
    facts = _parse_scalar_mapping(payload["facts"], name="facts")
    magnitude = _parse_scalar_mapping(payload["magnitude"], name="magnitude")
    relevance_raw = _exact_tuple(payload["relevance"], name="relevance")
    relevance = tuple(
        _text(item, name=f"relevance[{index}]")
        for index, item in enumerate(relevance_raw)
    )
    if len(relevance) != len(set(relevance)):
        raise NewsReplayError("relevance labels must be unique")

    template = _parse_template(payload["template"])
    corrects_raw = payload.get("corrects_news_id")
    if event.kind in _RELATION_KINDS:
        corrects_news_id = _text(
            corrects_raw,
            name="corrects_news_id",
        )
    else:
        if corrects_raw is not None:
            raise NewsReplayError(
                "only NEWS_UPDATE/NEWS_CORRECTION may set corrects_news_id"
            )
        corrects_news_id = None

    return (
        _ParsedNews(
            source=event,
            raw_news_id=raw_news_id,
            public_news_id=_news_token(
                key_digest=key_digest,
                experiment_id=experiment_id,
                raw_news_id=raw_news_id,
            ),
            entities=entities,
            template=template,
            facts=facts,
            magnitude=magnitude,
            relevance=relevance,
            corrects_news_id=corrects_news_id,
        ),
        raw_identities,
    )


def _render_statement(
    item: _ParsedNews,
    *,
    all_raw_identities: frozenset[str],
) -> str:
    rendered: list[str] = []
    used_roles: set[str] = set()
    for index, segment in enumerate(item.template):
        segment_type = segment["type"]
        if segment_type == "text":
            text_value = segment["text"]
            _scan_neutral(
                text_value,
                raw_identities=all_raw_identities,
                path=f"template[{index}].text",
            )
            rendered.append(text_value)
        elif segment_type == "entity":
            role = segment["role"]
            if role not in item.entities:
                raise NewsReplayError(
                    f"template[{index}] references unknown entity role {role}"
                )
            used_roles.add(role)
            rendered.append(item.entities[role].pseudonym)
        elif segment_type == "fact":
            key = segment["key"]
            if key not in item.facts:
                raise NewsReplayError(
                    f"template[{index}] references unknown fact {key}"
                )
            rendered.append(_neutral_scalar_text(item.facts[key]))
        else:  # defensive: _parse_template already seals this
            raise NewsReplayError("unexpected parsed template segment")

    statement = "".join(rendered).strip()
    if not statement:
        raise NewsReplayError("rendered news statement is empty")

    # Every declared entity must actually be represented in the semantic text;
    # otherwise a hidden raw entity could affect metadata without an observable
    # anonymized counterpart.
    missing = set(item.entities) - used_roles
    if missing:
        raise NewsReplayError(
            f"declared news entities are not rendered: {sorted(missing)}"
        )
    return statement


@dataclass(frozen=True, slots=True)
class AnonymizedNewsReplay:
    """Privileged provenance wrapper around one anonymized causal-news dataset."""

    source_dataset_sha256: str
    experiment_id: str
    shuffle_key_commitment_sha256: str
    dataset: CausalDataset

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_dataset_sha256",
            _digest(self.source_dataset_sha256, name="source_dataset_sha256"),
        )
        object.__setattr__(
            self,
            "experiment_id",
            _text(self.experiment_id, name="experiment_id"),
        )
        object.__setattr__(
            self,
            "shuffle_key_commitment_sha256",
            _digest(
                self.shuffle_key_commitment_sha256,
                name="shuffle_key_commitment_sha256",
            ),
        )
        if type(self.dataset) is not CausalDataset:
            raise TypeError("dataset must be exact CausalDataset")

    @property
    def schema_version(self) -> int:
        return _SCHEMA_VERSION


def anonymize_news_dataset(
    *,
    dataset: CausalDataset,
    experiment_id: str,
    shuffle_key_sha256: str,
) -> AnonymizedNewsReplay:
    """Anonymize one frozen structured-news dataset without future leakage.

    Event/publication/ingest times and economic fact values are preserved on
    the privileged causal dataset.  Identity and free-text output are replaced
    before strategy-facing replay.  Update/correction relations are checked
    against causal publication order.
    """

    if type(dataset) is not CausalDataset:
        raise TypeError("dataset must be exact CausalDataset")
    experiment = _text(experiment_id, name="experiment_id")
    key_digest = _digest(shuffle_key_sha256, name="shuffle_key_sha256")
    key_commitment = "sha256:" + sha256(key_digest.encode("utf-8")).hexdigest()

    # Reconstruct through canonical CausalDataset validation so any mutated
    # graph/digest is rejected before producing a derived artifact.
    snapshot = CausalDataset(
        manifest_sha256=dataset.manifest_sha256,
        events=dataset.events,
        dataset_sha256=dataset.dataset_sha256,
    )
    if not snapshot.events:
        raise NewsReplayError("historical news replay requires at least one event")

    parsed: list[_ParsedNews] = []
    raw_identity_union: set[str] = set()
    raw_ids: set[str] = set()
    for event in snapshot.events:
        item, identities = _parse_news_event(
            event,
            key_digest=key_digest,
            experiment_id=experiment,
        )
        if item.raw_news_id in raw_ids:
            raise NewsReplayError(f"duplicate news_id: {item.raw_news_id}")
        raw_ids.add(item.raw_news_id)
        parsed.append(item)
        raw_identity_union.update(identities)

    all_raw_identities = frozenset(raw_identity_union)

    # News relation targets must already exist in the complete causal prefix at
    # the moment the update/correction is published.
    index_by_raw_id: dict[str, int] = {}
    for index, item in enumerate(parsed):
        if item.corrects_news_id is not None:
            target_index = index_by_raw_id.get(item.corrects_news_id)
            if target_index is None:
                raise NewsReplayError(
                    "news update/correction must reference an earlier published news_id"
                )
        index_by_raw_id[item.raw_news_id] = index

    public_by_raw_id = {
        item.raw_news_id: item.public_news_id for item in parsed
    }

    anonymized_events: list[CausalEvent] = []
    for item in parsed:
        statement = _render_statement(
            item,
            all_raw_identities=all_raw_identities,
        )
        _scan_neutral(
            item.facts,
            raw_identities=all_raw_identities,
            path="facts",
        )
        _scan_neutral(
            item.magnitude,
            raw_identities=all_raw_identities,
            path="magnitude",
        )
        _scan_neutral(
            item.relevance,
            raw_identities=all_raw_identities,
            path="relevance",
        )

        relation: Mapping[str, object] | None
        if item.corrects_news_id is None:
            relation = None
        else:
            relation = MappingProxyType(
                {
                    "type": _RELATION_KINDS[item.source.kind],
                    "news_ref": public_by_raw_id[item.corrects_news_id],
                }
            )

        payload = {
            "news_ref": item.public_news_id,
            "statement": statement,
            "facts": item.facts,
            "magnitude": item.magnitude,
            "relevance": item.relevance,
            "relation": relation,
        }
        anonymized_events.append(
            CausalEvent.create(
                event_id=item.public_news_id,
                kind=item.source.kind,
                event_time=item.source.event_time,
                available_at=item.source.available_at,
                ingested_at=item.source.ingested_at,
                source_priority=item.source.source_priority,
                source_sequence=item.source.source_sequence,
                payload=payload,
            )
        )

    derived_manifest = "sha256:" + sha256(
        _canonical_bytes(
            {
                "schema_version": _SCHEMA_VERSION,
                "source_manifest_sha256": snapshot.manifest_sha256,
                "source_dataset_sha256": snapshot.dataset_sha256,
                "experiment_id": experiment,
                "shuffle_key_commitment_sha256": key_commitment,
            }
        )
    ).hexdigest()
    derived = CausalDataset.create(
        manifest_sha256=derived_manifest,
        events=anonymized_events,
    )
    return AnonymizedNewsReplay(
        source_dataset_sha256=snapshot.dataset_sha256,
        experiment_id=experiment,
        shuffle_key_commitment_sha256=key_commitment,
        dataset=derived,
    )
