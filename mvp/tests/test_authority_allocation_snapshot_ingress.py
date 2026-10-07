import unittest
from collections.abc import Mapping

from mvp.autotrade_mvp.allocation import AllocationPolicy
from mvp.autotrade_mvp.authority import AllocationAuthoritySnapshot


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


if __name__ == "__main__":
    unittest.main()
