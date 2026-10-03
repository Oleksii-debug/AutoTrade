"""Durable journal evidence for runtime load qualification.

WP-65 performance qualification must not accept caller-authored event counts as
proof that financial events survived a load/restart campaign.  This module is a
small bridge from the canonical :class:`JournalStore` to the existing runtime
budget evaluator.  It owns no scheduler, load generator, host, or financial
authority.

The collector reads one contiguous authenticated journal tail and derives the
recovered set only from exact durable event identities declared by the scenario
before execution.  Missing identities therefore remain missing; a caller cannot
turn them into recovered events by supplying a larger integer count.
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


def _canonical_event_ids(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RuntimeLoadEvidenceError("expected_event_ids must be a sequence")
    normalized = tuple(_text(value, name="expected_event_id") for value in values)
    if not normalized:
        raise RuntimeLoadEvidenceError("expected_event_ids cannot be empty")
    if len(normalized) != len(set(normalized)):
        raise RuntimeLoadEvidenceError("expected_event_ids must be unique")
    return tuple(sorted(normalized))


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

    Construction is intentionally diagnostic rather than an independent issuer:
    authoritative evaluation below re-collects this evidence directly from the
    exact canonical JournalStore instead of trusting a caller-created instance.
    """

    scenario_id: str
    store_identity_digest: str
    start_journal_sequence: int
    end_journal_sequence: int
    expected_event_ids: tuple[str, ...]
    recovered_event_ids: tuple[str, ...]
    missing_event_ids: tuple[str, ...]
    recovered_event_digest: str

    @property
    def digest(self) -> str:
        return payload_digest(
            {
                "schema_version": "1.0.0",
                "scenario_id": self.scenario_id,
                "store_identity_digest": self.store_identity_digest,
                "start_journal_sequence": self.start_journal_sequence,
                "end_journal_sequence": self.end_journal_sequence,
                "expected_event_ids": list(self.expected_event_ids),
                "recovered_event_ids": list(self.recovered_event_ids),
                "missing_event_ids": list(self.missing_event_ids),
                "recovered_event_digest": self.recovered_event_digest,
            }
        )


def collect_journal_conservation_evidence(
    store: JournalStore,
    *,
    scenario_id: str,
    start_journal_sequence: int,
    expected_event_ids: Sequence[str],
    max_journal_events: int = 100_000,
) -> JournalConservationEvidence:
    """Derive recovered financial-event identities from one durable journal cut.

    ``start_journal_sequence`` is the pre-campaign cut.  Only events committed
    after it can satisfy the predeclared expected identities.  The underlying
    JournalStore tail reader verifies global sequence contiguity and fails when
    the requested bounded tail is incomplete; this function never treats a
    truncated read as evidence of event conservation.
    """

    scenario = _text(scenario_id, name="scenario_id")
    start = _non_negative_int(
        start_journal_sequence,
        name="start_journal_sequence",
    )
    limit = _positive_int(max_journal_events, name="max_journal_events")
    expected = _canonical_event_ids(expected_event_ids)

    identity = require_exact_journal_store_authority(
        store,
        subject="runtime qualification JournalStore",
    )
    # Invoke the canonical implementation explicitly after sealing the exact
    # JournalStore authority.  Instance-level method shadowing is therefore not
    # allowed to manufacture a different qualification view.
    events = JournalStore.load_events_after_journal_sequence(
        store,
        start,
        limit=limit,
        allow_partial=False,
    )

    end = start
    expected_set = set(expected)
    recovered_records: list[dict[str, object]] = []
    recovered_ids: list[str] = []
    for event in events:
        sequence = event.get("journal_sequence")
        if type(sequence) is not int or sequence <= end:
            raise RuntimeLoadEvidenceError(
                "journal qualification cut is not strictly contiguous"
            )
        end = sequence
        event_id = event.get("event_id")
        if event_id not in expected_set:
            continue
        if type(event_id) is not str:
            raise RuntimeLoadEvidenceError("recovered journal event_id is invalid")
        recovered_ids.append(event_id)
        recovered_records.append(
            {
                "event_id": event_id,
                "event_type": event.get("event_type"),
                "aggregate_type": event.get("aggregate_type"),
                "aggregate_id": event.get("aggregate_id"),
                "aggregate_version": event.get("aggregate_version"),
                "journal_sequence": sequence,
                "payload_hash": event.get("payload_hash"),
            }
        )

    if len(recovered_ids) != len(set(recovered_ids)):
        # JournalStore already enforces event_id uniqueness.  Keep this explicit
        # so a future storage migration cannot silently turn duplicates into a
        # successful conservation count.
        raise RuntimeLoadEvidenceError(
            "journal qualification contains duplicate expected event identities"
        )

    recovered = tuple(sorted(recovered_ids))
    missing = tuple(sorted(expected_set.difference(recovered)))
    return JournalConservationEvidence(
        scenario_id=scenario,
        store_identity_digest=payload_digest(_store_identity_payload(identity)),
        start_journal_sequence=start,
        end_journal_sequence=end,
        expected_event_ids=expected,
        recovered_event_ids=recovered,
        missing_event_ids=missing,
        recovered_event_digest=payload_digest(recovered_records),
    )


def evaluate_journal_backed_runtime_budget(
    spec: RuntimeBudgetSpec,
    store: JournalStore,
    *,
    start_journal_sequence: int,
    expected_event_ids: Sequence[str],
    financial_latency_us: Sequence[int],
    financial_staleness_us: Sequence[int],
    research_interference_us: Sequence[int],
    reconnect_backlog_remaining: int,
    declared_duration_us: int | None = None,
    observed_duration_us: int | None = None,
    max_journal_events: int = 100_000,
) -> tuple[RuntimeBudgetDecision, JournalConservationEvidence]:
    """Evaluate a runtime budget with event counts derived from durable truth.

    Latency/staleness/interference samples remain measurement inputs owned by the
    later target-host campaign harness.  This bridge deliberately closes only
    the financial-event conservation seam: expected/recovered counts cannot be
    passed by the caller and are derived from exact event identities at one
    contiguous JournalStore cut.
    """

    if type(spec) is not RuntimeBudgetSpec:
        raise TypeError("spec must be exact RuntimeBudgetSpec")
    evidence = collect_journal_conservation_evidence(
        store,
        scenario_id=spec.scenario_id,
        start_journal_sequence=start_journal_sequence,
        expected_event_ids=expected_event_ids,
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
