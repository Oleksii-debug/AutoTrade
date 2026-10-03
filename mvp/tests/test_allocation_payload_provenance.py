from collections.abc import Mapping
from types import MappingProxyType
import unittest

from mvp.autotrade_mvp.allocation import ImmutableAllocationEvidence
from mvp.autotrade_mvp.allocation_valuation import (
    AllocationValuationError,
    normalize_allocation_valuation,
)


class _HostileMapping(Mapping):
    def __init__(self):
        self.calls = []

    def __getitem__(self, key):
        self.calls.append(("getitem", key))
        raise AssertionError("hostile mapping callback executed")

    def __iter__(self):
        self.calls.append(("iter", None))
        raise AssertionError("hostile mapping callback executed")

    def __len__(self):
        self.calls.append(("len", None))
        raise AssertionError("hostile mapping callback executed")

    def get(self, key, default=None):
        self.calls.append(("get", key))
        raise AssertionError("hostile mapping callback executed")


class _HostileText(str):
    calls = []

    def __hash__(self):
        type(self).calls.append("hash")
        return str.__hash__(self)

    def __eq__(self, other):
        type(self).calls.append("eq")
        return str.__eq__(self, other)

    def strip(self, *args, **kwargs):
        type(self).calls.append("strip")
        return str.strip(self, *args, **kwargs)


class AllocationPayloadProvenanceTests(unittest.TestCase):
    OBSERVED_AT = "2026-09-25T18:20:00Z"
    VALID_UNTIL = "2026-09-25T18:40:00Z"

    def evidence(self, payload):
        return ImmutableAllocationEvidence.create(
            evidence_id="valuation:aaa:sealed:v1",
            kind="VALUATION",
            environment="SIMULATION",
            schema_version="1.0.0",
            observed_at=self.OBSERVED_AT,
            valid_until=self.VALID_UNTIL,
            payload=payload,
        )

    def test_direct_hostile_mapping_is_rejected_before_callbacks(self):
        hostile = _HostileMapping()

        with self.assertRaisesRegex(TypeError, "unsupported allocation evidence value type"):
            self.evidence(hostile)

        self.assertEqual(hostile.calls, [])

    def test_mappingproxy_over_hostile_mapping_is_rejected_before_callbacks(self):
        hostile = _HostileMapping()
        proxy = MappingProxyType(hostile)
        hostile.calls.clear()

        with self.assertRaisesRegex(TypeError, "lacks sealed canonical provenance"):
            self.evidence(proxy)

        self.assertEqual(hostile.calls, [])

    def test_hostile_string_key_is_rejected_before_key_callbacks(self):
        key = _HostileText("symbol")
        payload = {key: "AAA"}
        _HostileText.calls.clear()

        with self.assertRaisesRegex(TypeError, "keys must be exact strings"):
            self.evidence(payload)

        self.assertEqual(_HostileText.calls, [])

    def test_nested_hostile_mappings_are_rejected_before_callbacks(self):
        for field in ("fx_quote", "cost_rate_components", "cost_evidence_refs"):
            with self.subTest(field=field):
                hostile = _HostileMapping()
                payload = {"symbol": "AAA", field: MappingProxyType(hostile)}
                hostile.calls.clear()

                with self.assertRaisesRegex(TypeError, "lacks sealed canonical provenance"):
                    self.evidence(payload)

                self.assertEqual(hostile.calls, [])

    def test_canonical_frozen_payload_and_nested_proxy_can_be_reused(self):
        payload = {
            "symbol": "AAA",
            "fx_quote": {
                "source_id": "fx:test:v1",
                "max_age_seconds": 60,
            },
        }
        first = self.evidence(payload)

        from_root = self.evidence(first.payload)
        from_nested = self.evidence(
            {
                "symbol": "AAA",
                "fx_quote": first.payload["fx_quote"],
            }
        )

        self.assertEqual(first.digest, from_root.digest)
        self.assertEqual(first.digest, from_nested.digest)
        with self.assertRaises(TypeError):
            first.payload["fx_quote"]["source_id"] = "changed"

    def test_direct_valuation_boundary_rejects_hostile_mapping_without_callbacks(self):
        hostile = _HostileMapping()

        with self.assertRaisesRegex(
            AllocationValuationError,
            "exact built-in dictionary",
        ):
            normalize_allocation_valuation(
                symbol="AAA",
                market_payload=hostile,
                valuation_payload={},
                source_price="10",
                expected_cost_rate="0",
                expected_capital_requirement_rate="1",
                expected_min_notional_base="0",
                expected_fee_floor_base="0",
                expected_max_executable_notional_base="100",
                decision_time="2026-09-25T18:30:00Z",
                portfolio_base_currency="USD",
            )

        self.assertEqual(hostile.calls, [])

    def test_direct_valuation_boundary_rejects_hostile_mappingproxy_without_callbacks(self):
        hostile = _HostileMapping()
        proxy = MappingProxyType(hostile)
        hostile.calls.clear()

        with self.assertRaisesRegex(
            AllocationValuationError,
            "exact built-in dictionary",
        ):
            normalize_allocation_valuation(
                symbol="AAA",
                market_payload=proxy,
                valuation_payload={},
                source_price="10",
                expected_cost_rate="0",
                expected_capital_requirement_rate="1",
                expected_min_notional_base="0",
                expected_fee_floor_base="0",
                expected_max_executable_notional_base="100",
                decision_time="2026-09-25T18:30:00Z",
                portfolio_base_currency="USD",
            )

        self.assertEqual(hostile.calls, [])


if __name__ == "__main__":
    unittest.main()
