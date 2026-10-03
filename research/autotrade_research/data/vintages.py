"""Immutable historical vintages and point-in-time research views.

This module never invents missing ticks, never replaces older revisions, and
never filters delisted instruments merely because they are not active today.
It is provider-neutral and contains no network or trading capability.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import UUID

from autotrade_research.artifacts import ArtifactStore
from autotrade_research.artifacts.durable_publish import atomic_write_json, durable_path_lock
from autotrade_research.io.strict_json import strict_json_loads


class HistoricalDataError(ValueError):
    pass


class HistoricalConflict(HistoricalDataError):
    pass


_MARKET_POPULATION_MEDIA_TYPE = "application/vnd.autotrade.market-event-population+json"
_MARKET_EVENT_KINDS = frozenset(
    {
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
)
_MARKET_EVENT_CANONICAL_FIELDS = frozenset(
    {
        "event_id",
        "instrument_version",
        "kind",
        "source_event_at",
        "available_at",
        "availability_basis",
        "ingested_at",
        "source_sequence",
        "revision",
        "payload",
        "quality_flags",
        "raw_evidence_ref",
    }
)
_MARKET_EVENT_REQUIRED_FIELDS = _MARKET_EVENT_CANONICAL_FIELDS - {"source_sequence"}
_EVIDENCE_REF_CANONICAL_FIELDS = frozenset(
    {"artifact_id", "sha256", "source_uri", "observed_at", "rights_id"}
)
_EVIDENCE_REF_REQUIRED_FIELDS = frozenset({"artifact_id", "sha256", "observed_at"})


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalDataError(f"{name} must be non-empty text")
    return value.strip()


def _sequence(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise HistoricalDataError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise HistoricalDataError(f"{name} must be a positive integer") from error
    if parsed < 1 or str(parsed) != str(value):
        raise HistoricalDataError(f"{name} must be a canonical positive integer")
    return parsed


def _optional_non_negative_sequence(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise HistoricalDataError(
            f"{name} must be a canonical non-negative integer"
        )
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise HistoricalDataError(
            f"{name} must be a canonical non-negative integer"
        ) from error
    if parsed < 0 or str(parsed) != str(value):
        raise HistoricalDataError(
            f"{name} must be a canonical non-negative integer"
        )
    return parsed


def _non_negative_sequence(value: Any, name: str) -> int:
    parsed = _optional_non_negative_sequence(value, name)
    if parsed is None:
        raise HistoricalDataError(
            f"{name} must be a canonical non-negative integer"
        )
    return parsed


def _utc(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise HistoricalDataError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)
    text = _text(value, name)
    if not text.endswith("Z"):
        raise HistoricalDataError(f"{name} must be UTC and end in Z")
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise HistoricalDataError(f"{name} must be an ISO date-time") from error
    return parsed.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any, name: str = "digest") -> str:
    text = _text(value, name)
    if not text.startswith("sha256:") or len(text) != 71:
        raise HistoricalDataError(f"{name} must be a canonical SHA-256 digest")
    if any(ch not in "0123456789abcdef" for ch in text[7:]):
        raise HistoricalDataError(f"{name} must be lowercase hexadecimal")
    return text


def _uuid(value: Any, name: str) -> str:
    text = _text(value, name)
    try:
        return str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise HistoricalDataError(f"{name} must be a UUID") from error


def _canonical_bytes(value: Any) -> bytes:
    try:
        payload = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise HistoricalDataError("value is not canonically serializable") from error
    return payload.encode("utf-8")



def canonical_market_event_population_bytes(
    events: Iterable[Mapping[str, Any]],
) -> bytes:
    """Encode one complete population as a versioned aggregate artifact.

    The explicit artifact type prevents an arbitrary partition/content object
    from being mistaken for the complete population merely because its digest
    also appears in DatasetManifest.content_hashes.
    """

    rows: list[tuple[bytes, dict[str, Any]]] = []
    for raw in events:
        if not isinstance(raw, Mapping):
            raise HistoricalDataError("market event population must contain objects")
        canonical = _canonical_bytes(dict(raw))
        decoded = strict_json_loads(canonical.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise HistoricalDataError("market event must canonically encode as an object")
        rows.append((canonical, decoded))
    if not rows:
        raise HistoricalDataError("market event population must be non-empty")
    rows.sort(key=lambda item: item[0])
    return _canonical_bytes(
        {
            "artifact_type": "AUTOTRADE_MARKET_EVENT_POPULATION",
            "events": [item[1] for item in rows],
            "schema_version": "1.0.0",
        }
    )


def market_event_population_digest(
    events: Iterable[Mapping[str, Any]],
) -> str:
    """Return the content hash of canonical market-population artifact bytes.

    A dataset that uses this resolver must register this exact versioned
    aggregate artifact in DatasetManifest.content_hashes. The digest is thus a
    real byte-content hash and cannot silently alias an unlabeled partition.
    """

    return "sha256:" + sha256(
        canonical_market_event_population_bytes(events)
    ).hexdigest()


@dataclass(frozen=True)
class FrozenMarketPopulation:
    """Immutable identity for one manifest-authenticated causal event cut."""

    dataset_id: str
    version: int
    manifest_digest: str
    cutoff: datetime
    source_artifact_id: str
    source_content_digest: str
    visible_event_json: tuple[str, ...]
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "dataset_id", _uuid(self.dataset_id, "dataset_id"))
        object.__setattr__(self, "version", _sequence(self.version, "version"))
        object.__setattr__(
            self,
            "manifest_digest",
            _digest(self.manifest_digest, "manifest_digest"),
        )
        object.__setattr__(self, "cutoff", _utc(self.cutoff, "cutoff"))
        object.__setattr__(
            self,
            "source_artifact_id",
            _uuid(self.source_artifact_id, "source_artifact_id"),
        )
        object.__setattr__(
            self,
            "source_content_digest",
            _digest(self.source_content_digest, "source_content_digest"),
        )
        rows = tuple(self.visible_event_json)
        if not rows:
            raise HistoricalDataError("frozen market population must contain events")
        for row in rows:
            if not isinstance(row, str) or not row:
                raise HistoricalDataError("visible_event_json must contain canonical JSON text")
            try:
                parsed = strict_json_loads(row)
            except (TypeError, ValueError) as error:
                raise HistoricalDataError("visible_event_json is invalid") from error
            if not isinstance(parsed, Mapping):
                raise HistoricalDataError("visible_event_json must decode to objects")
            if row != _canonical_bytes(parsed).decode("utf-8"):
                raise HistoricalDataError("visible_event_json must use canonical encoding")
        object.__setattr__(self, "visible_event_json", rows)
        object.__setattr__(
            self,
            "fingerprint",
            _digest(self.fingerprint, "population fingerprint"),
        )

    def events(self) -> tuple[dict[str, Any], ...]:
        """Return detached decoded events; mutating them cannot alter this identity."""

        return tuple(
            dict(strict_json_loads(row))
            for row in self.visible_event_json
        )


def _evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalDataError("source evidence must be an object")
    fields = set(value)
    if not _EVIDENCE_REF_REQUIRED_FIELDS.issubset(fields):
        raise HistoricalDataError("source evidence is incomplete")
    unknown = fields - _EVIDENCE_REF_CANONICAL_FIELDS
    if unknown:
        raise HistoricalDataError(
            "source evidence fields differ from canonical EvidenceRef contract; "
            f"unknown={sorted(unknown)}"
        )
    result: dict[str, Any] = {
        "artifact_id": _uuid(value["artifact_id"], "artifact_id"),
        "sha256": _digest(value["sha256"], "evidence sha256"),
        "observed_at": _utc_text(_utc(value["observed_at"], "observed_at")),
    }
    if "source_uri" in value:
        result["source_uri"] = _text(value["source_uri"], "source_uri")
    if "rights_id" in value:
        result["rights_id"] = _text(value["rights_id"], "rights_id")
    return result



def causal_market_event_history(
    events: Iterable[Mapping[str, Any]],
    cutoff: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return every event revision causally visible by the requested cutoff.

    Historical revisions remain present so downstream causal feature builders
    can reconstruct which revision was knowable at each earlier decision time.
    Future revisions remain invisible. Exact duplicate rows collapse; conflicting
    bytes or impossible revision chronology fail closed.
    """

    point = _utc(cutoff, "cutoff")
    visible_revisions: dict[
        str,
        dict[int, tuple[datetime, datetime, str, bytes, dict[str, Any]]],
    ] = {}
    visible_knowledge: dict[str, dict[int, datetime]] = {}
    for raw in events:
        if not isinstance(raw, Mapping):
            raise HistoricalDataError("market event must be an object")
        event = dict(raw)
        event_fields = set(event)
        missing_fields = _MARKET_EVENT_REQUIRED_FIELDS - event_fields
        unknown_fields = event_fields - _MARKET_EVENT_CANONICAL_FIELDS
        if missing_fields or unknown_fields:
            raise HistoricalDataError(
                "market event fields differ from canonical MarketEvent contract; "
                f"missing={sorted(missing_fields)}, unknown={sorted(unknown_fields)}"
            )
        event_id = _uuid(event.get("event_id"), "event_id")
        instrument_version = _text(event.get("instrument_version"), "instrument_version")
        event_kind = _text(event.get("kind"), "kind")
        if event_kind not in _MARKET_EVENT_KINDS:
            raise HistoricalDataError("market event kind is not canonical")
        if type(event.get("payload")) is not dict:
            raise HistoricalDataError("market event payload must be an object")
        quality_flags = event.get("quality_flags")
        if (
            type(quality_flags) is not list
            or any(type(flag) is not str or len(flag) < 1 for flag in quality_flags)
            or len(quality_flags) != len(set(quality_flags))
        ):
            raise HistoricalDataError(
                "market event quality_flags must be a unique array of non-empty strings"
            )
        if "source_sequence" in event and type(event["source_sequence"]) is not str:
            raise HistoricalDataError("source_sequence must be a canonical string Sequence")
        _optional_non_negative_sequence(
            event.get("source_sequence"),
            "source_sequence",
        )
        if type(event.get("revision")) is not str:
            raise HistoricalDataError("revision must be a canonical string Sequence")
        revision = _non_negative_sequence(event.get("revision"), "revision")
        available = _utc(event.get("available_at"), "available_at")
        source_at = _utc(event.get("source_event_at"), "source_event_at")
        ingested = _utc(event.get("ingested_at"), "ingested_at")
        _text(event.get("availability_basis"), "availability_basis")
        if available < source_at:
            raise HistoricalDataError("available_at cannot precede source_event_at")
        if ingested < available:
            raise HistoricalDataError("ingested_at cannot precede evidenced available_at")
        raw_evidence = _evidence(event.get("raw_evidence_ref"))
        evidence_observed = _utc(raw_evidence["observed_at"], "raw evidence observed_at")
        if evidence_observed < source_at:
            raise HistoricalDataError(
                "raw evidence cannot be observed before source_event_at"
            )
        if evidence_observed > ingested:
            raise HistoricalDataError("raw evidence cannot be observed after event ingestion")
        if (
            available > point
            or ingested > point
            or evidence_observed > point
        ):
            continue
        effective_knowledge = max(available, ingested, evidence_observed)

        canonical = _canonical_bytes(event)
        history = visible_revisions.setdefault(event_id, {})
        knowledge_history = visible_knowledge.setdefault(event_id, {})
        same_revision = history.get(revision)
        if same_revision is not None:
            if same_revision[3] != canonical:
                raise HistoricalConflict("same event revision has conflicting bytes")
            continue

        for other_revision, (
            other_available,
            other_source_at,
            other_instrument_version,
            _,
            _other_event,
        ) in history.items():
            other_kind = _text(_other_event.get("kind"), "kind")
            if (
                source_at != other_source_at
                or instrument_version != other_instrument_version
                or event_kind != other_kind
            ):
                raise HistoricalConflict("event revision changed source identity metadata")
            if revision > other_revision and available < other_available:
                raise HistoricalConflict("higher event revision cannot backdate availability")
            if revision < other_revision and available > other_available:
                raise HistoricalConflict(
                    "higher event revision cannot predate lower revision availability"
                )
            other_knowledge = knowledge_history[other_revision]
            if (
                (revision > other_revision and effective_knowledge <= other_knowledge)
                or (
                    revision < other_revision
                    and effective_knowledge >= other_knowledge
                )
            ):
                raise HistoricalConflict(
                    "higher event revision must have later effective knowledge time"
                )
        history[revision] = (
            available,
            source_at,
            instrument_version,
            canonical,
            event,
        )
        knowledge_history[revision] = effective_knowledge

    rows = [
        item[4]
        for revisions in visible_revisions.values()
        for item in revisions.values()
    ]
    rows.sort(
        key=lambda row: (
            _utc(row["available_at"], "available_at"),
            _utc(row["source_event_at"], "source_event_at"),
            row["event_id"],
            _non_negative_sequence(row["revision"], "revision"),
        )
    )
    return tuple(rows)


def point_in_time_market_events(
    events: Iterable[Mapping[str, Any]],
    cutoff: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return the latest revision per event identity knowable at the cutoff."""

    history = causal_market_event_history(events, cutoff)
    selected: dict[str, tuple[int, bytes, dict[str, Any]]] = {}
    for event in history:
        event_id = _uuid(event.get("event_id"), "event_id")
        revision = _non_negative_sequence(event.get("revision"), "revision")
        canonical = _canonical_bytes(event)
        previous = selected.get(event_id)
        if previous is None or revision > previous[0]:
            selected[event_id] = (revision, canonical, event)
        elif revision == previous[0] and canonical != previous[1]:
            raise HistoricalConflict("same event revision has conflicting bytes")

    rows = [item[2] for item in selected.values()]
    rows.sort(
        key=lambda row: (
            _utc(row["available_at"], "available_at"),
            _utc(row["source_event_at"], "source_event_at"),
            row["event_id"],
            _non_negative_sequence(row["revision"], "revision"),
        )
    )
    return tuple(rows)

def point_in_time_universe(
    instrument_versions: Iterable[Mapping[str, Any]],
    cutoff: datetime,
) -> tuple[dict[str, Any], ...]:
    """Select the latest instrument version actually knowable by cutoff.

    DELISTED and EXPIRED records are retained; this function never performs a
    present-day-active filter, preventing survivorship bias.
    """

    point = _utc(cutoff, "cutoff")
    selected: dict[str, tuple[int, dict[str, Any]]] = {}
    for raw in instrument_versions:
        if not isinstance(raw, Mapping):
            raise HistoricalDataError("instrument version must be an object")
        row = dict(raw)
        instrument_id = _uuid(row.get("instrument_id"), "instrument_id")
        version = _sequence(row.get("version"), "version")
        effective_from = _utc(row.get("effective_from"), "effective_from")
        evidence = row.get("metadata_evidence")
        if not isinstance(evidence, list) or not evidence:
            raise HistoricalDataError("historical instrument version requires metadata evidence")
        known_at = max(_utc(_evidence(item)["observed_at"], "observed_at") for item in evidence)
        if effective_from > point or known_at > point:
            continue

        previous = selected.get(instrument_id)
        if previous is None or version > previous[0]:
            selected[instrument_id] = (version, row)
        elif version == previous[0] and _canonical_bytes(row) != _canonical_bytes(previous[1]):
            raise HistoricalConflict("same instrument version has conflicting bytes")

    rows = [item[1] for item in selected.values()]
    rows.sort(key=lambda row: row["instrument_id"])
    return tuple(rows)


@dataclass(frozen=True)
class MissingnessReport:
    expected_count: int
    observed_count: int
    missing_keys: tuple[str, ...]
    invented_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "missing_keys": list(self.missing_keys),
            "invented_count": self.invented_count,
        }


def explicit_missingness(
    expected_keys: Iterable[str],
    observed_keys: Iterable[str],
) -> MissingnessReport:
    expected = tuple(_text(value, "expected key") for value in expected_keys)
    observed = tuple(_text(value, "observed key") for value in observed_keys)
    if len(set(expected)) != len(expected):
        raise HistoricalDataError("expected keys must be unique")
    if len(set(observed)) != len(observed):
        raise HistoricalDataError("observed keys must be unique")
    unknown = set(observed) - set(expected)
    if unknown:
        raise HistoricalDataError("observed keys contain values outside expected coverage")
    missing = tuple(sorted(set(expected) - set(observed)))
    return MissingnessReport(
        expected_count=len(expected),
        observed_count=len(observed),
        missing_keys=missing,
        invented_count=0,
    )


def validate_multiplicative_adjustment(
    raw: Mapping[str, str | int | Decimal],
    adjusted: Mapping[str, str | int | Decimal],
    factors: Mapping[str, str | int | Decimal],
) -> None:
    """Validate an explicitly multiplicative adjustment policy with exact decimals."""

    def exact_parts(value: Decimal) -> tuple[int, int]:
        # Decimal multiplication uses the caller's mutable precision. Compare
        # integer coefficients and powers of ten instead, so replay validation
        # cannot accept a rounded adjustment or reject an exact one.
        sign, digits, exponent = value.as_tuple()
        if len(digits) > 256:
            raise HistoricalDataError("adjustment input exceeds 256 significant digits")
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        if sign:
            coefficient = -coefficient
        if coefficient == 0:
            return (0, 0)
        while coefficient % 10 == 0:
            coefficient //= 10
            exponent += 1
        return (coefficient, exponent)

    if set(raw) != set(adjusted) or set(raw) != set(factors):
        raise HistoricalDataError("raw, adjusted and factor series must cover identical keys")
    for key in raw:
        values: list[Decimal] = []
        for value in (raw[key], adjusted[key], factors[key]):
            if isinstance(value, bool) or isinstance(value, float):
                raise HistoricalDataError("adjustment inputs must use exact decimal values")
            try:
                parsed = value if isinstance(value, Decimal) else Decimal(value)
            except (InvalidOperation, ValueError, TypeError) as error:
                raise HistoricalDataError("adjustment input is not a decimal") from error
            if not parsed.is_finite():
                raise HistoricalDataError("adjustment input must be finite")
            values.append(parsed)
        raw_value, adjusted_value, factor = values
        if factor <= 0:
            raise HistoricalDataError("adjustment factor must be positive")
        raw_coefficient, raw_exponent = exact_parts(raw_value)
        factor_coefficient, factor_exponent = exact_parts(factor)
        product_coefficient = raw_coefficient * factor_coefficient
        product_exponent = raw_exponent + factor_exponent
        if product_coefficient == 0:
            product = (0, 0)
        else:
            while product_coefficient % 10 == 0:
                product_coefficient //= 10
                product_exponent += 1
            product = (product_coefficient, product_exponent)
        if product != exact_parts(adjusted_value):
            raise HistoricalDataError(f"adjusted series is inconsistent at {key}")


def _validate_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(manifest, Mapping):
        raise HistoricalDataError("dataset manifest must be an object")
    required = {
        "dataset_id",
        "version",
        "content_hashes",
        "instrument_universe_version",
        "calendar_version",
        "coverage",
        "availability_policy",
        "revision_policy",
        "normalization_version",
        "adjustment_policy",
        "rights",
        "missingness_report",
        "source_evidence",
        "created_at",
    }
    allowed = required
    if not required.issubset(manifest) or set(manifest) - allowed:
        missing = required - set(manifest)
        unknown = set(manifest) - allowed
        raise HistoricalDataError(
            f"dataset manifest keys differ; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )

    content_hashes = manifest["content_hashes"]
    if not isinstance(content_hashes, list) or not content_hashes:
        raise HistoricalDataError("content_hashes must be a non-empty list")
    hashes = [_digest(value, "content hash") for value in content_hashes]
    if len(set(hashes)) != len(hashes):
        raise HistoricalDataError("content_hashes must be unique")


    source_evidence = manifest["source_evidence"]
    if not isinstance(source_evidence, list) or not source_evidence:
        raise HistoricalDataError("source_evidence must be non-empty")
    evidence = [_evidence(item) for item in source_evidence]

    created_at = _utc(manifest["created_at"], "created_at")
    latest_evidence_at = max(
        _utc(item["observed_at"], "source evidence observed_at")
        for item in evidence
    )
    if created_at < latest_evidence_at:
        raise HistoricalDataError("created_at cannot precede source evidence observation")

    for name in (
        "coverage",
        "availability_policy",
        "revision_policy",
        "adjustment_policy",
        "rights",
        "missingness_report",
    ):
        if not isinstance(manifest[name], Mapping):
            raise HistoricalDataError(f"{name} must be an object")

    availability = dict(manifest["availability_policy"])
    if availability.get("point_in_time") is not True or availability.get("no_future_leakage") is not True:
        raise HistoricalDataError("availability policy must enforce point-in-time no-future-leakage")
    availability_cutoff = _utc(availability.get("cutoff"), "availability cutoff")
    if availability_cutoff > created_at:
        raise HistoricalDataError(
            "availability cutoff cannot be later than dataset creation"
        )
    _text(availability.get("basis"), "availability basis")

    revision = dict(manifest["revision_policy"])
    if revision.get("append_only") is not True or revision.get("replace_prior_vintages") is not False:
        raise HistoricalDataError("revision policy must retain prior vintages")

    adjustment = dict(manifest["adjustment_policy"])
    if adjustment.get("raw_retained") is not True:
        raise HistoricalDataError("adjustment policy must retain raw data")

    rights = dict(manifest["rights"])
    if rights.get("storage") is not True or rights.get("research_use") is not True:
        raise HistoricalDataError("dataset rights must explicitly permit storage and research use")
    _text(rights.get("basis"), "rights basis")

    missingness = dict(manifest["missingness_report"])
    if set(missingness) != {"expected_count", "observed_count", "missing_keys", "invented_count"}:
        raise HistoricalDataError("missingness report has unexpected fields")
    for name in ("expected_count", "observed_count", "invented_count"):
        value = missingness[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise HistoricalDataError("missingness counts must be non-negative integers")
    missing_keys = missingness["missing_keys"]
    if not isinstance(missing_keys, list):
        raise HistoricalDataError("missing keys must be a list")
    keys = [_text(value, "missing key") for value in missing_keys]
    if len(keys) != len(set(keys)):
        raise HistoricalDataError("missing keys must be unique")
    if missingness["expected_count"] != missingness["observed_count"] + len(keys):
        raise HistoricalDataError("missingness counts do not reconcile")
    if missingness["invented_count"] != 0:
        raise HistoricalDataError("historical dataset cannot invent missing observations")

    normalized = {
        "dataset_id": _uuid(manifest["dataset_id"], "dataset_id"),
        "version": str(_sequence(manifest["version"], "version")),
        "content_hashes": hashes,
        "instrument_universe_version": _text(
            manifest["instrument_universe_version"], "instrument_universe_version"
        ),
        "calendar_version": _text(manifest["calendar_version"], "calendar_version"),
        "coverage": dict(manifest["coverage"]),
        "availability_policy": availability,
        "revision_policy": revision,
        "normalization_version": _text(
            manifest["normalization_version"], "normalization_version"
        ),
        "adjustment_policy": adjustment,
        "rights": rights,
        "missingness_report": missingness,
        "source_evidence": evidence,
        "created_at": _utc_text(created_at),
    }
    return normalized


class HistoricalVintageRegistry:
    """Append-only manifest registry; old dataset versions are immutable."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, dataset_id: str, version: int) -> Path:
        canonical_id = _uuid(dataset_id, "dataset_id")
        canonical_version = _sequence(version, "version")
        return self.root / canonical_id / f"{canonical_version}.json"

    def commit(self, manifest: Mapping[str, Any]) -> str:
        normalized = _validate_manifest(manifest)
        version = _sequence(normalized["version"], "version")
        path = self._path(normalized["dataset_id"], version)
        digest = "sha256:" + sha256(_canonical_bytes(normalized)).hexdigest()
        # The immutable check and publication share one cross-process
        # critical section. Otherwise two writers can both observe absence and
        # the later os.replace could silently win with different bytes.
        with durable_path_lock(path):
            if path.exists():
                try:
                    existing = strict_json_loads(path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as error:
                    raise HistoricalConflict("existing dataset manifest is unreadable") from error
                if existing != normalized:
                    raise HistoricalConflict("dataset version is immutable")
                return digest
            atomic_write_json(path, normalized)
        return digest

    def load(self, dataset_id: str, version: int) -> dict[str, Any]:
        canonical_id = _uuid(dataset_id, "dataset_id")
        canonical_version = _sequence(version, "version")
        path = self._path(canonical_id, canonical_version)
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            value = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise HistoricalDataError("dataset manifest is unreadable") from error
        manifest = _validate_manifest(value)
        if (
            manifest["dataset_id"] != canonical_id
            or _sequence(manifest["version"], "version") != canonical_version
        ):
            raise HistoricalConflict("dataset manifest identity differs from requested path")
        return manifest

    def digest(self, dataset_id: str, version: int) -> str:
        manifest = self.load(dataset_id, version)
        return "sha256:" + sha256(_canonical_bytes(manifest)).hexdigest()


    def resolve_market_population(
        self,
        dataset_id: str,
        version: int,
        *,
        manifest_digest: str,
        artifact_store: ArtifactStore,
        cutoff: datetime,
        events: Iterable[Mapping[str, Any]] | None = None,
    ) -> FrozenMarketPopulation:
        """Resolve one ArtifactStore-authenticated point-in-time market population.

        The canonical DatasetManifest binds content hashes and EvidenceRef objects.
        This resolver requires exactly one source_evidence entry whose digest is
        also a declared content hash, then authenticates that ArtifactStore object
        and requires the market-population media type. Optional caller events are
        only an exact-byte cache assertion.
        """

        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be exact ArtifactStore")
        canonical_id = _uuid(dataset_id, "dataset_id")
        canonical_version = _sequence(version, "version")
        expected_manifest_digest = _digest(manifest_digest, "manifest_digest")
        manifest = self.load(canonical_id, canonical_version)
        actual_manifest_digest = "sha256:" + sha256(
            _canonical_bytes(manifest)
        ).hexdigest()
        if actual_manifest_digest != expected_manifest_digest:
            raise HistoricalConflict("dataset manifest digest differs from requested identity")

        point = _utc(cutoff, "cutoff")
        manifest_cutoff = _utc(
            manifest["availability_policy"]["cutoff"],
            "availability cutoff",
        )
        if point > manifest_cutoff:
            raise HistoricalDataError(
                "population cutoff exceeds registered dataset availability cutoff"
            )

        if len(manifest["content_hashes"]) != 1:
            raise HistoricalDataError(
                "qualified market population manifest must declare exactly one content hash"
            )
        population_refs = [
            item
            for item in manifest["source_evidence"]
            if item["sha256"] in manifest["content_hashes"]
        ]
        if len(population_refs) != 1:
            raise HistoricalDataError(
                "dataset manifest must bind exactly one ArtifactStore content object "
                "through canonical source_evidence"
            )
        population_ref = population_refs[0]
        artifact_manifest, raw_population = artifact_store.read_authenticated_snapshot(
            population_ref["artifact_id"]
        )
        if artifact_manifest.get("artifact_id") != population_ref["artifact_id"]:
            raise HistoricalConflict(
                "market population artifact identity differs from dataset binding"
            )
        if artifact_manifest.get("sha256") != population_ref["sha256"]:
            raise HistoricalConflict(
                "market population artifact digest differs from dataset binding"
            )
        if artifact_manifest.get("media_type") != _MARKET_POPULATION_MEDIA_TYPE:
            raise HistoricalDataError(
                "market population artifact media type is not canonical"
            )
        expected_rights_id = population_ref.get("rights_id")
        if expected_rights_id is None:
            raise HistoricalDataError(
                "market population source evidence must bind rights_id"
            )
        if (
            artifact_manifest.get("rights", {}).get("rights_id")
            != expected_rights_id
        ):
            raise HistoricalConflict(
                "market population artifact rights identity differs from dataset binding"
            )
        source_content_digest = "sha256:" + sha256(raw_population).hexdigest()
        if source_content_digest != population_ref["sha256"]:
            raise HistoricalConflict(
                "market population authenticated bytes differ from dataset binding"
            )

        try:
            population_document = strict_json_loads(raw_population.decode("utf-8"))
        except (UnicodeDecodeError, TypeError, ValueError) as error:
            raise HistoricalDataError(
                "market population artifact is not valid canonical UTF-8 JSON"
            ) from error
        if (
            not isinstance(population_document, Mapping)
            or set(population_document)
            != {"artifact_type", "events", "schema_version"}
            or population_document.get("artifact_type")
            != "AUTOTRADE_MARKET_EVENT_POPULATION"
            or population_document.get("schema_version") != "1.0.0"
            or not isinstance(population_document.get("events"), list)
            or not population_document["events"]
        ):
            raise HistoricalDataError("market population artifact contract is invalid")

        authoritative_events = population_document["events"]
        canonical_population = canonical_market_event_population_bytes(
            authoritative_events
        )
        if canonical_population != raw_population:
            raise HistoricalConflict(
                "market population artifact bytes are not canonical"
            )
        detached = []
        for raw in authoritative_events:
            if not isinstance(raw, Mapping):
                raise HistoricalDataError("market event population must contain objects")
            detached.append(
                dict(strict_json_loads(_canonical_bytes(dict(raw)).decode("utf-8")))
            )

        if events is not None:
            asserted_population = canonical_market_event_population_bytes(events)
            if asserted_population != raw_population:
                raise HistoricalConflict(
                    "caller market event cache differs from authenticated artifact bytes"
                )

        visible_history = causal_market_event_history(detached, point)
        visible_json = tuple(
            _canonical_bytes(row).decode("utf-8")
            for row in visible_history
        )
        visible_identities = []
        for row, encoded in zip(visible_history, visible_json):
            raw_evidence = _evidence(row["raw_evidence_ref"])
            visible_identities.append(
                {
                    "event_id": _uuid(row["event_id"], "event_id"),
                    "instrument_version": _text(
                        row["instrument_version"],
                        "instrument_version",
                    ),
                    "revision": _non_negative_sequence(row["revision"], "revision"),
                    "source_event_at": _utc_text(
                        _utc(row["source_event_at"], "source_event_at")
                    ),
                    "available_at": _utc_text(
                        _utc(row["available_at"], "available_at")
                    ),
                    "ingested_at": _utc_text(
                        _utc(row["ingested_at"], "ingested_at")
                    ),
                    "raw_evidence": raw_evidence,
                    "event_digest": "sha256:" + sha256(
                        encoded.encode("utf-8")
                    ).hexdigest(),
                }
            )
        material = {
            "schema_version": "2.0.0",
            "dataset_id": canonical_id,
            "version": canonical_version,
            "manifest_digest": actual_manifest_digest,
            "cutoff": _utc_text(point),
            "source_artifact": dict(population_ref),
            "source_content_digest": source_content_digest,
            "instrument_universe_version": manifest["instrument_universe_version"],
            "calendar_version": manifest["calendar_version"],
            "normalization_version": manifest["normalization_version"],
            "availability_policy": manifest["availability_policy"],
            "revision_policy": manifest["revision_policy"],
            "visible_event_revisions": visible_identities,
        }
        fingerprint = "sha256:" + sha256(_canonical_bytes(material)).hexdigest()
        return FrozenMarketPopulation(
            dataset_id=canonical_id,
            version=canonical_version,
            manifest_digest=actual_manifest_digest,
            cutoff=point,
            source_artifact_id=population_ref["artifact_id"],
            source_content_digest=source_content_digest,
            visible_event_json=visible_json,
            fingerprint=fingerprint,
        )

    def revalidate_market_population(
        self,
        population: FrozenMarketPopulation,
        *,
        artifact_store: ArtifactStore,
    ) -> FrozenMarketPopulation:
        """Re-resolve a frozen population through its canonical persisted authorities.

        A FrozenMarketPopulation is a value object, not an issuer token.  Any
        qualified consumer that receives one must cross this seam before using
        its rows as authoritative scientific input.
        """

        if type(population) is not FrozenMarketPopulation:
            raise TypeError("population must be FrozenMarketPopulation")
        if type(artifact_store) is not ArtifactStore:
            raise TypeError("artifact_store must be exact ArtifactStore")
        canonical = self.resolve_market_population(
            population.dataset_id,
            population.version,
            manifest_digest=population.manifest_digest,
            artifact_store=artifact_store,
            cutoff=population.cutoff,
            events=None,
        )
        if canonical != population:
            raise HistoricalConflict(
                "frozen market population differs from authenticated authority"
            )
        return canonical
