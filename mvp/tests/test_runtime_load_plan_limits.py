import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import (
    RuntimeLoadPlanError,
    declare_runtime_event_plan,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="runtime-load-limit",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _expected() -> tuple[ExpectedJournalEvent, ...]:
    return (
        ExpectedJournalEvent(
            event_id="runtime-limit-financial-1",
            event_type="RuntimeQualificationFinancialEvent",
            aggregate_type="runtime_load_limit_fixture",
            aggregate_id="runtime-load-limit",
            aggregate_version=1,
        ),
    )


class RuntimeLoadPlanLimitTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load-limit.sqlite")

    def test_plan_rejects_limit_above_journal_reader_capacity_before_mutation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            before = store.current_journal_sequence()

            with self.assertRaisesRegex(
                RuntimeLoadPlanError,
                "max_journal_events must be between 1 and 100000",
            ):
                declare_runtime_event_plan(
                    store,
                    plan_id="too-large",
                    spec=_spec(),
                    expected_events=_expected(),
                    max_journal_events=100_001,
                )

            self.assertEqual(store.current_journal_sequence(), before)

    def test_plan_rejects_noncanonical_limits_before_mutation(self):
        for value in (0, -1, True):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                store = self._store(root)
                with self.assertRaisesRegex(
                    RuntimeLoadPlanError,
                    "max_journal_events must be between 1 and 100000",
                ):
                    declare_runtime_event_plan(
                        store,
                        plan_id="invalid-limit",
                        spec=_spec(),
                        expected_events=_expected(),
                        max_journal_events=value,
                    )
                self.assertEqual(store.current_journal_sequence(), 0)

    def test_plan_accepts_exact_journal_reader_capacity(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            plan = declare_runtime_event_plan(
                store,
                plan_id="exact-limit",
                spec=_spec(),
                expected_events=_expected(),
                max_journal_events=100_000,
            )

            self.assertEqual(plan.max_journal_events, 100_000)
            self.assertEqual(store.current_journal_sequence(), 1)


if __name__ == "__main__":
    unittest.main()
