import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.journal_taxonomy import (
    FINANCIAL,
    FINANCIAL_CONTROL,
    NON_FINANCIAL,
    QUALIFICATION_FINANCIAL,
    QUALIFICATION_NON_FINANCIAL,
    require_journal_aggregate_descriptor,
)
from mvp.autotrade_mvp.runtime_load_evidence import (
    ExpectedJournalEvent,
    RuntimeLoadEvidenceError,
    collect_journal_conservation_evidence,
    evaluate_journal_backed_runtime_budget,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="journal-load",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=1,
    )


def _expected(
    event_id: str,
    version: int,
    *,
    aggregate_id: str = "journal-load",
    event_type: str = EVENT_TYPE,
) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=event_type,
        aggregate_type="risk_decision",
        aggregate_id=aggregate_id,
        aggregate_version=version,
    )


def _append(
    store: JournalStore,
    expected: ExpectedJournalEvent,
    *,
    actual_event_type: str | None = None,
) -> None:
    payload = {"scenario": "journal-load", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": actual_event_type or expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T03:50:00Z",
        }
    )


class RuntimeLoadEvidenceTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load.sqlite")

    def test_current_production_writer_families_are_classified(self):
        expected = {
            "provider_financing_charge": (FINANCIAL, QUALIFICATION_FINANCIAL),
            "perpetual_funding": (FINANCIAL, QUALIFICATION_FINANCIAL),
            "valuation_observation": (FINANCIAL_CONTROL, QUALIFICATION_FINANCIAL),
            "canonical_simulation_store_owner": (
                NON_FINANCIAL,
                QUALIFICATION_NON_FINANCIAL,
            ),
            "canonical_autonomous_simulation": (
                NON_FINANCIAL,
                QUALIFICATION_NON_FINANCIAL,
            ),
        }
        for aggregate_type, classification in expected.items():
            with self.subTest(aggregate_type=aggregate_type):
                descriptor = require_journal_aggregate_descriptor(aggregate_type)
                self.assertEqual(
                    (descriptor.domain_classification, descriptor.qualification_visibility),
                    classification,
                )

    def test_expected_event_rejects_nonfinancial_aggregate(self):
        with self.assertRaisesRegex(
            RuntimeLoadEvidenceError,
            "not qualification-financial",
        ):
            ExpectedJournalEvent(
                event_id="not-financial",
                event_type=EVENT_TYPE,
                aggregate_type="model_budget",
                aggregate_id="journal-load",
                aggregate_version=1,
            )

    def test_expected_event_rejects_unclassified_aggregate(self):
        with self.assertRaisesRegex(
            RuntimeLoadEvidenceError,
            "unclassified durable aggregate",
        ):
            ExpectedJournalEvent(
                event_id="unknown-financial",
                event_type=EVENT_TYPE,
                aggregate_type="future_unknown_runtime_writer",
                aggregate_id="journal-load",
                aggregate_version=1,
            )

    def test_complete_declared_event_sequence_can_pass_existing_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = (_expected("financial-1", 1), _expected("financial-2", 2))
            expected_ids = tuple(event.event_id for event in expected)
            start = store.current_journal_sequence()
            for event in expected:
                _append(store, event)

            decision, evidence = evaluate_journal_backed_runtime_budget(
                _spec(),
                store,
                start_journal_sequence=start,
                expected_events=expected,
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                financial_latency_event_ids=expected_ids,
                financial_staleness_event_ids=expected_ids,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "PASS")
            self.assertEqual(
                evidence.expected_event_ids,
                ("financial-1", "financial-2"),
            )
            self.assertEqual(evidence.recovered_event_ids, evidence.expected_event_ids)
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(evidence.start_journal_sequence, start)
            self.assertEqual(evidence.end_journal_sequence, start + 2)
            self.assertTrue(evidence.expected_event_digest.startswith("sha256:"))
            self.assertTrue(evidence.digest.startswith("sha256:"))

    def test_equal_length_anonymous_metric_series_cannot_gain_event_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = (_expected("financial-1", 1), _expected("financial-2", 2))
            start = store.current_journal_sequence()
            for event in expected:
                _append(store, event)

            decision, _evidence = evaluate_journal_backed_runtime_budget(
                _spec(),
                store,
                start_journal_sequence=start,
                expected_events=expected,
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "INCONCLUSIVE")
            self.assertIn("unbound_financial_latency_samples", decision.reasons)
            self.assertIn("incomplete_financial_latency_coverage", decision.reasons)
            self.assertIn("unbound_financial_staleness_samples", decision.reasons)
            self.assertNotIn("p95_financial_latency_us", decision.metrics)
            self.assertNotIn("max_financial_staleness_us", decision.metrics)

    def test_missing_durable_event_forces_financial_event_loss(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = (_expected("financial-1", 1), _expected("financial-2", 2))
            start = store.current_journal_sequence()
            _append(store, expected[0])

            decision, evidence = evaluate_journal_backed_runtime_budget(
                _spec(),
                store,
                start_journal_sequence=start,
                expected_events=expected,
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "FAIL")
            self.assertIn("financial_event_loss", decision.reasons)
            self.assertEqual(evidence.recovered_event_ids, ("financial-1",))
            self.assertEqual(evidence.missing_event_ids, ("financial-2",))

    def test_event_before_predeclared_campaign_cut_cannot_count_as_recovered(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            event = _expected("old-financial", 1)
            _append(store, event)
            start = store.current_journal_sequence()

            evidence = collect_journal_conservation_evidence(
                store,
                scenario_id="journal-load",
                start_journal_sequence=start,
                expected_events=(event,),
            )

            self.assertEqual(evidence.recovered_event_ids, ())
            self.assertEqual(evidence.missing_event_ids, ("old-financial",))
            self.assertEqual(evidence.end_journal_sequence, start)

    def test_store_generation_change_after_selection_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = _expected("financial-generation", 1)
            _append(store, expected)

            other = JournalStore(Path(root) / "other-runtime-load.sqlite")
            other.current_journal_sequence()
            other_identity = other.store_identity
            original_current_sequence = JournalStore.current_journal_sequence

            def retarget_before_read(selected_store):
                selected_store.path = other.path
                selected_store._store_identity = other_identity
                return original_current_sequence(selected_store)

            with (
                patch.object(
                    JournalStore,
                    "current_journal_sequence",
                    new=retarget_before_read,
                ),
                self.assertRaisesRegex(
                    RuntimeError,
                    "journal operation authority changed before connection",
                ),
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=0,
                    expected_events=(expected,),
                )

    def test_writer_after_frozen_terminal_cut_cannot_expand_evidence_horizon(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            declared = _expected("financial-declared", 1, aggregate_id="declared")
            late = _expected("financial-late", 1, aggregate_id="late")
            start = store.current_journal_sequence()
            _append(store, declared)

            original_reader = JournalStore.load_events_after_journal_sequence

            def append_after_cut(selected_store, after_sequence, *, limit=10_000):
                # The collector has already frozen cut_end=1 before entering this
                # reader.  A concurrent writer extends the live journal to 2.
                _append(store, late)
                return original_reader(
                    selected_store,
                    after_sequence,
                    limit=limit,
                )

            with patch.object(
                JournalStore,
                "load_events_after_journal_sequence",
                new=append_after_cut,
            ):
                evidence = collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=start,
                    expected_events=(declared,),
                )

            self.assertEqual(evidence.recovered_event_ids, ("financial-declared",))
            self.assertEqual(evidence.end_journal_sequence, start + 1)
            self.assertEqual(store.current_journal_sequence(), start + 2)

    def test_incomplete_bounded_journal_tail_fails_instead_of_counting_partial(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected = (_expected("financial-1", 1), _expected("financial-2", 2))
            start = store.current_journal_sequence()
            for event in expected:
                _append(store, event)

            with self.assertRaisesRegex(ValueError, "journal tail is incomplete"):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=start,
                    expected_events=expected,
                    max_journal_events=1,
                )

    def test_right_event_id_with_wrong_event_type_is_not_financial_conservation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            event = _expected("financial-1", 1)
            start = store.current_journal_sequence()
            _append(store, event, actual_event_type="UnrelatedDiagnosticEvent")

            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "does not match its predeclared financial binding",
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=start,
                    expected_events=(event,),
                )

    def test_matching_set_in_wrong_durable_order_fails_qualification(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            first = _expected("financial-1", 1, aggregate_id="order-a")
            second = _expected("financial-2", 1, aggregate_id="order-b")
            start = store.current_journal_sequence()
            _append(store, second)
            _append(store, first)

            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "out of declared canonical order",
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=start,
                    expected_events=(first, second),
                )

    def test_duplicate_expected_identity_is_rejected_before_evaluation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "expected event IDs must be unique",
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=0,
                    expected_events=(
                        _expected("financial-1", 1, aggregate_id="a"),
                        _expected("financial-1", 1, aggregate_id="b"),
                    ),
                )

    def test_post_construction_expected_event_mutation_is_revalidated(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            event = _expected("financial-1", 1)
            object.__setattr__(event, "aggregate_version", True)
            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "aggregate_version must be a positive integer",
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=0,
                    expected_events=(event,),
                )

    def test_polymorphic_journal_store_cannot_issue_qualification_evidence(self):
        class ForgedStore(JournalStore):
            pass

        with tempfile.TemporaryDirectory() as root:
            forged = ForgedStore(Path(root) / "forged.sqlite")
            with self.assertRaisesRegex(TypeError, "exact JournalStore"):
                collect_journal_conservation_evidence(
                    forged,
                    scenario_id="journal-load",
                    start_journal_sequence=0,
                    expected_events=(_expected("financial-1", 1),),
                )


if __name__ == "__main__":
    unittest.main()
