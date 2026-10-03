import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import (
    RuntimeLoadPlanError,
    declare_runtime_event_plan,
    evaluate_declared_runtime_budget,
    load_declared_runtime_event_plan,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec(**overrides) -> RuntimeBudgetSpec:
    values = {
        "scenario_id": "declared-journal-load",
        "release_sha": SHA,
        "configuration_hash": HASH,
        "host_fingerprint": HOST,
        "strategy_horizon_us": 1_000,
        "max_p95_financial_latency_us": 500,
        "max_financial_staleness_us": 500,
        "max_research_interference_us": 500,
        "min_financial_samples": 2,
        "min_research_samples": 1,
    }
    values.update(overrides)
    return RuntimeBudgetSpec(**values)


def _event(event_id: str, version: int) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=EVENT_TYPE,
        aggregate_type="runtime_load_plan_fixture",
        aggregate_id="declared-journal-load",
        aggregate_version=version,
    )


def _append(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "declared-journal-load", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": f"2026-10-03T03:55:{expected.aggregate_version:02d}Z",
        }
    )


class RuntimeLoadPlanTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load-plan.sqlite")

    def test_declared_plan_drives_event_conservation_without_evaluation_counts(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            current = _spec()
            expected = (_event("financial-1", 1), _event("financial-2", 2))
            plan = declare_runtime_event_plan(
                store,
                plan_id="campaign-1",
                spec=current,
                expected_events=expected,
            )
            self.assertEqual(plan.expected_events, expected)
            self.assertEqual(
                plan.expected_event_ids,
                ("financial-1", "financial-2"),
            )
            self.assertEqual(
                plan.declared_journal_sequence,
                store.current_journal_sequence(),
            )

            for event in expected:
                _append(store, event)
            decision, evidence, loaded = evaluate_declared_runtime_budget(
                current,
                store,
                plan_id="campaign-1",
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "PASS")
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(
                evidence.start_journal_sequence,
                plan.declared_journal_sequence,
            )
            self.assertEqual(loaded.digest, plan.digest)

    def test_event_before_plan_cannot_satisfy_predeclared_campaign(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            old = _event("old-financial", 1)
            _append(store, old)
            plan = declare_runtime_event_plan(
                store,
                plan_id="campaign-after-old-event",
                spec=_spec(),
                expected_events=(old,),
            )

            decision, evidence, _ = evaluate_declared_runtime_budget(
                _spec(),
                store,
                plan_id=plan.plan_id,
                financial_latency_us=(100, 200),
                financial_staleness_us=(100, 200),
                research_interference_us=(50,),
                reconnect_backlog_remaining=0,
                declared_duration_us=1_000_000,
                observed_duration_us=1_000_000,
            )

            self.assertEqual(decision.status, "FAIL")
            self.assertIn("financial_event_loss", decision.reasons)
            self.assertEqual(evidence.recovered_event_ids, ())
            self.assertEqual(evidence.missing_event_ids, ("old-financial",))

    def test_exact_redeclaration_is_idempotent_without_advancing_journal(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            current = _spec()
            expected = (_event("financial-1", 1), _event("financial-2", 2))
            first = declare_runtime_event_plan(
                store,
                plan_id="campaign-idempotent",
                spec=current,
                expected_events=expected,
                max_journal_events=50,
            )
            sequence = store.current_journal_sequence()
            second = declare_runtime_event_plan(
                store,
                plan_id="campaign-idempotent",
                spec=current,
                expected_events=expected,
                max_journal_events=50,
            )

            self.assertEqual(first.digest, second.digest)
            self.assertEqual(store.current_journal_sequence(), sequence)

    def test_same_plan_identity_cannot_change_expected_event_binding(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            current = _spec()
            declare_runtime_event_plan(
                store,
                plan_id="campaign-conflict",
                spec=current,
                expected_events=(_event("financial-1", 1),),
            )
            changed = ExpectedJournalEvent(
                event_id="financial-1",
                event_type="DifferentFinancialEvent",
                aggregate_type="runtime_load_plan_fixture",
                aggregate_id="declared-journal-load",
                aggregate_version=1,
            )

            with self.assertRaisesRegex(
                RuntimeLoadPlanError,
                "already used for different content",
            ):
                declare_runtime_event_plan(
                    store,
                    plan_id="campaign-conflict",
                    spec=current,
                    expected_events=(changed,),
                )

    def test_same_plan_identity_cannot_reorder_expected_events(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            current = _spec()
            first = _event("financial-1", 1)
            second = _event("financial-2", 2)
            declare_runtime_event_plan(
                store,
                plan_id="campaign-order",
                spec=current,
                expected_events=(first, second),
            )
            with self.assertRaisesRegex(
                RuntimeLoadPlanError,
                "already used for different content",
            ):
                declare_runtime_event_plan(
                    store,
                    plan_id="campaign-order",
                    spec=current,
                    expected_events=(second, first),
                )

    def test_declared_plan_is_bound_to_exact_budget_spec(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            declare_runtime_event_plan(
                store,
                plan_id="campaign-budget-bound",
                spec=_spec(),
                expected_events=(_event("financial-1", 1),),
            )

            with self.assertRaisesRegex(
                RuntimeLoadPlanError,
                "budget spec conflicts",
            ):
                load_declared_runtime_event_plan(
                    store,
                    plan_id="campaign-budget-bound",
                    spec=_spec(max_p95_financial_latency_us=400),
                )

    def test_missing_plan_cannot_be_reconstructed_after_outcome(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            _append(store, _event("financial-1", 1))
            with self.assertRaisesRegex(
                RuntimeLoadPlanError,
                "not durably declared",
            ):
                evaluate_declared_runtime_budget(
                    _spec(),
                    store,
                    plan_id="never-declared",
                    financial_latency_us=(100, 200),
                    financial_staleness_us=(100, 200),
                    research_interference_us=(50,),
                    reconnect_backlog_remaining=0,
                    declared_duration_us=1_000_000,
                    observed_duration_us=1_000_000,
                )

    def test_post_construction_budget_mutation_is_revalidated_before_declaration(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            current = _spec()
            object.__setattr__(current, "min_financial_samples", True)
            with self.assertRaisesRegex(ValueError, "min_financial_samples"):
                declare_runtime_event_plan(
                    store,
                    plan_id="campaign-mutated-budget",
                    spec=current,
                    expected_events=(_event("financial-1", 1),),
                )


if __name__ == "__main__":
    unittest.main()
