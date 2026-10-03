from inspect import signature
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import (
    RuntimeBudgetError,
    RuntimeBudgetSpec,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignCut,
    RuntimeCampaignEvidence,
    RuntimeCampaignPlan,
    begin_runtime_campaign,
    collect_runtime_campaign_evidence,
    evaluate_runtime_campaign,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
WORKLOAD = "sha256:" + ("d" * 64)
RESOURCE = "sha256:" + ("e" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-store-authority",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _plan(spec: RuntimeBudgetSpec) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1,
        expected_financial_event_ids=("fin-expected",),
        financial_aggregate_types=("risk_decision",),
    )


def _envelope(event_id: str) -> dict[str, object]:
    payload = {"event_id": event_id, "kind": "risk_decision"}
    return {
        "event_id": event_id,
        "event_type": "QualificationEvent",
        "aggregate_type": "risk_decision",
        "aggregate_id": event_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T04:45:00+00:00",
    }


def _rebind_store(target: JournalStore, source: JournalStore) -> None:
    """Simulate hostile same-object rebinding to another otherwise-valid store."""

    target.path = source.path
    target._store_identity = source.store_identity


class _ForgedJournalStore(JournalStore):
    def current_journal_sequence(self) -> int:
        return 0

    def pending_outbox_count(self) -> int:
        return 0

    def load_events_after_journal_sequence(self, *args, **kwargs):
        return []


class _ForgedRuntimeCampaignPlan(RuntimeCampaignPlan):
    pass


class _ForgedRuntimeCampaignCut(RuntimeCampaignCut):
    def __post_init__(self, _token: object | None) -> None:
        # Deliberately bypass the base issuance token to model a hostile subclass.
        return None


class _ForgedRuntimeCampaignEvidence(RuntimeCampaignEvidence):
    def __post_init__(self, _token: object | None) -> None:
        # Deliberately bypass the base issuance token to model a hostile subclass.
        return None

    def to_observation(self, spec: RuntimeBudgetSpec):
        raise AssertionError("forged polymorphic evidence was invoked")


class RuntimeLoadQualificationStoreAuthorityTests(unittest.TestCase):
    def test_campaign_api_has_no_caller_selected_clock_seam(self):
        self.assertNotIn("monotonic_ns", signature(begin_runtime_campaign).parameters)
        self.assertNotIn(
            "monotonic_ns",
            signature(collect_runtime_campaign_evidence).parameters,
        )

    def test_campaign_plan_factory_cannot_issue_subclass_authority(self):
        spec = _spec()
        issued = _ForgedRuntimeCampaignPlan.create(
            spec=spec,
            workload_profile_hash=WORKLOAD,
            declared_duration_ms=1,
            expected_financial_event_ids=("fin-expected",),
            financial_aggregate_types=("risk_decision",),
        )
        self.assertIs(type(issued), RuntimeCampaignPlan)

    def test_campaign_rejects_plan_subclass_before_clock_sampling(self):
        spec = _spec()
        forged = _ForgedRuntimeCampaignPlan(
            scenario_id=spec.scenario_id,
            spec_digest=spec.digest,
            release_sha=spec.release_sha,
            configuration_hash=spec.configuration_hash,
            host_fingerprint=spec.host_fingerprint,
            workload_profile_hash=WORKLOAD,
            declared_duration_ms=1,
            expected_financial_event_ids=("fin-expected",),
            financial_aggregate_types=("risk_decision",),
        )
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            clock_called = False

            def clock() -> int:
                nonlocal clock_called
                clock_called = True
                return 1_000_000_000

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                side_effect=clock,
            ), self.assertRaisesRegex(TypeError, "exact RuntimeCampaignPlan"):
                begin_runtime_campaign(journal=journal, spec=spec, plan=forged)
            self.assertFalse(clock_called)

    def test_collect_rejects_forged_cut_subclass(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            forged_cut = _ForgedRuntimeCampaignCut(
                plan_digest=current_plan.digest,
                spec_digest=spec.digest,
                start_journal_sequence=0,
                started_monotonic_ns=1_000_000_000,
                journal_store_identity_digest="sha256:" + ("f" * 64),
            )
            with self.assertRaisesRegex(TypeError, "exact RuntimeCampaignCut"):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=forged_cut,
                    financial_latency_us=(),
                    financial_staleness_us=(),
                    research_interference_us=(),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )

    def test_evaluator_rejects_forged_evidence_subclass_before_polymorphic_dispatch(self):
        spec = _spec()
        forged = _ForgedRuntimeCampaignEvidence(
            plan_digest="sha256:" + ("f" * 64),
            spec_digest=spec.digest,
            release_sha=spec.release_sha,
            configuration_hash=spec.configuration_hash,
            host_fingerprint=spec.host_fingerprint,
            declared_duration_us=1_000,
            observed_duration_us=1_000,
            start_journal_sequence=0,
            end_journal_sequence=0,
            expected_financial_event_ids=("fin-expected",),
            recovered_financial_event_bindings=(),
            financial_latency_us=(),
            financial_staleness_us=(),
            research_interference_us=(),
            reconnect_backlog_remaining=0,
            resource_evidence_hash=RESOURCE,
            resource_metrics={},
            journal_taxonomy_digest="sha256:" + ("f" * 64),
            journal_store_identity_digest="sha256:" + ("f" * 64),
        )
        with self.assertRaisesRegex(TypeError, "exact RuntimeCampaignEvidence"):
            evaluate_runtime_campaign(spec, forged)

    def test_campaign_rejects_journal_store_subclass_authority(self):
        with TemporaryDirectory() as directory:
            journal = _ForgedJournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            clock_called = False

            def clock() -> int:
                nonlocal clock_called
                clock_called = True
                return 1_000_000_000

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                side_effect=clock,
            ), self.assertRaisesRegex(TypeError, "exact JournalStore"):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                )
            self.assertFalse(clock_called)

    def test_start_clock_cannot_rebind_campaign_to_another_journal_generation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal-a.sqlite3")
            other = JournalStore(f"{directory}/journal-b.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)

            def starting_clock() -> int:
                _rebind_store(journal, other)
                return 1_000_000_000

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                side_effect=starting_clock,
            ), self.assertRaisesRegex(
                RuntimeError,
                "journal operation authority changed before connection",
            ):
                begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                )

    def test_collect_rejects_cut_from_another_journal_generation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal-a.sqlite3")
            other = JournalStore(f"{directory}/journal-b.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                )
            other.append_event(_envelope("fin-expected"))
            _rebind_store(journal, other)

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_001_000,
            ), self.assertRaisesRegex(
                RuntimeBudgetError,
                "campaign cut belongs to another journal generation",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100,),
                    financial_staleness_us=(100,),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )

    def test_collect_snapshots_plan_before_journal_io(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                )
            journal.append_event(_envelope("fin-expected"))
            journal.append_event(_envelope("fin-extra"))

            original_pending = JournalStore.pending_outbox_count
            mutated = False

            def pending_with_plan_mutation(store: JournalStore) -> int:
                nonlocal mutated
                if store is journal and not mutated:
                    mutated = True
                    object.__setattr__(
                        current_plan,
                        "expected_financial_event_ids",
                        ("fin-expected", "fin-extra"),
                    )
                return original_pending(store)

            with patch.object(
                JournalStore,
                "pending_outbox_count",
                new=pending_with_plan_mutation,
            ), patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_001_000,
            ), self.assertRaisesRegex(
                RuntimeBudgetError,
                "undeclared financial event identities.*fin-extra",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100, 100),
                    financial_staleness_us=(100, 100),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )

            self.assertTrue(mutated)
            self.assertEqual(
                current_plan.expected_financial_event_ids,
                ("fin-expected", "fin-extra"),
            )

    def test_terminal_clock_shadow_cannot_hide_undeclared_financial_event(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            spec = _spec()
            current_plan = _plan(spec)
            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                return_value=1_000_000_000,
            ):
                cut = begin_runtime_campaign(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                )
            journal.append_event(_envelope("fin-expected"))
            journal.append_event(_envelope("fin-extra"))

            def ending_clock() -> int:
                forged = _envelope("fin-expected")
                forged["journal_sequence"] = 2
                journal.load_events_after_journal_sequence = (  # type: ignore[method-assign]
                    lambda *args, **kwargs: [forged]
                )
                return 1_000_001_000

            with patch(
                "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
                side_effect=ending_clock,
            ), self.assertRaisesRegex(
                RuntimeBudgetError,
                "undeclared financial event identities.*fin-extra",
            ):
                collect_runtime_campaign_evidence(
                    journal=journal,
                    spec=spec,
                    plan=current_plan,
                    cut=cut,
                    financial_latency_us=(100,),
                    financial_staleness_us=(100,),
                    research_interference_us=(100,),
                    resource_evidence_hash=RESOURCE,
                    resource_metrics={"cpu_peak_millis": 1},
                )


if __name__ == "__main__":
    unittest.main()
