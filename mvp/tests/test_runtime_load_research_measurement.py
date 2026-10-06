from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import runtime_load_research_measurement as research_measurement_module
from mvp.autotrade_mvp.journal_taxonomy import (
    QUALIFICATION_NON_FINANCIAL,
    require_journal_aggregate_descriptor,
)
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import measure_declared_financial_operation
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan
from mvp.autotrade_mvp.runtime_load_research_measurement import (
    ExpectedResearchInterferenceSample,
    RuntimeLoadResearchMeasurementError,
    declare_research_interference_plan,
    evaluate_durable_provider_free_runtime_budget,
    load_declared_research_interference_samples,
    measure_declared_research_interference,
)


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="durable-research-load",
        release_sha=SHA,
        configuration_hash=HASH,
        host_fingerprint=HOST,
        strategy_horizon_us=1_000,
        max_p95_financial_latency_us=500,
        max_financial_staleness_us=500,
        max_research_interference_us=500,
        min_financial_samples=2,
        min_research_samples=2,
    )


def _financial_event(event_id: str, version: int) -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id=event_id,
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="durable-research-load",
        aggregate_version=version,
    )


def _append_financial(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"scenario": "durable-research-load", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-04T05:30:00Z",
        }
    )


class RuntimeLoadResearchMeasurementTests(unittest.TestCase):
    def _store(self, root: str) -> JournalStore:
        return JournalStore(Path(root) / "runtime-load-research-measurement.sqlite")

    def _plans(self, root: str):
        store = self._store(root)
        first = _financial_event("financial-research-1", 1)
        second = _financial_event("financial-research-2", 2)
        financial = declare_runtime_event_plan(
            store,
            plan_id="financial-plan",
            spec=_spec(),
            expected_events=(first, second),
        )
        research = declare_research_interference_plan(
            store,
            _spec(),
            plan_id="research-plan",
            financial_plan_id=financial.plan_id,
            expected_samples=(
                ExpectedResearchInterferenceSample("research-1", "cpu-pressure"),
                ExpectedResearchInterferenceSample("research-2", "model-pressure"),
            ),
        )
        return store, financial, research, first, second

    def test_research_aggregate_families_are_explicit_non_financial_controls(self):
        for aggregate_type in (
            "runtime_qualification_research_plan",
            "runtime_qualification_research_interference",
        ):
            descriptor = require_journal_aggregate_descriptor(aggregate_type)
            self.assertEqual(
                descriptor.qualification_visibility,
                QUALIFICATION_NON_FINANCIAL,
            )
            self.assertFalse(descriptor.is_financial_for_qualification)

    def test_research_plan_and_raw_monotonic_samples_are_durable(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            callback = Mock(return_value="pressure-done")

            with patch(
                "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                side_effect=(1_000_000, 1_125_000),
            ):
                result, sample = measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-1",
                    operation=callback,
                )

            self.assertEqual(result, "pressure-done")
            callback.assert_called_once_with()
            self.assertEqual(sample.phase, "cpu-pressure")
            self.assertEqual(sample.monotonic_start_ns, 1_000_000)
            self.assertEqual(sample.monotonic_end_ns, 1_125_000)
            self.assertEqual(sample.interference_us, 125)
            self.assertGreater(
                sample.measurement_journal_sequence,
                research.declared_journal_sequence,
            )

            with patch(
                "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                side_effect=(2_000_000, 2_250_000),
            ):
                _result, second = measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-2",
                    operation=lambda: None,
                )
            loaded = load_declared_research_interference_samples(
                store,
                _spec(),
                plan_id=research.plan_id,
            )
            self.assertEqual(
                tuple(value.sample_id for value in loaded),
                ("research-1", "research-2"),
            )
            self.assertEqual(
                tuple(value.interference_us for value in loaded),
                (125, 250),
            )
            self.assertEqual(loaded[0].digest, sample.digest)
            self.assertEqual(loaded[1].digest, second.digest)

    def test_undeclared_research_sample_is_rejected_before_callback(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            callback = Mock()
            with self.assertRaisesRegex(
                RuntimeLoadResearchMeasurementError,
                "not present in the durable pre-run research plan",
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-never-declared",
                    operation=callback,
                )
            callback.assert_not_called()

    def test_loader_requires_every_predeclared_research_sample(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            with patch(
                "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                side_effect=(100, 200),
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-1",
                    operation=lambda: None,
                )
            with self.assertRaisesRegex(
                RuntimeLoadResearchMeasurementError,
                "lacks a durable monotonic measurement",
            ):
                load_declared_research_interference_samples(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                )

    def test_research_callback_cannot_mutate_qualification_journal(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)

            def mutate_journal() -> None:
                payload = {"kind": "research-side-effect"}
                store.append_event(
                    {
                        "event_id": "research-side-effect",
                        "event_type": "HostControlObserved",
                        "aggregate_type": "HOST_CONTROL",
                        "aggregate_id": "research-side-effect",
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-10-04T05:31:00Z",
                    }
                )

            with (
                patch(
                    "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                    side_effect=(100, 200),
                ),
                self.assertRaisesRegex(
                    RuntimeLoadResearchMeasurementError,
                    "mutated the qualification JournalStore",
                ),
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-1",
                    operation=mutate_journal,
                )

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationResearchInterferenceMeasured",
                event_types,
            )

    def test_research_callback_cannot_rebind_post_callback_decoder(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            original_decoder = research_measurement_module._decode_sample

            def mutate_decoder() -> None:
                research_measurement_module._decode_sample = lambda **_kwargs: None

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "authority changed during callback: _decode_sample",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=mutate_decoder,
                    )
            finally:
                research_measurement_module._decode_sample = original_decoder

    def test_research_callback_cannot_mutate_decoder_executable_in_place(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            decoder = research_measurement_module._decode_sample
            original_code = decoder.__code__

            def mutate_decoder_code() -> None:
                decoder.__code__ = (lambda **_kwargs: None).__code__

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "executable authority changed during callback: _decode_sample",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=mutate_decoder_code,
                    )
            finally:
                decoder.__code__ = original_code

    def test_research_callback_cannot_rebind_digest_transitive_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            digest_namespace = research_measurement_module.payload_digest.__globals__
            original_canonical_json = digest_namespace["canonical_json"]

            def mutate_digest_dependency() -> None:
                digest_namespace["canonical_json"] = lambda _value: "{}"

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "transitive authority changed during callback: payload_digest.canonical_json",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=mutate_digest_dependency,
                    )
            finally:
                digest_namespace["canonical_json"] = original_canonical_json

    def test_research_callback_cannot_mutate_json_encoder_executable_in_place(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            canonical_json = research_measurement_module.payload_digest.__globals__[
                "canonical_json"
            ]
            json_module = canonical_json.__globals__["json"]
            encoder = json_module.JSONEncoder
            original_code = encoder.encode.__code__

            def mutate_encoder_code() -> None:
                encoder.encode.__code__ = (lambda self, value: "{}").__code__

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "class executable authority changed during callback: json.JSONEncoder.encode",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=mutate_encoder_code,
                    )
            finally:
                encoder.encode.__code__ = original_code

            event_types = tuple(
                event["event_type"]
                for event in store.load_events_after_journal_sequence(0)
            )
            self.assertNotIn(
                "RuntimeQualificationResearchInterferenceMeasured",
                event_types,
            )

    def test_research_callback_cannot_rebind_json_encoder_iterencode_dependency(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            canonical_json = research_measurement_module.payload_digest.__globals__[
                "canonical_json"
            ]
            json_module = canonical_json.__globals__["json"]
            iterencode_globals = json_module.JSONEncoder.iterencode.__globals__
            original_factory = iterencode_globals["_make_iterencode"]

            def mutate_iterencode_dependency() -> None:
                iterencode_globals["_make_iterencode"] = lambda *_args, **_kwargs: None

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "transitive global authority changed during callback: _make_iterencode",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=mutate_iterencode_dependency,
                    )
            finally:
                iterencode_globals["_make_iterencode"] = original_factory

    def test_research_callback_cannot_retarget_store_instance(self):
        with tempfile.TemporaryDirectory() as root:
            store, _financial, research, _first, _second = self._plans(root)
            original_path = store.path

            def retarget_store() -> None:
                store.path = Path(root) / "alternate-runtime-load.sqlite"

            try:
                with (
                    patch(
                        "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                        side_effect=(100, 200),
                    ),
                    self.assertRaisesRegex(
                        RuntimeLoadResearchMeasurementError,
                        "JournalStore instance state changed during callback: path",
                    ),
                ):
                    measure_declared_research_interference(
                        store,
                        _spec(),
                        plan_id=research.plan_id,
                        sample_id="research-1",
                        operation=retarget_store,
                    )
            finally:
                store.path = original_path

    def test_provider_free_evaluator_uses_only_durable_metric_series_and_stays_inconclusive(self):
        with tempfile.TemporaryDirectory() as root:
            store, financial, research, first, second = self._plans(root)

            with patch(
                "mvp.autotrade_mvp.runtime_load_research_measurement.perf_counter_ns",
                side_effect=(1_000_000, 1_050_000, 2_000_000, 2_075_000),
            ):
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-1",
                    operation=lambda: None,
                )
                measure_declared_research_interference(
                    store,
                    _spec(),
                    plan_id=research.plan_id,
                    sample_id="research-2",
                    operation=lambda: None,
                )

            with patch(
                "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
                side_effect=(3_000_000, 3_100_000, 4_000_000, 4_200_000),
            ):
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=financial.plan_id,
                    event_id=first.event_id,
                    operation=lambda: _append_financial(store, first),
                )
                measure_declared_financial_operation(
                    store,
                    _spec(),
                    plan_id=financial.plan_id,
                    event_id=second.event_id,
                    operation=lambda: _append_financial(store, second),
                )

            decision, evidence, loaded_plan, financial_samples, research_samples = (
                evaluate_durable_provider_free_runtime_budget(
                    _spec(),
                    store,
                    financial_plan_id=financial.plan_id,
                    research_plan_id=research.plan_id,
                )
            )
            self.assertEqual(decision.status, "INCONCLUSIVE")
            self.assertEqual(
                decision.reasons,
                ("unverified_runtime_measurement_provenance",),
            )
            self.assertEqual(
                tuple(value.latency_us for value in financial_samples),
                (100, 200),
            )
            self.assertEqual(
                tuple(value.interference_us for value in research_samples),
                (50, 75),
            )
            self.assertEqual(evidence.missing_event_ids, ())
            self.assertEqual(loaded_plan.digest, financial.digest)


if __name__ == "__main__":
    unittest.main()
