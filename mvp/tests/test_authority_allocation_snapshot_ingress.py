import unittest
from collections.abc import Mapping

from mvp.autotrade_mvp.allocation import AllocationPolicy, ImmutableAllocationEvidence
from mvp.autotrade_mvp.authority import (
    AllocationAuthoritySnapshot,
    InstrumentVersionIdentity,
)


class _CallbackMapping(Mapping):
    def __init__(self):
        self.calls = []

    def __getitem__(self, key):
        self.calls.append(("getitem", key))
        raise AssertionError("authority boundary executed mapping __getitem__")

    def __iter__(self):
        self.calls.append(("iter", None))
        raise AssertionError("authority boundary executed mapping __iter__")

    def __len__(self):
        self.calls.append(("len", None))
        raise AssertionError("authority boundary executed mapping __len__")


class _CallbackDict(dict):
    def items(self):
        raise AssertionError("authority boundary executed dict-subclass items")

    def __iter__(self):
        raise AssertionError("authority boundary executed dict-subclass __iter__")

    def __getitem__(self, key):
        raise AssertionError("authority boundary executed dict-subclass __getitem__")


class _CallbackEvidence(ImmutableAllocationEvidence):
    calls = []

    def __getattribute__(self, name):
        type(self).calls.append(name)
        raise AssertionError(
            f"authority boundary executed evidence subclass attribute {name}"
        )


class _CallbackText(str):
    calls = []

    def strip(self, *args, **kwargs):
        type(self).calls.append("strip")
        raise AssertionError("authority boundary executed string-subclass strip")


class _CallbackInstrumentIdentity(InstrumentVersionIdentity):
    calls = []

    def __getattribute__(self, name):
        type(self).calls.append(name)
        raise AssertionError(
            f"authority boundary executed instrument identity attribute {name}"
        )


class _CallbackInt(int):
    calls = []

    def __lt__(self, other):
        type(self).calls.append(("lt", other))
        raise AssertionError("authority boundary executed int-subclass comparison")


def _policy():
    return AllocationPolicy.create(
        cash_available="1000",
        max_gross_notional="1000",
        max_net_notional="1000",
        max_symbol_notional="1000",
        max_total_cost="50",
        max_stress_loss="500",
        max_turnover_notional="1000",
        max_execution_states=100,
    )


def _snapshot(**overrides):
    values = {
        "resolved_evidence": {},
        "provider_id": "TEST_PROVIDER",
        "account_id": "account-1",
        "policy_version": "policy-v1",
        "allocation_policy": _policy(),
        "max_candidate_sets": 8,
        "instrument_versions": {},
        "financial_instruments": {},
        "capability_snapshot_ids": {},
        "account_snapshot_id": "account-snapshot-1",
        "reconciliation_run_id": "reconciliation-1",
        "account_state_version": 0,
    }
    values.update(overrides)
    return AllocationAuthoritySnapshot(**values)


class AllocationAuthoritySnapshotIngressTests(unittest.TestCase):
    def test_callback_mapping_is_rejected_before_protocol_execution(self):
        for field in (
            "resolved_evidence",
            "instrument_versions",
            "financial_instruments",
            "capability_snapshot_ids",
        ):
            with self.subTest(field=field):
                hostile = _CallbackMapping()
                with self.assertRaisesRegex(TypeError, "exact built-in dict"):
                    _snapshot(**{field: hostile})
                self.assertEqual(hostile.calls, [])

    def test_dict_subclass_is_rejected_before_overridden_methods_execute(self):
        for field in (
            "resolved_evidence",
            "instrument_versions",
            "financial_instruments",
            "capability_snapshot_ids",
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(TypeError, "exact built-in dict"):
                    _snapshot(**{field: _CallbackDict()})

    def test_exact_dict_inputs_are_detached_from_later_caller_mutation(self):
        instrument_versions = {"ABC": "instrument-v1"}
        capability_ids = {"ABC": "capability-v1"}
        snapshot = _snapshot(
            instrument_versions=instrument_versions,
            capability_snapshot_ids=capability_ids,
        )
        instrument_versions["ABC"] = "instrument-v2"
        capability_ids["ABC"] = "capability-v2"
        self.assertEqual(dict(snapshot.instrument_versions), {"ABC": "instrument-v1"})
        self.assertEqual(
            dict(snapshot.capability_snapshot_ids),
            {"ABC": "capability-v1"},
        )


    def test_evidence_subclass_is_rejected_before_attribute_dispatch(self):
        hostile = object.__new__(_CallbackEvidence)
        _CallbackEvidence.calls.clear()

        with self.assertRaisesRegex(TypeError, "exact ImmutableAllocationEvidence"):
            _snapshot(resolved_evidence={"evidence-1": hostile})

        self.assertEqual(_CallbackEvidence.calls, [])

    def test_text_subclasses_are_rejected_before_strip_callbacks(self):
        cases = (
            ("provider_id", _CallbackText("TEST_PROVIDER")),
            (
                "instrument_versions",
                {_CallbackText("ABC"): "instrument-v1"},
            ),
            (
                "instrument_versions",
                {"ABC": _CallbackText("instrument-v1")},
            ),
            (
                "capability_snapshot_ids",
                {"ABC": _CallbackText("capability-v1")},
            ),
        )
        for field, value in cases:
            with self.subTest(field=field):
                _CallbackText.calls.clear()
                with self.assertRaisesRegex(TypeError, "exact text"):
                    _snapshot(**{field: value})
                self.assertEqual(_CallbackText.calls, [])

    def test_instrument_identity_subclass_is_rejected_before_attribute_dispatch(self):
        hostile = object.__new__(_CallbackInstrumentIdentity)
        _CallbackInstrumentIdentity.calls.clear()

        with self.assertRaisesRegex(
            TypeError,
            "exact InstrumentVersionIdentity, tuple or list",
        ):
            _snapshot(financial_instruments={"ABC": hostile})

        self.assertEqual(_CallbackInstrumentIdentity.calls, [])

    def test_financial_instrument_identity_is_detached_from_later_mutation(self):
        instrument_id = "00000000-0000-0000-0000-000000000001"
        source = InstrumentVersionIdentity(instrument_id, 1)
        snapshot = _snapshot(financial_instruments={"ABC": source})
        stored = dict(snapshot.financial_instruments)["ABC"]

        self.assertIsNot(stored, source)
        object.__setattr__(source, "version", 99)
        self.assertEqual(object.__getattribute__(stored, "version"), 1)
        self.assertEqual(
            object.__getattribute__(stored, "instrument_id"),
            instrument_id,
        )

    def test_exact_list_instrument_identity_is_snapshotted_before_later_mutation(self):
        instrument_id = "00000000-0000-0000-0000-000000000002"
        source = [instrument_id, 2]
        snapshot = _snapshot(financial_instruments={"ABC": source})
        source[1] = 99
        stored = dict(snapshot.financial_instruments)["ABC"]

        self.assertEqual(object.__getattribute__(stored, "version"), 2)
        self.assertEqual(
            object.__getattribute__(stored, "instrument_id"),
            instrument_id,
        )


    def test_account_state_version_int_subclass_is_rejected_before_comparison(self):
        hostile = _CallbackInt(0)
        _CallbackInt.calls.clear()

        with self.assertRaisesRegex(ValueError, "non-negative exact integer"):
            _snapshot(account_state_version=hostile)

        self.assertEqual(_CallbackInt.calls, [])


if __name__ == "__main__":
    unittest.main()
