import unittest
from uuid import uuid4

from mvp.autotrade_mvp.simulated_contract_harness import SimulatedProviderContractHarness
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider


class FutureAbsencePositiveObservationTests(unittest.TestCase):
    def test_existing_order_is_found_even_when_absence_window_ends_in_future(self):
        provider = SimulatedProvider()
        provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="known-order",
            instrument_version="ABC@1",
            side="BUY",
            quantity="2",
            price="100",
            now="2026-09-27T02:00:00Z",
            fill_immediately=False,
        )

        result = provider.query_order(
            client_order_id="known-order",
            coverage_start="2026-09-27T01:00:00Z",
            coverage_end="2026-09-27T04:00:00Z",
            pagination_complete=False,
            now="2026-09-27T03:00:00Z",
        )

        self.assertEqual(result["verdict"], "FOUND")
        self.assertEqual(result["order"]["client_order_id"], "known-order")
        self.assertEqual(result["order"]["status"], "WORKING")
        self.assertEqual(result["consistency_horizon"], "2026-09-27T03:00:00Z")
        self.assertNotIn("reason_codes", result)

    def test_missing_order_future_window_remains_inconclusive(self):
        provider = SimulatedProvider()

        result = provider.query_order(
            client_order_id="missing-order",
            coverage_start="2026-09-27T01:00:00Z",
            coverage_end="2026-09-27T04:00:00Z",
            pagination_complete=True,
            now="2026-09-27T03:00:00Z",
        )

        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertEqual(result["reason_codes"], ["coverage_end_after_query_time"])
        self.assertEqual(result["consistency_horizon"], "2026-09-27T03:00:00Z")
        self.assertNotIn("order", result)

    def test_incomplete_pagination_still_blocks_absence_proof(self):
        provider = SimulatedProvider()

        result = provider.query_order(
            client_order_id="missing-order",
            coverage_start="2026-09-27T01:00:00Z",
            coverage_end="2026-09-27T02:30:00Z",
            pagination_complete=False,
            now="2026-09-27T03:00:00Z",
        )

        self.assertEqual(result["verdict"], "INCONCLUSIVE")
        self.assertNotIn("order", result)

    def test_contract_harness_preserves_positive_observation_without_future_horizon(self):
        harness = SimulatedProviderContractHarness()
        harness.transport_send(
            "known-order",
            {
                "attempt_id": str(uuid4()),
                "instrument_version": "ABC@1",
                "side": "BUY",
                "quantity": "2",
                "price": "100",
                "now": "2026-09-27T02:00:00Z",
                "fill_immediately": False,
            },
            lambda: None,
        )
        window = {
            "coverage_start": "2026-09-27T01:00:00Z",
            "coverage_end": "2026-09-27T04:00:00Z",
            "pagination_complete": False,
            "now": "2026-09-27T03:00:00Z",
        }
        found = harness.query_order(client_order_id="known-order", **window)
        self.assertEqual(found["verdict"], "FOUND")
        self.assertEqual(found["order"]["status"], "WORKING")
        self.assertEqual(found["consistency_horizon"], window["now"])

        missing = harness.query_order(client_order_id="missing-order", **window)
        self.assertEqual(missing["verdict"], "INCONCLUSIVE")
        self.assertEqual(missing["reason_codes"], ["coverage_end_after_query_time"])
        self.assertEqual(missing["consistency_horizon"], window["now"])

        outside = harness.query_order(
            client_order_id="known-order",
            coverage_start="2026-09-27T00:00:00Z",
            coverage_end="2026-09-27T01:00:00Z",
            pagination_complete=True,
            now=window["now"],
        )
        self.assertEqual(outside["verdict"], "INCONCLUSIVE")
        self.assertEqual(outside["reason_codes"], ["submission_outside_coverage"])
        self.assertEqual(outside["consistency_horizon"], "2026-09-27T01:00:00Z")
        future_outside = harness.query_order(
            client_order_id="known-order",
            coverage_start="2026-09-27T03:30:00Z",
            coverage_end=window["coverage_end"],
            pagination_complete=True,
            now=window["now"],
        )
        self.assertEqual(future_outside["verdict"], "INCONCLUSIVE")
        self.assertEqual(future_outside["reason_codes"], ["submission_outside_coverage"])
        self.assertEqual(future_outside["consistency_horizon"], window["now"])

        past = {**window, "coverage_end": "2026-09-27T02:30:00Z"}
        self.assertEqual(
            harness.query_order(client_order_id="missing-order", **past)["verdict"],
            "INCONCLUSIVE",
        )
        self.assertEqual(
            harness.query_order(
                client_order_id="missing-order",
                **{**past, "pagination_complete": True},
            )["verdict"],
            "PROVEN_ABSENT",
        )


if __name__ == "__main__":
    unittest.main()
