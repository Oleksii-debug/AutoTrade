"""Current-stack raw target-host measurement composition for WP-65.

This module composes already-canonical durable authorities instead of replaying
the historical target-host stack.  It consumes:

* the pre-run :mod:`runtime_target_host_campaign_authority` binding;
* the durable financial latency samples issued by ``runtime_load_measurement``;
* the durable research-interference samples issued by
  ``runtime_load_research_measurement``; and
* the canonical JournalStore conservation cut.

The resulting artifact is raw provider-free evidence.  Its financial-staleness
basis is deliberately the bounded same-host monotonic interval already used by
the current provider-free evaluator (declared operation start to durable event
observation).  It is not provider/exchange source-clock freshness.  Resource
telemetry is also not collected here.  Therefore this artifact cannot by itself
mint terminal WP-65 PASS, release authority, provider/PAPER/LIVE authority, or
an economic-edge claim.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from hashlib import sha256
import json
import re

from .performance_qualification import RuntimeBudgetSpec
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .runtime_load_measurement import DurableFinancialLatencySample
from .runtime_load_research_measurement import (
    DurableResearchInterferenceSample,
    evaluate_durable_provider_free_runtime_budget,
)
from .runtime_target_host_campaign_authority import (
    RuntimeTargetHostCampaignAuthority,
    load_runtime_target_host_campaign_authority,
)


_SCHEMA_VERSION = "2.0.0"
_EVIDENCE_TYPE = "AUTOTRADE_TARGET_HOST_RAW_MEASUREMENT_CURRENT"
_STALENESS_BASIS = (
    "declared_financial_operation_start_to_durable_event_observation_same_host_monotonic"
)
_RESOURCE_EVIDENCE_STATUS = "NOT_COLLECTED"
_ARTIFACT_TOKEN = object()
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")


class RuntimeTargetHostMeasurementError(ValueError):
    """Raised when current-stack raw target-host evidence is not canonical."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _sha(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _SHA256.fullmatch(value) is None:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be canonical sha256:<64 lowercase hex>"
        )
    return value


def _git_sha(value: object, *, name: str) -> str:
    value = _text(value, name=name)
    if _GIT_SHA.fullmatch(value) is None:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a lowercase 40- or 64-character Git SHA"
        )
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeTargetHostMeasurementError(
            f"{name} must be a non-negative integer"
        )
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeTargetHostMeasurementError(f"{name} must be a positive integer")
    return value


def _snapshot_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
    if type(value) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    return RuntimeBudgetSpec(
        scenario_id=value.scenario_id,
        release_sha=value.release_sha,
        configuration_hash=value.configuration_hash,
        host_fingerprint=value.host_fingerprint,
        strategy_horizon_us=value.strategy_horizon_us,
        max_p95_financial_latency_us=value.max_p95_financial_latency_us,
        max_financial_staleness_us=value.max_financial_staleness_us,
        max_research_interference_us=value.max_research_interference_us,
        min_financial_samples=value.min_financial_samples,
        min_research_samples=value.min_research_samples,
    )


@dataclass(frozen=True, slots=True)
class TargetHostFinancialSample:
    """Detached raw financial timing sample with durable event identity."""

    event_id: str
    event_journal_sequence: int
    measurement_event_id: str
    measurement_journal_sequence: int
    event_payload_hash: str
    monotonic_start_ns: int
    monotonic_end_ns: int
    latency_us: int
    staleness_us: int

    @classmethod
    def from_durable(
        cls,
        value: DurableFinancialLatencySample,
    ) -> "TargetHostFinancialSample":
        if type(value) is not DurableFinancialLatencySample:
            raise TypeError("financial sample must be exact DurableFinancialLatencySample")
        event_id = _text(value.event_id, name="financial event_id")
        measurement_event_id = _text(
            value.measurement_event_id,
            name="financial measurement_event_id",
        )
        event_sequence = _positive_int(
            value.event_journal_sequence,
            name="financial event_journal_sequence",
        )
        measurement_sequence = _positive_int(
            value.measurement_journal_sequence,
            name="financial measurement_journal_sequence",
        )
        if measurement_sequence <= event_sequence:
            raise RuntimeTargetHostMeasurementError(
                "financial measurement must follow its durable financial event"
            )
        start_ns = _non_negative_int(
            value.monotonic_start_ns,
            name="financial monotonic_start_ns",
        )
        end_ns = _non_negative_int(
            value.monotonic_end_ns,
            name="financial monotonic_end_ns",
        )
        if end_ns < start_ns:
            raise RuntimeTargetHostMeasurementError(
                "financial monotonic interval moved backwards"
            )
        latency_us = _non_negative_int(value.latency_us, name="financial latency_us")
        recomputed = (end_ns - start_ns + 999) // 1_000
        if latency_us != recomputed:
            raise RuntimeTargetHostMeasurementError(
                "financial latency does not recompute from raw monotonic endpoints"
            )
        return cls(
            event_id=event_id,
            event_journal_sequence=event_sequence,
            measurement_event_id=measurement_event_id,
            measurement_journal_sequence=measurement_sequence,
            event_payload_hash=_sha(
                value.event_payload_hash,
                name="financial event_payload_hash",
            ),
            monotonic_start_ns=start_ns,
            monotonic_end_ns=end_ns,
            latency_us=latency_us,
            # Current provider-free staleness semantics intentionally reuse the
            # same bounded local interval.  Provider/source-clock freshness is a
            # separate terminal authority and is not represented here.
            staleness_us=latency_us,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_journal_sequence": self.event_journal_sequence,
            "measurement_event_id": self.measurement_event_id,
            "measurement_journal_sequence": self.measurement_journal_sequence,
            "event_payload_hash": self.event_payload_hash,
            "monotonic_start_ns": self.monotonic_start_ns,
            "monotonic_end_ns": self.monotonic_end_ns,
            "latency_us": self.latency_us,
            "staleness_us": self.staleness_us,
        }


@dataclass(frozen=True, slots=True)
class TargetHostResearchSample:
    """Detached raw research/contention timing sample."""

    sample_id: str
    phase: str
    measurement_event_id: str
    measurement_journal_sequence: int
    monotonic_start_ns: int
    monotonic_end_ns: int
    interference_us: int

    @classmethod
    def from_durable(
        cls,
        value: DurableResearchInterferenceSample,
    ) -> "TargetHostResearchSample":
        if type(value) is not DurableResearchInterferenceSample:
            raise TypeError("research sample must be exact DurableResearchInterferenceSample")
        start_ns = _non_negative_int(
            value.monotonic_start_ns,
            name="research monotonic_start_ns",
        )
        end_ns = _non_negative_int(
            value.monotonic_end_ns,
            name="research monotonic_end_ns",
        )
        if end_ns < start_ns:
            raise RuntimeTargetHostMeasurementError(
                "research monotonic interval moved backwards"
            )
        interference_us = _non_negative_int(
            value.interference_us,
            name="research interference_us",
        )
        if interference_us != (end_ns - start_ns + 999) // 1_000:
            raise RuntimeTargetHostMeasurementError(
                "research interference does not recompute from raw monotonic endpoints"
            )
        return cls(
            sample_id=_text(value.sample_id, name="research sample_id"),
            phase=_text(value.phase, name="research phase"),
            measurement_event_id=_text(
                value.measurement_event_id,
                name="research measurement_event_id",
            ),
            measurement_journal_sequence=_positive_int(
                value.measurement_journal_sequence,
                name="research measurement_journal_sequence",
            ),
            monotonic_start_ns=start_ns,
            monotonic_end_ns=end_ns,
            interference_us=interference_us,
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "phase": self.phase,
            "measurement_event_id": self.measurement_event_id,
            "measurement_journal_sequence": self.measurement_journal_sequence,
            "monotonic_start_ns": self.monotonic_start_ns,
            "monotonic_end_ns": self.monotonic_end_ns,
            "interference_us": self.interference_us,
        }


@dataclass(frozen=True, slots=True)
class RuntimeTargetHostMeasurementArtifact:
    """Journal-derived current-stack raw target-host evidence.

    Construction is issuer-protected so a caller-authored dataclass cannot be
    mistaken for collected evidence.  This artifact remains deliberately
    nonterminal while resource/source-clock/independent-signing authorities are
    absent.
    """

    authority_id: str
    authority_digest: str
    source_sha: str
    release_artifact_id: str
    release_artifact_sha256: str
    scenario_id: str
    spec_digest: str
    configuration_hash: str
    host_fingerprint: str
    financial_plan_id: str
    financial_plan_digest: str
    store_identity_digest: str
    journal_taxonomy_digest: str
    authority_journal_sequence: int
    conservation_start_journal_sequence: int
    conservation_end_journal_sequence: int
    conservation_digest: str
    reconnect_backlog_remaining: int
    financial_samples: tuple[TargetHostFinancialSample, ...]
    research_samples: tuple[TargetHostResearchSample, ...]
    staleness_basis: str = _STALENESS_BASIS
    resource_evidence_status: str = _RESOURCE_EVIDENCE_STATUS
    schema_version: str = _SCHEMA_VERSION
    evidence_type: str = _EVIDENCE_TYPE
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ARTIFACT_TOKEN:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement artifact must come from canonical collector"
            )

    @property
    def terminal_qualification_eligible(self) -> bool:
        return False

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

    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "evidence_type": self.evidence_type,
            "authority_id": self.authority_id,
            "authority_digest": self.authority_digest,
            "source_sha": self.source_sha,
            "release_artifact_id": self.release_artifact_id,
            "release_artifact_sha256": self.release_artifact_sha256,
            "scenario_id": self.scenario_id,
            "spec_digest": self.spec_digest,
            "configuration_hash": self.configuration_hash,
            "host_fingerprint": self.host_fingerprint,
            "financial_plan_id": self.financial_plan_id,
            "financial_plan_digest": self.financial_plan_digest,
            "store_identity_digest": self.store_identity_digest,
            "journal_taxonomy_digest": self.journal_taxonomy_digest,
            "authority_journal_sequence": self.authority_journal_sequence,
            "conservation_start_journal_sequence": self.conservation_start_journal_sequence,
            "conservation_end_journal_sequence": self.conservation_end_journal_sequence,
            "conservation_digest": self.conservation_digest,
            "reconnect_backlog_remaining": self.reconnect_backlog_remaining,
            "staleness_basis": self.staleness_basis,
            "resource_evidence_status": self.resource_evidence_status,
            "financial_samples": [sample.payload for sample in self.financial_samples],
            "research_samples": [sample.payload for sample in self.research_samples],
        }

    def canonical_bytes(self) -> bytes:
        return (
            json.dumps(
                self.canonical_payload(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")

    @property
    def digest(self) -> str:
        return "sha256:" + sha256(self.canonical_bytes()).hexdigest()


def _assert_authority_binding(
    authority: RuntimeTargetHostCampaignAuthority,
    spec: RuntimeBudgetSpec,
) -> None:
    if type(authority) is not RuntimeTargetHostCampaignAuthority:
        raise TypeError("authority must be exact RuntimeTargetHostCampaignAuthority")
    checks = {
        "source_sha": (authority.source_sha, spec.release_sha),
        "scenario_id": (authority.scenario_id, spec.scenario_id),
        "spec_digest": (authority.spec_digest, spec.digest),
        "configuration_hash": (
            authority.configuration_hash,
            spec.configuration_hash,
        ),
        "host_fingerprint": (authority.host_fingerprint, spec.host_fingerprint),
    }
    mismatches = [name for name, pair in checks.items() if pair[0] != pair[1]]
    if mismatches:
        raise RuntimeTargetHostMeasurementError(
            "target-host campaign authority belongs to another runtime spec: "
            + ", ".join(mismatches)
        )


def collect_runtime_target_host_measurement(
    store: JournalStore,
    spec: RuntimeBudgetSpec,
    *,
    authority_id: str,
    research_plan_id: str,
) -> RuntimeTargetHostMeasurementArtifact:
    """Collect a raw current-stack target-host artifact from durable samples.

    Every financial event and every retained measurement must occur *after* the
    pre-run target-host authority declaration.  The terminal JournalStore cut is
    then required to remain unchanged while reconnect backlog is sampled, so the
    returned artifact cannot splice two journal horizons.
    """

    spec = _snapshot_spec(spec)
    authority_id = _text(authority_id, name="authority_id")
    research_plan_id = _text(research_plan_id, name="research_plan_id")
    store_identity = require_exact_journal_store_authority(
        store,
        subject="target-host measurement JournalStore",
    )

    with journal_store_authority_scope(store, store_identity):
        authority = load_runtime_target_host_campaign_authority(
            store,
            spec,
            authority_id=authority_id,
        )
        _assert_authority_binding(authority, spec)

        (
            _decision,
            conservation,
            financial_plan,
            durable_financial,
            durable_research,
        ) = evaluate_durable_provider_free_runtime_budget(
            spec,
            store,
            financial_plan_id=authority.financial_plan_id,
            research_plan_id=research_plan_id,
        )

        if financial_plan.digest != authority.financial_plan_digest:
            raise RuntimeTargetHostMeasurementError(
                "target-host financial plan changed after pre-run authority declaration"
            )
        if conservation.store_identity_digest != authority.store_identity_digest:
            raise RuntimeTargetHostMeasurementError(
                "target-host conservation evidence belongs to another JournalStore generation"
            )
        if conservation.missing_event_ids:
            raise RuntimeTargetHostMeasurementError(
                "target-host campaign is missing predeclared financial events"
            )

        financial_samples = tuple(
            TargetHostFinancialSample.from_durable(sample)
            for sample in durable_financial
        )
        research_samples = tuple(
            TargetHostResearchSample.from_durable(sample)
            for sample in durable_research
        )
        if not financial_samples:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement requires durable financial samples"
            )
        if not research_samples:
            raise RuntimeTargetHostMeasurementError(
                "target-host measurement requires durable research samples"
            )

        if tuple(sample.event_id for sample in financial_samples) != tuple(
            conservation.recovered_event_ids
        ):
            raise RuntimeTargetHostMeasurementError(
                "target-host financial samples do not match the conserved journal identities"
            )
        for sample in financial_samples:
            if sample.event_journal_sequence <= authority.declared_journal_sequence:
                raise RuntimeTargetHostMeasurementError(
                    "financial event predates target-host pre-run authority"
                )
            if sample.measurement_journal_sequence <= authority.declared_journal_sequence:
                raise RuntimeTargetHostMeasurementError(
                    "financial measurement predates target-host pre-run authority"
                )
        for sample in research_samples:
            if sample.measurement_journal_sequence <= authority.declared_journal_sequence:
                raise RuntimeTargetHostMeasurementError(
                    "research measurement predates target-host pre-run authority"
                )

        terminal_sequence = JournalStore.current_journal_sequence(store)
        if terminal_sequence != conservation.end_journal_sequence:
            raise RuntimeTargetHostMeasurementError(
                "JournalStore changed after target-host conservation cut"
            )
        reconnect_backlog = JournalStore.pending_outbox_count(store)
        reconnect_backlog = _non_negative_int(
            reconnect_backlog,
            name="reconnect_backlog_remaining",
        )
        if JournalStore.current_journal_sequence(store) != terminal_sequence:
            raise RuntimeTargetHostMeasurementError(
                "JournalStore changed while sampling target-host terminal state"
            )

        return RuntimeTargetHostMeasurementArtifact(
            authority_id=authority.authority_id,
            authority_digest=_sha(authority.digest, name="authority_digest"),
            source_sha=_git_sha(authority.source_sha, name="source_sha"),
            release_artifact_id=authority.release_artifact_id,
            release_artifact_sha256=_sha(
                authority.release_artifact_sha256,
                name="release_artifact_sha256",
            ),
            scenario_id=authority.scenario_id,
            spec_digest=_sha(authority.spec_digest, name="spec_digest"),
            configuration_hash=_sha(
                authority.configuration_hash,
                name="configuration_hash",
            ),
            host_fingerprint=_sha(
                authority.host_fingerprint,
                name="host_fingerprint",
            ),
            financial_plan_id=authority.financial_plan_id,
            financial_plan_digest=_sha(
                authority.financial_plan_digest,
                name="financial_plan_digest",
            ),
            store_identity_digest=_sha(
                authority.store_identity_digest,
                name="store_identity_digest",
            ),
            journal_taxonomy_digest=_sha(
                authority.journal_taxonomy_digest,
                name="journal_taxonomy_digest",
            ),
            authority_journal_sequence=_positive_int(
                authority.declared_journal_sequence,
                name="authority_journal_sequence",
            ),
            conservation_start_journal_sequence=_non_negative_int(
                conservation.start_journal_sequence,
                name="conservation_start_journal_sequence",
            ),
            conservation_end_journal_sequence=_non_negative_int(
                conservation.end_journal_sequence,
                name="conservation_end_journal_sequence",
            ),
            conservation_digest=_sha(
                conservation.digest,
                name="conservation_digest",
            ),
            reconnect_backlog_remaining=reconnect_backlog,
            financial_samples=financial_samples,
            research_samples=research_samples,
            _token=_ARTIFACT_TOKEN,
        )
