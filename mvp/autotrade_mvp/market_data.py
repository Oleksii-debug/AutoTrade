"""Deterministic, evidence-bound market-data normalization for the AutoTrade MVP."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_bounded_exact_decimal,
)
from ._market_payload_snapshot import PayloadSnapshotError, snapshot_market_payload
from .instruments import (
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentRegistryError,
    InstrumentVersion,
)


_MAX_RETAINED_BOOK_EVENTS_PER_STREAM = 8_192


KINDS = {
    "TRADE",
    "QUOTE",
    "BOOK_SNAPSHOT",
    "BOOK_DELTA",
    "BAR",
    "FUNDING",
    "MARK",
    "INDEX",
    "STATUS",
}


class MarketDataError(ValueError):
    """Raised when raw market data cannot be normalized safely."""


class SequenceConflict(MarketDataError):
    """Raised when one sequence identity is reused with changed content."""


def _text(value: str, field: str) -> str:
    if type(value) is not str:
        raise MarketDataError(f"{field} must be an exact string")
    normalized = value.strip()
    if not normalized:
        raise MarketDataError(f"{field} is required")
    return normalized


def _admission_text(value: object, field: str) -> str:
    """Detach RawMarketUpdate identity from caller-defined str subclasses."""

    if type(value) is not str:
        raise MarketDataError(f"{field} must be an exact string")
    normalized = value.strip()
    if not normalized:
        raise MarketDataError(f"{field} is required")
    return normalized


_MAX_BOOK_CAUSAL_TEXT_UTF8_BYTES = 1024


def _book_causal_text(value: object, field: str) -> str:
    """Admit bounded exact provider book identity text without caller callbacks."""

    if type(value) is not str:
        raise MarketDataError(f"{field} must be an exact string")
    normalized = value.strip()
    if not normalized:
        raise MarketDataError(f"{field} is required")
    try:
        encoded = normalized.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise MarketDataError(f"{field} must be valid UTF-8 text") from error
    if len(encoded) > _MAX_BOOK_CAUSAL_TEXT_UTF8_BYTES:
        raise MarketDataError(
            f"{field} exceeds the supported book-causality text resource envelope"
        )
    return normalized


def _admission_instant(value: object, field: str) -> datetime:
    """Normalize one exact datetime without executing caller timezone code."""

    if type(value) is not datetime:
        raise MarketDataError(f"{field} must be an exact timezone-aware datetime")
    if type(value.tzinfo) is not timezone:
        raise MarketDataError(
            f"{field} must use an exact datetime with a built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise MarketDataError(f"{field} must be timezone-aware")
    try:
        normalized = datetime.astimezone(value, timezone.utc)
    except (OverflowError, ValueError, TypeError) as error:
        raise MarketDataError(
            f"{field} must have a deterministic timezone"
        ) from error
    if type(normalized) is not datetime:
        raise MarketDataError(f"{field} must normalize to an exact datetime")
    return normalized


def _admission_sequence(value: object, field: str) -> int | None:
    """Accept only exact built-in integer sequence identity at admission."""

    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise MarketDataError(f"{field} must be an exact non-negative integer")
    return value


def _instant(value: datetime, field: str) -> datetime:
    return _admission_instant(value, field)


def _sequence(value: int | None, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MarketDataError(f"{field} must be a non-negative integer")
    return value


def _decimal(value: Decimal | str | int, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise MarketDataError(f"{field} must use exact decimal input")
    try:
        result = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, ValueError, TypeError) as error:
        raise MarketDataError(
            f"{field} must be a finite decimal within the supported resource envelope"
        ) from error
    if positive and result <= 0:
        raise MarketDataError(f"{field} must be positive")
    return result


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise MarketDataError(
            "market decimal exceeds the supported exact-decimal resource envelope"
        ) from error


def _utc_text(value: datetime) -> str:
    return _admission_instant(value, "timestamp").isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not value:
        raise MarketDataError("raw_evidence_ref is required")
    detached: dict[str, object] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise MarketDataError("raw_evidence_ref keys must be exact strings")
        detached[key] = item
    required = {"artifact_id", "sha256", "observed_at"}
    allowed = required | {"source_uri", "rights_id"}
    keys = set(detached)
    value = detached
    if required - keys:
        raise MarketDataError("raw_evidence_ref is missing required fields")
    if keys - allowed:
        raise MarketDataError("raw_evidence_ref contains unknown fields")

    artifact_id = _admission_text(value["artifact_id"], "artifact_id")
    try:
        UUID(artifact_id)
    except (ValueError, TypeError, AttributeError) as error:
        raise MarketDataError("raw evidence artifact_id must be a UUID") from error

    digest = _admission_text(value["sha256"], "sha256")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
        raise MarketDataError("raw evidence sha256 must be a canonical SHA-256 digest")

    observed_at = _admission_text(value["observed_at"], "observed_at")
    if not observed_at.endswith("Z"):
        raise MarketDataError("raw evidence observed_at must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(observed_at[:-1] + "+00:00")
    except ValueError as error:
        raise MarketDataError("raw evidence observed_at must be an ISO date-time") from error
    if parsed.utcoffset() != timedelta(0):
        raise MarketDataError("raw evidence observed_at must be UTC")

    normalized: dict[str, object] = {
        "artifact_id": artifact_id,
        "sha256": digest,
        "observed_at": observed_at,
    }
    if "source_uri" in value:
        source_uri = _admission_text(value["source_uri"], "source_uri")
        if not urlsplit(source_uri).scheme:
            raise MarketDataError("raw evidence source_uri must be an absolute URI")
        normalized["source_uri"] = source_uri
    if "rights_id" in value:
        normalized["rights_id"] = _admission_text(value["rights_id"], "rights_id")
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class RawMarketUpdate:
    provider_id: str
    venue_id: str
    provider_symbol: str
    kind: str
    source_event_at: datetime
    available_at: datetime
    ingested_at: datetime
    availability_basis: str
    revision: int
    payload: Mapping[str, Any]
    raw_evidence_ref: Mapping[str, object]
    source_sequence: int | None = None
    stream_generation: int | None = None
    sequence_stream: str | None = None

    def __post_init__(self) -> None:
        for field in ("provider_id", "venue_id", "provider_symbol", "availability_basis"):
            object.__setattr__(
                self,
                field,
                _admission_text(getattr(self, field), field),
            )
        kind = _admission_text(self.kind, "kind").upper()
        if kind not in KINDS:
            raise MarketDataError("kind is unsupported")
        object.__setattr__(self, "kind", kind)
        source = _admission_instant(self.source_event_at, "source_event_at")
        available = _admission_instant(self.available_at, "available_at")
        ingested = _admission_instant(self.ingested_at, "ingested_at")
        if source > available:
            raise MarketDataError("source_event_at must not be after available_at")
        if available > ingested:
            raise MarketDataError("available_at must not be after ingested_at")
        object.__setattr__(self, "source_event_at", source)
        object.__setattr__(self, "available_at", available)
        object.__setattr__(self, "ingested_at", ingested)
        revision = _admission_sequence(self.revision, "revision")
        if revision is None:
            raise MarketDataError("revision is required")
        object.__setattr__(self, "revision", revision)
        object.__setattr__(
            self,
            "source_sequence",
            _admission_sequence(self.source_sequence, "source_sequence"),
        )
        object.__setattr__(
            self,
            "stream_generation",
            _admission_sequence(self.stream_generation, "stream_generation"),
        )
        try:
            payload_snapshot = snapshot_market_payload(self.payload)
        except PayloadSnapshotError as error:
            raise MarketDataError(str(error)) from error
        object.__setattr__(self, "payload", payload_snapshot)
        normalized_evidence = _evidence(self.raw_evidence_ref)
        evidence_observed_text = normalized_evidence["observed_at"]
        assert isinstance(evidence_observed_text, str)
        evidence_observed = datetime.fromisoformat(
            evidence_observed_text[:-1] + "+00:00"
        ).astimezone(timezone.utc)
        if evidence_observed < source:
            raise MarketDataError(
                "raw evidence cannot be observed before source_event_at"
            )
        if evidence_observed > ingested:
            raise MarketDataError(
                "raw evidence cannot be observed after ingested_at"
            )
        object.__setattr__(self, "raw_evidence_ref", normalized_evidence)
        if self.sequence_stream is not None:
            object.__setattr__(
                self,
                "sequence_stream",
                _admission_text(self.sequence_stream, "sequence_stream"),
            )


@dataclass(frozen=True)
class NormalizedMarketEvent:
    event_id: str
    instrument_version: str
    kind: str
    source_event_at: datetime
    available_at: datetime
    availability_basis: str
    ingested_at: datetime
    revision: int
    payload_json: str
    quality_flags: tuple[str, ...]
    raw_evidence_ref: Mapping[str, object]
    source_sequence: int | None = None
    stream_generation: int | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return json.loads(self.payload_json)

    def to_contract_dict(self) -> dict[str, Any]:
        result = {
            "event_id": self.event_id,
            "instrument_version": self.instrument_version,
            "kind": self.kind,
            "source_event_at": _utc_text(self.source_event_at),
            "available_at": _utc_text(self.available_at),
            "availability_basis": self.availability_basis,
            "ingested_at": _utc_text(self.ingested_at),
            "revision": str(self.revision),
            "payload": self.payload,
            "quality_flags": list(self.quality_flags),
            "raw_evidence_ref": dict(self.raw_evidence_ref),
        }
        if self.source_sequence is not None:
            result["source_sequence"] = str(self.source_sequence)
        if self.stream_generation is not None:
            result["stream_generation"] = str(self.stream_generation)
        return result


@dataclass(frozen=True)
class BookStreamPolicyBinding:
    """Static product binding to one canonical provider continuity policy.

    This value carries identity only. Ordinary composition cannot inject a
    callable evaluator; MarketNormalizer resolves the reviewed implementation
    from the exact policy_id and rejects unknown policy identities.
    """

    provider_id: str
    venue_id: str
    stream: str
    policy_id: str

    def __post_init__(self) -> None:
        for field_name in ("provider_id", "venue_id", "stream", "policy_id"):
            object.__setattr__(
                self,
                field_name,
                _admission_text(getattr(self, field_name), field_name),
            )


def _issue_book_stream_policy_binding(
    *,
    provider_id: str,
    venue_id: str,
    stream: str,
    policy_id: str,
) -> BookStreamPolicyBinding:
    """Internal adapter factory for one reviewed provider-policy identity."""

    return BookStreamPolicyBinding(
        provider_id=provider_id,
        venue_id=venue_id,
        stream=stream,
        policy_id=policy_id,
    )


def _resolve_book_stream_policy_evaluator(policy_id: str) -> object:
    """Resolve only reviewed product-owned provider continuity implementations."""

    admitted_policy = _admission_text(policy_id, "policy_id")
    if admitted_policy == "BINANCE_SPOT_DIFF_DEPTH_V1":
        # Lazy import avoids an import cycle while keeping evaluator selection
        # outside caller-controlled composition.
        from .binance_spot import _evaluate_binance_spot_depth_range

        return _evaluate_binance_spot_depth_range
    raise MarketDataError(
        "book stream policy_id is not a canonical provider policy"
    )


@dataclass(frozen=True)
class QualifiedBookRangeAdmission:
    """Deterministic provider-policy decision bound to one normalized event.

    This value is evidence, not authority by possession.  MarketNormalizer
    recomputes the decision with the registered provider evaluator before any
    preview/application, so caller-authored instances cannot relabel a GAP as
    APPLY.
    """

    policy_id: str
    event_id: str
    disposition: str
    prior_sequence: int
    first_sequence: int
    last_sequence: int
    next_sequence: int | None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "policy_id",
            _admission_text(self.policy_id, "policy_id"),
        )
        event_id = _admission_text(self.event_id, "event_id")
        try:
            UUID(event_id)
        except (ValueError, TypeError, AttributeError) as error:
            raise MarketDataError("event_id must be a UUID") from error
        object.__setattr__(self, "event_id", event_id)
        disposition = _admission_text(self.disposition, "disposition").upper()
        if disposition not in {"APPLY", "DISCARD", "GAP"}:
            raise MarketDataError("range disposition is unsupported")
        object.__setattr__(self, "disposition", disposition)
        prior = _admission_sequence(self.prior_sequence, "prior_sequence")
        first = _admission_sequence(self.first_sequence, "first_sequence")
        last = _admission_sequence(self.last_sequence, "last_sequence")
        if prior is None or first is None or last is None:
            raise MarketDataError("range admission sequences are required")
        if first > last:
            raise MarketDataError("first_sequence must not exceed last_sequence")
        object.__setattr__(self, "prior_sequence", prior)
        object.__setattr__(self, "first_sequence", first)
        object.__setattr__(self, "last_sequence", last)
        next_sequence = _admission_sequence(self.next_sequence, "next_sequence")
        if disposition == "APPLY":
            if next_sequence is None:
                raise MarketDataError("APPLY range admission requires next_sequence")
        elif next_sequence is not None:
            raise MarketDataError(
                "non-APPLY range admission must not carry next_sequence"
            )
        object.__setattr__(self, "next_sequence", next_sequence)


def _issue_qualified_book_range_admission(
    *,
    policy_id: str,
    event_id: str,
    disposition: str,
    prior_sequence: int,
    first_sequence: int,
    last_sequence: int,
    next_sequence: int | None,
) -> QualifiedBookRangeAdmission:
    """Construct a policy decision value; the coordinator still recomputes it."""

    return QualifiedBookRangeAdmission(
        policy_id=policy_id,
        event_id=event_id,
        disposition=disposition,
        prior_sequence=prior_sequence,
        first_sequence=first_sequence,
        last_sequence=last_sequence,
        next_sequence=next_sequence,
    )


class MarketNormalizer:
    """Normalize provider-shaped events without inventing missing market facts."""

    def __init__(
        self,
        registry: InstrumentRegistry,
        *,
        max_available_age: timedelta = timedelta(seconds=5),
        max_book_age: timedelta = timedelta(seconds=5),
        max_book_levels_per_side: int = 10_000,
        max_retained_book_events_per_stream: int = _MAX_RETAINED_BOOK_EVENTS_PER_STREAM,
        book_stream_policies: tuple[BookStreamPolicyBinding, ...] = (),
    ) -> None:
        if type(registry) is not InstrumentRegistry:
            raise TypeError("registry must be an exact InstrumentRegistry")
        if type(max_available_age) is not timedelta or max_available_age <= timedelta(0):
            raise MarketDataError("max_available_age must be an exact positive timedelta")
        if type(max_book_age) is not timedelta or max_book_age <= timedelta(0):
            raise MarketDataError("max_book_age must be an exact positive timedelta")
        if type(max_book_levels_per_side) is not int or max_book_levels_per_side <= 0:
            raise MarketDataError("max_book_levels_per_side must be an exact positive integer")
        if (
            type(max_retained_book_events_per_stream) is not int
            or not 0 < max_retained_book_events_per_stream <= _MAX_RETAINED_BOOK_EVENTS_PER_STREAM
        ):
            raise MarketDataError(
                "max_retained_book_events_per_stream must be an exact positive integer "
                "within the supported retention envelope"
            )
        self._registry = registry
        if type(book_stream_policies) is not tuple:
            raise MarketDataError("book_stream_policies must be an exact tuple")
        self._book_stream_policies: dict[tuple[str, str, str], str] = {}
        self._book_stream_policy_evaluators: dict[
            tuple[str, str, str], object
        ] = {}
        for binding in book_stream_policies:
            if type(binding) is not BookStreamPolicyBinding:
                raise MarketDataError(
                    "book stream policy entries must be exact BookStreamPolicyBinding values"
                )
            key = (binding.provider_id, binding.venue_id, binding.stream)
            if key in self._book_stream_policies:
                raise MarketDataError("duplicate book stream policy binding")
            self._book_stream_policies[key] = binding.policy_id
            self._book_stream_policy_evaluators[key] = (
                _resolve_book_stream_policy_evaluator(binding.policy_id)
            )
        self._max_available_age = max_available_age
        self._max_book_age = max_book_age
        self._max_book_levels_per_side = max_book_levels_per_side
        self._max_retained_book_events_per_stream = (
            max_retained_book_events_per_stream
        )
        self._last_sequence: dict[tuple[str, str, str, str], int] = {}
        self._seen_sequence_ids: set[tuple[str, str, str, str, int]] = set()
        self._last_revision: dict[tuple[str, str, str, str, int], int] = {}
        self._last_revision_available_at: dict[
            tuple[str, str, str, str, int], datetime
        ] = {}
        self._sequence_source_identity: dict[
            tuple[str, str, str, str, int], tuple[str, str, datetime]
        ] = {}
        self._seen_revision: dict[
            tuple[str, str, str, str, int, int], tuple[str, str]
        ] = {}
        self._book_state: dict[tuple[str, str, str, str], str] = {}
        self._book_last_available_at: dict[
            tuple[str, str, str, str], datetime
        ] = {}
        self._book_levels: dict[
            tuple[str, str, str, str],
            tuple[dict[str, str], dict[str, str]],
        ] = {}
        self._provider_book_cursor: dict[
            tuple[str, str, str, str], int
        ] = {}
        self._provider_book_baseline_cursor: dict[
            tuple[str, str, str, str], int
        ] = {}
        self._book_event_keys: dict[
            str, tuple[str, str, str, str]
        ] = {}
        self._book_event_contracts: dict[str, str] = {}
        self._book_event_order: dict[
            tuple[str, str, str, str], deque[str]
        ] = {}
        self._active_book_generation: dict[
            tuple[str, str, str, str], int
        ] = {}
        self._generic_book_generation: dict[
            tuple[str, str, str, str], int
        ] = {}
        self._invalidated_provider_book_generations: set[
            tuple[str, str, str, str]
        ] = set()
        self._provider_book_causal_order: dict[
            tuple[str, str, str, str, int], deque[tuple[Any, ...]]
        ] = {}
        self._provider_book_sequence_refcounts: dict[
            tuple[Any, ...], int
        ] = {}
        self._provider_book_evicted_sequence_watermark: dict[
            tuple[str, str, str, str, int, str], int
        ] = {}
        self._provider_book_evicted_revision_watermark: dict[
            tuple[Any, ...], int
        ] = {}

    @staticmethod
    def _book_key(
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str,
    ) -> tuple[str, str, str, str]:
        return (
            _text(provider_id, "provider_id"),
            _text(venue_id, "venue_id"),
            _text(provider_symbol, "provider_symbol"),
            _text(stream, "stream"),
        )

    @staticmethod
    def _provider_book_generation_key(
        key: tuple[str, str, str, str],
        generation: int,
    ) -> tuple[str, str, str, str, int]:
        return (*key, generation)

    def _require_provider_book_causal_window(
        self,
        *,
        key: tuple[str, str, str, str],
        generation: int,
        kind: str,
        sequence_identity: tuple[Any, ...],
        revision_identity: tuple[Any, ...],
        source_sequence: int,
        revision: int,
    ) -> None:
        """Reject provider-book facts older than the bounded retained window."""

        generation_key = self._provider_book_generation_key(key, generation)
        sequence_watermark = self._provider_book_evicted_sequence_watermark.get(
            (*generation_key, kind)
        )
        if (
            sequence_identity not in self._seen_sequence_ids
            and sequence_watermark is not None
            and source_sequence <= sequence_watermark
        ):
            raise SequenceConflict(
                "provider book sequence is outside the retained causal window"
            )

        revision_watermark = self._provider_book_evicted_revision_watermark.get(
            sequence_identity
        )
        if (
            revision_identity not in self._seen_revision
            and revision_watermark is not None
            and revision <= revision_watermark
        ):
            raise SequenceConflict(
                "provider book revision is outside the retained causal window"
            )

    def _retain_provider_book_causal_revision(
        self,
        *,
        key: tuple[str, str, str, str],
        generation: int,
        sequence_identity: tuple[Any, ...],
        revision_identity: tuple[Any, ...],
    ) -> None:
        """Bound exact sequence/revision authority for one active provider generation."""

        generation_key = self._provider_book_generation_key(key, generation)
        order = self._provider_book_causal_order.setdefault(
            generation_key,
            deque(),
        )
        order.append(revision_identity)
        self._provider_book_sequence_refcounts[sequence_identity] = (
            self._provider_book_sequence_refcounts.get(sequence_identity, 0) + 1
        )

        while len(order) > self._max_retained_book_events_per_stream:
            evicted_revision_identity = order.popleft()
            evicted_sequence_identity = evicted_revision_identity[:-1]
            evicted_revision = evicted_revision_identity[-1]
            if type(evicted_revision) is not int:
                raise MarketDataError(
                    "provider book causal revision identity is invalid"
                )
            self._seen_revision.pop(evicted_revision_identity, None)

            remaining = (
                self._provider_book_sequence_refcounts.get(
                    evicted_sequence_identity,
                    0,
                )
                - 1
            )
            if remaining > 0:
                self._provider_book_sequence_refcounts[
                    evicted_sequence_identity
                ] = remaining
                prior_revision_watermark = (
                    self._provider_book_evicted_revision_watermark.get(
                        evicted_sequence_identity
                    )
                )
                self._provider_book_evicted_revision_watermark[
                    evicted_sequence_identity
                ] = (
                    evicted_revision
                    if prior_revision_watermark is None
                    else max(prior_revision_watermark, evicted_revision)
                )
                continue

            self._provider_book_sequence_refcounts.pop(
                evicted_sequence_identity,
                None,
            )
            self._seen_sequence_ids.discard(evicted_sequence_identity)
            self._last_revision.pop(evicted_sequence_identity, None)
            self._last_revision_available_at.pop(
                evicted_sequence_identity,
                None,
            )
            self._sequence_source_identity.pop(
                evicted_sequence_identity,
                None,
            )
            self._provider_book_evicted_revision_watermark.pop(
                evicted_sequence_identity,
                None,
            )

            qualified_stream_kind = evicted_sequence_identity[3]
            source_sequence = evicted_sequence_identity[-1]
            prefix = f"{key[3]}:"
            if (
                type(qualified_stream_kind) is not str
                or not qualified_stream_kind.startswith(prefix)
                or type(source_sequence) is not int
            ):
                raise MarketDataError(
                    "provider book causal sequence identity is invalid"
                )
            evicted_kind = qualified_stream_kind[len(prefix) :]
            if evicted_kind not in {"BOOK_SNAPSHOT", "BOOK_DELTA"}:
                raise MarketDataError(
                    "provider book causal sequence kind is invalid"
                )
            watermark_key = (*generation_key, evicted_kind)
            prior_sequence_watermark = (
                self._provider_book_evicted_sequence_watermark.get(
                    watermark_key
                )
            )
            self._provider_book_evicted_sequence_watermark[watermark_key] = (
                source_sequence
                if prior_sequence_watermark is None
                else max(prior_sequence_watermark, source_sequence)
            )

    def _clear_provider_book_causal_state(
        self,
        key: tuple[str, str, str, str],
        generation: int,
    ) -> None:
        """Release superseded-generation detail after the generation fence advances."""

        generation_key = self._provider_book_generation_key(key, generation)
        order = self._provider_book_causal_order.pop(generation_key, ())
        retained_sequences = {
            revision_identity[:-1]
            for revision_identity in order
        }
        for revision_identity in order:
            self._seen_revision.pop(revision_identity, None)
        for sequence_identity in retained_sequences:
            self._seen_sequence_ids.discard(sequence_identity)
            self._last_revision.pop(sequence_identity, None)
            self._last_revision_available_at.pop(sequence_identity, None)
            self._sequence_source_identity.pop(sequence_identity, None)
            self._provider_book_sequence_refcounts.pop(
                sequence_identity,
                None,
            )
            self._provider_book_evicted_revision_watermark.pop(
                sequence_identity,
                None,
            )

        for kind in ("BOOK_SNAPSHOT", "BOOK_DELTA"):
            self._provider_book_evicted_sequence_watermark.pop(
                (*generation_key, kind),
                None,
            )
        self._last_sequence.pop(
            (
                key[0],
                key[1],
                key[2],
                f"{key[3]}#GENERATION:{generation}",
            ),
            None,
        )

    def _clear_retained_book_events(
        self,
        key: tuple[str, str, str, str],
    ) -> None:
        """Drop stale-generation event authority for one provider book stream."""

        order = self._book_event_order.pop(key, ())
        for event_id in order:
            self._book_event_keys.pop(event_id, None)
            self._book_event_contracts.pop(event_id, None)

    def _retain_book_event_identity(
        self,
        event: NormalizedMarketEvent,
        key: tuple[str, str, str, str],
    ) -> None:
        """Retain one canonical event identity inside a deterministic FIFO envelope."""

        contract = _canonical(event.to_contract_dict())
        existing_key = self._book_event_keys.get(event.event_id)
        if existing_key is not None:
            if (
                existing_key != key
                or self._book_event_contracts.get(event.event_id) != contract
            ):
                raise MarketDataError("normalized book event identity collision")
            return

        order = self._book_event_order.setdefault(key, deque())
        order.append(event.event_id)
        self._book_event_keys[event.event_id] = key
        self._book_event_contracts[event.event_id] = contract
        while len(order) > self._max_retained_book_events_per_stream:
            evicted_event_id = order.popleft()
            self._book_event_keys.pop(evicted_event_id, None)
            self._book_event_contracts.pop(evicted_event_id, None)

    def book_state(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
    ) -> str:
        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        return self._book_state.get(key, "UNINITIALIZED")

    def require_executable_book(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
        as_of: datetime | None = None,
    ) -> None:
        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        state = self._book_state.get(key, "UNINITIALIZED")
        if state != "READY":
            raise MarketDataError(f"book state is {state}; new risk is blocked")
        if as_of is None:
            raise MarketDataError(
                "as_of is required to prove current executable-book freshness"
            )
        decision_at = _admission_instant(as_of, "as_of")
        last_available_at = self._book_last_available_at.get(key)
        if last_available_at is None:
            raise MarketDataError(
                "book freshness cut is unavailable; new risk is blocked"
            )
        if decision_at < last_available_at:
            raise MarketDataError(
                "as_of precedes the accepted book availability cut; new risk is blocked"
            )
        if decision_at - last_available_at > self._max_book_age:
            raise MarketDataError("book data is stale; new risk is blocked")
        if key not in self._book_levels:
            raise MarketDataError(
                "materialized executable book is unavailable; new risk is blocked"
            )

    def executable_book(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
        as_of: datetime,
    ) -> dict[str, list[dict[str, str]]]:
        """Return a detached, freshness-gated materialized book view."""

        self.require_executable_book(
            provider_id=provider_id,
            venue_id=venue_id,
            provider_symbol=provider_symbol,
            stream=stream,
            as_of=as_of,
        )
        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        bids, asks = self._book_levels[key]
        return {
            "bids": [
                {"price": price, "quantity": bids[price]}
                for price in sorted(bids, key=Decimal, reverse=True)
            ],
            "asks": [
                {"price": price, "quantity": asks[price]}
                for price in sorted(asks, key=Decimal)
            ],
        }

    def _book_depth_exceeds_limit(
        self,
        book: tuple[dict[str, str], dict[str, str]],
    ) -> bool:
        return any(
            len(side) > self._max_book_levels_per_side
            for side in book
        )

    @staticmethod
    def _materialized_book(
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        bids = {
            level["price"]: level["quantity"]
            for level in payload["bids"]
        }
        asks = {
            level["price"]: level["quantity"]
            for level in payload["asks"]
        }
        return bids, asks

    @staticmethod
    def _apply_book_delta(
        current: tuple[dict[str, str], dict[str, str]],
        payload: Mapping[str, Any],
    ) -> tuple[dict[str, str], dict[str, str]]:
        bids = dict(current[0])
        asks = dict(current[1])
        for side, levels in ((bids, payload["bids"]), (asks, payload["asks"])):
            for level in levels:
                price = level["price"]
                quantity = level["quantity"]
                if quantity == "0":
                    side.pop(price, None)
                else:
                    side[price] = quantity
        return bids, asks

    @staticmethod
    def _materialized_book_is_crossed(
        book: tuple[dict[str, str], dict[str, str]],
    ) -> bool:
        bids, asks = book
        if not bids or not asks:
            return False
        return max(map(Decimal, bids)) > min(map(Decimal, asks))

    def begin_provider_book_generation(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        generation: int,
        stream: str = "book",
        policy_id: str,
    ) -> None:
        """Bind the exact active provider generation before any book ingress."""

        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        expected_policy = self._book_stream_policies.get(
            (key[0], key[1], key[3])
        )
        admitted_policy = _admission_text(policy_id, "policy_id")
        if expected_policy is None or admitted_policy != expected_policy:
            raise MarketDataError("provider book policy binding does not match")
        admitted_generation = _admission_sequence(generation, "generation")
        if admitted_generation is None:
            raise MarketDataError("generation is required")
        prior = self._active_book_generation.get(key)
        if prior is not None and admitted_generation <= prior:
            raise MarketDataError(
                "provider book generation must strictly increase"
            )
        if prior is not None:
            self._clear_provider_book_causal_state(key, prior)
        self._clear_retained_book_events(key)
        self._invalidated_provider_book_generations.discard(key)
        self._active_book_generation[key] = admitted_generation
        self._book_state[key] = "UNINITIALIZED"
        self._book_last_available_at.pop(key, None)
        self._book_levels.pop(key, None)
        self._provider_book_cursor.pop(key, None)
        self._provider_book_baseline_cursor.pop(key, None)

    def provider_book_generation(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
    ) -> int | None:
        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        return self._active_book_generation.get(key)

    def invalidate_provider_book_stream(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
        policy_id: str,
    ) -> None:
        """Revoke executable provider-book state before reconnect/rebootstrap."""

        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        expected_policy = self._book_stream_policies.get(
            (key[0], key[1], key[3])
        )
        admitted_policy = _admission_text(policy_id, "policy_id")
        if expected_policy is None or admitted_policy != expected_policy:
            raise MarketDataError("provider book policy binding does not match")
        if key in self._active_book_generation:
            self._invalidated_provider_book_generations.add(key)
        self._book_state[key] = "UNINITIALIZED"
        self._book_last_available_at.pop(key, None)
        self._book_levels.pop(key, None)
        self._provider_book_cursor.pop(key, None)
        self._provider_book_baseline_cursor.pop(key, None)
        self._clear_retained_book_events(key)

    def provider_book_cursor(
        self,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
    ) -> int:
        """Return the retained provider-qualified cursor for composition only."""

        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        cursor = self._provider_book_cursor.get(key)
        if cursor is None:
            raise MarketDataError("provider book cursor is unavailable")
        return cursor

    def register_provider_book_snapshot(
        self,
        event: NormalizedMarketEvent,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
        policy_id: str,
        cursor_sequence: int,
    ) -> None:
        """Install a provider-policy snapshot baseline without authorizing risk."""

        if type(event) is not NormalizedMarketEvent:
            raise MarketDataError("event must be an exact NormalizedMarketEvent")
        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        expected_policy = self._book_stream_policies.get(
            (key[0], key[1], key[3])
        )
        admitted_policy = _admission_text(policy_id, "policy_id")
        if expected_policy is None or admitted_policy != expected_policy:
            raise MarketDataError("provider book policy binding does not match")
        if self._book_event_keys.get(event.event_id) != key:
            raise MarketDataError("book event does not belong to this stream")
        retained = self._book_event_contracts.get(event.event_id)
        if retained is None or retained != _canonical(event.to_contract_dict()):
            raise MarketDataError("book event differs from retained normalized identity")
        active_generation = self._active_book_generation.get(key)
        if active_generation is None:
            raise MarketDataError(
                "provider book stream has no active generation"
            )
        if event.stream_generation != active_generation:
            raise MarketDataError(
                "book event belongs to a superseded stream generation"
            )
        if key in self._invalidated_provider_book_generations:
            raise MarketDataError(
                "invalidated provider book stream requires a newer generation"
            )
        if self._book_state.get(key, "UNINITIALIZED") != "UNINITIALIZED":
            raise MarketDataError(
                "provider book snapshot baseline requires UNINITIALIZED stream"
            )
        if event.kind != "BOOK_SNAPSHOT":
            raise MarketDataError("provider book baseline requires BOOK_SNAPSHOT")
        if "BOOK_PROVIDER_CONTINUITY_PENDING" not in event.quality_flags:
            raise MarketDataError("snapshot is not pending provider continuity")
        if "BOOK_RANGE_CONTINUITY_UNVERIFIED" in event.quality_flags:
            raise MarketDataError(
                "snapshot provider continuity is not qualified"
            )
        if any(
            flag in event.quality_flags
            for flag in (
                "STALE",
                "CORRECTION",
                "DUPLICATE",
                "OUT_OF_ORDER",
                "REVISION_BASE_MISSING",
                "REVISION_GAP",
                "OUT_OF_ORDER_REVISION",
            )
        ):
            raise MarketDataError("snapshot is not eligible as a provider baseline")
        cursor = _admission_sequence(cursor_sequence, "cursor_sequence")
        if cursor is None or event.source_sequence != cursor:
            raise MarketDataError(
                "provider snapshot cursor must equal event source_sequence"
            )
        materialized = self._materialized_book(event.payload)
        if self._materialized_book_is_crossed(materialized):
            raise MarketDataError("provider snapshot baseline is crossed")
        self._book_levels[key] = materialized
        self._provider_book_cursor[key] = cursor
        self._provider_book_baseline_cursor[key] = cursor
        self._book_state[key] = "BOOTSTRAPPING"
        # Snapshot availability is a causal prerequisite for every buffered
        # delta replay. Preserve it as a non-executable lower bound so the
        # reconstructed book cannot appear available before its baseline.
        self._book_last_available_at[key] = event.available_at

    def _derive_qualified_book_range_admission(
        self,
        event: NormalizedMarketEvent,
        *,
        key: tuple[str, str, str, str],
    ) -> QualifiedBookRangeAdmission:
        """Recompute provider continuity from retained event and current cursor."""

        if type(event) is not NormalizedMarketEvent:
            raise MarketDataError("event must be an exact NormalizedMarketEvent")
        if self._book_event_keys.get(event.event_id) != key:
            raise MarketDataError("book event does not belong to this stream")
        retained = self._book_event_contracts.get(event.event_id)
        if retained is None or retained != _canonical(event.to_contract_dict()):
            raise MarketDataError("book event differs from retained normalized identity")

        policy_key = (key[0], key[1], key[3])
        expected_policy = self._book_stream_policies.get(policy_key)
        evaluator = self._book_stream_policy_evaluators.get(policy_key)
        if expected_policy is None or evaluator is None:
            raise MarketDataError("provider book policy binding is unavailable")

        active_generation = self._active_book_generation.get(key)
        if active_generation is None:
            raise MarketDataError("provider book stream has no active generation")
        if event.stream_generation != active_generation:
            raise MarketDataError(
                "book event belongs to a superseded stream generation"
            )
        if event.kind != "BOOK_DELTA":
            raise MarketDataError("qualified provider range requires BOOK_DELTA")
        if "BOOK_PROVIDER_CONTINUITY_PENDING" not in event.quality_flags:
            raise MarketDataError("delta is not pending provider continuity")

        payload = event.payload
        try:
            first_text = payload["first_sequence"]
            last_text = payload["last_sequence"]
            if type(first_text) is not str or type(last_text) is not str:
                raise TypeError
            first_sequence = int(first_text)
            last_sequence = int(last_text)
        except (KeyError, TypeError, ValueError) as error:
            raise MarketDataError(
                "qualified range event must carry exact first/last sequence"
            ) from error
        if (
            str(first_sequence) != first_text
            or str(last_sequence) != last_text
            or first_sequence < 0
            or last_sequence < 0
            or first_sequence > last_sequence
            or event.source_sequence != last_sequence
        ):
            raise MarketDataError("qualified range event sequence identity is invalid")

        cursor = self._provider_book_cursor.get(key)
        if cursor is None:
            raise MarketDataError("provider book cursor is unavailable")
        state = self._book_state.get(key, "UNINITIALIZED")
        if state == "BOOTSTRAPPING":
            bootstrap = True
        elif state == "READY":
            bootstrap = False
        else:
            raise MarketDataError(
                f"provider book state {state} cannot accept a ranged event"
            )

        try:
            admission = evaluator(
                policy_id=expected_policy,
                event_id=event.event_id,
                provider_symbol=key[2],
                prior_sequence=cursor,
                first_sequence=first_sequence,
                last_sequence=last_sequence,
                bootstrap=bootstrap,
            )
        except (TypeError, ValueError) as error:
            raise MarketDataError("provider book policy evaluation failed") from error
        if type(admission) is not QualifiedBookRangeAdmission:
            raise MarketDataError(
                "provider book policy evaluator returned an invalid decision"
            )
        if (
            admission.policy_id != expected_policy
            or admission.event_id != event.event_id
            or admission.prior_sequence != cursor
            or admission.first_sequence != first_sequence
            or admission.last_sequence != last_sequence
        ):
            raise MarketDataError(
                "provider book policy decision does not match retained event identity"
            )
        if (
            admission.disposition == "APPLY"
            and admission.next_sequence != last_sequence
        ):
            raise MarketDataError(
                "provider book policy APPLY must advance to the event final sequence"
            )
        return admission

    @staticmethod
    def _require_policy_equivalent_admission(
        supplied: QualifiedBookRangeAdmission | None,
        derived: QualifiedBookRangeAdmission,
    ) -> None:
        """Treat caller decision values only as optional assertions, never authority."""

        if supplied is None:
            return
        if type(supplied) is not QualifiedBookRangeAdmission:
            raise MarketDataError(
                "admission must be an exact QualifiedBookRangeAdmission"
            )
        if supplied != derived:
            raise MarketDataError(
                "caller range admission differs from registered provider policy"
            )

    def preview_qualified_book_range(
        self,
        event: NormalizedMarketEvent,
        admission: QualifiedBookRangeAdmission | None = None,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
    ) -> dict[str, list[dict[str, str]]]:
        """Preview the registered provider policy's APPLY candidate without mutation."""

        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        derived = self._derive_qualified_book_range_admission(event, key=key)
        self._require_policy_equivalent_admission(admission, derived)
        admission = derived
        if admission.disposition != "APPLY":
            raise MarketDataError("only APPLY range admission has a candidate book")
        if any(
            flag in event.quality_flags
            for flag in (
                "STALE",
                "CORRECTION",
                "DUPLICATE",
                "REVISION_BASE_MISSING",
                "REVISION_GAP",
                "OUT_OF_ORDER_REVISION",
            )
        ):
            raise MarketDataError("qualified range event is not eligible for preview")
        payload = event.payload
        current = self._book_levels.get(key)
        if current is None:
            raise MarketDataError("provider book baseline is unavailable")
        candidate = self._apply_book_delta(current, payload)
        if self._book_depth_exceeds_limit(candidate):
            raise MarketDataError(
                "qualified range candidate exceeds the configured book-depth resource envelope"
            )
        if self._materialized_book_is_crossed(candidate):
            raise MarketDataError("qualified range candidate would create a crossed book")
        bids, asks = candidate
        return {
            "bids": [
                {"price": price, "quantity": bids[price]}
                for price in sorted(bids, key=Decimal, reverse=True)
            ],
            "asks": [
                {"price": price, "quantity": asks[price]}
                for price in sorted(asks, key=Decimal)
            ],
        }

    def apply_qualified_book_range(
        self,
        event: NormalizedMarketEvent,
        admission: QualifiedBookRangeAdmission | None = None,
        *,
        provider_id: str,
        venue_id: str,
        provider_symbol: str,
        stream: str = "book",
    ) -> QualifiedBookRangeAdmission:
        """Apply the registered provider policy's exact continuity decision."""

        key = self._book_key(provider_id, venue_id, provider_symbol, stream)
        derived = self._derive_qualified_book_range_admission(event, key=key)
        self._require_policy_equivalent_admission(admission, derived)
        admission = derived
        payload = event.payload
        cursor = admission.prior_sequence

        if admission.disposition == "DISCARD":
            if "CORRECTION" in event.quality_flags:
                baseline_cursor = self._provider_book_baseline_cursor.get(key)
                if (
                    baseline_cursor is None
                    or admission.last_sequence > baseline_cursor
                ):
                    # A correction to any range covered after the accepted
                    # snapshot can change levels already materialized into the
                    # current book.  Sequence policy may classify that old range
                    # as DISCARD relative to the current cursor, but correction
                    # semantics take precedence: rebuild from a fresh snapshot.
                    self._book_state[key] = "GAPPED"
                    self._book_last_available_at.pop(key, None)
                    self._book_levels.pop(key, None)
                    self._provider_book_cursor.pop(key, None)
                    self._provider_book_baseline_cursor.pop(key, None)
                    raise MarketDataError(
                        "historical provider range correction requires book rebuild"
                    )
            return admission
        if (
            "DUPLICATE" in event.quality_flags
            and admission.disposition == "APPLY"
            and admission.last_sequence == cursor
            and admission.next_sequence == cursor
        ):
            # Immutable revision identity already proved exact replay. Preserve
            # the accepted book/cursor/freshness without reapplying levels.
            return admission
        if admission.disposition == "GAP":
            self._book_state[key] = "GAPPED"
            self._book_last_available_at.pop(key, None)
            self._book_levels.pop(key, None)
            self._provider_book_cursor.pop(key, None)
            self._provider_book_baseline_cursor.pop(key, None)
            return admission

        if admission.next_sequence != admission.last_sequence:
            raise MarketDataError(
                "qualified range APPLY must advance to the event final sequence"
            )
        if any(
            flag in event.quality_flags
            for flag in (
                "STALE",
                "CORRECTION",
                "DUPLICATE",
                "REVISION_BASE_MISSING",
                "REVISION_GAP",
                "OUT_OF_ORDER_REVISION",
            )
        ):
            self._book_state[key] = "GAPPED"
            self._book_last_available_at.pop(key, None)
            self._book_levels.pop(key, None)
            self._provider_book_cursor.pop(key, None)
            self._provider_book_baseline_cursor.pop(key, None)
            raise MarketDataError(
                "qualified range event is not eligible for executable application"
            )
        current = self._book_levels.get(key)
        if current is None or self._book_state.get(key) not in {
            "BOOTSTRAPPING",
            "READY",
        }:
            raise MarketDataError("provider book baseline is unavailable")
        candidate = self._apply_book_delta(current, payload)
        if self._book_depth_exceeds_limit(candidate):
            self._book_state[key] = "GAPPED"
            self._book_last_available_at.pop(key, None)
            self._book_levels.pop(key, None)
            self._provider_book_cursor.pop(key, None)
            self._provider_book_baseline_cursor.pop(key, None)
            raise MarketDataError(
                "qualified range delta exceeds the configured book-depth resource envelope"
            )
        if self._materialized_book_is_crossed(candidate):
            self._book_state[key] = "GAPPED"
            self._book_last_available_at.pop(key, None)
            self._book_levels.pop(key, None)
            self._provider_book_cursor.pop(key, None)
            self._provider_book_baseline_cursor.pop(key, None)
            raise MarketDataError(
                "qualified range delta would create a crossed book"
            )
        self._book_levels[key] = candidate
        assert admission.next_sequence is not None
        self._provider_book_cursor[key] = admission.next_sequence
        self._book_state[key] = "READY"
        prior_available_at = self._book_last_available_at.get(key)
        self._book_last_available_at[key] = (
            event.available_at
            if prior_available_at is None
            else max(prior_available_at, event.available_at)
        )
        return admission

    @staticmethod
    def _instrument_version_id(instrument: InstrumentVersion) -> str:
        return f"{instrument.instrument_id}:{instrument.version}"

    @staticmethod
    def _price(instrument: InstrumentVersion, value: Any, field: str) -> str:
        exact = _decimal(value, field)
        try:
            return _decimal_text(instrument.validate_price(exact))
        except InstrumentRegistryError as error:
            raise MarketDataError(f"{field}: {error}") from error

    @staticmethod
    def _quantity(
        instrument: InstrumentVersion,
        value: Any,
        field: str,
        *,
        allow_zero: bool = False,
    ) -> str:
        exact = _decimal(value, field)
        if allow_zero and exact == 0:
            return "0"
        try:
            return _decimal_text(instrument.validate_quantity(exact))
        except InstrumentRegistryError as error:
            raise MarketDataError(f"{field}: {error}") from error

    def _levels(
        self,
        instrument: InstrumentVersion,
        values: Any,
        field: str,
        *,
        allow_zero: bool,
    ) -> list[dict[str, str]]:
        if not isinstance(values, (list, tuple)):
            raise MarketDataError(f"{field} must be a list")
        if len(values) > self._max_book_levels_per_side:
            raise MarketDataError(
                f"{field} exceeds the configured book-depth resource envelope"
            )
        result: list[dict[str, str]] = []
        seen_prices: set[str] = set()
        for index, level in enumerate(values):
            if not isinstance(level, (list, tuple)) or len(level) != 2:
                raise MarketDataError(f"{field}[{index}] must contain price and quantity")
            price = self._price(instrument, level[0], f"{field}[{index}].price")
            quantity = self._quantity(
                instrument,
                level[1],
                f"{field}[{index}].quantity",
                allow_zero=allow_zero,
            )
            if price in seen_prices:
                raise MarketDataError(f"{field} contains duplicate price levels")
            seen_prices.add(price)
            result.append({"price": price, "quantity": quantity})
        return result

    def _normalize_payload(
        self,
        instrument: InstrumentVersion,
        kind: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        raw = dict(payload)
        if kind == "TRADE":
            result = {
                "price": self._price(instrument, raw.get("price"), "price"),
                "quantity": self._quantity(instrument, raw.get("quantity"), "quantity"),
            }
            side = raw.get("side")
            if side is not None:
                normalized_side = _text(side, "side").upper()
                if normalized_side not in {"BUY", "SELL", "UNKNOWN"}:
                    raise MarketDataError("trade side is unsupported")
                result["side"] = normalized_side
            return result

        if kind == "QUOTE":
            bid = self._price(instrument, raw.get("bid_price"), "bid_price")
            ask = self._price(instrument, raw.get("ask_price"), "ask_price")
            if Decimal(bid) > Decimal(ask):
                raise MarketDataError("quote is crossed")
            return {
                "bid_price": bid,
                "bid_quantity": self._quantity(
                    instrument, raw.get("bid_quantity"), "bid_quantity"
                ),
                "ask_price": ask,
                "ask_quantity": self._quantity(
                    instrument, raw.get("ask_quantity"), "ask_quantity"
                ),
            }

        if kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}:
            allow_zero = kind == "BOOK_DELTA"
            bids = self._levels(
                instrument,
                raw.get("bids"),
                "bids",
                allow_zero=allow_zero,
            )
            asks = self._levels(
                instrument,
                raw.get("asks"),
                "asks",
                allow_zero=allow_zero,
            )
            if kind == "BOOK_SNAPSHOT" and bids and asks:
                best_bid = max(Decimal(level["price"]) for level in bids)
                best_ask = min(Decimal(level["price"]) for level in asks)
                if best_bid > best_ask:
                    raise MarketDataError("book snapshot is crossed")

            result: dict[str, Any] = {"bids": bids, "asks": asks}
            sequence_values: dict[str, int] = {}
            for field in ("first_sequence", "last_sequence", "previous_sequence"):
                value = _sequence(raw.get(field), field)
                if value is not None:
                    sequence_values[field] = value
                    result[field] = str(value)
            first_sequence = sequence_values.get("first_sequence")
            last_sequence = sequence_values.get("last_sequence")
            if (
                first_sequence is not None
                and last_sequence is not None
                and first_sequence > last_sequence
            ):
                raise MarketDataError(
                    "book first_sequence must not exceed last_sequence"
                )
            for field in ("snapshot_id", "checksum"):
                value = raw.get(field)
                if value is not None:
                    result[field] = _book_causal_text(value, field)
            return result

        if kind == "BAR":
            prices = {
                name: self._price(instrument, raw.get(name), name)
                for name in ("open", "high", "low", "close")
            }
            opened = Decimal(prices["open"])
            high = Decimal(prices["high"])
            low = Decimal(prices["low"])
            closed = Decimal(prices["close"])
            if high < max(opened, low, closed) or low > min(opened, high, closed):
                raise MarketDataError("bar OHLC bounds are inconsistent")
            volume_raw = raw.get("volume")
            volume = _decimal(volume_raw, "volume")
            if volume < 0:
                raise MarketDataError("volume must be non-negative")
            if volume == 0:
                volume_text = "0"
            else:
                volume_text = self._quantity(instrument, volume, "volume")
            return {**prices, "volume": volume_text}

        if kind == "FUNDING":
            rate = _decimal(raw.get("rate"), "rate")
            result: dict[str, Any] = {"rate": _decimal_text(rate)}
            if raw.get("next_funding_at") is not None:
                value = raw["next_funding_at"]
                if isinstance(value, datetime):
                    result["next_funding_at"] = _utc_text(_instant(value, "next_funding_at"))
                elif (
                    isinstance(value, str)
                    and value.endswith("Z")
                    and "T" in value
                ):
                    try:
                        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
                    except ValueError as error:
                        raise MarketDataError(
                            "next_funding_at must be an UTC instant"
                        ) from error
                    result["next_funding_at"] = _utc_text(
                        _instant(parsed, "next_funding_at")
                    )
                else:
                    raise MarketDataError("next_funding_at must be an UTC instant")
            return result

        if kind in {"MARK", "INDEX"}:
            return {"price": self._price(instrument, raw.get("price"), "price")}

        if kind == "STATUS":
            return {"status": _text(raw.get("status"), "status").upper()}

        raise MarketDataError("kind is unsupported")

    def normalize(self, update: RawMarketUpdate) -> NormalizedMarketEvent:
        # Constructor-time validation is not a use-time authority seal.  Reject
        # subclasses (whose inherited dataclass __init__ can dispatch a hostile
        # __post_init__) and re-admit the exact current graph before any
        # instrument lookup or normalizer-state mutation.
        if type(update) is not RawMarketUpdate:
            raise MarketDataError("update must be an exact RawMarketUpdate")
        update = RawMarketUpdate(
            provider_id=update.provider_id,
            venue_id=update.venue_id,
            provider_symbol=update.provider_symbol,
            kind=update.kind,
            source_event_at=update.source_event_at,
            available_at=update.available_at,
            ingested_at=update.ingested_at,
            availability_basis=update.availability_basis,
            revision=update.revision,
            payload=update.payload,
            raw_evidence_ref=update.raw_evidence_ref,
            source_sequence=update.source_sequence,
            stream_generation=update.stream_generation,
            sequence_stream=update.sequence_stream,
        )
        try:
            instrument = self._registry.resolve(
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                update.source_event_at,
            )
        except (InstrumentRegistryError, InstrumentNotFound) as error:
            raise MarketDataError("market update cannot be resolved to an instrument version") from error

        stream = update.sequence_stream or update.kind
        stream_key = (
            update.provider_id,
            update.venue_id,
            update.provider_symbol,
            stream,
        )
        book_kind = update.kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}
        bound_streams = {
            bound_stream
            for (provider_id, venue_id, bound_stream) in self._book_stream_policies
            if provider_id == update.provider_id and venue_id == update.venue_id
        }
        if book_kind and bound_streams and (
            update.sequence_stream is None or stream not in bound_streams
        ):
            raise MarketDataError(
                "provider book event must select an explicitly bound sequence_stream"
            )
        provider_policy_id = self._book_stream_policies.get(
            (update.provider_id, update.venue_id, stream)
        )
        provider_qualified_stream = book_kind and provider_policy_id is not None
        active_generation = (
            self._active_book_generation.get(stream_key)
            if provider_qualified_stream
            else None
        )
        if provider_qualified_stream:
            if active_generation is None:
                raise MarketDataError(
                    "provider book stream requires an active generation before ingress"
                )
            if update.stream_generation is None:
                raise MarketDataError(
                    "provider book event requires the active stream generation"
                )
            if update.stream_generation != active_generation:
                raise MarketDataError(
                    "provider book event belongs to a superseded stream generation"
                )
        elif book_kind:
            generic_generation = self._generic_book_generation.get(stream_key)
            if generic_generation is not None and update.stream_generation is None:
                raise MarketDataError(
                    "book stream requires the active stream generation"
                )
            if update.stream_generation is not None:
                if (
                    generic_generation is not None
                    and update.stream_generation < generic_generation
                ):
                    raise MarketDataError(
                        "book event belongs to a superseded stream generation"
                    )
                if (
                    generic_generation is None
                    or update.stream_generation > generic_generation
                ):
                    # A generation boundary invalidates the previous materialized
                    # book before any event from the new subscription can use it.
                    # The new generation must establish its own snapshot baseline;
                    # a first delta therefore remains non-executable.
                    self._generic_book_generation[
                        stream_key
                    ] = update.stream_generation
                    self._book_state[stream_key] = "UNINITIALIZED"
                    self._book_last_available_at.pop(stream_key, None)
                    self._book_levels.pop(stream_key, None)
                    self._provider_book_cursor.pop(stream_key, None)
                    self._provider_book_baseline_cursor.pop(stream_key, None)
                    self._clear_retained_book_events(stream_key)
        try:
            normalized_payload = self._normalize_payload(
                instrument,
                update.kind,
                update.payload,
            )
        except MarketDataError:
            if (
                update.kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}
                and (
                    stream_key in self._book_state
                    or stream_key in self._book_levels
                    or stream_key in self._provider_book_cursor
                )
            ):
                # Rejected initial input has no authority to mutate. A rejected
                # replacement after accepted state exists must revoke that older
                # executable authority rather than silently leaving it current.
                self._book_state[stream_key] = "GAPPED"
                self._book_last_available_at.pop(stream_key, None)
                self._book_levels.pop(stream_key, None)
                self._provider_book_cursor.pop(stream_key, None)
                self._provider_book_baseline_cursor.pop(stream_key, None)
            raise
        provider_continuity_fields = {
            "first_sequence",
            "last_sequence",
            "previous_sequence",
            "checksum",
        }
        has_provider_continuity_fields = (
            update.kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}
            and bool(provider_continuity_fields.intersection(normalized_payload))
        )
        payload_json = _canonical(normalized_payload)
        payload_digest = sha256(payload_json.encode("utf-8")).hexdigest()
        flags: set[str] = set()

        if update.ingested_at - update.available_at > self._max_available_age:
            flags.add("STALE")
        try:
            self._registry.require_tradable(instrument.instrument_id, update.source_event_at)
        except InstrumentRegistryError:
            flags.add("NOT_TRADABLE_AT_EVENT_TIME")

        has_unqualified_provider_continuity = (
            has_provider_continuity_fields or provider_qualified_stream
        )
        generation_identity = (
            "NO_GENERATION"
            if update.stream_generation is None
            else f"GENERATION:{update.stream_generation}"
        )
        sequence_state_key = (
            stream_key
            if update.stream_generation is None
            else (
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                f"{stream}#GENERATION:{update.stream_generation}",
            )
        )
        identity_stream_key = (
            (
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                stream,
                generation_identity,
            )
            if not has_unqualified_provider_continuity
            else (
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                f"{stream}:{update.kind}",
                generation_identity,
            )
        )
        sequence_identity = (
            *identity_stream_key,
            update.source_sequence,
        ) if update.source_sequence is not None else None

        revision_identity = (
            *sequence_identity,
            update.revision,
        ) if sequence_identity is not None else None

        if (
            provider_qualified_stream
            and active_generation is not None
            and sequence_identity is not None
            and revision_identity is not None
        ):
            assert update.source_sequence is not None
            self._require_provider_book_causal_window(
                key=stream_key,
                generation=active_generation,
                kind=update.kind,
                sequence_identity=sequence_identity,
                revision_identity=revision_identity,
                source_sequence=update.source_sequence,
                revision=update.revision,
            )

        new_sequence = False
        new_revision = False
        if sequence_identity is not None and revision_identity is not None:
            instrument_version_id = self._instrument_version_id(instrument)
            source_identity = (
                instrument_version_id,
                update.kind,
                update.source_event_at,
            )
            existing_source_identity = self._sequence_source_identity.get(
                sequence_identity
            )
            if (
                existing_source_identity is not None
                and existing_source_identity != source_identity
            ):
                raise SequenceConflict(
                    "source sequence revision changed immutable source identity"
                )

            revision_fingerprint = sha256(
                _canonical(
                    {
                        "instrument_version": instrument_version_id,
                        "kind": update.kind,
                        "source_event_at": _utc_text(update.source_event_at),
                        "available_at": _utc_text(update.available_at),
                        "availability_basis": update.availability_basis,
                        "revision": str(update.revision),
                        "payload": normalized_payload,
                    }
                ).encode("utf-8")
            ).hexdigest()
            existing = self._seen_revision.get(revision_identity)
            if existing is not None:
                existing_fingerprint, _ = existing
                if existing_fingerprint != revision_fingerprint:
                    raise SequenceConflict(
                        "source sequence revision was reused with changed causal content"
                    )
                flags.add("DUPLICATE")
            else:
                new_revision = True
                last_revision = self._last_revision.get(sequence_identity)
                last_available = self._last_revision_available_at.get(sequence_identity)
                if last_revision is None:
                    if update.revision > 0:
                        flags.add("REVISION_BASE_MISSING")
                else:
                    flags.add("CORRECTION")
                    if update.revision > last_revision + 1:
                        flags.add("REVISION_GAP")
                    elif update.revision < last_revision:
                        flags.add("OUT_OF_ORDER_REVISION")
                    if (
                        update.revision > last_revision
                        and last_available is not None
                        and update.available_at < last_available
                    ):
                        raise SequenceConflict(
                            "higher source revision cannot backdate availability"
                        )

                if (
                    last_revision is None
                    or update.revision > last_revision
                ):
                    self._last_revision[sequence_identity] = update.revision
                    self._last_revision_available_at[
                        sequence_identity
                    ] = update.available_at

                if sequence_identity not in self._seen_sequence_ids:
                    new_sequence = True
                    last = self._last_sequence.get(sequence_state_key)
                    if has_unqualified_provider_continuity:
                        # Do not feed provider range/predecessor/checksum semantics
                        # into the generic scalar +1 cursor. The exact event is
                        # still revision/dedup tracked, but executable continuity
                        # remains unverified until a qualified stream policy owns it.
                        pass
                    elif update.kind == "BOOK_SNAPSHOT":
                        # A verified snapshot may establish a new baseline only
                        # when it does not move the stream sequence backward.
                        if last is not None and update.source_sequence < last:
                            flags.add("OUT_OF_ORDER")
                        else:
                            self._last_sequence[sequence_state_key] = update.source_sequence
                    else:
                        if last is not None:
                            if update.source_sequence > last + 1:
                                flags.add("SEQUENCE_GAP")
                            elif update.source_sequence < last:
                                flags.add("OUT_OF_ORDER")
                        self._last_sequence[sequence_state_key] = (
                            update.source_sequence
                            if last is None
                            else max(last, update.source_sequence)
                        )
                    self._seen_sequence_ids.add(sequence_identity)
                    self._sequence_source_identity[
                        sequence_identity
                    ] = source_identity

        if update.kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}:
            if update.source_sequence is None:
                flags.add("BOOK_SEQUENCE_UNVERIFIED")
                flags.add("BOOK_UNUSABLE")
                self._book_state[stream_key] = "UNVERIFIED"
                self._book_last_available_at.pop(stream_key, None)
            elif has_unqualified_provider_continuity:
                # A product-bound provider policy owns this stream from its
                # snapshot onward. Do not let a range-less REST snapshot mint
                # generic scalar READY before buffered provider continuity is
                # verified by that policy.
                if provider_qualified_stream:
                    flags.add("BOOK_PROVIDER_CONTINUITY_PENDING")
                if has_provider_continuity_fields:
                    flags.add("BOOK_RANGE_CONTINUITY_UNVERIFIED")
                flags.add("BOOK_UNUSABLE")
                if provider_qualified_stream:
                    # The event is pending policy admission, but normalization
                    # alone must not erase the previously qualified baseline or
                    # executable state. The provider decision below owns the
                    # state transition. For the first event only, expose an
                    # explicit non-executable state until a baseline is installed.
                    self._book_state.setdefault(stream_key, "UNVERIFIED")
                else:
                    self._book_state[stream_key] = "UNVERIFIED"
                    self._book_last_available_at.pop(stream_key, None)
            elif update.kind == "BOOK_SNAPSHOT":
                current = self._book_state.get(stream_key, "UNINITIALIZED")
                last_sequence = self._last_sequence.get(sequence_state_key)
                historical_correction = (
                    "CORRECTION" in flags
                    and update.source_sequence is not None
                    and last_sequence is not None
                    and update.source_sequence < last_sequence
                )
                if "CORRECTION" in flags:
                    # A snapshot revision can invalidate state already derived
                    # from its prior revision. Rebuild deterministically rather
                    # than keeping old materialized levels under corrected truth.
                    if historical_correction:
                        flags.add("HISTORICAL_BOOK_CORRECTION")
                    flags.add("BOOK_CORRECTION_REBUILD_REQUIRED")
                    self._book_state[stream_key] = "GAPPED"
                    self._book_last_available_at.pop(stream_key, None)
                    flags.add("BOOK_UNUSABLE")
                elif "STALE" in flags:
                    # A delayed snapshot cannot establish current executable
                    # book truth even when its provider sequence is valid.
                    self._book_state[stream_key] = "GAPPED"
                    self._book_last_available_at.pop(stream_key, None)
                    flags.add("BOOK_UNUSABLE")
                elif new_sequence and "OUT_OF_ORDER" not in flags:
                    materialized = self._materialized_book(normalized_payload)
                    if self._materialized_book_is_crossed(materialized):
                        # Defensive parity with normalization-time snapshot
                        # checking. Never expose an internally crossed book.
                        flags.add("BOOK_CROSSED")
                        flags.add("BOOK_UNUSABLE")
                        self._book_state[stream_key] = "GAPPED"
                        self._book_last_available_at.pop(stream_key, None)
                    else:
                        self._book_levels[stream_key] = materialized
                        self._book_state[stream_key] = "READY"
                        self._book_last_available_at[stream_key] = update.available_at
                elif "OUT_OF_ORDER" in flags:
                    # A stale snapshot is unusable as a new baseline. Preserve
                    # the newer current state rather than rolling sequence truth back.
                    flags.add("BOOK_UNUSABLE")
                elif current != "READY":
                    # Replaying an old duplicate snapshot after a later gap cannot
                    # silently re-authorize the book.
                    flags.add("BOOK_UNUSABLE")
            else:
                current = self._book_state.get(stream_key, "UNINITIALIZED")
                last_sequence = self._last_sequence.get(sequence_state_key)
                historical_correction = (
                    "CORRECTION" in flags
                    and update.source_sequence is not None
                    and last_sequence is not None
                    and update.source_sequence < last_sequence
                )
                if "CORRECTION" in flags:
                    # A corrected delta cannot be applied safely on top of a
                    # book that already incorporated its prior revision without
                    # deterministic replay from a qualified snapshot.
                    if historical_correction:
                        flags.add("HISTORICAL_BOOK_CORRECTION")
                    flags.add("BOOK_CORRECTION_REBUILD_REQUIRED")
                    self._book_state[stream_key] = "GAPPED"
                    self._book_last_available_at.pop(stream_key, None)
                    flags.add("BOOK_UNUSABLE")
                elif (
                    current != "READY"
                    or "SEQUENCE_GAP" in flags
                    or "OUT_OF_ORDER" in flags
                    or "STALE" in flags
                ):
                    self._book_state[stream_key] = "GAPPED"
                    self._book_last_available_at.pop(stream_key, None)
                    flags.add("BOOK_UNUSABLE")
                elif new_sequence:
                    current_book = self._book_levels.get(stream_key)
                    if current_book is None:
                        self._book_state[stream_key] = "GAPPED"
                        self._book_last_available_at.pop(stream_key, None)
                        flags.add("BOOK_MATERIALIZATION_MISSING")
                        flags.add("BOOK_UNUSABLE")
                    else:
                        candidate = self._apply_book_delta(
                            current_book,
                            normalized_payload,
                        )
                        if self._book_depth_exceeds_limit(candidate):
                            self._book_state[stream_key] = "GAPPED"
                            self._book_last_available_at.pop(stream_key, None)
                            self._book_levels.pop(stream_key, None)
                            flags.add("BOOK_RESOURCE_LIMIT")
                            flags.add("BOOK_UNUSABLE")
                        elif self._materialized_book_is_crossed(candidate):
                            self._book_state[stream_key] = "GAPPED"
                            self._book_last_available_at.pop(stream_key, None)
                            flags.add("BOOK_CROSSED")
                            flags.add("BOOK_UNUSABLE")
                        else:
                            self._book_levels[stream_key] = candidate
                            self._book_state[stream_key] = "READY"
                            self._book_last_available_at[
                                stream_key
                            ] = update.available_at
                else:
                    # Exact duplicate replay is state-idempotent and cannot
                    # mutate levels or extend the accepted freshness cut.
                    self._book_state[stream_key] = "READY"

        identity_material = _canonical(
            [
                update.provider_id,
                update.venue_id,
                update.provider_symbol,
                stream,
                (
                    str(update.stream_generation)
                    if update.stream_generation is not None
                    else None
                ),
                (
                    str(update.source_sequence)
                    if update.source_sequence is not None
                    else None
                ),
                str(update.revision),
                payload_digest,
                _utc_text(update.source_event_at),
                _utc_text(update.available_at),
                _utc_text(update.ingested_at),
                sorted(flags),
                dict(update.raw_evidence_ref),
            ]
        )
        event_id = str(uuid5(NAMESPACE_URL, identity_material))
        if revision_identity is not None and new_revision:
            self._seen_revision[revision_identity] = (
                revision_fingerprint,
                event_id,
            )
        event = NormalizedMarketEvent(
            event_id=event_id,
            instrument_version=self._instrument_version_id(instrument),
            kind=update.kind,
            source_event_at=update.source_event_at,
            available_at=update.available_at,
            availability_basis=update.availability_basis,
            ingested_at=update.ingested_at,
            source_sequence=update.source_sequence,
            stream_generation=update.stream_generation,
            revision=update.revision,
            payload_json=payload_json,
            quality_flags=tuple(sorted(flags)),
            raw_evidence_ref=update.raw_evidence_ref,
        )
        if update.kind in {"BOOK_SNAPSHOT", "BOOK_DELTA"}:
            self._retain_book_event_identity(event, stream_key)
            if (
                provider_qualified_stream
                and active_generation is not None
                and new_revision
                and sequence_identity is not None
                and revision_identity is not None
            ):
                self._retain_provider_book_causal_revision(
                    key=stream_key,
                    generation=active_generation,
                    sequence_identity=sequence_identity,
                    revision_identity=revision_identity,
                )
        return event
