"""Canonical raw target-host measurement artifact for WP-65.

This module is a measurement-evidence boundary, not a load generator, scheduler,
performance policy, signer, release authority, or trading authority. It keeps the
existing :mod:`performance_qualification` evaluator and RuntimeCampaignEvidence
as the only budget policy/evidence authorities.

The purpose of this artifact is narrower: terminal campaign summaries must be
recomputable from retained identified raw timing/resource records rather than
from caller-authored healthy integer tuples. Version 1 deliberately supports
staleness only when both endpoints are expressed in one explicitly bound host
monotonic clock domain. External/provider wall-clock freshness needs a separate
clock-authority contract and cannot be smuggled into this schema.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from types import MappingProxyType
from typing import Mapping, Sequence
from uuid import UUID

from .performance_qualification import RuntimeBudgetSpec
from .persistence import JournalStore
from .runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
    collect_runtime_campaign_evidence,
)


class RuntimeTargetHostMeasurementError(ValueError):
    """Raised when retained target-host measurement evidence is not canonical."""


SCHEMA_VERSION = "1.0.0"
MEASUREMENT_METHOD_ID = "wp65-target-host-monotonic-v1"
MEASUREMENT_METHOD_VERSION = "1.0.0"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _git_sha(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a lowercase 40- or 64-character Git SHA"
        )
    return text


def _sha256(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be canonical sha256:<64 hex>"
        )
    return text


def _uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != text:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a canonical UUID"
        )
    return canonical


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a positive integer"
        )
    return value


def _elapsed_us(start_ns: int, end_ns: int, *, name: str) -> int:
    start = _non_negative_int(start_ns, name=f"{name}_start_ns")
    end = _non_negative_int(end_ns, name=f"{name}_end_ns")
    if end < start:
        raise RuntimeTargetHostMeasurementError(f"{name} clock moved backwards")
    return (end - start + 999) // 1_000


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _reject_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement contains duplicate JSON object key"
            )
        result[key] = value
    return result


def _strict_json(raw: bytes, *, name: str) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostMeasurementError(f"{name} must be non-empty bytes")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object,
            parse_constant=lambda token: (_ for _ in ()).throw(
                RuntimeTargetHostMeasurementError(
                    f"{name} contains invalid JSON constant {token}"
                )
            ),
        )
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeTargetHostMeasurementError(
            f"{name} is not valid UTF-8 JSON"
        ) from error
    if type(value) is not dict:
        raise RuntimeTargetHostMeasurementError(f"{name} must be a JSON object")
    return value


@dataclass(frozen=True, slots=True)
class FinancialTargetHostSample:
    """Identified raw financial timing endpoints in one host clock domain."""

    sample_id: str
    event_id: str
    journal_sequence: int
    latency_start_monotonic_ns: int
    latency_end_monotonic_ns: int
    staleness_source_monotonic_ns: int
    staleness_observed_monotonic_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, name="sample_id"))
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(
            self,
            "journal_sequence",
            _positive_int(self.journal_sequence, name="journal_sequence"),
        )
        for field in (
            "latency_start_monotonic_ns",
            "latency_end_monotonic_ns",
            "staleness_source_monotonic_ns",
            "staleness_observed_monotonic_ns",
        ):
            object.__setattr__(
                self,
                field,
                _non_negative_int(getattr(self, field), name=field),
            )
        _elapsed_us(
            self.latency_start_monotonic_ns,
            self.latency_end_monotonic_ns,
            name="financial_latency",
        )
        _elapsed_us(
            self.staleness_source_monotonic_ns,
            self.staleness_observed_monotonic_ns,
            name="financial_staleness",
        )

    @property
    def latency_us(self) -> int:
        return _elapsed_us(
            self.latency_start_monotonic_ns,
            self.latency_end_monotonic_ns,
            name="financial_latency",
        )

    @property
    def staleness_us(self) -> int:
        return _elapsed_us(
            self.staleness_source_monotonic_ns,
            self.staleness_observed_monotonic_ns,
            name="financial_staleness",
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "journal_sequence": self.journal_sequence,
            "latency_end_monotonic_ns": self.latency_end_monotonic_ns,
            "latency_start_monotonic_ns": self.latency_start_monotonic_ns,
            "latency_us": self.latency_us,
            "sample_id": self.sample_id,
            "staleness_observed_monotonic_ns": self.staleness_observed_monotonic_ns,
            "staleness_source_monotonic_ns": self.staleness_source_monotonic_ns,
            "staleness_us": self.staleness_us,
        }

    @classmethod
    def from_payload(cls, value: object) -> "FinancialTargetHostSample":
        if type(value) is not dict:
            raise RuntimeTargetHostMeasurementError(
                "financial sample must be a JSON object"
            )
        expected = {
            "event_id",
            "journal_sequence",
            "latency_end_monotonic_ns",
            "latency_start_monotonic_ns",
            "latency_us",
            "sample_id",
            "staleness_observed_monotonic_ns",
            "staleness_source_monotonic_ns",
            "staleness_us",
        }
        if set(value) != expected:
            raise RuntimeTargetHostMeasurementError(
                "financial sample fields are non-canonical"
            )
        sample = cls(
            sample_id=value["sample_id"],
            event_id=value["event_id"],
            journal_sequence=value["journal_sequence"],
            latency_start_monotonic_ns=value["latency_start_monotonic_ns"],
            latency_end_monotonic_ns=value["latency_end_monotonic_ns"],
            staleness_source_monotonic_ns=value["staleness_source_monotonic_ns"],
            staleness_observed_monotonic_ns=value[
                "staleness_observed_monotonic_ns"
            ],
        )
        if value["latency_us"] != sample.latency_us:
            raise RuntimeTargetHostMeasurementError(
                "financial sample latency_us does not match raw monotonic endpoints"
            )
        if value["staleness_us"] != sample.staleness_us:
            raise RuntimeTargetHostMeasurementError(
                "financial sample staleness_us does not match raw monotonic endpoints"
            )
        return sample


@dataclass(frozen=True, slots=True)
class ResearchInterferenceSample:
    """One retained research/contention delay interval on the host monotonic clock."""

    sample_id: str
    phase: str
    start_monotonic_ns: int
    end_monotonic_ns: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, name="sample_id"))
        object.__setattr__(self, "phase", _text(self.phase, name="phase"))
        object.__setattr__(
            self,
            "start_monotonic_ns",
            _non_negative_int(self.start_monotonic_ns, name="start_monotonic_ns"),
        )
        object.__setattr__(
            self,
            "end_monotonic_ns",
            _non_negative_int(self.end_monotonic_ns, name="end_monotonic_ns"),
        )
        _elapsed_us(
            self.start_monotonic_ns,
            self.end_monotonic_ns,
            name="research_interference",
        )

    @property
    def interference_us(self) -> int:
        return _elapsed_us(
            self.start_monotonic_ns,
            self.end_monotonic_ns,
            name="research_interference",
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "end_monotonic_ns": self.end_monotonic_ns,
            "interference_us": self.interference_us,
            "phase": self.phase,
            "sample_id": self.sample_id,
            "start_monotonic_ns": self.start_monotonic_ns,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ResearchInterferenceSample":
        if type(value) is not dict:
            raise RuntimeTargetHostMeasurementError(
                "research sample must be a JSON object"
            )
        expected = {
            "end_monotonic_ns",
            "interference_us",
            "phase",
            "sample_id",
            "start_monotonic_ns",
        }
        if set(value) != expected:
            raise RuntimeTargetHostMeasurementError(
                "research sample fields are non-canonical"
            )
        sample = cls(
            sample_id=value["sample_id"],
            phase=value["phase"],
            start_monotonic_ns=value["start_monotonic_ns"],
            end_monotonic_ns=value["end_monotonic_ns"],
        )
        if value["interference_us"] != sample.interference_us:
            raise RuntimeTargetHostMeasurementError(
                "research interference_us does not match raw monotonic endpoints"
            )
        return sample


@dataclass(frozen=True, slots=True)
class ResourceTargetHostSample:
    """One monotonic host/resource telemetry sample with stable metric keys."""

    sample_id: str
    monotonic_ns: int
    phase: str
    metrics: Mapping[str, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "sample_id", _text(self.sample_id, name="sample_id"))
        object.__setattr__(self, "phase", _text(self.phase, name="phase"))
        object.__setattr__(
            self,
            "monotonic_ns",
            _non_negative_int(self.monotonic_ns, name="monotonic_ns"),
        )
        if not isinstance(self.metrics, Mapping) or not self.metrics:
            raise RuntimeTargetHostMeasurementError(
                "resource sample metrics must be a non-empty mapping"
            )
        normalized: dict[str, int] = {}
        for key, value in self.metrics.items():
            metric = _text(key, name="resource metric")
            if metric in normalized:
                raise RuntimeTargetHostMeasurementError(
                    "resource metric keys must be unique"
                )
            normalized[metric] = _non_negative_int(
                value,
                name=f"resource metric {metric}",
            )
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(dict(sorted(normalized.items()))),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "metrics": dict(self.metrics),
            "monotonic_ns": self.monotonic_ns,
            "phase": self.phase,
            "sample_id": self.sample_id,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ResourceTargetHostSample":
        if type(value) is not dict:
            raise RuntimeTargetHostMeasurementError(
                "resource sample must be a JSON object"
            )
        expected = {"metrics", "monotonic_ns", "phase", "sample_id"}
        if set(value) != expected:
            raise RuntimeTargetHostMeasurementError(
                "resource sample fields are non-canonical"
            )
        return cls(
            sample_id=value["sample_id"],
            monotonic_ns=value["monotonic_ns"],
            phase=value["phase"],
            metrics=value["metrics"],
        )


@dataclass(frozen=True, slots=True)
class TargetHostMeasurementArtifact:
    """Immutable recomputable raw measurement bundle for one exact WP-65 cut."""

    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    plan_digest: str
    journal_taxonomy_digest: str
    journal_store_identity_digest: str
    start_journal_sequence: int
    end_journal_sequence: int
    monotonic_clock_id: str
    staleness_basis: str
    research_interference_basis: str
    financial_samples: tuple[FinancialTargetHostSample, ...]
    research_samples: tuple[ResearchInterferenceSample, ...]
    resource_samples: tuple[ResourceTargetHostSample, ...]
    measurement_method_id: str = MEASUREMENT_METHOD_ID
    measurement_method_version: str = MEASUREMENT_METHOD_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeTargetHostMeasurementError(
                "unsupported target-host measurement schema_version"
            )
        if self.measurement_method_id != MEASUREMENT_METHOD_ID:
            raise RuntimeTargetHostMeasurementError(
                "unsupported target-host measurement_method_id"
            )
        if self.measurement_method_version != MEASUREMENT_METHOD_VERSION:
            raise RuntimeTargetHostMeasurementError(
                "unsupported target-host measurement_method_version"
            )
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(
            self,
            "release_artifact_id",
            _uuid(self.release_artifact_id, name="release_artifact_id"),
        )
        for field in (
            "release_artifact_sha256",
            "spec_digest",
            "configuration_hash",
            "host_fingerprint",
            "workload_profile_hash",
            "plan_digest",
            "journal_taxonomy_digest",
            "journal_store_identity_digest",
        ):
            object.__setattr__(
                self, field, _sha256(getattr(self, field), name=field)
            )
        for field in (
            "scenario_id",
            "monotonic_clock_id",
            "staleness_basis",
            "research_interference_basis",
        ):
            object.__setattr__(
                self, field, _text(getattr(self, field), name=field)
            )
        start = _non_negative_int(
            self.start_journal_sequence, name="start_journal_sequence"
        )
        end = _non_negative_int(self.end_journal_sequence, name="end_journal_sequence")
        if end < start:
            raise RuntimeTargetHostMeasurementError(
                "end_journal_sequence cannot precede start_journal_sequence"
            )
        object.__setattr__(self, "start_journal_sequence", start)
        object.__setattr__(self, "end_journal_sequence", end)

        financial: list[FinancialTargetHostSample] = []
        seen_sample_ids: set[str] = set()
        seen_event_ids: set[str] = set()
        previous_sequence = start
        for value in self.financial_samples:
            if type(value) is not FinancialTargetHostSample:
                raise TypeError(
                    "financial_samples must contain exact FinancialTargetHostSample"
                )
            sample = FinancialTargetHostSample.from_payload(value.canonical_payload())
            if sample.sample_id in seen_sample_ids:
                raise RuntimeTargetHostMeasurementError(
                    "financial sample IDs must be unique"
                )
            if sample.event_id in seen_event_ids:
                raise RuntimeTargetHostMeasurementError(
                    "financial samples must bind unique event IDs"
                )
            if sample.journal_sequence <= previous_sequence:
                raise RuntimeTargetHostMeasurementError(
                    "financial samples must follow strict journal order after the cut"
                )
            if sample.journal_sequence > end:
                raise RuntimeTargetHostMeasurementError(
                    "financial sample lies beyond measurement end cut"
                )
            previous_sequence = sample.journal_sequence
            seen_sample_ids.add(sample.sample_id)
            seen_event_ids.add(sample.event_id)
            financial.append(sample)
        object.__setattr__(self, "financial_samples", tuple(financial))

        research: list[ResearchInterferenceSample] = []
        previous_research_start = -1
        for value in self.research_samples:
            if type(value) is not ResearchInterferenceSample:
                raise TypeError(
                    "research_samples must contain exact ResearchInterferenceSample"
                )
            sample = ResearchInterferenceSample.from_payload(value.canonical_payload())
            if sample.sample_id in seen_sample_ids:
                raise RuntimeTargetHostMeasurementError(
                    "measurement sample IDs must be globally unique"
                )
            if sample.start_monotonic_ns < previous_research_start:
                raise RuntimeTargetHostMeasurementError(
                    "research samples must be monotonic in capture order"
                )
            previous_research_start = sample.start_monotonic_ns
            seen_sample_ids.add(sample.sample_id)
            research.append(sample)
        object.__setattr__(self, "research_samples", tuple(research))

        resources: list[ResourceTargetHostSample] = []
        previous_resource_time = -1
        metric_keys: tuple[str, ...] | None = None
        for value in self.resource_samples:
            if type(value) is not ResourceTargetHostSample:
                raise TypeError(
                    "resource_samples must contain exact ResourceTargetHostSample"
                )
            sample = ResourceTargetHostSample.from_payload(value.canonical_payload())
            if sample.sample_id in seen_sample_ids:
                raise RuntimeTargetHostMeasurementError(
                    "measurement sample IDs must be globally unique"
                )
            if sample.monotonic_ns < previous_resource_time:
                raise RuntimeTargetHostMeasurementError(
                    "resource samples must be monotonic in capture order"
                )
            keys = tuple(sample.metrics)
            if metric_keys is None:
                metric_keys = keys
            elif keys != metric_keys:
                raise RuntimeTargetHostMeasurementError(
                    "resource samples must retain one stable metric-key set"
                )
            previous_resource_time = sample.monotonic_ns
            seen_sample_ids.add(sample.sample_id)
            resources.append(sample)
        if not resources:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement requires retained resource samples"
            )
        object.__setattr__(self, "resource_samples", tuple(resources))

    @property
    def financial_event_ids(self) -> tuple[str, ...]:
        return tuple(sample.event_id for sample in self.financial_samples)

    @property
    def financial_latency_us(self) -> tuple[int, ...]:
        return tuple(sample.latency_us for sample in self.financial_samples)

    @property
    def financial_staleness_us(self) -> tuple[int, ...]:
        return tuple(sample.staleness_us for sample in self.financial_samples)

    @property
    def research_interference_us(self) -> tuple[int, ...]:
        return tuple(sample.interference_us for sample in self.research_samples)

    @property
    def resource_metric_maxima(self) -> Mapping[str, int]:
        keys = tuple(self.resource_samples[0].metrics)
        return MappingProxyType(
            {
                key: max(sample.metrics[key] for sample in self.resource_samples)
                for key in keys
            }
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "configuration_hash": self.configuration_hash,
            "end_journal_sequence": self.end_journal_sequence,
            "financial_samples": [
                value.canonical_payload() for value in self.financial_samples
            ],
            "host_fingerprint": self.host_fingerprint,
            "journal_store_identity_digest": self.journal_store_identity_digest,
            "journal_taxonomy_digest": self.journal_taxonomy_digest,
            "measurement_method_id": self.measurement_method_id,
            "measurement_method_version": self.measurement_method_version,
            "monotonic_clock_id": self.monotonic_clock_id,
            "plan_digest": self.plan_digest,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "research_interference_basis": self.research_interference_basis,
            "research_samples": [
                value.canonical_payload() for value in self.research_samples
            ],
            "resource_samples": [
                value.canonical_payload() for value in self.resource_samples
            ],
            "scenario_id": self.scenario_id,
            "schema_version": self.schema_version,
            "source_sha": self.source_sha,
            "spec_digest": self.spec_digest,
            "staleness_basis": self.staleness_basis,
            "start_journal_sequence": self.start_journal_sequence,
            "workload_profile_hash": self.workload_profile_hash,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def parse(cls, raw: bytes) -> "TargetHostMeasurementArtifact":
        value = _strict_json(raw, name="target-host measurement artifact")
        expected = {
            "configuration_hash",
            "end_journal_sequence",
            "financial_samples",
            "host_fingerprint",
            "journal_store_identity_digest",
            "journal_taxonomy_digest",
            "measurement_method_id",
            "measurement_method_version",
            "monotonic_clock_id",
            "plan_digest",
            "release_artifact_id",
            "release_artifact_sha256",
            "research_interference_basis",
            "research_samples",
            "resource_samples",
            "scenario_id",
            "schema_version",
            "source_sha",
            "spec_digest",
            "staleness_basis",
            "start_journal_sequence",
            "workload_profile_hash",
        }
        if set(value) != expected:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement artifact fields are non-canonical"
            )
        financial_raw = value["financial_samples"]
        research_raw = value["research_samples"]
        resource_raw = value["resource_samples"]
        if type(financial_raw) is not list:
            raise RuntimeTargetHostMeasurementError("financial_samples must be an array")
        if type(research_raw) is not list:
            raise RuntimeTargetHostMeasurementError("research_samples must be an array")
        if type(resource_raw) is not list:
            raise RuntimeTargetHostMeasurementError("resource_samples must be an array")
        artifact = cls(
            source_sha=value["source_sha"],
            release_artifact_id=value["release_artifact_id"],
            release_artifact_sha256=value["release_artifact_sha256"],
            scenario_id=value["scenario_id"],
            spec_digest=value["spec_digest"],
            configuration_hash=value["configuration_hash"],
            host_fingerprint=value["host_fingerprint"],
            workload_profile_hash=value["workload_profile_hash"],
            plan_digest=value["plan_digest"],
            journal_taxonomy_digest=value["journal_taxonomy_digest"],
            journal_store_identity_digest=value["journal_store_identity_digest"],
            start_journal_sequence=value["start_journal_sequence"],
            end_journal_sequence=value["end_journal_sequence"],
            monotonic_clock_id=value["monotonic_clock_id"],
            staleness_basis=value["staleness_basis"],
            research_interference_basis=value["research_interference_basis"],
            financial_samples=tuple(
                FinancialTargetHostSample.from_payload(item) for item in financial_raw
            ),
            research_samples=tuple(
                ResearchInterferenceSample.from_payload(item) for item in research_raw
            ),
            resource_samples=tuple(
                ResourceTargetHostSample.from_payload(item) for item in resource_raw
            ),
            measurement_method_id=value["measurement_method_id"],
            measurement_method_version=value["measurement_method_version"],
            schema_version=value["schema_version"],
        )
        if artifact.canonical_bytes() != raw:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement artifact bytes are not canonical JSON"
            )
        return artifact

    def require_campaign_binding(
        self,
        *,
        spec: RuntimeBudgetSpec,
        plan: RuntimeCampaignPlan,
        cut: RuntimeCampaignCut,
    ) -> None:
        if type(spec) is not RuntimeBudgetSpec:
            raise TypeError("spec must be exact RuntimeBudgetSpec")
        if type(plan) is not RuntimeCampaignPlan:
            raise TypeError("plan must be exact RuntimeCampaignPlan")
        if type(cut) is not RuntimeCampaignCut:
            raise TypeError("cut must be exact RuntimeCampaignCut")
        if plan.release_artifact_sha256 is None:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement requires exact delivered artifact SHA"
            )
        checks = {
            "source_sha": (self.source_sha, spec.release_sha),
            "scenario_id": (self.scenario_id, spec.scenario_id),
            "spec_digest": (self.spec_digest, spec.digest),
            "configuration_hash": (
                self.configuration_hash,
                spec.configuration_hash,
            ),
            "host_fingerprint": (self.host_fingerprint, spec.host_fingerprint),
            "workload_profile_hash": (
                self.workload_profile_hash,
                plan.workload_profile_hash,
            ),
            "plan_digest": (self.plan_digest, plan.digest),
            "journal_taxonomy_digest": (
                self.journal_taxonomy_digest,
                plan.journal_taxonomy_digest,
            ),
            "journal_store_identity_digest": (
                self.journal_store_identity_digest,
                cut.journal_store_identity_digest,
            ),
            "start_journal_sequence": (
                self.start_journal_sequence,
                cut.start_journal_sequence,
            ),
            "release_artifact_sha256": (
                self.release_artifact_sha256,
                plan.release_artifact_sha256,
            ),
        }
        mismatches = [name for name, (actual, expected) in checks.items() if actual != expected]
        if mismatches:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement belongs to another campaign identity: "
                + ", ".join(mismatches)
            )

    def require_evidence_match(self, evidence: RuntimeCampaignEvidence) -> None:
        if type(evidence) is not RuntimeCampaignEvidence:
            raise TypeError("evidence must be exact RuntimeCampaignEvidence")
        if evidence.start_journal_sequence != self.start_journal_sequence:
            raise RuntimeTargetHostMeasurementError(
                "measurement start cut does not match campaign evidence"
            )
        if evidence.end_journal_sequence != self.end_journal_sequence:
            raise RuntimeTargetHostMeasurementError(
                "measurement end cut does not match campaign evidence"
            )
        recovered = tuple(
            (event_id, sequence)
            for event_id, _payload_hash, sequence in evidence.recovered_financial_event_bindings
        )
        measured = tuple(
            (sample.event_id, sample.journal_sequence) for sample in self.financial_samples
        )
        if measured != recovered:
            raise RuntimeTargetHostMeasurementError(
                "measurement financial identities do not exactly match recovered journal events"
            )
        if evidence.financial_latency_us != self.financial_latency_us:
            raise RuntimeTargetHostMeasurementError(
                "campaign latency summary does not recompute from retained measurement"
            )
        if evidence.financial_staleness_us != self.financial_staleness_us:
            raise RuntimeTargetHostMeasurementError(
                "campaign staleness summary does not recompute from retained measurement"
            )
        if evidence.research_interference_us != self.research_interference_us:
            raise RuntimeTargetHostMeasurementError(
                "campaign research summary does not recompute from retained measurement"
            )
        if evidence.resource_evidence_hash != self.digest:
            raise RuntimeTargetHostMeasurementError(
                "campaign resource evidence hash does not bind retained measurement"
            )
        if dict(evidence.resource_metrics) != dict(self.resource_metric_maxima):
            raise RuntimeTargetHostMeasurementError(
                "campaign resource summary does not recompute from retained measurement"
            )


def collect_runtime_campaign_evidence_from_measurement_artifact(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
    cut: RuntimeCampaignCut,
    measurement: TargetHostMeasurementArtifact,
    max_events: int = 100_000,
) -> RuntimeCampaignEvidence:
    """Derive existing RuntimeCampaignEvidence from one retained raw artifact.

    The artifact is snapshotted through canonical bytes before use. The existing
    campaign collector still owns JournalStore conservation/backlog/end-cut and
    duration authority. After collection, the exact durable event identities and
    frozen end cut must match the retained measurement artifact or the operation
    fails closed.
    """

    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    measurement = TargetHostMeasurementArtifact.parse(measurement.canonical_bytes())
    measurement.require_campaign_binding(spec=spec, plan=plan, cut=cut)

    evidence = collect_runtime_campaign_evidence(
        journal=journal,
        spec=spec,
        plan=plan,
        cut=cut,
        financial_latency_us=measurement.financial_latency_us,
        financial_staleness_us=measurement.financial_staleness_us,
        research_interference_us=measurement.research_interference_us,
        resource_evidence_hash=measurement.digest,
        resource_metrics=dict(measurement.resource_metric_maxima),
        max_events=max_events,
    )
    measurement.require_evidence_match(evidence)

    campaign_end_upper_ns = cut.started_monotonic_ns + evidence.observed_duration_us * 1_000
    for sample in measurement.financial_samples:
        if sample.latency_start_monotonic_ns < cut.started_monotonic_ns:
            raise RuntimeTargetHostMeasurementError(
                "financial latency sample starts before campaign monotonic cut"
            )
        if sample.latency_end_monotonic_ns > campaign_end_upper_ns:
            raise RuntimeTargetHostMeasurementError(
                "financial latency sample ends after campaign monotonic cut"
            )
        if sample.staleness_observed_monotonic_ns > campaign_end_upper_ns:
            raise RuntimeTargetHostMeasurementError(
                "financial staleness observation occurs after campaign end"
            )
    for sample in measurement.research_samples:
        if sample.start_monotonic_ns < cut.started_monotonic_ns:
            raise RuntimeTargetHostMeasurementError(
                "research sample starts before campaign monotonic cut"
            )
        if sample.end_monotonic_ns > campaign_end_upper_ns:
            raise RuntimeTargetHostMeasurementError(
                "research sample ends after campaign monotonic cut"
            )
    for sample in measurement.resource_samples:
        if not cut.started_monotonic_ns <= sample.monotonic_ns <= campaign_end_upper_ns:
            raise RuntimeTargetHostMeasurementError(
                "resource sample lies outside campaign monotonic cut"
            )
    return evidence
