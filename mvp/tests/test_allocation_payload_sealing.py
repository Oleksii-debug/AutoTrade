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
        self.calls = 0

    def __getitem__(self, key):
        self.calls += 1
        raise AssertionError("hostile __getitem__ executed")

    def __iter__(self):
        self.calls += 1
        raise AssertionError("hostile __iter__ executed")

    def __len__(self):
        self.calls += 1
        raise AssertionError("hostile __len__ executed")

    def get(self, key, default=None):
        self.calls += 1
        raise AssertionError("hostile get executed")

    def items(self):
        self.calls += 1
        raise AssertionError("hostile items executed")


class _HostileText(str):
    strip_calls = 0

    def strip(self, *args, **kwargs):
        type(self).strip_calls += 1
        raise AssertionError("hostile strip executed")


class AllocationPayloadSealingTests(unittest.TestCase):
    def evidence(self, payload):
        return ImmutableAllocationEvidence.create(
            evidence_id="objective:sealed:v1",
            kind="OBJECTIVE",
            environment="SIMULATION",
            schema_version="1.0.0",
            observed_at="2026-10-06T12:00:00Z",
            valid_until="2026-10-06T13:00:00Z",
            payload=payload,
        )

    def test_canonical_payload_snapshot_is_detached_exact_builtins(self):
        evidence = self.evidence(
            {
                "symbol": "AAA",
                "nested": {"source": "canonical"},
                "values": ["1", "2"],
            }
        )

        snapshot = evidence.canonical_payload_snapshot()

        self.assertIs(type(snapshot), dict)
        self.assertIs(type(snapshot["nested"]), dict)
        self.assertIs(type(snapshot["values"]), list)
        snapshot["nested"]["source"] = "mutated"
        self.assertEqual(
            evidence.canonical_payload_snapshot()["nested"]["source"],
            "canonical",
        )

    def test_direct_hostile_mapping_is_rejected_without_callbacks(self):
        hostile = _HostileMapping()

        with self.assertRaisesRegex(ValueError, "exact dict"):
            self.evidence(hostile)

        self.assertEqual(hostile.calls, 0)

    def test_mappingproxy_over_hostile_mapping_is_rejected_without_callbacks(self):
        hostile = _HostileMapping()
        proxied = MappingProxyType(hostile)

        with self.assertRaisesRegex(ValueError, "exact dict"):
            self.evidence(proxied)

        self.assertEqual(hostile.calls, 0)

    def test_hostile_text_key_is_rejected_before_text_normalization(self):
        _HostileText.strip_calls = 0
        payload = {_HostileText("symbol"): "AAA"}

        with self.assertRaisesRegex(TypeError, "keys must be exact strings"):
            self.evidence(payload)

        self.assertEqual(_HostileText.strip_calls, 0)

    def test_nested_hostile_mapping_is_rejected_without_callbacks(self):
        hostile = _HostileMapping()

        with self.assertRaisesRegex(TypeError, "unsupported allocation evidence value type"):
            self.evidence({"symbol": "AAA", "nested": hostile})

        self.assertEqual(hostile.calls, 0)

    def test_sealed_payload_tamper_fails_digest_revalidation(self):
        evidence = self.evidence({"symbol": "AAA"})
        object.__setattr__(evidence, "_sealed_payload_json", '{"symbol":"BBB"}')

        with self.assertRaisesRegex(ValueError, "no longer matches digest"):
            evidence.canonical_payload_snapshot()

    def test_valuation_rejects_unsealed_mappingproxy_before_mapping_use(self):
        payload = MappingProxyType({"instrument_version": "v1"})

        with self.assertRaisesRegex(
            AllocationValuationError,
            "market payload must be an exact built-in dict",
        ):
            normalize_allocation_valuation(
                symbol="AAA",
                market_payload=payload,
                valuation_payload={},
                source_price="1",
                expected_cost_rate="0",
                expected_capital_requirement_rate="1",
                expected_min_notional_base="0",
                expected_fee_floor_base="0",
                expected_max_executable_notional_base=None,
                decision_time="2026-10-06T12:30:00Z",
                portfolio_base_currency="USD",
            )


if __name__ == "__main__":
    unittest.main()
