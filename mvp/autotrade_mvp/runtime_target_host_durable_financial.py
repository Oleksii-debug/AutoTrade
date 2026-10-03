"""Bind WP-65 target-host raw financial samples to durable JournalStore latency truth.

This bridge does not measure a second latency, evaluate a budget, or grant runtime
or trading authority. It reloads the repository's existing durable latency
measurements and proves that the target-host artifact used the exact same durable
financial event identities and raw latency endpoints.

Python 3.12 does not specify that ``perf_counter`` and ``monotonic`` use the same
clock. The existing durable latency seam uses ``perf_counter_ns`` while campaign
cuts/target-host windows use ``monotonic_ns``. Python 3.13 specifies one clock for
both, so this bridge fails closed on older interpreters instead of comparing
undefined reference points.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
import sys
from types import MappingProxyType
from typing import Mapping
from uuid import UUID

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .runtime_load_measurement import (
    DurableFinancialLatencySample,
    load_declared_financial_latency_samples,
)
from .runtime_load_plan import load_declared_runtime_event_plan
from .runtime_load_qualification import RuntimeCampaignCut, RuntimeCampaignPlan
from .runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementError,
    TargetHostMeasurementArtifact,
)


SCHEMA_VERSION = "1.0.0"
CLOCK_CONTRACT_ID = "python-perf-counter-equals-monotonic>=3.13"
TARGET_HOST_SHARED_CLOCK_ID = "python-time.monotonic_ns"
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class RuntimeTargetHostDurableFinancialError(RuntimeTargetHostMeasurementError):
    """Raised when target-host financial evidence diverges from durable truth."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _canonical_uuid(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        canonical = str(UUID(text))
    except (ValueError, TypeError, AttributeError) as error:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be a canonical UUID"
        ) from error
    if canonical != text:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be a canonical UUID"
        )
    return canonical


def _git_sha(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _GIT_SHA.fullmatch(text) is None:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be a lowercase 40- or 64-character Git SHA"
        )
    return text


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be canonical sha256:<64 hex>"
        )
    return text


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be a positive integer"
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostDurableFinancialError(
            f"{name} must be a non-negative integer"
        )
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _require_shared_monotonic_clock() -> None:
    version = sys.version_info
    if (version.major, version.minor) < (3, 13):
        raise RuntimeTargetHostDurableFinancialError(
            "durable latency cannot join target-host campaign cuts before Python 3.13 "
            "because perf_counter and monotonic are not one specified clock"
        )


def _snapshot_runtime_budget_spec(spec: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    """Detach caller-owned budget state before any JournalStore authority read."""

    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=spec.scenario_id,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        strategy_horizon_us=spec.strategy_horizon_us,
        max_p95_financial_latency_us=spec.max_p95_financial_latency_us,
        max_financial_staleness_us=spec.max_financial_staleness_us,
        max_research_interference_us=spec.max_research_interference_us,
        min_financial_samples=spec.min_financial_samples,
        min_research_samples=spec.min_research_samples,
    )


@dataclass(frozen=True, slots=True)
class DurableFinancialIdentityBinding:
    """One exact JournalStore financial event plus its durable latency record."""

    event_id: str
    event_journal_sequence: int
    event_payload_hash: str
    latency_measurement_event_id: str
    latency_measurement_journal_sequence: int
    durable_latency_sample_digest: str
    latency_start_monotonic_ns: int
    latency_end_monotonic_ns: int
    latency_us: int
    target_sample_id: str

    def __post_init__(self) -> None:
        for field in ("event_id", "latency_measurement_event_id", "target_sample_id"):
            object.__setattr__(self, field, _text(getattr(self, field), name=field))
        for field in (
            "event_payload_hash",
            "durable_latency_sample_digest",
        ):
            object.__setattr__(self, field, _digest(getattr(self, field), name=field))
        for field in (
            "event_journal_sequence",
            "latency_measurement_journal_sequence",
        ):
            object.__setattr__(self, field, _positive_int(getattr(self, field), name=field))
        for field in (
            "latency_start_monotonic_ns",
            "latency_end_monotonic_ns",
            "latency_us",
        ):
            object.__setattr__(self, field, _non_negative_int(getattr(self, field), name=field))
        if self.latency_measurement_journal_sequence <= self.event_journal_sequence:
            raise RuntimeTargetHostDurableFinancialError(
                "durable latency record must follow its financial event"
            )
        if self.latency_end_monotonic_ns < self.latency_start_monotonic_ns:
            raise RuntimeTargetHostDurableFinancialError(
                "durable latency clock moved backwards"
            )
        recomputed = (
            self.latency_end_monotonic_ns - self.latency_start_monotonic_ns + 999
        ) // 1_000
        if self.latency_us != recomputed:
            raise RuntimeTargetHostDurableFinancialError(
                "durable latency duration does not recompute from raw endpoints"
            )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "durable_latency_sample_digest": self.durable_latency_sample_digest,
            "event_id": self.event_id,
            "event_journal_sequence": self.event_journal_sequence,
            "event_payload_hash": self.event_payload_hash,
            "latency_end_monotonic_ns": self.latency_end_monotonic_ns,
            "latency_measurement_event_id": self.latency_measurement_event_id,
            "latency_measurement_journal_sequence": self.latency_measurement_journal_sequence,
            "latency_start_monotonic_ns": self.latency_start_monotonic_ns,
            "latency_us": self.latency_us,
            "target_sample_id": self.target_sample_id,
        }


@dataclass(frozen=True, slots=True)
class DurableTargetHostFinancialBinding:
    """Canonical bridge from one target-host artifact to durable latency records."""

    target_host_measurement_digest: str
    source_sha: str
    spec_digest: str
    declared_plan_id: str
    declared_plan_digest: str
    clock_contract_id: str
    bindings: tuple[DurableFinancialIdentityBinding, ...]
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise RuntimeTargetHostDurableFinancialError(
                "unsupported durable target-host binding schema_version"
            )
        if self.clock_contract_id != CLOCK_CONTRACT_ID:
            raise RuntimeTargetHostDurableFinancialError(
                "unsupported durable target-host clock contract"
            )
        object.__setattr__(
            self,
            "target_host_measurement_digest",
            _digest(self.target_host_measurement_digest, name="target_host_measurement_digest"),
        )
        object.__setattr__(
            self, "source_sha", _git_sha(self.source_sha, name="source_sha")
        )
        object.__setattr__(self, "spec_digest", _digest(self.spec_digest, name="spec_digest"))
        object.__setattr__(
            self,
            "declared_plan_id",
            _text(self.declared_plan_id, name="declared_plan_id"),
        )
        object.__setattr__(
            self,
            "declared_plan_digest",
            _digest(self.declared_plan_digest, name="declared_plan_digest"),
        )
        if type(self.bindings) is not tuple:
            raise TypeError("bindings must be an exact tuple")
        snapshotted: list[DurableFinancialIdentityBinding] = []
        seen_events: set[str] = set()
        seen_target_samples: set[str] = set()
        seen_latency_events: set[str] = set()
        seen_latency_sequences: set[int] = set()
        previous_sequence = 0
        for value in self.bindings:
            if type(value) is not DurableFinancialIdentityBinding:
                raise TypeError(
                    "bindings must contain exact DurableFinancialIdentityBinding"
                )
            value = DurableFinancialIdentityBinding(**value.canonical_payload())
            if value.event_id in seen_events:
                raise RuntimeTargetHostDurableFinancialError(
                    "durable financial bindings must have unique event IDs"
                )
            if value.event_journal_sequence <= previous_sequence:
                raise RuntimeTargetHostDurableFinancialError(
                    "durable financial bindings must follow strict journal order"
                )
            if value.target_sample_id in seen_target_samples:
                raise RuntimeTargetHostDurableFinancialError(
                    "durable financial bindings must have unique target sample IDs"
                )
            if value.latency_measurement_event_id in seen_latency_events:
                raise RuntimeTargetHostDurableFinancialError(
                    "durable financial bindings must have unique latency measurement event IDs"
                )
            if value.latency_measurement_journal_sequence in seen_latency_sequences:
                raise RuntimeTargetHostDurableFinancialError(
                    "durable financial bindings must have unique latency measurement journal sequences"
                )
            previous_sequence = value.event_journal_sequence
            seen_events.add(value.event_id)
            seen_target_samples.add(value.target_sample_id)
            seen_latency_events.add(value.latency_measurement_event_id)
            seen_latency_sequences.add(value.latency_measurement_journal_sequence)
            snapshotted.append(value)
        if not snapshotted:
            raise RuntimeTargetHostDurableFinancialError(
                "durable target-host binding requires financial samples"
            )
        object.__setattr__(self, "bindings", tuple(snapshotted))

    def canonical_payload(self) -> dict[str, object]:
        return {
            "bindings": [value.canonical_payload() for value in self.bindings],
            "clock_contract_id": self.clock_contract_id,
            "declared_plan_digest": self.declared_plan_digest,
            "declared_plan_id": self.declared_plan_id,
            "schema_version": self.schema_version,
            "source_sha": self.source_sha,
            "spec_digest": self.spec_digest,
            "target_host_measurement_digest": self.target_host_measurement_digest,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_payload())

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()

    @property
    def payload_hash_by_event(self) -> Mapping[str, str]:
        return MappingProxyType(
            {value.event_id: value.event_payload_hash for value in self.bindings}
        )


def _binding_from_durable_sample(
    durable: DurableFinancialLatencySample,
    *,
    target_sample_id: str,
) -> DurableFinancialIdentityBinding:
    if type(durable) is not DurableFinancialLatencySample:
        raise TypeError("durable sample must be exact DurableFinancialLatencySample")
    return DurableFinancialIdentityBinding(
        event_id=durable.event_id,
        event_journal_sequence=durable.event_journal_sequence,
        event_payload_hash=durable.event_payload_hash,
        latency_measurement_event_id=durable.measurement_event_id,
        latency_measurement_journal_sequence=durable.measurement_journal_sequence,
        durable_latency_sample_digest=durable.digest,
        latency_start_monotonic_ns=durable.monotonic_start_ns,
        latency_end_monotonic_ns=durable.monotonic_end_ns,
        latency_us=durable.latency_us,
        target_sample_id=target_sample_id,
    )


def bind_durable_financial_latency_to_target_host_measurement(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    declared_plan_id: str,
    measurement: TargetHostMeasurementArtifact,
) -> DurableTargetHostFinancialBinding:
    """Prove target-host financial latency is the exact durable JournalStore sample set."""

    _require_shared_monotonic_clock()
    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    measurement = TargetHostMeasurementArtifact.parse(measurement.canonical_bytes())
    if measurement.monotonic_clock_id != TARGET_HOST_SHARED_CLOCK_ID:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement does not declare canonical time.monotonic_ns authority"
        )
    spec = _snapshot_runtime_budget_spec(spec)
    declared_plan_id = _text(declared_plan_id, name="declared_plan_id")
    if measurement.source_sha != spec.release_sha:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement source SHA conflicts with runtime budget"
        )
    if measurement.scenario_id != spec.scenario_id:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement scenario conflicts with runtime budget"
        )
    if measurement.spec_digest != spec.digest:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement spec digest conflicts with runtime budget"
        )
    if measurement.configuration_hash != spec.configuration_hash:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement configuration conflicts with runtime budget"
        )
    if measurement.host_fingerprint != spec.host_fingerprint:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement host conflicts with runtime budget"
        )

    store_identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    # Select one physical JournalStore generation before either durable
    # projection is read. Nested helper scopes stay defense-in-depth; the
    # post-read identity and plan-digest checks prevent accepted splices.
    with journal_store_authority_scope(store, store_identity):
        declared_plan = load_declared_runtime_event_plan(
            store,
            plan_id=declared_plan_id,
            spec=spec,
        )
        if (
            measurement.journal_store_identity_digest
            != declared_plan.store_identity_digest
        ):
            raise RuntimeTargetHostDurableFinancialError(
                "target-host measurement belongs to another JournalStore generation"
            )
        if declared_plan.declared_journal_sequence > measurement.start_journal_sequence:
            raise RuntimeTargetHostDurableFinancialError(
                "durable runtime event plan was declared after target-host campaign start"
            )
        durable_samples = load_declared_financial_latency_samples(
            store,
            spec,
            plan_id=declared_plan_id,
        )
        if (
            require_exact_journal_store_authority(
                store,
                subject="runtime qualification JournalStore",
            )
            != store_identity
        ):
            raise RuntimeTargetHostDurableFinancialError(
                "JournalStore generation changed during durable financial binding"
            )

    for durable in durable_samples:
        if (
            durable.plan_id != declared_plan.plan_id
            or durable.plan_digest != declared_plan.digest
        ):
            raise RuntimeTargetHostDurableFinancialError(
                "durable latency sample plan identity conflicts with declared plan"
            )

    target_samples = measurement.financial_samples
    if len(durable_samples) != len(target_samples):
        raise RuntimeTargetHostDurableFinancialError(
            "target-host financial sample count does not match durable plan"
        )
    expected_event_ids = tuple(value.event_id for value in declared_plan.expected_events)
    if measurement.financial_event_ids != expected_event_ids:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host financial event identities do not match durable plan"
        )

    bindings: list[DurableFinancialIdentityBinding] = []
    for durable, target in zip(durable_samples, target_samples, strict=True):
        if target.event_id != durable.event_id:
            raise RuntimeTargetHostDurableFinancialError(
                "target-host financial event ID conflicts with durable latency"
            )
        if target.journal_sequence != durable.event_journal_sequence:
            raise RuntimeTargetHostDurableFinancialError(
                "target-host financial journal sequence conflicts with durable latency"
            )
        if not (
            measurement.start_journal_sequence
            < durable.event_journal_sequence
            < durable.measurement_journal_sequence
            <= measurement.end_journal_sequence
        ):
            raise RuntimeTargetHostDurableFinancialError(
                "durable latency dependency lies outside target-host journal cut"
            )
        if target.latency_start_monotonic_ns != durable.monotonic_start_ns or (
            target.latency_end_monotonic_ns != durable.monotonic_end_ns
        ):
            raise RuntimeTargetHostDurableFinancialError(
                "target-host raw latency endpoints conflict with durable latency"
            )
        if target.latency_us != durable.latency_us:
            raise RuntimeTargetHostDurableFinancialError(
                "target-host latency duration conflicts with durable latency"
            )
        bindings.append(
            _binding_from_durable_sample(
                durable,
                target_sample_id=target.sample_id,
            )
        )

    return DurableTargetHostFinancialBinding(
        target_host_measurement_digest=measurement.digest,
        source_sha=spec.release_sha,
        spec_digest=spec.digest,
        declared_plan_id=declared_plan.plan_id,
        declared_plan_digest=declared_plan.digest,
        clock_contract_id=CLOCK_CONTRACT_ID,
        bindings=tuple(bindings),
    )


def bind_release_bound_durable_financial_latency_to_target_host_measurement(
    *,
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    campaign_plan: RuntimeCampaignPlan,
    campaign_cut: RuntimeCampaignCut,
    declared_plan_id: str,
    measurement: TargetHostMeasurementArtifact,
    expected_release_artifact_id: str,
    expected_release_artifact_sha256: str,
) -> DurableTargetHostFinancialBinding:
    """Terminal facade for one externally frozen delivered-release identity.

    The low-level durable bridge proves JournalStore/measurement identity. Terminal
    consumers must additionally freeze the delivered artifact UUID and SHA-256
    outside caller-owned measurement state and reuse the parent WP-65 campaign
    authority for exact workload/plan/taxonomy/start-cut identity.
    """

    if type(measurement) is not TargetHostMeasurementArtifact:
        raise TypeError("measurement must be exact TargetHostMeasurementArtifact")
    measurement = TargetHostMeasurementArtifact.parse(measurement.canonical_bytes())
    frozen_release_artifact_id = _canonical_uuid(
        expected_release_artifact_id,
        name="expected_release_artifact_id",
    )
    frozen_release_artifact_sha256 = _digest(
        expected_release_artifact_sha256,
        name="expected_release_artifact_sha256",
    )
    if measurement.release_artifact_id != frozen_release_artifact_id:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement belongs to another delivered release artifact"
        )
    if measurement.release_artifact_sha256 != frozen_release_artifact_sha256:
        raise RuntimeTargetHostDurableFinancialError(
            "target-host measurement belongs to another delivered release digest"
        )
    # Reuse the parent measurement authority rather than reproducing its campaign
    # contract here. This checks exact workload profile, plan digest, taxonomy,
    # JournalStore generation/start cut and plan-delivered-artifact SHA before any
    # durable-financial JournalStore mechanics are allowed to run.
    try:
        measurement.require_campaign_binding(
            spec=spec,
            plan=campaign_plan,
            cut=campaign_cut,
        )
    except RuntimeTargetHostMeasurementError as error:
        raise RuntimeTargetHostDurableFinancialError(str(error)) from error
    return bind_durable_financial_latency_to_target_host_measurement(
        store,
        spec,
        declared_plan_id=declared_plan_id,
        measurement=measurement,
    )
