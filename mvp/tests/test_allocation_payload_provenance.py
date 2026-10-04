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

    def test_resolver_rejects_tampered_payload_before_callbacks(self):
        evidence = self.evidence({"symbol": "AAA"})
        hostile = _HostileMapping()
        object.__setattr__(evidence, "payload", MappingProxyType(hostile))
        hostile.calls.clear()

        with self.assertRaisesRegex(ValueError, "payload provenance is not sealed"):
            allocation_module._resolve_allocation_evidence(
                evidence,
                {evidence.evidence_id: evidence},
                expected_kind="VALUATION",
                expected_environment="SIMULATION",
                at="2026-09-25T18:30:00Z",
            )

        self.assertEqual(hostile.calls, [])

    def test_sealed_payload_canonical_source_is_closure_private(self):
        evidence = self.evidence({"symbol": "AAA"})
        owner = evidence._payload_owners[-1]

        with self.assertRaises(AttributeError):
            object.__setattr__(owner, "_canonical_json", '{"symbol":"BBB"}')

        resolved = allocation_module._resolve_allocation_evidence(
            evidence,
            {evidence.evidence_id: evidence},
            expected_kind="VALUATION",
            expected_environment="SIMULATION",
            at="2026-09-25T18:30:00Z",
        )
        self.assertIs(resolved, evidence)
        self.assertEqual(
            allocation_module._allocation_payload_snapshot(evidence),
            {"symbol": "AAA"},
        )

    def test_reachable_owner_class_cannot_inject_provenance_lookup_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        owner = evidence._payload_owners[-1]
        owner_type = type(owner)
        touched = []

        def hostile_proxy(_self):
            touched.append("proxy")
            raise AssertionError("reachable owner descriptor executed")

        owner_type.proxy = property(hostile_proxy)
        try:
            resolved = allocation_module._resolve_allocation_evidence(
                evidence,
                {evidence.evidence_id: evidence},
                expected_kind="VALUATION",
                expected_environment="SIMULATION",
                at="2026-09-25T18:30:00Z",
            )
            snapshot = allocation_module._allocation_payload_snapshot(evidence)
        finally:
            del owner_type.proxy

        self.assertIs(resolved, evidence)
        self.assertEqual(snapshot, {"symbol": "AAA"})
        self.assertEqual(touched, [])

    def test_tampered_owner_collection_fails_before_iterable_callbacks(self):
        evidence = self.evidence({"symbol": "AAA"})
        hostile = _HostileMapping()
        object.__setattr__(evidence, "_payload_owners", hostile)
        hostile.calls.clear()

        with self.assertRaisesRegex(ValueError, "payload provenance is not sealed"):
            allocation_module._resolve_allocation_evidence(
                evidence,
                {evidence.evidence_id: evidence},
                expected_kind="VALUATION",
                expected_environment="SIMULATION",
                at="2026-09-25T18:30:00Z",
            )

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


    def test_post_issuance_helper_rebinding_cannot_retarget_sealed_snapshot(self):
        evidence = self.evidence({"symbol": "AAA"})
        original_json = allocation_module.json
        original_canonicalize = allocation_module._canonical_evidence_value
        original_canonical_json = allocation_module._canonical_evidence_json
        original_digest = allocation_module._allocation_evidence_digest
        original_sha256 = allocation_module.sha256

        class ForgedHash:
            def hexdigest(self):
                return evidence.digest

        class ForgedJson:
            @staticmethod
            def loads(_value):
                return {"symbol": "FORGED"}

        allocation_module.json = ForgedJson
        allocation_module._canonical_evidence_value = (
            lambda _value: {"symbol": "FORGED"}
        )
        allocation_module._canonical_evidence_json = (
            lambda _value: '{"symbol":"FORGED"}'
        )
        allocation_module._allocation_evidence_digest = (
            lambda **_kwargs: evidence.digest
        )
        allocation_module.sha256 = lambda _value: ForgedHash()
        try:
            self.assertEqual(
                allocation_module._allocation_payload_snapshot(evidence),
                {"symbol": "AAA"},
            )
            self.assertEqual(
                original_canonicalize(evidence.payload),
                {"symbol": "AAA"},
            )
        finally:
            allocation_module.json = original_json
            allocation_module._canonical_evidence_value = original_canonicalize
            allocation_module._canonical_evidence_json = original_canonical_json
            allocation_module._allocation_evidence_digest = original_digest
            allocation_module.sha256 = original_sha256


    def test_sealer_ignores_post_import_mappingproxy_and_weakref_rebinding(self):
        original_mapping_proxy = allocation_module.MappingProxyType
        original_weakref = allocation_module.weakref
        touched = []

        class ForgedWeakref:
            @staticmethod
            def ref(*args, **kwargs):
                touched.append("weakref")
                raise AssertionError("forged weakref authority executed")

        def forged_mapping_proxy(*args, **kwargs):
            touched.append("mappingproxy")
            raise AssertionError("forged mappingproxy authority executed")

        allocation_module.MappingProxyType = forged_mapping_proxy
        allocation_module.weakref = ForgedWeakref
        try:
            evidence = self.evidence({"symbol": "AAA"})
            self.assertEqual(
                allocation_module._allocation_payload_snapshot(evidence),
                {"symbol": "AAA"},
            )
        finally:
            allocation_module.MappingProxyType = original_mapping_proxy
            allocation_module.weakref = original_weakref

        self.assertEqual(touched, [])


    def test_json_encoder_global_rebind_is_not_serialization_authority(self):
        evidence = self.evidence({"symbol": "AAA"})
        dumps_globals = allocation_module.json.dumps.__globals__
        original_encoder = dumps_globals["JSONEncoder"]
        touched = []

        class ForgedEncoder:
            def __init__(self, *args, **kwargs):
                touched.append("init")
                raise AssertionError("forged JSON encoder executed")

        dumps_globals["JSONEncoder"] = ForgedEncoder
        try:
            self.assertEqual(
                allocation_module._allocation_payload_snapshot(evidence),
                {"symbol": "AAA"},
            )
        finally:
            dumps_globals["JSONEncoder"] = original_encoder

        self.assertEqual(touched, [])

    def test_json_encoder_constructor_retarget_fails_before_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        encoder = allocation_module.json.JSONEncoder
        encoder_dict = type.__getattribute__(encoder, "__dict__")
        self.assertNotIn("__new__", encoder_dict)
        touched = []

        def forged_new(cls, *args, **kwargs):
            touched.append("new")
            raise AssertionError("forged JSON encoder constructor executed")

        encoder.__new__ = staticmethod(forged_new)
        try:
            with self.assertRaisesRegex(
                ValueError,
                "serializer authority changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            del encoder.__new__

        self.assertEqual(touched, [])


    def test_json_encoder_helper_retarget_fails_before_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        encoder_globals = allocation_module.json.JSONEncoder.iterencode.__globals__
        original_helper = encoder_globals["_make_iterencode"]
        touched = []

        def forged_helper(*args, **kwargs):
            touched.append("helper")
            raise AssertionError("forged JSON helper executed")

        encoder_globals["_make_iterencode"] = forged_helper
        try:
            with self.assertRaisesRegex(
                ValueError,
                "serializer authority changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            encoder_globals["_make_iterencode"] = original_helper

        self.assertEqual(touched, [])

    def test_json_default_decoder_global_rebind_is_not_decode_authority(self):
        evidence = self.evidence({"symbol": "AAA"})
        loads_globals = allocation_module.json.loads.__globals__
        original_decoder = loads_globals["_default_decoder"]
        touched = []

        class ForgedDecoder:
            def decode(self, _value):
                touched.append("decode")
                raise AssertionError("forged JSON decoder executed")

        loads_globals["_default_decoder"] = ForgedDecoder()
        try:
            self.assertEqual(
                allocation_module._allocation_payload_snapshot(evidence),
                {"symbol": "AAA"},
            )
        finally:
            loads_globals["_default_decoder"] = original_decoder

        self.assertEqual(touched, [])

    def test_json_decoder_scanner_retarget_fails_before_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        decoder = allocation_module.json.loads.__globals__["_default_decoder"]
        original_scan_once = decoder.scan_once
        touched = []

        def forged_scan_once(*args, **kwargs):
            touched.append("scan_once")
            raise AssertionError("forged JSON scanner executed")

        decoder.scan_once = forged_scan_once
        try:
            with self.assertRaisesRegex(
                ValueError,
                "decoder authority changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            decoder.scan_once = original_scan_once

        self.assertEqual(touched, [])

    def test_json_infinity_retarget_fails_before_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        encoder_globals = allocation_module.json.JSONEncoder.iterencode.__globals__
        original_infinity = encoder_globals["INFINITY"]
        touched = []

        class ForgedInfinity:
            def __neg__(self):
                touched.append("neg")
                raise AssertionError("forged JSON infinity executed")

        encoder_globals["INFINITY"] = ForgedInfinity()
        try:
            with self.assertRaisesRegex(
                ValueError,
                "serializer authority changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            encoder_globals["INFINITY"] = original_infinity

        self.assertEqual(touched, [])


    def test_json_c_encoder_retarget_fails_before_callback(self):
        evidence = self.evidence({"symbol": "AAA"})
        encoder_globals = allocation_module.json.JSONEncoder.iterencode.__globals__
        original_encoder = encoder_globals["c_make_encoder"]
        touched = []

        def forged_encoder(*args, **kwargs):
            touched.append("c_make_encoder")
            raise AssertionError("forged C encoder shim executed")

        encoder_globals["c_make_encoder"] = forged_encoder
        try:
            with self.assertRaisesRegex(
                ValueError,
                "serializer authority changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            encoder_globals["c_make_encoder"] = original_encoder

        self.assertEqual(touched, [])


    def test_same_function_digest_code_mutation_fails_before_execution(self):
        evidence = self.evidence({"symbol": "AAA"})
        helper = allocation_module._allocation_evidence_digest
        original_code = helper.__code__

        def forged_digest(**_kwargs):
            raise AssertionError("forged digest helper executed")

        try:
            helper.__code__ = forged_digest.__code__
            with self.assertRaisesRegex(
                ValueError,
                "trust helper executable changed after binding",
            ):
                allocation_module._allocation_payload_snapshot(evidence)
        finally:
            helper.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
