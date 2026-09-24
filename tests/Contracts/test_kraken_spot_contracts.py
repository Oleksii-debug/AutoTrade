import unittest
from uuid import uuid4

from mvp.autotrade_mvp.kraken_spot import (
    build_spot_order_payload,
    classify_transport_failure,
    parse_submission_response,
)
from mvp.autotrade_mvp.provider_core import PROVIDERS


class KrakenSpotContractTests(unittest.TestCase):
    def test_provider_registry_keeps_spot_and_derivatives_under_kraken_family(self):
        provider = PROVIDERS["KRAKEN"]
        self.assertIn("SPOT", provider.product_families)
        self.assertIn("MARGIN", provider.product_families)
        self.assertIn("DERIVATIVES", provider.product_families)

    def test_submission_shape_preserves_acknowledgement_fill_boundary(self):
        attempt = str(uuid4())
        client = str(uuid4())
        result = parse_submission_response(
            attempt_id=attempt,
            client_order_id=client,
            observed_at="2026-09-24T20:00:00Z",
            response={
                "error": [],
                "result": {"txid": ["O1"], "descr": {"order": "example"}},
            },
        )
        self.assertEqual(result["outcome"], "ACKNOWLEDGED")
        self.assertEqual(result["client_order_id"], client)
        self.assertNotIn("filled_quantity", result)
        self.assertNotIn("fill_price", result)

    def test_possible_send_failure_maps_to_reconcile_first(self):
        result = classify_transport_failure(
            attempt_id=str(uuid4()),
            client_order_id=str(uuid4()),
            send_started=True,
            observed_at="2026-09-24T20:00:01Z",
            reason_code="CONNECTION_LOST",
        )
        self.assertEqual(result["outcome"], "UNKNOWN")
        self.assertEqual(result["retry_disposition"], "RECONCILE_FIRST")

    def test_payload_never_contains_withdrawal_or_transfer_surface(self):
        payload = build_spot_order_payload(
            pair="XBTUSD",
            side="BUY",
            order_type="LIMIT",
            volume="0.1",
            price="60000",
            client_order_id=str(uuid4()),
        )
        forbidden = {"withdraw", "transfer", "key", "secret", "destination"}
        self.assertTrue(forbidden.isdisjoint(payload))


if __name__ == "__main__":
    unittest.main()
