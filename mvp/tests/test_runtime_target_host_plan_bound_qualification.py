from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import (
    RuntimeLoadPlanError,
    declare_runtime_event_plan,
)
from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound_module
from mvp.autotrade_mvp.runtime_target_host_composed_qualification import (
    RuntimeTargetHostCompositionError,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
RELEASE_ID = "60000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + ("d" * 64)


def verify_declared_plan_runtime_target_host_qualification(*args, **kwargs):
    """Focused tests inject current seams without reopening production authority."""

    chronology_free = plan_bound_module._build_chronology_free_verifier(
        verify_composed=(
            plan_bound_module.verify_composed_runtime_target_host_qualification
        ),
    )
    verifier = plan_bound_module._build_product_verifier(
        plan_bound_module._terminal_chronology_dispatch,
        signed_receipt_type=plan_bound_module.SignedQualificationAttestation,
        composition_error_type=plan_bound_module.RuntimeTargetHostCompositionError,
        chronology_free_verifier=chronology_free,
    )
    return verifier(*args, **kwargs)


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
    def test_terminal_delegates_to_composed_authority_with_durable_plan_id(self) -> None:
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
            campaign_plan = object()
            campaign_cut = object()
            measurement = object()
            accepted = object()
            terminal = Mock(return_value=accepted)
            verifier = plan_bound_module._build_chronology_free_verifier(
                verify_composed=terminal,
            )
            result = verifier(
                receipt,
                evidence_store=evidence_store,
                evidence_root=directory,
                journal_store=store,
                plan_id=plan.plan_id,
                spec=spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=campaign_plan,
                campaign_cut=campaign_cut,
                measurement=measurement,
            )

            self.assertIs(result, accepted)
            args = terminal.call_args
            self.assertEqual(args.args, (receipt,))
            self.assertIs(args.kwargs["evidence_store"], evidence_store)
            self.assertEqual(args.kwargs["evidence_root"], directory)
            self.assertIs(args.kwargs["journal_store"], store)
            self.assertIsNot(args.kwargs["spec"], spec)
            self.assertEqual(args.kwargs["spec"].digest, spec.digest)
            self.assertIs(args.kwargs["campaign_plan"], campaign_plan)
            self.assertIs(args.kwargs["campaign_cut"], campaign_cut)
            self.assertEqual(args.kwargs["declared_plan_id"], plan.plan_id)
            self.assertIs(args.kwargs["measurement"], measurement)
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
            campaign_plan = object()
            campaign_cut = object()
            measurement = object()

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
            terminal = Mock(return_value=accepted)
            verifier = plan_bound_module._build_chronology_free_verifier(
                load_declared_plan=mutating_loader,
                verify_composed=terminal,
            )
            result = verifier(
                object(),
                evidence_store=object(),
                evidence_root=directory,
                journal_store=store,
                plan_id=plan.plan_id,
                spec=caller_spec,
                expected_release_artifact_id=RELEASE_ID,
                expected_release_artifact_sha256=RELEASE_SHA,
                campaign_plan=campaign_plan,
                campaign_cut=campaign_cut,
                measurement=measurement,
            )

            self.assertIs(result, accepted)
            self.assertEqual(
                terminal.call_args.kwargs["spec"].configuration_hash,
                CONFIG,
            )
            self.assertEqual(
                terminal.call_args.kwargs["spec"].release_sha,
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
            with patch.object(
                plan_bound_module,
                "verify_composed_runtime_target_host_qualification",
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

            with patch.object(
                JournalStore,
                "get_event",
                new=rebound_get_event,
            ), patch.object(
                plan_bound_module,
                "verify_composed_runtime_target_host_qualification",
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

    def test_selected_store_generation_is_revalidated_after_terminal_verifier(self) -> None:
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
            accepted = object()

            def rebound_after_verification(*args, **kwargs):
                selected.path = alternate.path
                selected._store_identity = alternate.store_identity
                return accepted

            terminal = Mock(side_effect=rebound_after_verification)
            verifier = plan_bound_module._build_chronology_free_verifier(
                verify_composed=terminal,
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "journal operation authority changed",
            ):
                verifier(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=selected,
                    plan_id=plan.plan_id,
                    spec=spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                    campaign_plan=object(),
                    campaign_cut=object(),
                    measurement=object(),
                )

            terminal.assert_called_once()

    def test_production_delegate_ignores_rebound_authority_globals(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="terminal-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            forged_loader = Mock(
                side_effect=AssertionError("rebound durable-plan loader ran")
            )
            forged_authority = Mock(
                side_effect=AssertionError("rebound JournalStore authority ran")
            )
            forged_scope = Mock(
                side_effect=AssertionError("rebound JournalStore scope ran")
            )
            forged_composed = Mock(
                side_effect=AssertionError("rebound composed verifier ran")
            )

            with (
                patch.object(
                    plan_bound_module,
                    "load_declared_runtime_event_plan",
                    forged_loader,
                ),
                patch.object(
                    plan_bound_module,
                    "require_exact_journal_store_authority",
                    forged_authority,
                ),
                patch.object(
                    plan_bound_module,
                    "journal_store_authority_scope",
                    forged_scope,
                ),
                patch.object(
                    plan_bound_module,
                    "verify_composed_runtime_target_host_qualification",
                    forged_composed,
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostCompositionError,
                    "requires composed target-host measurement authority",
                ),
            ):
                plan_bound_module._verify_declared_plan_runtime_target_host_qualification_without_chronology(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )

            forged_loader.assert_not_called()
            forged_authority.assert_not_called()
            forged_scope.assert_not_called()
            forged_composed.assert_not_called()

    def test_legacy_signed_pass_cannot_bypass_missing_composed_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            plan = declare_runtime_event_plan(
                store,
                plan_id="terminal-plan",
                spec=spec,
                expected_events=(_expected_event(),),
            )
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_qualification."
                "verify_runtime_target_host_qualification",
                return_value=object(),
            ) as legacy, self.assertRaisesRegex(
                RuntimeTargetHostCompositionError,
                "requires composed target-host measurement authority",
            ):
                verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=plan.plan_id,
                    spec=spec,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )
            legacy.assert_not_called()

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
