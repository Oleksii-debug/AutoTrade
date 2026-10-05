from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_research_measurement import (
    ExpectedResearchInterferenceSample,
    declare_research_interference_plan,
)
from mvp.autotrade_mvp.runtime_target_host_campaign_authority import (
    declare_runtime_target_host_campaign_authority,
)
from mvp.autotrade_mvp.runtime_target_host_inventory import host_identity_fingerprint
from mvp.autotrade_mvp.runtime_target_host_runner import (
    RuntimeTargetHostRunnerError,
    run_declared_target_host_campaign,
)


SOURCE_SHA = "a" * 40
CONFIG = "sha256:" + ("b" * 64)
RELEASE_ARTIFACT_ID = "00000000-0000-4000-8000-000000000065"
RELEASE_ARTIFACT_SHA = "sha256:" + ("d" * 64)
INVENTORY_ARTIFACT_ID = "00000000-0000-4000-8000-000000000066"
MEASUREMENT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000067"
RUN_RECEIPT_ARTIFACT_ID = "00000000-0000-4000-8000-000000000068"
HOST_IDENTITY = {
    "system": "Windows",
    "release": "11",
    "machine": "AMD64",
    "python_implementation": "CPython",
    "python_version": "3.12.11",
    "cpu_count": 8,
}
HOST = host_identity_fingerprint(HOST_IDENTITY)


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="current-target-host-runner",
        release_sha=SOURCE_SHA,
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
        event_id="runner-financial-1",
        event_type="RuntimeQualificationFinancialEvent",
        aggregate_type="risk_decision",
        aggregate_id="current-target-host-runner",
        aggregate_version=1,
    )


def _append_financial(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "current-target-host-runner", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-05T13:30:00Z",
        }
    )


class RuntimeTargetHostRunnerTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runner.sqlite3")

    def _plans(self, store: JournalStore):
        expected = _financial_event()
        financial = declare_runtime_event_plan(
            store,
            plan_id="runner-financial-plan",
            spec=_spec(),
            expected_events=(expected,),
        )
        research = declare_research_interference_plan(
            store,
            _spec(),
            plan_id="runner-research-plan",
            financial_plan_id=financial.plan_id,
            expected_samples=(
                ExpectedResearchInterferenceSample("runner-research-1", "cpu-pressure"),
            ),
        )
        return expected, financial, research

    def _authority(self, store: JournalStore, financial):
        return declare_runtime_target_host_campaign_authority(
            store,
            _spec(),
            authority_id="runner-authority",
            financial_plan_id=financial.plan_id,
            release_artifact_id=RELEASE_ARTIFACT_ID,
            release_artifact_sha256=RELEASE_ARTIFACT_SHA,
        )

    def test_executes_only_predeclared_work_and_retains_nonterminal_chain(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)

            def financial_operation() -> str:
                _append_financial(store, expected)
                return "financial-done"

            def research_operation() -> str:
                return "research-done"

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                    return_value=dict(HOST_IDENTITY),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(1_000_000, 1_080_000),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(2_000_000, 2_125_000),
                ),
            ):
                result = run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )

            self.assertEqual(result.authority.digest, authority.digest)
            self.assertEqual(result.research_plan.digest, research.digest)
            self.assertEqual(result.measurement.financial_event_ids, (expected.event_id,))
            self.assertEqual(result.measurement.financial_latency_us, (125,))
            self.assertEqual(result.measurement.research_interference_us, (80,))
            self.assertEqual(result.measurement.resource_evidence_status, "NOT_COLLECTED")
            self.assertFalse(result.measurement.terminal_qualification_eligible)
            self.assertFalse(result.run_receipt.terminal_qualification_eligible)
            self.assertFalse(result.terminal_qualification_eligible)
            self.assertEqual(result.run_receipt.research_plan_id, research.plan_id)
            self.assertEqual(result.run_receipt.research_plan_digest, research.digest)
            self.assertLess(
                result.run_receipt.research_plan_declared_journal_sequence,
                result.run_receipt.authority_journal_sequence,
            )
            self.assertEqual(
                result.run_receipt.measurement_payload_sha256,
                result.measurement.digest,
            )

            inventory_manifest, _inventory_raw = evidence_store.read_authenticated_snapshot(
                INVENTORY_ARTIFACT_ID
            )
            measurement_manifest, measurement_raw = (
                evidence_store.read_authenticated_snapshot(MEASUREMENT_ARTIFACT_ID)
            )
            receipt_manifest, receipt_raw = evidence_store.read_authenticated_snapshot(
                RUN_RECEIPT_ARTIFACT_ID
            )
            self.assertEqual(
                inventory_manifest["sha256"],
                result.published_inventory.payload_sha256,
            )
            self.assertEqual(measurement_raw, result.measurement.canonical_bytes())
            self.assertEqual(
                measurement_manifest["sha256"],
                result.published_measurement.payload_sha256,
            )
            self.assertEqual(receipt_raw, result.run_receipt.canonical_bytes())
            self.assertEqual(
                receipt_manifest["sha256"],
                result.run_receipt_payload_sha256,
            )
            self.assertEqual(
                receipt_manifest["metadata"]["resource_evidence_status"],
                "NOT_COLLECTED",
            )
            self.assertFalse(
                receipt_manifest["metadata"]["terminal_qualification_eligible"]
            )

    def test_artifact_store_instance_method_shadow_cannot_replace_runner_io(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)
            forged_calls = []

            def forged_publish(**_kwargs):
                forged_calls.append("publish")
                raise AssertionError("instance-shadowed publisher executed")

            def forged_read(_artifact_id):
                forged_calls.append("read")
                raise AssertionError("instance-shadowed reader executed")

            evidence_store.publish_bytes = forged_publish
            evidence_store.read_authenticated_snapshot = forged_read

            def financial_operation() -> str:
                _append_financial(store, expected)
                return "financial-done"

            def research_operation() -> str:
                return "research-done"

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                    return_value=dict(HOST_IDENTITY),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(1_000_000, 1_080_000),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                    side_effect=(2_000_000, 2_125_000),
                ),
            ):
                result = run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={
                        research.expected_sample_ids[0]: research_operation
                    },
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )

            self.assertEqual(forged_calls, [])
            self.assertFalse(result.terminal_qualification_eligible)
            for artifact_id in (
                INVENTORY_ARTIFACT_ID,
                MEASUREMENT_ARTIFACT_ID,
                RUN_RECEIPT_ARTIFACT_ID,
            ):
                manifest, _raw = ArtifactStore.read_authenticated_snapshot(
                    evidence_store,
                    artifact_id,
                )
                self.assertEqual(manifest["artifact_id"], artifact_id)

    def test_research_plan_declared_after_authority_is_rejected_before_callbacks(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected = _financial_event()
            financial = declare_runtime_event_plan(
                store,
                plan_id="runner-financial-plan",
                spec=_spec(),
                expected_events=(expected,),
            )
            authority = self._authority(store, financial)
            research = declare_research_interference_plan(
                store,
                _spec(),
                plan_id="late-research-plan",
                financial_plan_id=financial.plan_id,
                expected_samples=(
                    ExpectedResearchInterferenceSample("late-research-1", "cpu"),
                ),
            )
            financial_callback = Mock()
            research_callback = Mock()

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                "research plan must be durably declared before",
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_callback},
                    research_operations={research.expected_sample_ids[0]: research_callback},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )
            financial_callback.assert_not_called()
            research_callback.assert_not_called()
            self.assertEqual(list(evidence_store.manifests.iterdir()), [])

    def test_operation_key_mismatch_is_rejected_before_inventory_or_callbacks(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)

            def wrong_financial_operation() -> None:
                raise AssertionError("must not run")

            def research_operation() -> None:
                raise AssertionError("must not run")

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                "financial_operations keys must exactly match",
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={"wrong-event": wrong_financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )
            self.assertEqual(list(evidence_store.manifests.iterdir()), [])

    def test_artifact_store_path_subclass_is_rejected_before_resolve_or_callbacks(self):
        class HostilePath(type(Path())):
            resolve_calls = 0

            def resolve(self, *args, **kwargs):
                type(self).resolve_calls += 1
                raise AssertionError("hostile ArtifactStore path resolve must not execute")

        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)
            callback_calls = []

            evidence_store.root = HostilePath(evidence_store.root)

            def financial_operation() -> None:
                callback_calls.append("financial")
                _append_financial(store, expected)

            def research_operation() -> None:
                callback_calls.append("research")

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                "ArtifactStore root must remain an exact pathlib path",
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )

            self.assertEqual(HostilePath.resolve_calls, 0)
            self.assertEqual(callback_calls, [])
            self.assertEqual(list(evidence_store.manifests.iterdir()), [])


    def test_later_callback_code_mutation_is_rejected_before_financial_operation(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)

            def financial_operation() -> None:
                _append_financial(store, expected)

            def replacement_operation() -> None:
                if store is None or expected is None:
                    raise AssertionError("unreachable")
                raise AssertionError("mutated financial callback must not execute")

            def research_operation() -> None:
                financial_operation.__code__ = replacement_operation.__code__

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                    return_value=dict(HOST_IDENTITY),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(1_000_000, 1_080_000),
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "financial operation runner-financial-1 executable authority changed",
                ),
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )

            self.assertIsNone(store.get_event(expected.event_id))

    def test_artifact_store_namespace_redirect_is_rejected_after_callback(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            redirected_store = ArtifactStore(Path(root) / "redirected-evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)

            def financial_operation() -> None:
                _append_financial(store, expected)

            def research_operation() -> None:
                evidence_store.root = redirected_store.root

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                    return_value=dict(HOST_IDENTITY),
                ),
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(1_000_000, 1_080_000),
                ),
                self.assertRaisesRegex(
                    RuntimeTargetHostRunnerError,
                    "ArtifactStore root authority changed",
                ),
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )

            self.assertIsNone(store.get_event(expected.event_id))

    def test_artifact_store_class_publisher_mutation_is_rejected_after_callback(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)
            original_publish = ArtifactStore.publish_bytes

            def financial_operation() -> None:
                _append_financial(store, expected)

            def forged_publish(self, **_kwargs):
                raise AssertionError("mutated class publisher must not execute")

            def research_operation() -> None:
                ArtifactStore.publish_bytes = forged_publish

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_target_host_inventory.capture_runtime_host_identity",
                        return_value=dict(HOST_IDENTITY),
                    ),
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(1_000_000, 1_080_000),
                    ),
                    self.assertRaisesRegex(
                        RuntimeTargetHostRunnerError,
                        "ArtifactStore publisher authority changed",
                    ),
                ):
                    run_declared_target_host_campaign(
                        journal=store,
                        evidence_store=evidence_store,
                        spec=_spec(),
                        authority_id=authority.authority_id,
                        research_plan_id=research.plan_id,
                        financial_operations={expected.event_id: financial_operation},
                        research_operations={
                            research.expected_sample_ids[0]: research_operation
                        },
                        inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                        measurement_artifact_id=MEASUREMENT_ARTIFACT_ID,
                        run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                    )
            finally:
                ArtifactStore.publish_bytes = original_publish

            self.assertIsNone(store.get_event(expected.event_id))

    def test_evidence_artifact_ids_must_be_distinct_before_any_work(self):
        with tempfile.TemporaryDirectory() as root:
            store = self._store(root)
            evidence_store = ArtifactStore(Path(root) / "evidence")
            expected, financial, research = self._plans(store)
            authority = self._authority(store, financial)

            def financial_operation() -> None:
                _append_financial(store, expected)

            def research_operation() -> None:
                return None

            with self.assertRaisesRegex(
                RuntimeTargetHostRunnerError,
                "pairwise distinct",
            ):
                run_declared_target_host_campaign(
                    journal=store,
                    evidence_store=evidence_store,
                    spec=_spec(),
                    authority_id=authority.authority_id,
                    research_plan_id=research.plan_id,
                    financial_operations={expected.event_id: financial_operation},
                    research_operations={research.expected_sample_ids[0]: research_operation},
                    inventory_artifact_id=INVENTORY_ARTIFACT_ID,
                    measurement_artifact_id=INVENTORY_ARTIFACT_ID,
                    run_receipt_artifact_id=RUN_RECEIPT_ARTIFACT_ID,
                )
            self.assertEqual(list(evidence_store.manifests.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
