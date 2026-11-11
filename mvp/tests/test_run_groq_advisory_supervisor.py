from __future__ import annotations

import unittest

from tools.run_groq_advisory_supervisor import project_learning_checkpoint, run


class GroqAdvisorySupervisorTests(unittest.TestCase):
    def test_projection_is_bounded_and_has_no_execution_authority(self):
        checkpoint = {
            "schema": "autotrade-whitebit-live-zero-learning-v1",
            "mode": "ZERO",
            "episode_count": 50,
            "invalid_episode_count": 1,
            "model_version_count": 2,
            "public_data_only": True,
            "network_orders": False,
            "trading_authority": False,
            "unexpected": "must not cross model boundary",
            "learning_cycle": {
                "market": "BTC_USDT", "cycle": 3, "phase": "RESEARCH",
                "analysis": {"episodes": 40, "net_usdt": "-1", "extra": "drop"},
                "evaluation": {"count": 10, "profitability_proven": False},
                "config": {"fee_bps": "10", "notional": "100"},
            },
        }
        projected = project_learning_checkpoint(checkpoint)
        self.assertEqual(projected["market"], "BTC_USDT")
        self.assertFalse(projected["constraints"]["network_orders"])
        self.assertFalse(projected["constraints"]["trading_authority"])
        self.assertNotIn("unexpected", repr(projected))
        self.assertNotIn("extra", repr(projected))
        self.assertEqual(
            projected["accounting_contract"]["net_identity"],
            "net_usdt = gross_usdt - fees_usdt",
        )
        self.assertFalse(
            projected["accounting_contract"]["subtract_slippage_cost_again"]
        )
        self.assertFalse(
            projected["causal_split_contract"][
                "prospective_evaluation_used_for_training"
            ]
        )

    def test_nonzero_or_authorized_checkpoint_is_rejected(self):
        base = {
            "schema": "autotrade-whitebit-live-zero-learning-v1",
            "mode": "ZERO", "network_orders": False, "trading_authority": False,
            "learning_cycle": {"market": "BTC_USDT"},
        }
        for changed in (
            {**base, "mode": "RUNNING"},
            {**base, "network_orders": True},
            {**base, "trading_authority": True},
        ):
            with self.assertRaises(ValueError):
                project_learning_checkpoint(changed)


if __name__ == "__main__":
    unittest.main()
