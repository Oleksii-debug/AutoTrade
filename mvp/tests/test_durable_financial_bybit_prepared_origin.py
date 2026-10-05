from dataclasses import replace
import inspect
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import durable_financial_request_binding as binding_module
from mvp.autotrade_mvp.bybit_v5 import (
    guarded_order_projection,
    prepare_order_submission,
)
from mvp.autotrade_mvp.durable_financial_request_binding import (
    DurableFinancialRequestBindingError,
    DurableFinancialRequestBindingRegistry,
    _binding_payload,
    _production_request_origin_receipt,
    _require_bybit_prepared_request_origin,
)
from mvp.autotrade_mvp.persistence import payload_digest
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
