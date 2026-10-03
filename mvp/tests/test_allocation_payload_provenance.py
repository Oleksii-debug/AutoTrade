from collections.abc import Mapping
from types import MappingProxyType
import unittest

import mvp.autotrade_mvp.allocation as allocation_module
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

    @staticmethod
    def market():
        return {
            "instrument_version": "instrument:test:v1",
            "capability_snapshot_id": "capability:test:v1",
            "asset_class": "CASH_EQUITY",
            "payoff": "LINEAR",
            "quantity_unit": "SHARE",
            "contract_multiplier": "1",
            "quote_currency": "USD",
            "settlement_currency": "USD",
        }

    @staticmethod
    def valuation():
        return {
            "symbol": "AAA",
            "instrument_version": "instrument:test:v1",
            "capability_snapshot_id": "capability:test:v1",
            "asset_class": "CASH_EQUITY",
            "payoff": "LINEAR",
            "quantity_unit": "SHARE",
            "contract_multiplier": "1",
            "quote_currency": "USD",
            "settlement_currency": "USD",
            "source_price": "10",
            "portfolio_base_currency": "USD",
            "fx_rate": "1",
            "fx_source_id": "IDENTITY",
            "unit_base_notional": "10",
            "capital_requirement_rate": "1",
            "min_notional_base": "0",
            "fee_floor_base": "0",
            "max_executable_notional_base": "100",
            "payoff_identity": "cash_equity:linear:v1",
            "cost_rate_components": {
                "execution": "0.001",
                "financing": "0",
                "funding": "0",
                "borrow": "0",
                "fx": "0",
            },
            "cost_evidence_refs": {
                "execution": "execution:test:v1",
                "financing": "financing:none:test:v1",
                "funding": "funding:none:test:v1",
                "borrow": "borrow:none:test:v1",
                "fx": "fx:identity:test:v1",
            },
        }

    def normalize(self, market=None, valuation=None):
        return normalize_allocation_valuation(
            symbol="AAA",
            market_payload=self.market() if market is None else market,
            valuation_payload=self.valuation() if valuation is None else valuation,
            source_price="10",
            expected_cost_rate="0.001",
            expected_capital_requirement_rate="1",
            expected_min_notional_base="0",
            expected_fee_floor_base="0",
            expected_max_executable_notional_base="100",
            decision_time="2026-09-25T18:30:00Z",
            portfolio_base_currency="USD",
        )

    def test_direct_hostile_mapping_is_rejected_before_callbacks(self):
        hostile = _HostileMapping()

        with self.assertRaisesRegex(TypeError, "unsupported allocation evidence value type"):
            self.evidence(hostile)

        self.assertEqual(hostile.calls, [])

    def test_module_globals_cannot_mint_foreign_payload_seals(self):
        self.assertFalse(hasattr(allocation_module, "_SEALED_ALLOCATION_PAYLOADS"))
        self.assertFalse(hasattr(allocation_module, "_register_allocation_payload"))
        self.assertFalse(hasattr(allocation_module, "_SealedAllocationPayload"))

        proxy = MappingProxyType({"symbol": "AAA"})
        with self.assertRaisesRegex(TypeError, "lacks sealed canonical provenance"):
            self.evidence(proxy)

    def test_lookup_global_rebinding_cannot_admit_foreign_proxy(self):
        original = allocation_module._registered_allocation_payload
        proxy = MappingProxyType({"symbol": "AAA"})

        class ForgedOwner:
            canonical_json = '{"symbol":"AAA"}'

        allocation_module._registered_allocation_payload = lambda value: ForgedOwner()
        try:
            with self.assertRaisesRegex(TypeError, "lacks sealed canonical provenance"):
                self.evidence(proxy)
        finally:
            allocation_module._registered_allocation_payload = original

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
        with self.assertRaisesRegex(AttributeError, "read-only"):
            first._payload_owners[-1].canonical_json = "{}"

    def test_direct_valuation_rejects_hostile_outer_key_before_callbacks(self):
        key = _HostileText("instrument_version")
        market = self.market()
        market[key] = market.pop("instrument_version")
        _HostileText.calls.clear()

        with self.assertRaisesRegex(
            AllocationValuationError,
            "keys must be exact built-in strings",
        ):
            self.normalize(market=market)

        self.assertEqual(_HostileText.calls, [])

    def test_identity_fx_rejects_nested_hostile_mapping_before_callbacks(self):
        hostile = _HostileMapping()
        valuation = self.valuation()
        valuation["fx_quote"] = MappingProxyType(hostile)
        hostile.calls.clear()

        with self.assertRaisesRegex(
            AllocationValuationError,
            "fx_quote must be an exact built-in dictionary",
        ):
            self.normalize(valuation=valuation)

        self.assertEqual(hostile.calls, [])

    def test_cost_mapping_rejects_hostile_key_before_callbacks(self):
        key = _HostileText("execution")
        valuation = self.valuation()
        components = dict(valuation["cost_rate_components"])
        components[key] = components.pop("execution")
        valuation["cost_rate_components"] = components
        _HostileText.calls.clear()

        with self.assertRaisesRegex(
            AllocationValuationError,
            "cost_rate_components keys must be exact built-in strings",
        ):
            self.normalize(valuation=valuation)

        self.assertEqual(_HostileText.calls, [])

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
