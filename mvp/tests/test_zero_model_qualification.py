from decimal import Decimal
import unittest

from qualification.zero_model.qualify import qualify


class ZeroModelQualificationTests(unittest.TestCase):
    def test_zero_model_slice_is_replayable_reconciled_and_cost_free(self):
        evidence = qualify("a" * 40)

        self.assertEqual(evidence["qualification"], "WP-62_ZERO_MODEL_FOUNDATION")
        self.assertEqual(evidence["source_sha"], "a" * 40)
        route = evidence["model_route"]
        self.assertEqual(route["status"], "NO_MODEL")
        self.assertIsNone(route["model_id"])
        self.assertIsNone(route["provider_id"])
        self.assertEqual(route["reserved_cost"], "0")
        self.assertFalse(route["model_inventory_touched"])

        outages = evidence["outage_routes"]
        self.assertEqual(set(outages), {"remote_outage", "local_resource_exhaustion"})
        for outage in outages.values():
            self.assertEqual(outage["status"], "NO_MODEL")
            self.assertIsNone(outage["model_id"])
            self.assertIsNone(outage["provider_id"])
            self.assertEqual(outage["reserved_cost"], "0")
            self.assertEqual(outage["reason"], "no_admissible_model")
        self.assertEqual(evidence["model_cost_total"], "0")

        financial = evidence["deterministic_financial_slice"]
        self.assertTrue(financial["resumed"])
        self.assertTrue(financial["same_order_identity"])
        self.assertTrue(financial["same_fill_identity"])
        self.assertTrue(financial["reconciled"])
        self.assertTrue(financial["replay_verified"])

        economics = evidence["economics"]
        self.assertEqual(economics["economic_edge_claim"], "UNPROVEN_SIMULATION_ONLY")
        self.assertEqual(economics["evidence_count"], 1)
        self.assertEqual(economics["trade_count"], 1)
        self.assertGreaterEqual(Decimal(economics["total_fees"]), Decimal("0"))

        campaign = evidence["multi_episode_economics"]
        self.assertEqual(campaign["episode_statuses"], ["filled", "filled"])
        self.assertEqual(campaign["replay_statuses"], ["filled", "filled"])
        self.assertTrue(campaign["restart_resumed"])
        self.assertTrue(campaign["same_order_identities"])
        self.assertTrue(campaign["same_fill_identities"])
        self.assertTrue(campaign["reconciled"])
        campaign_economics = campaign["economics"]
        self.assertEqual(campaign_economics["trade_count"], 2)
        self.assertEqual(campaign_economics["evidence_count"], 2)
        self.assertEqual(campaign_economics["ending_position"], "0")
        self.assertEqual(
            campaign_economics["economic_edge_claim"],
            "UNPROVEN_SIMULATION_ONLY",
        )
        self.assertGreater(Decimal(campaign_economics["total_fees"]), Decimal("0"))
        self.assertLess(Decimal(campaign_economics["net_pnl"]), Decimal("0"))

        small = evidence["small_capital"]
        self.assertEqual(small["status"], "risk_rejected")
        self.assertTrue(small["resumed"])
        self.assertIsNone(small["order_id"])
        self.assertIsNone(small["fill_id"])
        self.assertTrue(small["reconciled"])
        self.assertTrue(small["replay_verified"])
        self.assertEqual(small["economics"]["trade_count"], 0)
        self.assertEqual(small["economics"]["net_pnl"], "0E-8")

        claims = evidence["claims"]
        self.assertFalse(claims["network_or_model_call_performed"])
        self.assertFalse(claims["live_trading_qualified"])
        self.assertFalse(claims["economic_edge_proven"])
        self.assertFalse(claims["all_wp62_workflows_qualified"])

    def test_source_sha_is_exact_not_a_label_or_prefix(self):
        for invalid in ("abc", "g" * 40, "a" * 39, "a" * 41, "A" * 40, " " + "a" * 40, "a" * 40 + " "):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    qualify(invalid)


if __name__ == "__main__":
    unittest.main()
