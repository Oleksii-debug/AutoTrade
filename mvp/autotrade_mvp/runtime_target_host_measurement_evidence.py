"""Semantic verification for retained WP-65 target-host measurement payloads.

The signed target-host profile proves artifact integrity, identity binding and raw
payload retention. This module adds the missing semantic bridge from three raw
measurement families back to the canonical ``RuntimeLoadObservation`` carried by
the campaign evidence. It creates no measurements, trust roots, chronology,
release authority, provider authority or trading authority.

Staleness and interference payloads retain the exact microsecond series consumed
by the existing runtime-budget evaluator. Resource/pressure evidence retains only
the already-canonical campaign conservation/backlog/throughput facts; it does not
invent a universal CPU/RSS budget that WP-65 never declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Callable

from autotrade_runtime.artifacts import (
    ArtifactIntegrityError,
    ArtifactStore,
    trusted_authenticated_reader,
)
from autotrade_runtime.strict_json import (
    DuplicateJsonKeyError,
    InvalidJsonDomainError,
    NonStandardJsonConstantError,
    strict_json_loads,
)

from .runtime_target_host_campaign import (
    ParsedRuntimeTargetHostCampaign,
    RuntimeTargetHostCampaignError,
)
from .runtime_target_host_qualification import (
    AcceptedRuntimeTargetHostQualification,
    CAMPAIGN_EVIDENCE_KIND,
    INTERFERENCE_EVIDENCE_KIND,
    RESOURCE_EVIDENCE_KIND,
    STALENESS_EVIDENCE_KIND,
)


SCHEMA_VERSION = "1.0.0"
STALENESS_EVIDENCE_TYPE = "AUTOTRADE_RUNTIME_TARGET_HOST_STALENESS"
INTERFERENCE_EVIDENCE_TYPE = "AUTOTRADE_RUNTIME_TARGET_HOST_INTERFERENCE"
RESOURCE_EVIDENCE_TYPE = "AUTOTRADE_RUNTIME_TARGET_HOST_RESOURCES"
STALENESS_METRIC = "financial_staleness_us"
INTERFERENCE_METRIC = "research_interference_us"
UNIT_MICROSECONDS = "microseconds"
STALENESS_COLLECTOR_ID = "autotrade-runtime-target-host-staleness"
INTERFERENCE_COLLECTOR_ID = "autotrade-runtime-target-host-interference"
RESOURCE_COLLECTOR_ID = "autotrade-runtime-target-host-pressure"
COLLECTOR_VERSION = "1.0.0"


class RuntimeTargetHostMeasurementEvidenceError(ValueError):
    """Raised when retained target-host measurement semantics do not reconcile."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _strict_object(raw: bytes, *, name: str) -> dict[str, object]:
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} must be non-empty bytes"
        )
    try:
        value = strict_json_loads(raw.decode("utf-8"))
    except UnicodeError as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} is not valid UTF-8 JSON"
        ) from error
    except DuplicateJsonKeyError as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} contains duplicate JSON object key"
        ) from error
    except NonStandardJsonConstantError as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} contains invalid JSON constant"
        ) from error
    except InvalidJsonDomainError as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} exceeds bounded JSON domain"
        ) from error
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} is not valid UTF-8 JSON"
        ) from error
    if type(value) is not dict:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} must be a JSON object"
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} must be a positive integer"
        )
    return value


def _series(value: object, *, name: str) -> tuple[int, ...]:
    if type(value) is not list:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} must be a JSON array"
        )
    result = tuple(_non_negative_int(item, name=name) for item in value)
    if not result:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"{name} cannot be empty for terminal target-host evidence"
        )
    return result


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostTimingEvidence:
    """Canonical retained microsecond series consumed by runtime-budget evaluation."""

    evidence_type: str
    metric: str
    samples_us: tuple[int, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "unsupported target-host timing evidence schema_version"
            )
        allowed = {
            STALENESS_EVIDENCE_TYPE: STALENESS_METRIC,
            INTERFERENCE_EVIDENCE_TYPE: INTERFERENCE_METRIC,
        }
        if allowed.get(self.evidence_type) != self.metric:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing evidence type/metric pair is unsupported"
            )
        if type(self.samples_us) is not tuple:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "samples_us must be an exact tuple"
            )
        normalized = tuple(
            _non_negative_int(value, name="samples_us") for value in self.samples_us
        )
        if not normalized:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "samples_us cannot be empty for terminal target-host evidence"
            )
        object.__setattr__(self, "samples_us", normalized)

    def canonical_payload(self) -> dict[str, object]:
        return {
            "evidence_type": self.evidence_type,
            "metric": self.metric,
            "sample_count": len(self.samples_us),
            "samples_us": list(self.samples_us),
            "schema_version": self.schema_version,
            "unit": UNIT_MICROSECONDS,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @classmethod
    def parse(
        cls,
        raw: bytes,
        *,
        expected_evidence_type: str,
        expected_metric: str,
    ) -> "RuntimeTargetHostTimingEvidence":
        value = _strict_object(raw, name="target-host timing evidence")
        expected_fields = {
            "evidence_type",
            "metric",
            "sample_count",
            "samples_us",
            "schema_version",
            "unit",
        }
        if set(value) != expected_fields:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing evidence fields are non-canonical"
            )
        if value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "unsupported target-host timing evidence schema_version"
            )
        if value.get("evidence_type") != expected_evidence_type:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing evidence type conflicts"
            )
        if value.get("metric") != expected_metric:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing metric conflicts"
            )
        if value.get("unit") != UNIT_MICROSECONDS:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing unit conflicts"
            )
        samples = _series(value.get("samples_us"), name="samples_us")
        if value.get("sample_count") != len(samples):
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing sample_count conflicts"
            )
        result = cls(
            evidence_type=expected_evidence_type,
            metric=expected_metric,
            samples_us=samples,
        )
        if result.canonical_bytes() != raw:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host timing evidence bytes are not canonical JSON"
            )
        return result


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostResourceEvidence:
    """Canonical pressure/conservation facts already consumed by WP-65 evaluation."""

    expected_financial_events: int
    recovered_financial_events: int
    reconnect_backlog_remaining: int
    declared_duration_us: int
    observed_duration_us: int
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "unsupported target-host resource evidence schema_version"
            )
        expected = _positive_int(
            self.expected_financial_events,
            name="expected_financial_events",
        )
        recovered = _non_negative_int(
            self.recovered_financial_events,
            name="recovered_financial_events",
        )
        if recovered > expected:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "recovered_financial_events cannot exceed expected_financial_events"
            )
        object.__setattr__(self, "expected_financial_events", expected)
        object.__setattr__(self, "recovered_financial_events", recovered)
        object.__setattr__(
            self,
            "reconnect_backlog_remaining",
            _non_negative_int(
                self.reconnect_backlog_remaining,
                name="reconnect_backlog_remaining",
            ),
        )
        object.__setattr__(
            self,
            "declared_duration_us",
            _positive_int(self.declared_duration_us, name="declared_duration_us"),
        )
        object.__setattr__(
            self,
            "observed_duration_us",
            _positive_int(self.observed_duration_us, name="observed_duration_us"),
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "declared_duration_us": self.declared_duration_us,
            "evidence_type": RESOURCE_EVIDENCE_TYPE,
            "expected_financial_events": self.expected_financial_events,
            "observed_duration_us": self.observed_duration_us,
            "reconnect_backlog_remaining": self.reconnect_backlog_remaining,
            "recovered_financial_events": self.recovered_financial_events,
            "schema_version": self.schema_version,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @classmethod
    def parse(cls, raw: bytes) -> "RuntimeTargetHostResourceEvidence":
        value = _strict_object(raw, name="target-host resource evidence")
        expected_fields = {
            "declared_duration_us",
            "evidence_type",
            "expected_financial_events",
            "observed_duration_us",
            "reconnect_backlog_remaining",
            "recovered_financial_events",
            "schema_version",
        }
        if set(value) != expected_fields:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host resource evidence fields are non-canonical"
            )
        if value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "unsupported target-host resource evidence schema_version"
            )
        if value.get("evidence_type") != RESOURCE_EVIDENCE_TYPE:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host resource evidence type conflicts"
            )
        result = cls(
            expected_financial_events=value.get("expected_financial_events"),
            recovered_financial_events=value.get("recovered_financial_events"),
            reconnect_backlog_remaining=value.get("reconnect_backlog_remaining"),
            declared_duration_us=value.get("declared_duration_us"),
            observed_duration_us=value.get("observed_duration_us"),
        )
        if result.canonical_bytes() != raw:
            raise RuntimeTargetHostMeasurementEvidenceError(
                "target-host resource evidence bytes are not canonical JSON"
            )
        return result


def _expected_collector(kind: str) -> str:
    values = {
        STALENESS_EVIDENCE_KIND: STALENESS_COLLECTOR_ID,
        INTERFERENCE_EVIDENCE_KIND: INTERFERENCE_COLLECTOR_ID,
        RESOURCE_EVIDENCE_KIND: RESOURCE_COLLECTOR_ID,
    }
    return f"{values[kind]}@{COLLECTOR_VERSION}"


def _read_payload(
    reader: Callable[[str], tuple[dict[str, object], bytes]],
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    kind: str,
) -> bytes:
    artifact_id = accepted.payload_artifact_id_by_kind.get(kind)
    expected_digest = accepted.payload_sha256_by_kind.get(kind)
    if type(artifact_id) is not str or type(expected_digest) is not str:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"accepted target-host qualification lacks raw payload binding for {kind}"
        )
    try:
        _manifest, raw = reader(artifact_id)
    except (ArtifactIntegrityError, FileNotFoundError, OSError) as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"retained target-host measurement payload is unavailable for {kind}"
        ) from error
    if type(raw) is not bytes or not raw:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"retained target-host measurement payload is empty for {kind}"
        )
    observed_digest = "sha256:" + sha256(raw).hexdigest()
    if observed_digest != expected_digest:
        raise RuntimeTargetHostMeasurementEvidenceError(
            f"retained target-host measurement payload digest conflicts for {kind}"
        )
    return raw


def verify_runtime_target_host_measurement_evidence(
    accepted: AcceptedRuntimeTargetHostQualification,
    *,
    evidence_store: ArtifactStore,
    evidence_root: str,
) -> AcceptedRuntimeTargetHostQualification:
    """Reconcile retained raw measurement semantics with the accepted campaign.

    This function is intentionally downstream of the signed profile verifier. It
    reuses the already accepted payload artifact identities, re-authenticates the
    raw bytes, and requires each measurement family to describe the exact same
    canonical observation. It cannot turn INCONCLUSIVE/FAIL material into PASS.
    """

    if type(accepted) is not AcceptedRuntimeTargetHostQualification:
        raise TypeError(
            "accepted must be exact AcceptedRuntimeTargetHostQualification"
        )
    if type(evidence_store) is not ArtifactStore:
        raise TypeError("evidence_store must be exact ArtifactStore")
    try:
        reader = trusted_authenticated_reader(
            evidence_root,
            publication_store=evidence_store,
        )
    except (ArtifactIntegrityError, OSError, TypeError, ValueError) as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "target-host measurement evidence authority cannot be bound"
        ) from error

    campaign_raw = _read_payload(
        reader,
        accepted,
        kind=CAMPAIGN_EVIDENCE_KIND,
    )
    try:
        campaign = ParsedRuntimeTargetHostCampaign.parse(campaign_raw)
    except RuntimeTargetHostCampaignError as error:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "accepted target-host campaign payload is not canonical"
        ) from error
    observation = campaign.evidence.observation
    if (
        observation.release_sha != accepted.source_sha
        or observation.scenario_id != accepted.scenario_id
        or observation.spec_digest != accepted.spec_digest
        or observation.configuration_hash != accepted.configuration_hash
        or observation.host_fingerprint != accepted.host_fingerprint
    ):
        raise RuntimeTargetHostMeasurementEvidenceError(
            "accepted target-host campaign identity conflicts with qualification"
        )

    for kind in (
        STALENESS_EVIDENCE_KIND,
        INTERFERENCE_EVIDENCE_KIND,
        RESOURCE_EVIDENCE_KIND,
    ):
        if accepted.collector_by_kind.get(kind) != _expected_collector(kind):
            raise RuntimeTargetHostMeasurementEvidenceError(
                f"target-host measurement collector is non-canonical for {kind}"
            )

    staleness = RuntimeTargetHostTimingEvidence.parse(
        _read_payload(reader, accepted, kind=STALENESS_EVIDENCE_KIND),
        expected_evidence_type=STALENESS_EVIDENCE_TYPE,
        expected_metric=STALENESS_METRIC,
    )
    if staleness.samples_us != observation.financial_staleness_us:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "retained staleness samples conflict with campaign observation"
        )

    interference = RuntimeTargetHostTimingEvidence.parse(
        _read_payload(reader, accepted, kind=INTERFERENCE_EVIDENCE_KIND),
        expected_evidence_type=INTERFERENCE_EVIDENCE_TYPE,
        expected_metric=INTERFERENCE_METRIC,
    )
    if interference.samples_us != observation.research_interference_us:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "retained interference samples conflict with campaign observation"
        )

    resources = RuntimeTargetHostResourceEvidence.parse(
        _read_payload(reader, accepted, kind=RESOURCE_EVIDENCE_KIND)
    )
    expected_resource_tuple = (
        observation.expected_financial_events,
        observation.recovered_financial_events,
        observation.reconnect_backlog_remaining,
        observation.declared_duration_us,
        observation.observed_duration_us,
    )
    actual_resource_tuple = (
        resources.expected_financial_events,
        resources.recovered_financial_events,
        resources.reconnect_backlog_remaining,
        resources.declared_duration_us,
        resources.observed_duration_us,
    )
    if actual_resource_tuple != expected_resource_tuple:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "retained resource/pressure evidence conflicts with campaign observation"
        )
    if resources.recovered_financial_events != resources.expected_financial_events:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "terminal target-host evidence contains financial event loss"
        )
    if resources.reconnect_backlog_remaining != 0:
        raise RuntimeTargetHostMeasurementEvidenceError(
            "terminal target-host evidence retains reconnect backlog"
        )

    return accepted
