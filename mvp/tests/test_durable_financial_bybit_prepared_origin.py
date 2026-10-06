from dataclasses import replace
import inspect
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import bybit_v5 as bybit_module
from mvp.autotrade_mvp import durable_financial_request_binding as binding_module
from mvp.autotrade_mvp.bybit_v5 import (
    BybitPreparedSubmission,
    guarded_order_projection,
    prepare_order_submission,
    require_canonical_bybit_prepared_submission,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
    _BINDING_PAYLOAD_BASE_FIELDS,
    _BINDING_PAYLOAD_PRODUCTION_FIELDS,
    _binding_payload,
    _production_request_origin_receipt,
    _require_bybit_prepared_request_origin,
)
from mvp.autotrade_mvp.persistence import payload_digest
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.tests.test_bybit_v5 import READ_AT, submission_write_capability
from mvp.tests.test_financial_send_authority import (
    D8,
    binding as financial_binding,
)


BOUND_AT = "2026-10-05T08:00:00Z"


def canonical_case():
    capability = submission_write_capability(
        account_id="account-1",
        environment="PAPER",
        instrument_version="BTCUSDT@v1",
        provider_environment="TESTNET",
    )
    prepared = prepare_order_submission(
        capability=capability,
        at=READ_AT,
        provider_environment="TESTNET",
        product_family="LINEAR_DERIVATIVES",
        symbol="BTCUSDT",
        side="BUY",
        order_type="LIMIT",
        quantity="2",
        client_order_id="client-order-1",
        time_in_force="GTC",
        price="30000",
        reduce_only=False,
    )
    request = dict(guarded_order_projection(prepared))
    material = replace(
        financial_binding(),
        capability_snapshot_id=capability.snapshot_id,
        query_sha256=payload_digest({}),
        body_sha256=prepared.body_sha256,
        request_sha256=payload_digest(request),
        trigger_protection_digest=payload_digest({}),
    )
    return material, prepared


class DurableFinancialBybitPreparedOriginTests(unittest.TestCase):
    def test_exact_canonical_prepared_request_mints_origin_receipt(self):
        material, prepared = canonical_case()
        receipt = _require_bybit_prepared_request_origin(material, prepared)
        self.assertEqual(receipt, _production_request_origin_receipt(material))
        self.assertEqual(receipt["schema_version"], "bybit-prepared-origin.v1")
        self.assertEqual(receipt["request_sha256"], material.request_sha256)

    def test_paper_live_bind_cannot_bypass_prepared_origin(self):
        registry = object.__new__(DurableFinancialRequestBindingRegistry)
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "requires exact canonical BybitPreparedSubmission",
        ):
            registry.bind(
                admission_id="admission-1",
                material=financial_binding(),
                bound_at=BOUND_AT,
            )

    def test_other_provider_paper_live_is_fail_closed_until_origin_verifier_exists(self):
        registry = object.__new__(DurableFinancialRequestBindingRegistry)
        material = replace(financial_binding(), provider_id="KRAKEN")
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "no canonical prepared-request origin verifier",
        ):
            registry.bind(
                admission_id="admission-1",
                material=material,
                bound_at=BOUND_AT,
            )

    def test_simulation_does_not_require_or_claim_provider_origin(self):
        material = replace(
            financial_binding(),
            runtime_environment="SIMULATION",
            provider_environment="SIMULATION",
        )
        payload = _binding_payload(
            admission_id="admission-1",
            material=material,
            store_identity_digest=D8,
        )
        self.assertNotIn("provider_request_origin", payload)
        self.assertEqual(
            frozenset(payload),
            _BINDING_PAYLOAD_BASE_FIELDS,
        )

    def test_production_payload_requires_durable_origin_receipt(self):
        material, _prepared = canonical_case()
        payload = _binding_payload(
            admission_id="admission-1",
            material=material,
            store_identity_digest=D8,
        )
        self.assertEqual(
            payload["provider_request_origin"],
            _production_request_origin_receipt(material),
        )
        self.assertEqual(
            frozenset(payload),
            _BINDING_PAYLOAD_PRODUCTION_FIELDS,
        )
        legacy = dict(payload)
        legacy.pop("provider_request_origin")
        self.assertNotEqual(
            legacy,
            _binding_payload(
                admission_id="admission-1",
                material=material,
                store_identity_digest=D8,
            ),
        )

    def test_factory_product_must_be_exact_prepared_type(self):
        material, _prepared = canonical_case()
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "exact canonical BybitPreparedSubmission",
        ):
            _require_bybit_prepared_request_origin(material, object())

    def test_request_digest_drift_is_rejected(self):
        material, prepared = canonical_case()
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "prepared request differs",
        ):
            _require_bybit_prepared_request_origin(
                replace(material, request_sha256=D8),
                prepared,
            )

    def test_body_digest_drift_is_rejected(self):
        material, prepared = canonical_case()
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "prepared body differs",
        ):
            _require_bybit_prepared_request_origin(
                replace(material, body_sha256=D8),
                prepared,
            )

    def test_query_digest_must_be_canonical_empty_query(self):
        material, prepared = canonical_case()
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "canonical empty query",
        ):
            _require_bybit_prepared_request_origin(
                replace(material, query_sha256=D8),
                prepared,
            )

    def test_trigger_protection_close_semantics_are_adapter_derived(self):
        material, prepared = canonical_case()
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "trigger/protection/close semantics differ",
        ):
            _require_bybit_prepared_request_origin(
                replace(material, trigger_protection_digest=D8),
                prepared,
            )

    def test_provider_semantic_axes_cannot_drift_from_prepared_request(self):
        material, prepared = canonical_case()
        cases = {
            "client_order_id": "different-client",
            "side": "SELL",
            "quantity": "3",
            "price": "30001",
            "order_type": "MARKET",
            "time_in_force": "IOC",
            "reduce_only": True,
            "endpoint": "/v5/order/amend",
            "provider_environment": "DEMO",
            "account_id": "other-account",
            "capability_snapshot_id": "other-capability",
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                drifted = replace(material, **{field: value})
                with self.assertRaises(DurableFinancialRequestBindingError):
                    _require_bybit_prepared_request_origin(drifted, prepared)


    def test_exact_type_clone_without_canonical_issuance_is_rejected(self):
        material, prepared = canonical_case()
        forged = object.__new__(BybitPreparedSubmission)
        for name in (
            "endpoint",
            "body",
            "account_id",
            "environment",
            "provider_environment",
            "capability_snapshot_id",
            "entity_id",
            "instrument_version",
            "body_sha256",
        ):
            object.__setattr__(
                forged,
                name,
                object.__getattribute__(prepared, name),
            )
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "lacks canonical issuance provenance",
        ):
            _require_bybit_prepared_request_origin(material, forged)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "prepared submission authority changed",
        ):
            require_canonical_bybit_prepared_submission(forged)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "prepared submission authority changed",
        ):
            guarded_order_projection(forged)

    def test_post_issue_prepared_object_mutation_is_rejected(self):
        material, prepared = canonical_case()
        object.__setattr__(prepared, "endpoint", "/v5/order/amend")
        with self.assertRaisesRegex(
            DurableFinancialRequestBindingError,
            "lacks canonical issuance provenance",
        ):
            _require_bybit_prepared_request_origin(material, prepared)

    def test_provenance_verifier_rebinding_fails_before_forged_verifier_executes(self):
        material, prepared = canonical_case()
        calls = []

        def forged(_prepared):
            calls.append("forged")
            return _prepared

        with patch.object(
            binding_module,
            "require_canonical_bybit_prepared_submission",
            forged,
        ):
            with self.assertRaisesRegex(
                DurableFinancialRequestBindingError,
                "provenance authority changed",
            ):
                _require_bybit_prepared_request_origin(material, prepared)
        self.assertEqual(calls, [])

    def test_preparation_rebinding_of_provider_constants_fails_closed(self):
        material, prepared = canonical_case()
        del material
        del prepared
        cases = (
            ("BYBIT_DOCUMENTED_ENDPOINTS", {"PLACE_ORDER": "/v5/order/amend"}),
            (
                "_REST_BASE_BY_ENVIRONMENT",
                {"TESTNET": "https://attacker.invalid"},
            ),
            (
                "_RUNTIME_ENVIRONMENT_BY_PROVIDER_ENVIRONMENT",
                {"TESTNET": "LIVE"},
            ),
        )
        for name, forged in cases:
            with self.subTest(name=name):
                with patch.object(bybit_module, name, forged):
                    with self.assertRaisesRegex(
                        ProviderCoreError,
                        "prepared submission authority changed|authority is unavailable",
                    ):
                        canonical_case()

    def test_preparation_constructor_code_rebinding_fails_closed(self):
        _material, prepared = canonical_case()
        del prepared

        original = bybit_module.BybitPreparedSubmission.__post_init__
        forged = lambda self: None
        forged.__code__ = (lambda self: None).__code__
        with patch.object(
            bybit_module.BybitPreparedSubmission,
            "__post_init__",
            forged,
        ):
            with self.assertRaisesRegex(
                ProviderCoreError,
                "prepared submission authority changed",
            ):
                canonical_case()
        self.assertIs(
            bybit_module.BybitPreparedSubmission.__post_init__,
            original,
        )

    def test_preparation_constructor_same_function_code_mutation_fails_closed(self):
        _material, prepared = canonical_case()
        del prepared

        constructor = bybit_module.BybitPreparedSubmission.__post_init__
        original_code = constructor.__code__
        forged_code = (lambda self: None).__code__
        try:
            constructor.__code__ = forged_code
            with self.assertRaisesRegex(
                ProviderCoreError,
                "prepared submission authority changed",
            ):
                canonical_case()
        finally:
            constructor.__code__ = original_code

    def test_shared_projection_pins_provenance_verifier(self):
        _material, prepared = canonical_case()
        calls = []

        def forged(_prepared):
            calls.append("forged")
            return _prepared

        with patch.object(
            bybit_module,
            "require_canonical_bybit_prepared_submission",
            forged,
        ):
            with self.assertRaisesRegex(
                ProviderCoreError,
                "verifier authority changed",
            ):
                guarded_order_projection(prepared)
        self.assertEqual(calls, [])

    def test_projection_rebinding_fails_before_forged_projection_executes(self):
        material, prepared = canonical_case()
        calls = []

        def forged(_prepared):
            calls.append("forged")
            return {}

        with patch.object(binding_module, "guarded_order_projection", forged):
            with self.assertRaisesRegex(
                DurableFinancialRequestBindingError,
                "projection authority changed",
            ):
                _require_bybit_prepared_request_origin(material, prepared)
        self.assertEqual(calls, [])

    def test_bind_surface_exposes_only_prepared_object_not_raw_wire_overrides(self):
        parameters = inspect.signature(
            DurableFinancialRequestBindingRegistry.bind
        ).parameters
        self.assertIn("prepared_request", parameters)
        self.assertNotIn("request", parameters)
        self.assertNotIn("body", parameters)
        self.assertNotIn("endpoint", parameters)
        self.assertNotIn("query", parameters)


if __name__ == "__main__":
    unittest.main()
