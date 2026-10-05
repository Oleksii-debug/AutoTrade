from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import mvp.autotrade_mvp.runtime_load_measurement as measurement_module
from mvp.autotrade_mvp.performance_qualification import RuntimeBudgetSpec
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.runtime_load_evidence import ExpectedJournalEvent
from mvp.autotrade_mvp.runtime_load_measurement import (
    RuntimeLoadMeasurementError,
    measure_declared_financial_operation,
)
from mvp.autotrade_mvp.runtime_load_plan import declare_runtime_event_plan


SHA = "a" * 40
HASH = "sha256:" + ("b" * 64)
HOST = "sha256:" + ("c" * 64)
EVENT_TYPE = "RuntimeQualificationFinancialEvent"


def _spec() -> RuntimeBudgetSpec:
    return RuntimeBudgetSpec(
        scenario_id="latency-schema-authority",
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


def _expected() -> ExpectedJournalEvent:
    return ExpectedJournalEvent(
        event_id="financial-schema-1",
        event_type=EVENT_TYPE,
        aggregate_type="risk_decision",
        aggregate_id="latency-schema-authority",
        aggregate_version=1,
    )


def _append(store: JournalStore, expected: ExpectedJournalEvent) -> None:
    payload = {"kind": "risk_decision", "event_id": expected.event_id}
    store.append_event(
        {
            "event_id": expected.event_id,
            "event_type": expected.event_type,
            "aggregate_type": expected.aggregate_type,
            "aggregate_id": expected.aggregate_id,
            "aggregate_version": str(expected.aggregate_version),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": "2026-10-04T02:30:00Z",
        }
    )


class RuntimeLoadMeasurementSchemaAuthorityTests(unittest.TestCase):
    def _measure(
        self,
        store: JournalStore,
        expected: ExpectedJournalEvent,
        plan_id: str,
        operation,
    ) -> None:
        with patch(
            "mvp.autotrade_mvp.runtime_load_measurement.perf_counter_ns",
            side_effect=(100, 200),
        ):
            measure_declared_financial_operation(
                store,
                _spec(),
                plan_id=plan_id,
                event_id=expected.event_id,
                operation=operation,
            )

    def _fixture(self, root: str) -> tuple[JournalStore, ExpectedJournalEvent, str]:
        store = JournalStore(Path(root) / "latency-schema-authority.sqlite")
        expected = _expected()
        plan_id = declare_runtime_event_plan(
            store,
            plan_id="latency-schema-authority-plan",
            spec=_spec(),
            expected_events=(expected,),
        ).plan_id
        return store, expected, plan_id

    def _measurement_id(self, plan_id: str, event_id: str) -> str:
        return measurement_module._measurement_event_id(plan_id, event_id)

    def test_operation_cannot_execute_hostile_schema_version_comparison(self) -> None:
        class HostileSchemaVersion:
            def __init__(self) -> None:
                self.comparisons = 0

            def __eq__(self, _other: object) -> bool:
                self.comparisons += 1
                raise AssertionError("hostile schema equality executed after callback")

            def __ne__(self, _other: object) -> bool:
                self.comparisons += 1
                raise AssertionError("hostile schema inequality executed after callback")

        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            hostile = HostileSchemaVersion()
            original = JournalStore.SCHEMA_VERSION

            def attack() -> None:
                _append(store, expected)
                JournalStore.SCHEMA_VERSION = hostile

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"JournalStore schema authority changed",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                JournalStore.SCHEMA_VERSION = original

            self.assertEqual(hostile.comparisons, 0)
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_rejects_module_subclass_before_attribute_callback(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            canonical_json = measurement_module.payload_digest.__globals__["canonical_json"]
            json_module = canonical_json.__globals__["json"]
            touched = []

            class HostileModule(ModuleType):
                def __getattribute__(self, name):
                    if name not in {"__class__"}:
                        touched.append(name)
                        raise AssertionError(
                            "hostile module attribute callback executed during verification"
                        )
                    return ModuleType.__getattribute__(self, name)

            def attack() -> None:
                _append(store, expected)
                json_module.__class__ = HostileModule

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"transitive module dependency class changed",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                json_module.__class__ = ModuleType

            self.assertEqual(touched, [])
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_cannot_retarget_json_encoder_below_json_dumps(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            canonical_json = measurement_module.payload_digest.__globals__["canonical_json"]
            json_module = canonical_json.__globals__["json"]
            original_encoder = json_module.JSONEncoder
            touched = False

            class ForgedEncoder:
                def __init__(self, *_args, **_kwargs) -> None:
                    nonlocal touched
                    touched = True
                    raise AssertionError("forged JSONEncoder executed after callback")

            def attack() -> None:
                _append(store, expected)
                json_module.JSONEncoder = ForgedEncoder

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"transitive authority changed.*JSONEncoder|transitive global dependency changed.*JSONEncoder",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                json_module.JSONEncoder = original_encoder

            self.assertFalse(touched)
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_cannot_mutate_json_encoder_encode_code_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            canonical_json = measurement_module.payload_digest.__globals__["canonical_json"]
            json_module = canonical_json.__globals__["json"]
            encode = json_module.JSONEncoder.encode
            original_code = encode.__code__

            def forged_encode(self, _value):
                raise AssertionError("forged JSONEncoder.encode executed after callback")

            def attack() -> None:
                _append(store, expected)
                encode.__code__ = forged_encode.__code__

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"executable authority changed.*json\.JSONEncoder\.encode",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                encode.__code__ = original_code

            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_cannot_retarget_json_iterencode_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            canonical_json = measurement_module.payload_digest.__globals__["canonical_json"]
            json_module = canonical_json.__globals__["json"]
            iterencode = json_module.JSONEncoder.iterencode
            namespace = iterencode.__globals__
            original_make_iterencode = namespace["_make_iterencode"]

            def forged_make_iterencode(*_args, **_kwargs):
                raise AssertionError("forged _make_iterencode executed after callback")

            def attack() -> None:
                _append(store, expected)
                namespace["_make_iterencode"] = forged_make_iterencode

            try:
                with self.assertRaisesRegex(
                    RuntimeLoadMeasurementError,
                    r"transitive global dependency changed.*_make_iterencode",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                namespace["_make_iterencode"] = original_make_iterencode

            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_rejects_expected_event_descriptor_before_dispatch(self) -> None:
        touched: list[str] = []

        class HostileDescriptor:
            def __get__(self, _instance, _owner=None):
                touched.append("payload")
                raise AssertionError("hostile ExpectedJournalEvent descriptor executed")

        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            original = ExpectedJournalEvent.__dict__["payload"]

            def attack() -> None:
                _append(store, expected)
                ExpectedJournalEvent.payload = HostileDescriptor()

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"ExpectedJournalEvent class authority changed.*payload",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                ExpectedJournalEvent.payload = original

            self.assertEqual(touched, [])
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_rejects_plan_digest_descriptor_before_dispatch(self) -> None:
        touched: list[str] = []
        plan_type = measurement_module.DeclaredRuntimeEventPlan

        class HostileDescriptor:
            def __get__(self, _instance, _owner=None):
                touched.append("digest")
                raise AssertionError("hostile DeclaredRuntimeEventPlan descriptor executed")

        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)
            original = plan_type.__dict__["digest"]

            def attack() -> None:
                _append(store, expected)
                plan_type.digest = HostileDescriptor()

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"DeclaredRuntimeEventPlan class authority changed.*digest",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                plan_type.digest = original

            self.assertEqual(touched, [])
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_rejects_latency_sample_class_shape_before_constructor(self) -> None:
        touched: list[str] = []
        sample_type = measurement_module.DurableFinancialLatencySample
        original_init = sample_type.__dict__["__init__"]
        marker = "_hostile_class_shape_marker"

        def hostile_init(self, *_args, **_kwargs):
            touched.append("__init__")

        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)

            def attack() -> None:
                _append(store, expected)
                sample_type.__init__ = hostile_init
                setattr(sample_type, marker, object())

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"DurableFinancialLatencySample class shape changed",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                sample_type.__init__ = original_init
                if marker in sample_type.__dict__:
                    delattr(sample_type, marker)

            # The post-callback class-shape fence must fire before construction.
            # Avoid mutating __new__: deleting a dynamically installed __new__
            # leaves CPython's internal tp_new slot poisoned for later tests.
            self.assertEqual(touched, [])
            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )

    def test_operation_rejects_measurement_error_class_shape_with_builtin_fallback(self) -> None:
        error_type = RuntimeLoadMeasurementError
        had_marker = "_hostile_marker" in error_type.__dict__
        original_marker = error_type.__dict__.get("_hostile_marker")

        with tempfile.TemporaryDirectory() as root:
            store, expected, plan_id = self._fixture(root)

            def attack() -> None:
                _append(store, expected)
                error_type._hostile_marker = object()

            try:
                with self.assertRaisesRegex(
                    ValueError,
                    r"RuntimeLoadMeasurementError class shape changed",
                ):
                    self._measure(store, expected, plan_id, attack)
            finally:
                if had_marker:
                    error_type._hostile_marker = original_marker
                else:
                    del error_type._hostile_marker

            self.assertIsNone(
                store.get_event(self._measurement_id(plan_id, expected.event_id))
            )


if __name__ == "__main__":
    unittest.main()
