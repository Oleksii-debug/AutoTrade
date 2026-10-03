from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import (
    RuntimeLoadPlanError,
    declare_runtime_event_plan,
)
from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound_module
from mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification import (
    verify_declared_plan_runtime_target_host_qualification,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
RELEASE_ID = "60000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + ("d" * 64)


def _spec(**overrides: object) -> RuntimeBudgetSpec:
    values = {
        "scenario_id": "plan-bound-target-host",
        "release_sha": SOURCE_SHA,
        "configuration_hash": CONFIG,
        "host_fingerprint": HOST,
        "strategy_horizon_us": 1_000,
        "max_p95_financial_latency_us": 500,
        "max_financial_staleness_us": 500,
        "max_research_interference_us": 500,
        "min_financial_samples": 1,
        "min_research_samples": 1,
    }
    values.update(overrides)
    return RuntimeBudgetSpec(**values)


def _expected_event() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="plan-bound-financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="plan-bound-target-host",
        aggregate_version=1,
    )


class RuntimeTargetHostPlanBoundQualificationTests(unittest.TestCase):
    def test_terminal_expected_identities_are_derived_from_durable_plan(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="terminal-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            receipt = object()
            evidence_store = object()
            accepted = object()
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification."
                "verify_runtime_target_host_qualification",
                return_value=accepted,
            ) as terminal:
                result = verify_declared_plan_runtime_target_host_qualification(
                    receipt,
                    evidence_store=evidence_store,
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )

            self.assertIs(result, accepted)
            args = terminal.call_args
            self.assertEqual(args.args, (receipt,))
            self.assertIs(args.kwargs["evidence_store"], evidence_store)
            self.assertEqual(args.kwargs["evidence_root"], directory)
            self.assertEqual(args.kwargs["expected_source_sha"], SOURCE_SHA)
            self.assertEqual(
                args.kwargs["expected_scenario_id"],
                "plan-bound-target-host",
            )
            self.assertEqual(args.kwargs["expected_spec_digest"], spec.digest)
            self.assertEqual(args.kwargs["expected_configuration_hash"], CONFIG)
            self.assertEqual(args.kwargs["expected_host_fingerprint"], HOST)
            self.assertEqual(
                args.kwargs["expected_workload_profile_hash"],
                plan.digest,
            )
            self.assertEqual(
                args.kwargs["expected_journal_store_identity_digest"],
                plan.store_identity_digest,
            )
            self.assertEqual(
                args.kwargs["expected_release_artifact_id"],
                RELEASE_ID,
            )
            self.assertEqual(
                args.kwargs["expected_release_artifact_sha256"],
                RELEASE_SHA,
            )

    def test_caller_owned_spec_is_detached_before_durable_plan_read(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="terminal-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            original_loader = plan_bound_module.load_declared_runtime_event_plan
            accepted = object()

            def mutating_loader(current_store, *, plan_id, spec):
                self.assertIsNot(spec, caller_spec)
                object.__setattr__(
                    caller_spec,
                    "configuration_hash",
                    "sha256:" + ("9" * 64),
                )
                return original_loader(
                    current_store,
                    plan_id=plan_id,
                    spec=spec,
                )

            caller_spec = spec
            with patch.object(
                plan_bound_module,
                "load_declared_runtime_event_plan",
                side_effect=mutating_loader,
            ), patch.object(
                plan_bound_module,
                "verify_runtime_target_host_qualification",
                return_value=accepted,
            ) as terminal:
                result = verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=caller_spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )

            self.assertIs(result, accepted)
            self.assertEqual(
                terminal.call_args.kwargs["expected_configuration_hash"],
                CONFIG,
            )
            self.assertEqual(
                terminal.call_args.kwargs["expected_source_sha"],
                SOURCE_SHA,
            )
            self.assertNotEqual(caller_spec.configuration_hash, CONFIG)

    def test_changed_spec_cannot_rebind_existing_durable_plan(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            declared_spec = _spec()
            declare_runtime_event_plan(
                store,
                plan_id="terminal-plan",
                spec=declared_spec,
                expected_events=(_expected_event(),),
            )
            changed_spec = _spec(
                configuration_hash="sha256:" + ("e" * 64),
            )
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification."
                "verify_runtime_target_host_qualification",
            ) as terminal:
                with self.assertRaisesRegex(
                    RuntimeLoadPlanError,
                    "plan configuration conflicts",
                ):
                    verify_declared_plan_runtime_target_host_qualification(
                        object(),
                        evidence_store=object(),
                        evidence_root=directory,
                        journal_store=store,
                        plan_id="terminal-plan",
                        spec=changed_spec,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            terminal.assert_not_called()

    def test_selected_store_generation_is_held_through_plan_read(self) -> None:
        with TemporaryDirectory() as directory:
            selected = JournalStore(Path(directory) / "selected.sqlite3")
            alternate = JournalStore(Path(directory) / "alternate.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                selected,
                plan_id="terminal-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )

            selected_event = JournalStore.get_event(selected, plan.event_id)
            self.assertIsNotNone(selected_event)
            assert selected_event is not None
            JournalStore.append_event(
                alternate,
                {
                    "event_id": selected_event["event_id"],
                    "event_type": selected_event["event_type"],
                    "aggregate_type": selected_event["aggregate_type"],
                    "aggregate_id": selected_event["aggregate_id"],
                    "aggregate_version": str(selected_event["aggregate_version"]),
                    "payload": selected_event["payload"],
                    "payload_hash": selected_event["payload_hash"],
                    "committed_at": selected_event["committed_at"],
                },
            )

            original_get_event = JournalStore.get_event

            def rebound_get_event(store: JournalStore, event_id: str):
                store.path = alternate.path
                store._store_identity = alternate.store_identity
                return original_get_event(store, event_id)

            accepted = object()
            with patch.object(
                JournalStore,
                "get_event",
                new=rebound_get_event,
            ), patch.object(
                plan_bound_module,
                "verify_runtime_target_host_qualification",
                return_value=accepted,
            ) as terminal:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "journal operation authority changed",
                ):
                    verify_declared_plan_runtime_target_host_qualification(
                        object(),
                        evidence_store=object(),
                        evidence_root=directory,
                        journal_store=selected,
                        plan_id=plan.plan_id,
                        spec=spec,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )

            terminal.assert_not_called()

    def test_adapter_requires_exact_journal_store_and_budget_spec_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "journal_store must be exact JournalStore"):
            verify_declared_plan_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                plan_id="terminal-plan",
                spec=_spec(),
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
            )

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            with self.assertRaisesRegex(TypeError, "spec must be exact RuntimeBudgetSpec"):
                verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id="terminal-plan",
                    spec=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )


if __name__ == "__main__":
    unittest.main()
