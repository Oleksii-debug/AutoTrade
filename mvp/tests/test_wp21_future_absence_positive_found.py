import unittest
from uuid import uuid4

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
            pagination_complete=True,
            now="2026-09-27T03:00:00Z",
        )

        self.assertEqual(result["verdict"], "FOUND")
        self.assertEqual(result["order"]["client_order_id"], "known-order")
        self.assertEqual(result["order"]["status"], "WORKING")
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


if __name__ == "__main__":
    unittest.main()
