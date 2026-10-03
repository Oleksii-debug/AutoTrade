import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import measure_declared_financial_operation
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_qualification import (
    RuntimeCampaignPlan,
    begin_runtime_campaign,
)
from mvp.autotrade_mvp.runtime_target_host_durable_financial import (
    CLOCK_CONTRACT_ID,
    TARGET_HOST_SHARED_CLOCK_ID,
    RuntimeTargetHostDurableFinancialError,
    bind_durable_financial_latency_to_target_host_measurement,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    FinancialTargetHostSample,
    ResourceTargetHostSample,
    TargetHostMeasurementArtifact,
)


SOURCE = "a" * 40
CONFIG = "sha256:" + "b" * 64
HOST = "sha256:" + "c" * 64
WORKLOAD = "sha256:" + "d" * 64
RELEASE_ID = "50000000-0000-4000-8000-000000000001"
RELEASE_SHA = "sha256:" + "e" * 64
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="wp65-durable-financial",
        release_sha=SOURCE,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500_000,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def expected(event_id: str = "financial-1") -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=EVENT_TYPE,
        aggregate_type="runtime_target_host_fixture",
        aggregate_id="wp65-durable-financial",
        aggregate_version=1,
    )


def append_expected(store: JournalStore, value: ExpectedJournalEvent) -> None:
    payload = {"scenario": "wp65-durable-financial", "event_id": value.event_id}
    store.append_event(
        {
            "event_id": value.event_id,
            "event_type": value.event_type,
            "aggregate_type": value.aggregate_type,
            "aggregate_id": value.aggregate_id,
            "aggregate_version": str(value.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-03T07:20:00Z",
        }
    )


def campaign_plan(current_spec: RuntimeBudgetSpec, event_id: str) -> RuntimeCampaignPlan:
    return RuntimeCampaignPlan.create(
        spec=current_spec,
        workload_profile_hash=WORKLOAD,
        declared_duration_ms=1_000,
        expected_financial_event_ids=(event_id,),
        financial_aggregate_types=("runtime_target_host_fixture",),
        release_artifact_sha256=RELEASE_SHA,
    )


def target_measurement(
    *,
    current_spec,
    current_plan,
    cut,
    event_id,
    journal_sequence,
    end_journal_sequence,
    latency_start_ns=1_100_000_000,
    latency_end_ns=1_100_100_000,
    monotonic_clock_id=TARGET_HOST_SHARED_CLOCK_ID,
    journal_store_identity_digest=None,
):
    return TargetHostMeasurementArtifact(
        source_sha=SOURCE,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=RELEASE_SHA,
        scenario_id=current_plan.scenario_id,
        spec_digest=current_spec.digest,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        workload_profile_hash=WORKLOAD,
        plan_digest=current_plan.digest,
        journal_taxonomy_digest=current_plan.journal_taxonomy_digest,
        journal_store_identity_digest=(
            cut.journal_store_identity_digest
            if journal_store_identity_digest is None
            else journal_store_identity_digest
        ),
        start_journal_sequence=cut.start_journal_sequence,
        end_journal_sequence=end_journal_sequence,
        monotonic_clock_id=monotonic_clock_id,
        staleness_basis="host-monotonic-financial-state-age",
        research_interference_basis="host-monotonic-contention-delay",
        financial_samples=(
            FinancialTargetHostSample(
                sample_id="target-financial-1",
                event_id=event_id,
                journal_sequence=journal_sequence,
                latency_start_monotonic_ns=latency_start_ns,
                latency_end_monotonic_ns=latency_end_ns,
                staleness_source_monotonic_ns=1_100_100_000,
                staleness_observed_monotonic_ns=1_200_100_000,
            ),
        ),
        research_samples=(),
        resource_samples=(
            ResourceTargetHostSample(
                sample_id="resource-1",
                monotonic_ns=1_300_000_000,
                phase="steady",
                metrics={"memory_rss_bytes": 4096},
            ),
        ),
    )


class RuntimeTargetHostDurableFinancialTests(unittest.TestCase):
    def _prepared(self):
        temporary = tempfile.TemporaryDirectory()
        store = JournalStore(Path(temporary.name) / "journal.sqlite3")
        current_spec = spec()
        event = expected()
        current_plan = campaign_plan(current_spec, event.event_id)
        with patch(
            "mvp.autotrade_mvp.runtime_load_qualification.time.monotonic_ns",
            return_value=1_000_000_000,
        ):
            cut = begin_runtime_campaign(
                journal=store,
                spec=current_spec,
                plan=current_plan,
            )
        declared = declare_runtime_event_plan(
            store,
            plan_id="durable-financial-plan",
            spec=current_spec,
            expected_events=(event,),
        )
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(1_100_000_000, 1_100_100_000),
        ):
            _result, durable = measure_declared_financial_operation(
                store,
                current_spec,
                plan_id=declared.plan_id,
                event_id=event.event_id,
                operation=lambda: append_expected(store, event),
            )
        return temporary, store, current_spec, current_plan, cut, declared, durable

    @staticmethod
    def _python_313():
        return patch(
            "mvp.autotrade_mvp.runtime_target_host_durable_financial.sys.version_info",
            SimpleNamespace(major=3, minor=13),
        )

    def _measurement(self, store, current_spec, current_plan, cut, durable, **overrides):
        values = {
            "current_spec": current_spec,
            "current_plan": current_plan,
            "cut": cut,
            "event_id": durable.event_id,
            "journal_sequence": durable.event_journal_sequence,
            "end_journal_sequence": JournalStore.current_journal_sequence(store),
        }
        values.update(overrides)
        return target_measurement(**values)

    def test_binding_retains_payload_hash_and_exact_durable_latency_identity(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store, current_spec, current_plan, cut, durable
        )
        with self._python_313():
            binding = bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )
        self.assertEqual(binding.clock_contract_id, CLOCK_CONTRACT_ID)
        self.assertEqual(binding.target_host_measurement_digest, measurement.digest)
        self.assertEqual(binding.declared_plan_digest, declared.digest)
        self.assertEqual(binding.bindings[0].event_payload_hash, durable.event_payload_hash)
        self.assertEqual(
            binding.bindings[0].latency_measurement_journal_sequence,
            durable.measurement_journal_sequence,
        )
        self.assertEqual(
            binding.bindings[0].durable_latency_sample_digest,
            durable.digest,
        )
        self.assertEqual(
            binding.payload_hash_by_event[durable.event_id],
            durable.event_payload_hash,
        )
        self.assertTrue(binding.digest.startswith("sha256:"))

    def test_target_measurement_must_bind_exact_journal_store_generation(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            journal_store_identity_digest="sha256:" + "9" * 64,
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "another JournalStore generation",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_cross_generation_latency_sample_splice_is_rejected(self):
        first = self._prepared()
        second = self._prepared()
        first_temporary, store, current_spec, current_plan, cut, declared, durable = first
        second_temporary, _store2, _spec2, _plan2, _cut2, _declared2, durable2 = second
        self.addCleanup(first_temporary.cleanup)
        self.addCleanup(second_temporary.cleanup)
        measurement = self._measurement(
            store, current_spec, current_plan, cut, durable
        )
        with self._python_313(), patch(
            "mvp.autotrade_mvp.runtime_target_host_durable_financial."
            "load_declared_financial_latency_samples",
            return_value=(durable2,),
        ), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "plan identity conflicts",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_post_cut_durable_latency_record_cannot_validate_measurement(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            end_journal_sequence=durable.event_journal_sequence,
        )
        self.assertGreater(
            durable.measurement_journal_sequence,
            measurement.end_journal_sequence,
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "outside target-host journal cut",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_python_312_clock_domain_is_rejected_before_journal_read(self):
        with patch(
            "mvp.autotrade_mvp.runtime_target_host_durable_financial.sys.version_info",
            SimpleNamespace(major=3, minor=12),
        ), patch(
            "mvp.autotrade_mvp.runtime_target_host_durable_financial."
            "load_declared_runtime_event_plan"
        ) as load_plan, self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "before Python 3.13",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                object(),
                object(),
                declared_plan_id="not-read",
                measurement=object(),
            )
        load_plan.assert_not_called()

    def test_target_must_declare_exact_shared_clock_contract(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            monotonic_clock_id="unrelated-monotonic-clock",
        )
        with self._python_313(), patch(
            "mvp.autotrade_mvp.runtime_target_host_durable_financial."
            "load_declared_runtime_event_plan"
        ) as load_plan, self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "does not declare the shared",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )
        load_plan.assert_not_called()

    def test_target_latency_endpoint_substitution_is_rejected(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            latency_start_ns=1_100_000_001,
            latency_end_ns=1_100_100_001,
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "raw latency endpoints conflict",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_target_journal_sequence_substitution_is_rejected(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            journal_sequence=durable.measurement_journal_sequence,
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "journal sequence conflicts",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_target_event_identity_substitution_is_rejected(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        measurement = self._measurement(
            store,
            current_spec,
            current_plan,
            cut,
            durable,
            event_id="substituted-financial",
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "event identities do not match durable plan",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                current_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )

    def test_measurement_spec_identity_mismatch_is_rejected_before_durable_binding(self):
        temporary, store, current_spec, current_plan, cut, declared, durable = self._prepared()
        self.addCleanup(temporary.cleanup)
        other_spec = RuntimeBudgetSpec(
            scenario_id=current_spec.scenario_id,
            release_sha=SOURCE,
            configuration_hash="sha256:" + "9" * 64,
            host_fingerprint=HOST,
            strategy_horizon_us=1_000_000,
            max_p95_financial_latency_us=500,
            max_financial_staleness_us=500_000,
            max_research_interference_us=500,
            min_financial_samples=1,
            min_research_samples=1,
        )
        measurement = self._measurement(
            store, current_spec, current_plan, cut, durable
        )
        with self._python_313(), self.assertRaisesRegex(
            RuntimeTargetHostDurableFinancialError,
            "spec digest conflicts",
        ):
            bind_durable_financial_latency_to_target_host_measurement(
                store,
                other_spec,
                declared_plan_id=declared.plan_id,
                measurement=measurement,
            )


if __name__ == "__main__":
    unittest.main()
