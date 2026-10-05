from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import measure_declared_financial_operation
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_research_measurement import (
    ExpectedResearchInterferenceSample,
    declare_research_interference_plan,
    measure_declared_research_interference,
)
from mvp.autotrade_mvp.runtime_target_host_campaign_authority import (
    declare_runtime_target_host_campaign_authority,
)
from mvp.autotrade_mvp.runtime_target_host_measurement import (
    RuntimeTargetHostMeasurementArtifact,
    RuntimeTargetHostMeasurementError,
    collect_runtime_target_host_measurement,
)


SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
ARTIFACT_ID = "00000000-0000-0000-0000-000000000065"
ARTIFACT_SHA = "sha256:" + ("d" * 64)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="target-host-raw-current",
        release_sha=SHA,
        configuration_hash=CONFIG,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=1,
        min_research_samples=1,
    )


def _financial_event() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-target-host-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="target-host-raw-current",
        aggregate_version=1,
    )


def _append_financial(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "target-host-raw-current", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-05T13:15:00Z",
        }
    )


class RuntimeTargetHostMeasurementTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "target-host-measurement.sqlite3")

    def _plans(self, store: JournalStore):
        expected = _financial_event()
        financial_plan = declare_runtime_event_plan(
            store,
            plan_id="target-host-financial-plan",
            spec=_spec(),
            expected_events=(expected,),
        )
        research_plan = declare_research_interference_plan(
            store,
            _spec(),
            plan_id="target-host-research-plan",
            financial_plan_id=financial_plan.plan_id,
            expected_samples=(
                ExpectedResearchInterferenceSample(
                    "target-host-research-1",
                    "cpu-pressure",
                ),
            ),
        )
        return expected, financial_plan, research_plan

    def _authority(self, store: JournalStore, financial_plan):
        return declare_runtime_target_host_campaign_authority(
            store,
            _spec(),
            authority_id="target-host-run-current",
            financial_plan_id=financial_plan.plan_id,
            release_artifact_id=ARTIFACT_ID,
            release_artifact_sha256=ARTIFACT_SHA,
        )

    def _measure_financial(self, store: JournalStore, financial_plan, expected):
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(1_000_000, 1_125_000),
        ):
            _result, sample = measure_declared_financial_operation(
                store,
                _spec(),
                plan_id=financial_plan.plan_id,
                event_id=expected.event_id,
                operation=lambda: _append_financial(store, expected),
            )
        return sample

    def _measure_research(self, store: JournalStore, research_plan):
        with patch(
            "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
            side_effect=(2_000_000, 2_080_000),
        ):
            _result, sample = measure_declared_research_interference(
                store,
                _spec(),
                plan_id=research_plan.plan_id,
                sample_id="target-host-research-1",
                operation=lambda: None,
            )
        return sample

    def test_collects_raw_durable_samples_only_after_pre_run_authority(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected, financial_plan, research_plan = self._plans(store)
            authority = self._authority(store, financial_plan)
            financial = self._measure_financial(store, financial_plan, expected)
            research = self._measure_research(store, research_plan)

            artifact = collect_runtime_target_host_measurement(
                store,
                _spec(),
                authority_id=authority.authority_id,
                research_plan_id=research_plan.plan_id,
            )

            self.assertEqual(artifact.authority_digest, authority.digest)
            self.assertEqual(artifact.source_sha, SHA)
            self.assertEqual(artifact.release_artifact_id, ARTIFACT_ID)
            self.assertEqual(artifact.release_artifact_sha256, ARTIFACT_SHA)
            self.assertEqual(artifact.financial_plan_digest, financial_plan.digest)
            self.assertEqual(artifact.store_identity_digest, financial_plan.store_identity_digest)
            self.assertEqual(artifact.financial_event_ids, (expected.event_id,))
            self.assertEqual(artifact.financial_latency_us, (financial.latency_us,))
            self.assertEqual(artifact.financial_staleness_us, (financial.latency_us,))
            self.assertEqual(
                artifact.research_interference_us,
                (research.interference_us,),
            )
            self.assertGreater(
                artifact.financial_samples[0].event_journal_sequence,
                artifact.authority_journal_sequence,
            )
            self.assertGreater(
                artifact.research_samples[0].measurement_journal_sequence,
                artifact.authority_journal_sequence,
            )
            self.assertEqual(artifact.reconnect_backlog_remaining, 0)
            self.assertEqual(artifact.resource_evidence_status, "NOT_COLLECTED")
            self.assertFalse(artifact.terminal_qualification_eligible)
            self.assertTrue(artifact.digest.startswith("sha256:"))
            self.assertEqual(
                artifact.canonical_payload()["staleness_basis"],
                "declared_financial_operation_start_to_durable_event_observation_same_host_monotonic",
            )

    def test_financial_sample_from_before_pre_run_authority_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected, financial_plan, research_plan = self._plans(store)
            self._measure_financial(store, financial_plan, expected)
            authority = self._authority(store, financial_plan)
            self._measure_research(store, research_plan)

            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "financial event predates target-host pre-run authority",
            ):
                collect_runtime_target_host_measurement(
                    store,
                    _spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research_plan.plan_id,
                )

    def test_research_sample_from_before_pre_run_authority_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            expected, financial_plan, research_plan = self._plans(store)
            self._measure_research(store, research_plan)
            authority = self._authority(store, financial_plan)
            self._measure_financial(store, financial_plan, expected)

            with self.assertRaisesRegex(
                RuntimeTargetHostMeasurementError,
                "research measurement predates target-host pre-run authority",
            ):
                collect_runtime_target_host_measurement(
                    store,
                    _spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research_plan.plan_id,
                )

    def test_public_artifact_dataclass_cannot_self_mint_measurement(self):
        with self.assertRaisesRegex(
            RuntimeTargetHostMeasurementError,
            "must come from canonical collector",
        ):
            RuntimeTargetHostMeasurementArtifact(
                authority_id="forged",
                authority_digest=CONFIG,
                source_sha=SHA,
                release_artifact_id=ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                scenario_id="forged",
                spec_digest=CONFIG,
                configuration_hash=CONFIG,
                host_fingerprint=HOST,
                financial_plan_id="forged",
                financial_plan_digest=CONFIG,
                store_identity_digest=CONFIG,
                journal_taxonomy_digest=CONFIG,
                authority_journal_sequence=1,
                conservation_start_journal_sequence=0,
                conservation_end_journal_sequence=1,
                conservation_digest=CONFIG,
                reconnect_backlog_remaining=0,
                financial_samples=(),
                research_samples=(),
            )


if __name__ == "__main__":
    unittest.main()
