"""Bounded runtime-load campaign evidence for WP-65 qualification.

This module is a measurement adapter over the canonical JournalStore and the
existing performance evaluator. It does not schedule financial work, throttle
the runtime, or create trading authority. A campaign plan predeclares the exact
financial event identities and aggregate types it expects; completion derives
recovery from the durable journal cut rather than accepting caller-authored
event counts.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Mapping, Sequence
import json
import re

from .performance_qualification import (
    RuntimeBudgetDecision,
    RuntimeBudgetError,
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
    evaluate_runtime_budget,
)
from .persistence import JournalStore


_CUT_TOKEN = object()
_EVIDENCE_TOKEN = object()


def _text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise RuntimeBudgetError(f"{name} must be canonical non-empty text")
    return value


def _sha256_identity(value: object, *, name: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
    ):
        raise RuntimeBudgetError(f"{name} must be canonical sha256:<64 hex>")
    return value


def _positive_int(value: object, *, name: str, allow_zero: bool = False) -> int:
    if type(value) is not int:
        raise RuntimeBudgetError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        raise RuntimeBudgetError(f"{name} must be >= {minimum}")
    return value


def _series(values: Sequence[int], *, name: str) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeBudgetError(f"{name} must be a sequence")
    return tuple(
        _positive_int(value, name=name, allow_zero=True) for value in values
    )


def _sorted_unique_text(values: Sequence[str], *, name: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeBudgetError(f"{name} must be a sequence")
    normalized = tuple(_text(value, name=name) for value in values)
    if not normalized:
        raise RuntimeBudgetError(f"{name} must not be empty")
    if len(set(normalized)) != len(normalized):
        raise RuntimeBudgetError(f"{name} must be unique")
    return tuple(sorted(normalized))


def _digest(payload: Mapping[str, object]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RuntimeCampaignPlan:
    """Frozen workload identity and durable financial conservation expectation."""

    scenario_id: str
    spec_digest: str
    release_sha: str
    configuration_hash: str
    host_fingerprint: str
    workload_profile_hash: str
    declared_duration_ms: int
    expected_financial_event_ids: tuple[str, ...]
    financial_aggregate_types: tuple[str, ...]
    release_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "scenario_id", _text(self.scenario_id, name="scenario_id"))
        object.__setattr__(
            self, "spec_digest", _sha256_identity(self.spec_digest, name="spec_digest")
        )
        if (
            not isinstance(self.release_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.release_sha) is None
        ):
            raise RuntimeBudgetError("release_sha must be a canonical Git object id")
        object.__setattr__(
            self,
            "configuration_hash",
            _sha256_identity(self.configuration_hash, name="configuration_hash"),
        )
        object.__setattr__(
            self,
            "host_fingerprint",
            _sha256_identity(self.host_fingerprint, name="host_fingerprint"),
        )
        object.__setattr__(
            self,
            "workload_profile_hash",
            _sha256_identity(self.workload_profile_hash, name="workload_profile_hash"),
        )
        object.__setattr__(
            self,
            "declared_duration_ms",
            _positive_int(self.declared_duration_ms, name="declared_duration_ms"),
        )
        object.__setattr__(
            self,
            "expected_financial_event_ids",
            _sorted_unique_text(
                self.expected_financial_event_ids,
                name="expected_financial_event_ids",
            ),
        )
        object.__setattr__(
            self,
            "financial_aggregate_types",
            _sorted_unique_text(
                self.financial_aggregate_types,
                name="financial_aggregate_types",
            ),
        )
        if self.release_artifact_sha256 is not None:
            object.__setattr__(
                self,
                "release_artifact_sha256",
                _sha256_identity(
                    self.release_artifact_sha256,
                    name="release_artifact_sha256",
                ),
            )

    @classmethod
    def create(
        cls,
        *,
        spec: RuntimeBudgetSpec,
        workload_profile_hash: str,
        declared_duration_ms: int,
        expected_financial_event_ids: Sequence[str],
        financial_aggregate_types: Sequence[str],
        release_artifact_sha256: str | None = None,
    ) -> "RuntimeCampaignPlan":
        if not isinstance(spec, RuntimeBudgetSpec):
            raise TypeError("spec must be RuntimeBudgetSpec")
        if (
            isinstance(expected_financial_event_ids, (str, bytes))
            or not isinstance(expected_financial_event_ids, Sequence)
        ):
            raise RuntimeBudgetError(
                "expected_financial_event_ids must be a sequence"
            )
        if (
            isinstance(financial_aggregate_types, (str, bytes))
            or not isinstance(financial_aggregate_types, Sequence)
        ):
            raise RuntimeBudgetError(
                "financial_aggregate_types must be a sequence"
            )
        return cls(
            scenario_id=spec.scenario_id,
            spec_digest=spec.digest,
            release_sha=spec.release_sha,
            configuration_hash=spec.configuration_hash,
            host_fingerprint=spec.host_fingerprint,
            workload_profile_hash=workload_profile_hash,
            declared_duration_ms=declared_duration_ms,
            expected_financial_event_ids=tuple(expected_financial_event_ids),
            financial_aggregate_types=tuple(financial_aggregate_types),
            release_artifact_sha256=release_artifact_sha256,
        )

    @property
    def digest(self) -> str:
        return _digest(
            {
                "scenario_id": self.scenario_id,
                "spec_digest": self.spec_digest,
                "release_sha": self.release_sha,
                "configuration_hash": self.configuration_hash,
                "host_fingerprint": self.host_fingerprint,
                "workload_profile_hash": self.workload_profile_hash,
                "declared_duration_ms": self.declared_duration_ms,
                "expected_financial_event_ids": list(self.expected_financial_event_ids),
                "financial_aggregate_types": list(self.financial_aggregate_types),
                "release_artifact_sha256": self.release_artifact_sha256,
            }
        )


@dataclass(frozen=True)
class RuntimeCampaignCut:
    plan_digest: str
    spec_digest: str
    start_journal_sequence: int
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _CUT_TOKEN:
            raise RuntimeBudgetError("runtime campaign cuts must come from JournalStore")
        object.__setattr__(
            self, "plan_digest", _sha256_identity(self.plan_digest, name="plan_digest")
        )
        object.__setattr__(
            self, "spec_digest", _sha256_identity(self.spec_digest, name="spec_digest")
        )
        object.__setattr__(
            self,
            "start_journal_sequence",
            _positive_int(
                self.start_journal_sequence,
                name="start_journal_sequence",
                allow_zero=True,
            ),
        )


@dataclass(frozen=True)
class RuntimeCampaignEvidence:
    plan_digest: str
    spec_digest: str
    release_sha: str
    configuration_hash: str
    host_fingerprint: str
    start_journal_sequence: int
    end_journal_sequence: int
    expected_financial_event_ids: tuple[str, ...]
    recovered_financial_event_bindings: tuple[tuple[str, str, int], ...]
    financial_latency_us: tuple[int, ...]
    financial_staleness_us: tuple[int, ...]
    research_interference_us: tuple[int, ...]
    reconnect_backlog_remaining: int
    resource_evidence_hash: str
    resource_metrics: Mapping[str, int]
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _EVIDENCE_TOKEN:
            raise RuntimeBudgetError(
                "runtime campaign evidence must be derived from a JournalStore cut"
            )
        object.__setattr__(
            self, "plan_digest", _sha256_identity(self.plan_digest, name="plan_digest")
        )
        object.__setattr__(
            self, "spec_digest", _sha256_identity(self.spec_digest, name="spec_digest")
        )
        if (
            not isinstance(self.release_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", self.release_sha) is None
        ):
            raise RuntimeBudgetError("release_sha must be a canonical Git object id")
        object.__setattr__(
            self,
            "configuration_hash",
            _sha256_identity(self.configuration_hash, name="configuration_hash"),
        )
        object.__setattr__(
            self,
            "host_fingerprint",
            _sha256_identity(self.host_fingerprint, name="host_fingerprint"),
        )
        start = _positive_int(
            self.start_journal_sequence,
            name="start_journal_sequence",
            allow_zero=True,
        )
        end = _positive_int(
            self.end_journal_sequence,
            name="end_journal_sequence",
            allow_zero=True,
        )
        if end < start:
            raise RuntimeBudgetError("end_journal_sequence cannot precede campaign cut")
        object.__setattr__(self, "start_journal_sequence", start)
        object.__setattr__(self, "end_journal_sequence", end)
        object.__setattr__(
            self,
            "expected_financial_event_ids",
            _sorted_unique_text(
                self.expected_financial_event_ids,
                name="expected_financial_event_ids",
            ),
        )
        bindings: list[tuple[str, str, int]] = []
        seen: set[str] = set()
        previous_sequence = start
        for value in self.recovered_financial_event_bindings:
            if (
                not isinstance(value, tuple)
                or len(value) != 3
            ):
                raise RuntimeBudgetError("recovered event binding must be (id, digest, sequence)")
            event_id = _text(value[0], name="recovered event_id")
            event_digest = _sha256_identity(value[1], name="recovered payload_hash")
            sequence = _positive_int(value[2], name="recovered journal_sequence")
            if event_id in seen:
                raise RuntimeBudgetError("recovered financial event ids must be unique")
            if sequence <= previous_sequence:
                raise RuntimeBudgetError(
                    "recovered financial event bindings must follow journal order"
                )
            if sequence > end:
                raise RuntimeBudgetError("recovered event lies beyond campaign end cut")
            seen.add(event_id)
            previous_sequence = sequence
            bindings.append((event_id, event_digest, sequence))
        object.__setattr__(
            self, "recovered_financial_event_bindings", tuple(bindings)
        )
        object.__setattr__(
            self,
            "financial_latency_us",
            _series(self.financial_latency_us, name="financial_latency_us"),
        )
        object.__setattr__(
            self,
            "financial_staleness_us",
            _series(self.financial_staleness_us, name="financial_staleness_us"),
        )
        object.__setattr__(
            self,
            "research_interference_us",
            _series(self.research_interference_us, name="research_interference_us"),
        )
        object.__setattr__(
            self,
            "reconnect_backlog_remaining",
            _positive_int(
                self.reconnect_backlog_remaining,
                name="reconnect_backlog_remaining",
                allow_zero=True,
            ),
        )
        object.__setattr__(
            self,
            "resource_evidence_hash",
            _sha256_identity(
                self.resource_evidence_hash,
                name="resource_evidence_hash",
            ),
        )
        if not isinstance(self.resource_metrics, Mapping):
            raise RuntimeBudgetError("resource_metrics must be a mapping")
        normalized_metrics: dict[str, int] = {}
        for key, value in self.resource_metrics.items():
            normalized_metrics[_text(key, name="resource metric")] = _positive_int(
                value,
                name=f"resource_metrics[{key}]",
                allow_zero=True,
            )
        object.__setattr__(
            self, "resource_metrics", MappingProxyType(dict(sorted(normalized_metrics.items())))
        )

    @property
    def recovered_financial_event_ids(self) -> tuple[str, ...]:
        return tuple(value[0] for value in self.recovered_financial_event_bindings)

    @property
    def digest(self) -> str:
        return _digest(
            {
                "plan_digest": self.plan_digest,
                "spec_digest": self.spec_digest,
                "release_sha": self.release_sha,
                "configuration_hash": self.configuration_hash,
                "host_fingerprint": self.host_fingerprint,
                "start_journal_sequence": self.start_journal_sequence,
                "end_journal_sequence": self.end_journal_sequence,
                "expected_financial_event_ids": list(self.expected_financial_event_ids),
                "recovered_financial_event_bindings": [
                    [event_id, payload_hash, sequence]
                    for event_id, payload_hash, sequence
                    in self.recovered_financial_event_bindings
                ],
                "financial_latency_us": list(self.financial_latency_us),
                "financial_staleness_us": list(self.financial_staleness_us),
                "research_interference_us": list(self.research_interference_us),
                "reconnect_backlog_remaining": self.reconnect_backlog_remaining,
                "resource_evidence_hash": self.resource_evidence_hash,
                "resource_metrics": dict(self.resource_metrics),
            }
        )

    def to_observation(self, spec: RuntimeBudgetSpec) -> RuntimeLoadObservation:
        if not isinstance(spec, RuntimeBudgetSpec):
            raise TypeError("spec must be RuntimeBudgetSpec")
        if (
            spec.digest != self.spec_digest
            or spec.release_sha != self.release_sha
            or spec.configuration_hash != self.configuration_hash
            or spec.host_fingerprint != self.host_fingerprint
        ):
            raise RuntimeBudgetError("campaign evidence identity does not match runtime budget spec")
        return RuntimeLoadObservation.create(
            scenario_id=spec.scenario_id,
            spec_digest=spec.digest,
            release_sha=spec.release_sha,
            configuration_hash=spec.configuration_hash,
            host_fingerprint=spec.host_fingerprint,
            expected_financial_events=len(self.expected_financial_event_ids),
            recovered_financial_events=len(self.recovered_financial_event_bindings),
            financial_latency_us=self.financial_latency_us,
            financial_staleness_us=self.financial_staleness_us,
            research_interference_us=self.research_interference_us,
            reconnect_backlog_remaining=self.reconnect_backlog_remaining,
        )


def begin_runtime_campaign(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
) -> RuntimeCampaignCut:
    if not isinstance(journal, JournalStore):
        raise TypeError("journal must be JournalStore")
    if not isinstance(spec, RuntimeBudgetSpec):
        raise TypeError("spec must be RuntimeBudgetSpec")
    if not isinstance(plan, RuntimeCampaignPlan):
        raise TypeError("plan must be RuntimeCampaignPlan")
    if (
        plan.scenario_id != spec.scenario_id
        or plan.spec_digest != spec.digest
        or plan.release_sha != spec.release_sha
        or plan.configuration_hash != spec.configuration_hash
        or plan.host_fingerprint != spec.host_fingerprint
    ):
        raise RuntimeBudgetError("runtime campaign plan does not match budget spec")
    return RuntimeCampaignCut(
        plan_digest=plan.digest,
        spec_digest=spec.digest,
        start_journal_sequence=journal.current_journal_sequence(),
        _token=_CUT_TOKEN,
    )


def collect_runtime_campaign_evidence(
    *,
    journal: JournalStore,
    spec: RuntimeBudgetSpec,
    plan: RuntimeCampaignPlan,
    cut: RuntimeCampaignCut,
    financial_latency_us: Sequence[int],
    financial_staleness_us: Sequence[int],
    research_interference_us: Sequence[int],
    resource_evidence_hash: str,
    resource_metrics: Mapping[str, int],
    max_events: int = 100000,
) -> RuntimeCampaignEvidence:
    if not isinstance(journal, JournalStore):
        raise TypeError("journal must be JournalStore")
    if not isinstance(spec, RuntimeBudgetSpec):
        raise TypeError("spec must be RuntimeBudgetSpec")
    if not isinstance(plan, RuntimeCampaignPlan):
        raise TypeError("plan must be RuntimeCampaignPlan")
    if not isinstance(cut, RuntimeCampaignCut):
        raise TypeError("cut must be RuntimeCampaignCut")
    if cut.plan_digest != plan.digest or cut.spec_digest != spec.digest:
        raise RuntimeBudgetError("campaign cut belongs to another plan or spec")
    end_sequence = journal.current_journal_sequence()
    events = journal.load_events_after_journal_sequence(
        cut.start_journal_sequence,
        limit=max_events,
    )
    if end_sequence > cut.start_journal_sequence:
        if not events or events[-1].get("journal_sequence") != end_sequence:
            raise RuntimeBudgetError(
                "campaign journal range exceeds collector bound; evidence is incomplete"
            )

    expected = set(plan.expected_financial_event_ids)
    financial_events = [
        event
        for event in events
        if event.get("aggregate_type") in plan.financial_aggregate_types
    ]
    financial_ids = [str(event.get("event_id")) for event in financial_events]
    if len(financial_ids) != len(set(financial_ids)):
        raise RuntimeBudgetError("financial event identity is duplicated in campaign cut")
    unexpected = sorted(set(financial_ids) - expected)
    if unexpected:
        raise RuntimeBudgetError(
            "campaign produced undeclared financial event identities: "
            + ", ".join(unexpected)
        )
    recovered = [
        (
            str(event["event_id"]),
            _sha256_identity(event["payload_hash"], name="payload_hash"),
            int(event["journal_sequence"]),
        )
        for event in financial_events
        if str(event["event_id"]) in expected
    ]

    pending = journal.pending_outbox(limit=1000)
    backlog_remaining = len(pending)
    return RuntimeCampaignEvidence(
        plan_digest=plan.digest,
        spec_digest=spec.digest,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        start_journal_sequence=cut.start_journal_sequence,
        end_journal_sequence=end_sequence,
        expected_financial_event_ids=plan.expected_financial_event_ids,
        recovered_financial_event_bindings=tuple(recovered),
        financial_latency_us=tuple(financial_latency_us),
        financial_staleness_us=tuple(financial_staleness_us),
        research_interference_us=tuple(research_interference_us),
        reconnect_backlog_remaining=backlog_remaining,
        resource_evidence_hash=resource_evidence_hash,
        resource_metrics=resource_metrics,
        _token=_EVIDENCE_TOKEN,
    )


def evaluate_runtime_campaign(
    spec: RuntimeBudgetSpec,
    evidence: RuntimeCampaignEvidence,
) -> RuntimeBudgetDecision:
    if not isinstance(evidence, RuntimeCampaignEvidence):
        raise TypeError("evidence must be RuntimeCampaignEvidence")
    return evaluate_runtime_budget(spec, evidence.to_observation(spec))
