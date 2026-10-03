"""Durable journal evidence for runtime load qualification.

WP-65 performance qualification must not accept caller-authored event counts as
proof that financial events survived a load/restart campaign. This module is a
small bridge from the canonical :class:`JournalStore` to the existing runtime
budget evaluator. It owns no scheduler, load generator, host, or financial
authority.

The collector reads one contiguous authenticated journal tail and derives the
recovered set only from exact predeclared journal bindings. Missing identities,
wrong event/aggregate semantics, or reordered expected events therefore cannot
be hidden behind caller-supplied integer counts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .performance_qualification import (
    RuntimeBudgetDecision,
    RuntimeBudgetSpec,
    RuntimeLoadObservation,
    evaluate_runtime_budget,
)
from .persistence import (
    JournalStore,
    payload_digest,
    require_exact_journal_store_authority,
)
from .store_identity import JournalStoreIdentity


class RuntimeLoadEvidenceError(ValueError):
    """Raised when durable runtime-load evidence cannot be established."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise RuntimeLoadEvidenceError(f"{name} must be canonical non-empty text")
    return value


def _non_negative_int(value: object, *, name: str) -> int:
    if type(value) is not int or value < 0:
        raise RuntimeLoadEvidenceError(f"{name} must be a non-negative integer")
    return value


def _positive_int(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise RuntimeLoadEvidenceError(f"{name} must be a positive integer")
    return value


def _validated_spec(value: RuntimeBudgetSpec) -> RuntimeBudgetSpec:
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


@dataclass(frozen=True)
class ExpectedJournalEvent:
    """Exact journal identity expected from one declared financial workload step."""

    event_id: str
    event_type: str
    aggregate_type: str
    aggregate_id: str
    aggregate_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _text(self.event_id, name="event_id"))
        object.__setattr__(
            self,
            "event_type",
            _text(self.event_type, name="event_type"),
        )
        object.__setattr__(
            self,
            "aggregate_type",
            _text(self.aggregate_type, name="aggregate_type"),
        )
        object.__setattr__(
            self,
            "aggregate_id",
            _text(self.aggregate_id, name="aggregate_id"),
        )
        object.__setattr__(
            self,
            "aggregate_version",
            _positive_int(self.aggregate_version, name="aggregate_version"),
        )

    @property
    def payload(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "aggregate_version": self.aggregate_version,
        }

    @classmethod
    def from_payload(cls, value: object) -> "ExpectedJournalEvent":
        if type(value) is not dict:
            raise RuntimeLoadEvidenceError("expected event binding must be an object")
        expected_keys = {
            "event_id",
            "event_type",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
        }
        if set(value) != expected_keys:
            raise RuntimeLoadEvidenceError("expected event binding shape is invalid")
        return cls(
            event_id=value["event_id"],
            event_type=value["event_type"],
            aggregate_type=value["aggregate_type"],
            aggregate_id=value["aggregate_id"],
            aggregate_version=value["aggregate_version"],
        )


def snapshot_expected_journal_events(
    values: Sequence[ExpectedJournalEvent],
) -> tuple[ExpectedJournalEvent, ...]:
    """Revalidate and snapshot a declared ordered journal-event sequence."""

    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeLoadEvidenceError("expected_events must be a sequence")
    result: list[ExpectedJournalEvent] = []
    for value in values:
        if type(value) is not ExpectedJournalEvent:
            raise TypeError("expected event must be exact ExpectedJournalEvent")
        result.append(ExpectedJournalEvent.from_payload(value.payload))
    if not result:
        raise RuntimeLoadEvidenceError("expected_events cannot be empty")
    event_ids = [value.event_id for value in result]
    if len(event_ids) != len(set(event_ids)):
        raise RuntimeLoadEvidenceError("expected event IDs must be unique")
    aggregate_positions = [
        (value.aggregate_type, value.aggregate_id, value.aggregate_version)
        for value in result
    ]
    if len(aggregate_positions) != len(set(aggregate_positions)):
        raise RuntimeLoadEvidenceError(
            "expected events cannot claim the same aggregate version twice"
        )
    return tuple(result)


def _store_identity_payload(identity: JournalStoreIdentity) -> dict[str, object]:
    return {
        "canonical_path": identity.canonical_path,
        "filesystem_device": identity.filesystem_device,
        "filesystem_inode": identity.filesystem_inode,
        "identity_source": identity.identity_source,
        "windows_volume_serial": identity.windows_volume_serial,
        "windows_file_index_high": identity.windows_file_index_high,
        "windows_file_index_low": identity.windows_file_index_low,
    }


@dataclass(frozen=True)
class JournalConservationEvidence:
    """One immutable description of a journal-backed event-conservation cut.

    Construction is diagnostic rather than an independent issuer: authoritative
    evaluation re-collects this evidence directly from the exact canonical
    JournalStore instead of trusting a caller-created instance.
    """

    scenario_id: str
    store_identity_digest: str
    start_journal_sequence: int
    end_journal_sequence: int
    expected_event_ids: tuple[str, ...]
    recovered_event_ids: tuple[str, ...]
    missing_event_ids: tuple[str, ...]
    expected_event_digest: str
    recovered_event_digest: str

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": "1.1.0",
                "scenario_id": self.scenario_id,
                "store_identity_digest": self.store_identity_digest,
                "start_journal_sequence": self.start_journal_sequence,
                "end_journal_sequence": self.end_journal_sequence,
                "expected_event_ids": list(self.expected_event_ids),
                "recovered_event_ids": list(self.recovered_event_ids),
                "missing_event_ids": list(self.missing_event_ids),
                "expected_event_digest": self.expected_event_digest,
                "recovered_event_digest": self.recovered_event_digest,
            }
        )


def collect_journal_conservation_evidence(
    store: JournalStore,
    *,
    scenario_id: str,
    start_journal_sequence: int,
    expected_events: Sequence[ExpectedJournalEvent],
    max_journal_events: int = 100_000,
) -> JournalConservationEvidence:
    """Derive recovered financial-event bindings from one durable journal cut.

    ``start_journal_sequence`` is the pre-campaign cut. Only exact predeclared
    events committed after it can satisfy the workload. The tail reader verifies
    global contiguity and fails when a bounded read is incomplete. Expected event
    order is also preserved: a matching set in the wrong durable order is not
    accepted as conservation evidence.
    """

    scenario = _text(scenario_id, name="scenario_id")
    start = _non_negative_int(
        start_journal_sequence,
        name="start_journal_sequence",
    )
    limit = _positive_int(max_journal_events, name="max_journal_events")
    expected = snapshot_expected_journal_events(expected_events)
    expected_by_id = {value.event_id: value for value in expected}
    expected_index = {value.event_id: index for index, value in enumerate(expected)}

    identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    events = JournalStore.load_events_after_journal_sequence(
        store,
        start,
        limit=limit,
        allow_partial=False,
    )

    end = start
    recovered_records: list[dict[str, object]] = []
    recovered_ids: list[str] = []
    recovered_indices: list[int] = []
    for event in events:
        sequence = event.get("journal_sequence")
        if type(sequence) is not int or sequence != end + 1:
            raise RuntimeLoadEvidenceError(
                "journal qualification cut is not strictly contiguous"
            )
        end = sequence
        event_id = event.get("event_id")
        if event_id not in expected_by_id:
            continue
        if type(event_id) is not str:
            raise RuntimeLoadEvidenceError("recovered journal event_id is invalid")
        binding = expected_by_id[event_id]
        actual_binding = {
            "event_id": event_id,
            "event_type": event.get("event_type"),
            "aggregate_type": event.get("aggregate_type"),
            "aggregate_id": event.get("aggregate_id"),
            "aggregate_version": event.get("aggregate_version"),
        }
        if actual_binding != binding.payload:
            raise RuntimeLoadEvidenceError(
                "recovered journal event does not match its predeclared financial binding"
            )
        recovered_ids.append(event_id)
        recovered_indices.append(expected_index[event_id])
        recovered_records.append(
            {
                **actual_binding,
                "journal_sequence": sequence,
                "payload_hash": event.get("payload_hash"),
            }
        )

    if len(recovered_ids) != len(set(recovered_ids)):
        raise RuntimeLoadEvidenceError(
            "journal qualification contains duplicate expected event identities"
        )
    if recovered_indices != sorted(recovered_indices):
        raise RuntimeLoadEvidenceError(
            "expected financial events were recovered out of declared canonical order"
        )

    recovered = tuple(recovered_ids)
    recovered_set = set(recovered)
    expected_ids = tuple(value.event_id for value in expected)
    missing = tuple(value for value in expected_ids if value not in recovered_set)
    return JournalConservationEvidence(
        scenario_id=scenario,
        store_identity_digest=payload_digest(_store_identity_payload(identity)),
        start_journal_sequence=start,
        end_journal_sequence=end,
        expected_event_ids=expected_ids,
        recovered_event_ids=recovered,
        missing_event_ids=missing,
        expected_event_digest=payload_digest([value.payload for value in expected]),
        recovered_event_digest=payload_digest(recovered_records),
    )


def evaluate_journal_backed_runtime_budget(
    spec: RuntimeBudgetSpec,
    store: JournalStore,
    *,
    start_journal_sequence: int,
    expected_events: Sequence[ExpectedJournalEvent],
    financial_latency_us: Sequence[int],
    financial_staleness_us: Sequence[int],
    research_interference_us: Sequence[int],
    reconnect_backlog_remaining: int,
    declared_duration_us: int | None = None,
    observed_duration_us: int | None = None,
    max_journal_events: int = 100_000,
) -> tuple[RuntimeBudgetDecision, JournalConservationEvidence]:
    """Evaluate a runtime budget with conservation derived from durable truth.

    Latency/staleness/interference samples remain measurement inputs owned by the
    later target-host campaign harness. This bridge closes only financial-event
    conservation: both counts come from exact predeclared bindings and one
    contiguous JournalStore cut rather than caller-authored summary integers.
    """

    spec = _validated_spec(spec)
    evidence = collect_journal_conservation_evidence(
        store,
        scenario_id=spec.scenario_id,
        start_journal_sequence=start_journal_sequence,
        expected_events=expected_events,
        max_journal_events=max_journal_events,
    )
    observation = RuntimeLoadObservation.create(
        scenario_id=spec.scenario_id,
        spec_digest=spec.digest,
        release_sha=spec.release_sha,
        configuration_hash=spec.configuration_hash,
        host_fingerprint=spec.host_fingerprint,
        expected_financial_events=len(evidence.expected_event_ids),
        recovered_financial_events=len(evidence.recovered_event_ids),
        financial_latency_us=financial_latency_us,
        financial_staleness_us=financial_staleness_us,
        research_interference_us=research_interference_us,
        reconnect_backlog_remaining=reconnect_backlog_remaining,
        declared_duration_us=declared_duration_us,
        observed_duration_us=observed_duration_us,
    )
    return evaluate_runtime_budget(spec, observation), evidence
