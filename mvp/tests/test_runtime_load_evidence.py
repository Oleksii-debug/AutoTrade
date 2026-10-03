import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import (
    RuntimeLoadEvidenceError,
    collect_journal_conservation_evidence,
    evaluate_journal_backed_runtime_budget,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


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


def _append(store: JournalStore, event_id: str, version: int) -> None:
    payload = {"scenario": "journal-load", "event_id": event_id}
    store.append_event(
        {
            "event_id": event_id,
            "event_type": "RuntimeQualificationFinancialEvent",
            "aggregate_type": "runtime_qualification_fixture",
            "aggregate_id": "journal-load",
            "aggregate_version": str(version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": f"2026-10-03T03:40:{version:02d}Z",
        }
    )


class RuntimeLoadEvidenceTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load.sqlite")

    def test_complete_declared_event_set_can_pass_existing_budget(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            start = store.current_journal_sequence()
            _append(store, "financial-1", 1)
            _append(store, "financial-2", 2)

            decision, evidence = evaluate_journal_backed_runtime_budget(
                _spec(),
                store,
                start_journal_sequence=start,
                expected_event_ids=("financial-2", "financial-1"),
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "PASS")
            self.assertEqual(
                evidence.expected_event_ids,
                ("financial-1", "financial-2"),
            )
            self.assertEqual(
                evidence.recovered_event_ids,
                ("financial-1", "financial-2"),
            )
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(evidence.start_journal_sequence, start)
            self.assertEqual(evidence.end_journal_sequence, start + 2)
            self.assertTrue(evidence.digest.startswith("sha256:"))

    def test_missing_durable_event_forces_financial_event_loss(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            start = store.current_journal_sequence()
            _append(store, "financial-1", 1)

            decision, evidence = evaluate_journal_backed_runtime_budget(
                _spec(),
                store,
                start_journal_sequence=start,
                expected_event_ids=("financial-1", "financial-2"),
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
            _append(store, "old-financial", 1)
            start = store.current_journal_sequence()

            evidence = collect_journal_conservation_evidence(
                store,
                scenario_id="journal-load",
                start_journal_sequence=start,
                expected_event_ids=("old-financial",),
            )

            self.assertEqual(evidence.recovered_event_ids, ())
            self.assertEqual(evidence.missing_event_ids, ("old-financial",))
            self.assertEqual(evidence.end_journal_sequence, start)

    def test_incomplete_bounded_journal_tail_fails_instead_of_counting_partial(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            start = store.current_journal_sequence()
            _append(store, "financial-1", 1)
            _append(store, "financial-2", 2)

            with self.assertRaisesRegex(ValueError, "journal tail is incomplete"):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=start,
                    expected_event_ids=("financial-1", "financial-2"),
                    max_journal_events=1,
                )

    def test_duplicate_expected_identity_is_rejected_before_evaluation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            with self.assertRaisesRegex(
                RuntimeLoadEvidenceError,
                "expected_event_ids must be unique",
            ):
                collect_journal_conservation_evidence(
                    store,
                    scenario_id="journal-load",
                    start_journal_sequence=0,
                    expected_event_ids=("financial-1", "financial-1"),
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
                    expected_event_ids=("financial-1",),
                )


if __name__ == "__main__":
    unittest.main()
