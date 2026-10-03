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
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp import runtime_target_host_plan_bound_qualification as plan_bound_module
from mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification import (
    verify_declared_plan_runtime_target_host_qualification,
)
from mvp.autotrade_mvp.runtime_target_host_qualification import (
    RuntimeTargetHostQualificationError,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("e" * 64)
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


def _campaign_plan(spec: RuntimeBudgetSpec, **overrides: object) -> RuntimeCampaignPlan:
    values = {
        "scenario_id": spec.scenario_id,
        "spec_digest": spec.digest,
        "release_sha": spec.release_sha,
        "configuration_hash": spec.configuration_hash,
        "host_fingerprint": spec.host_fingerprint,
        "workload_profile_hash": WORKLOAD,
        "declared_duration_ms": 1,
        "expected_financial_event_ids": (_expected_event().event_id,),
        "financial_aggregate_types": (_expected_event().aggregate_type,),
        "release_artifact_sha256": RELEASE_SHA,
    }
    values.update(overrides)
    return RuntimeCampaignPlan(**values)


def _declared_and_campaign(store: JournalStore, spec: RuntimeBudgetSpec):
    declared = declare_runtime_event_plan(
        store,
        plan_id="terminal-plan",
        spec=spec,
        expected_events=(_expected_event(),),
    )
    campaign_plan = _campaign_plan(spec)
    cut = begin_runtime_campaign(journal=store, spec=spec, plan=campaign_plan)
    return declared, campaign_plan, cut


class RuntimeTargetHostPlanBoundQualificationTests(unittest.TestCase):
    def test_terminal_expected_identities_are_derived_from_campaign_and_durable_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            declared, campaign_plan, cut = _declared_and_campaign(store, spec)
            receipt = object()
            evidence_store = object()
            accepted = object()
            with patch(
                "mvp.autotrade_mvp.runtime_target_host_plan_bound_qualification."
                "verify_runtime_target_host_qualification",
                return_value=accepted,
            ) as terminal, patch.object(
                plan_bound_module,
                "_verify_measurement_campaign_composition",
                return_value=accepted,
            ) as composition:
                result = verify_declared_plan_runtime_target_host_qualification(
                    receipt,
                    evidence_store=evidence_store,
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=declared.plan_id,
                    spec=spec,
                    campaign_plan=campaign_plan,
                    campaign_cut=cut,
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
                WORKLOAD,
            )
            self.assertNotEqual(WORKLOAD, declared.digest)
            self.assertEqual(
                args.kwargs["expected_journal_store_identity_digest"],
                cut.journal_store_identity_digest,
            )
            self.assertEqual(
                args.kwargs["expected_journal_store_identity_digest"],
                declared.store_identity_digest,
            )
            self.assertEqual(
                args.kwargs["expected_release_artifact_id"],
                RELEASE_ID,
            )
            self.assertEqual(
                args.kwargs["expected_release_artifact_sha256"],
                RELEASE_SHA,
            )
            composition.assert_called_once()

    def test_caller_owned_spec_is_detached_before_durable_plan_read(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            declared, campaign_plan, cut = _declared_and_campaign(store, spec)
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
            ) as terminal, patch.object(
                plan_bound_module,
                "_verify_measurement_campaign_composition",
                return_value=accepted,
            ):
                result = verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=declared.plan_id,
                    spec=caller_spec,
                    campaign_plan=campaign_plan,
                    campaign_cut=cut,
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
            declared, campaign_plan, cut = _declared_and_campaign(store, declared_spec)
            changed_spec = _spec(
                configuration_hash="sha256:" + ("e" * 64),
            )
            with patch.object(
                plan_bound_module,
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
                        plan_id=declared.plan_id,
                        spec=changed_spec,
                        campaign_plan=campaign_plan,
                        campaign_cut=cut,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            terminal.assert_not_called()

    def test_declared_plan_digest_cannot_substitute_for_workload_identity(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            declared, _campaign, _cut = _declared_and_campaign(store, spec)
            bad_campaign = _campaign_plan(
                spec,
                workload_profile_hash=declared.digest,
            )
            bad_cut = begin_runtime_campaign(
                journal=store,
                spec=spec,
                plan=bad_campaign,
            )
            accepted = object()
            with patch.object(
                plan_bound_module,
                "verify_runtime_target_host_qualification",
                return_value=accepted,
            ) as terminal, patch.object(
                plan_bound_module,
                "_verify_measurement_campaign_composition",
                return_value=accepted,
            ):
                verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id=declared.plan_id,
                    spec=spec,
                    campaign_plan=bad_campaign,
                    campaign_cut=bad_cut,
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )
            self.assertEqual(
                terminal.call_args.kwargs["expected_workload_profile_hash"],
                declared.digest,
            )
            self.assertNotEqual(
                terminal.call_args.kwargs["expected_workload_profile_hash"],
                WORKLOAD,
            )

    def test_campaign_expected_identity_must_match_durable_declaration(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            spec = _spec()
            declared, _campaign, _cut = _declared_and_campaign(store, spec)
            other_campaign = _campaign_plan(
                spec,
                expected_financial_event_ids=("different-financial-event",),
            )
            other_cut = begin_runtime_campaign(
                journal=store,
                spec=spec,
                plan=other_campaign,
            )
            with patch.object(
                plan_bound_module,
                "verify_runtime_target_host_qualification",
            ) as terminal:
                with self.assertRaisesRegex(
                    RuntimeTargetHostQualificationError,
                    "expected financial identities conflict",
                ):
                    verify_declared_plan_runtime_target_host_qualification(
                        object(),
                        evidence_store=object(),
                        evidence_root=directory,
                        journal_store=store,
                        plan_id=declared.plan_id,
                        spec=spec,
                        campaign_plan=other_campaign,
                        campaign_cut=other_cut,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )
            terminal.assert_not_called()

    def test_selected_store_generation_is_held_through_plan_read(self) -> None:
        with TemporaryDirectory() as directory:
            selected = JournalStore(Path(directory) / "selected.sqlite3")
            alternate = JournalStore(Path(directory) / "alternate.sqlite3")
            spec = _spec()
            declared, campaign_plan, cut = _declared_and_campaign(selected, spec)

            selected_event = JournalStore.get_event(selected, declared.event_id)
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
                        plan_id=declared.plan_id,
                        spec=spec,
                        campaign_plan=campaign_plan,
                        campaign_cut=cut,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                    )

            terminal.assert_not_called()

    def test_adapter_requires_exact_journal_store_budget_plan_and_cut_types(self) -> None:
        with self.assertRaisesRegex(TypeError, "journal_store must be exact JournalStore"):
            verify_declared_plan_runtime_target_host_qualification(
                object(),
                evidence_store=object(),
                evidence_root="unused",
                journal_store=object(),
                plan_id="terminal-plan",
                spec=_spec(),
                campaign_plan=object(),
                campaign_cut=object(),
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
                    campaign_plan=object(),
                    campaign_cut=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )
            with self.assertRaisesRegex(
                TypeError,
                "campaign_plan must be exact RuntimeCampaignPlan",
            ):
                verify_declared_plan_runtime_target_host_qualification(
                    object(),
                    evidence_store=object(),
                    evidence_root=directory,
                    journal_store=store,
                    plan_id="terminal-plan",
                    spec=_spec(),
                    campaign_plan=object(),
                    campaign_cut=object(),
                    expected_release_artifact_id=RELEASE_ID,
                    expected_release_artifact_sha256=RELEASE_SHA,
                )


if __name__ == "__main__":
    unittest.main()
