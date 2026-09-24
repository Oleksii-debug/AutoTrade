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

from autotrade_research.artifacts.durable_publish import atomic_write_json, durable_path_lock
from autotrade_research.io.strict_json import strict_json_loads


class HistoricalDataError(ValueError):
    pass


class HistoricalConflict(HistoricalDataError):
    pass


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


def _evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise HistoricalDataError("source evidence must be an object")
    required = {"artifact_id", "sha256", "observed_at"}
    if not required.issubset(value):
        raise HistoricalDataError("source evidence is incomplete")
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


def point_in_time_market_events(
    events: Iterable[Mapping[str, Any]],
    cutoff: datetime,
) -> tuple[dict[str, Any], ...]:
    """Return only revisions that were available by the requested cutoff.

    Revisions after the cutoff remain invisible even if they are now known.
    Duplicate identities with different bytes are rejected rather than guessed.
    """

    point = _utc(cutoff, "cutoff")
    selected: dict[str, tuple[int, bytes, dict[str, Any]]] = {}
    for raw in events:
        if not isinstance(raw, Mapping):
            raise HistoricalDataError("market event must be an object")
        event = dict(raw)
        event_id = _uuid(event.get("event_id"), "event_id")
        _text(event.get("instrument_version"), "instrument_version")
        revision = _sequence(event.get("revision"), "revision")
        available = _utc(event.get("available_at"), "available_at")
        source_at = _utc(event.get("source_event_at"), "source_event_at")
        ingested = _utc(event.get("ingested_at"), "ingested_at")
        if ingested < available:
            raise HistoricalDataError("ingested_at cannot precede evidenced available_at")
        if available > point:
            continue

        canonical = _canonical_bytes(event)
        previous = selected.get(event_id)
        if previous is None or revision > previous[0]:
            selected[event_id] = (revision, canonical, event)
        elif revision == previous[0] and canonical != previous[1]:
            raise HistoricalConflict("same event revision has conflicting bytes")

    rows = [item[2] for item in selected.values()]
    rows.sort(
        key=lambda row: (
            _utc(row["source_event_at"], "source_event_at"),
            row["event_id"],
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
        if raw_value * factor != adjusted_value:
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
    if set(manifest) != required:
        missing = required - set(manifest)
        unknown = set(manifest) - required
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
    _utc(availability.get("cutoff"), "availability cutoff")
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
    if missingness.get("invented_count") != 0:
        raise HistoricalDataError("historical dataset cannot invent missing observations")

    return {
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
        "created_at": _utc_text(_utc(manifest["created_at"], "created_at")),
    }


class HistoricalVintageRegistry:
    """Append-only manifest registry; old dataset versions are immutable."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, dataset_id: str, version: int) -> Path:
        return self.root / _uuid(dataset_id, "dataset_id") / f"{version}.json"

    def commit(self, manifest: Mapping[str, Any]) -> str:
        normalized = _validate_manifest(manifest)
        version = _sequence(normalized["version"], "version")
        path = self._path(normalized["dataset_id"], version)
        digest = "sha256:" + sha256(_canonical_bytes(normalized)).hexdigest()

        # The immutable check and publication must share one cross-process
        # critical section. Otherwise two writers can both observe absence and
        # the later os.replace silently wins with different bytes.
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
        path = self._path(dataset_id, version)
        if not path.is_file():
            raise FileNotFoundError(path)
        try:
            value = strict_json_loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as error:
            raise HistoricalDataError("dataset manifest is unreadable") from error
        return _validate_manifest(value)

    def digest(self, dataset_id: str, version: int) -> str:
        manifest = self.load(dataset_id, version)
        return "sha256:" + sha256(_canonical_bytes(manifest)).hexdigest()
