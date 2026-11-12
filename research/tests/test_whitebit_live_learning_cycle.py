"""Economic, prospective and restart boundaries of live research waves."""
from decimal import Decimal
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_research.learning.whitebit_live_zero import WhiteBitLiveZeroLearning
from research.tests.test_whitebit_live_zero_learning import snapshot


class LiveLearningCycleTests(unittest.TestCase):
    def learner(self, root, **changes):
        options = dict(cycle_episodes=2, cycle_seconds=3600,
            evaluation_episodes=2, max_drawdown_usdt="20", cooldown_seconds=60, max_trials=1,
            min_evaluation_trades=2)
        options.update(changes)
        return WhiteBitLiveZeroLearning(root, **options)

    def tick(self, learner, index, price):
        return learner.process_snapshot(snapshot(index, bid=str(price), ask=str(Decimal(price)+Decimal("0.01"))))

    def test_positive_window_pauses_and_prospective_gate_resumes_with_frozen_models(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root)
            phases = []
            for i in range(1, 11):
                result = self.tick(learner, i, 100+i)
                cycle = learner.status()["learning_cycle"]
                phases.append(cycle["phase"])
                if cycle["phase"] in {"PAUSED", "ANALYZING", "TRAINING", "EVALUATING"}:
                    self.assertIsNone(result["new_decision"])
                # Restart at every boundary, including pending shadow prediction.
                learner = self.learner(root)
                self.assertEqual(learner.status()["learning_cycle"], cycle)
            self.assertEqual(phases[3:7], ["PAUSED", "ANALYZING", "TRAINING", "EVALUATING"])
            cycle = learner.status()["learning_cycle"]
            self.assertEqual(cycle["phase"], "RUNNING")
            self.assertEqual(cycle["incumbent_version"], 1)
            self.assertEqual(set(cycle["train_scores"]), {
                "MOMENTUM_COST_HURDLE", "CONTRARIAN_COST_HURDLE",
                "MOMENTUM_SHORT_COST_HURDLE",
                "CONTRARIAN_SHORT_COST_HURDLE",
            })
            self.assertTrue(cycle["evaluation"]["qualifies_for_research_simulation"])
            self.assertFalse(cycle["champion_promotion"])
            with closing(sqlite3.connect(learner.database)) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM live_shadow_episodes").fetchone()[0], 2)
                model = json.loads(con.execute("SELECT model_json FROM models WHERE version=1").fetchone()[0])
                self.assertEqual(model["trained_episode_count"], 2)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions WHERE episode_json IS NOT NULL").fetchone()[0], 2)

    def test_unprofitable_shadow_enters_cooldown_and_stops_retraining_at_budget(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(Path(tmp))
            for i in range(1, 8):
                self.tick(learner, i, 100+i)
            for i in range(8, 14):
                self.tick(learner, i, 107)
            cycle = learner.status()["learning_cycle"]
            self.assertEqual(cycle["phase"], "RESEARCH_ONLY")
            self.assertEqual(cycle["trials"], 1)
            self.assertFalse(cycle["evaluation"]["qualifies_for_research_simulation"])
            self.assertEqual(learner.status()["episode_count"], 2)

    def test_cost_hurdle_abstains_and_drawdown_pauses_before_new_action(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(Path(tmp), cycle_episodes=100, max_drawdown_usdt="0.1")
            self.assertFalse(learner._cycle_action("MOMENTUM_COST_HURDLE", Decimal("0.1"),
                dict(ask_price="100.01", bid_price="100")))
            self.tick(learner, 1, 100)
            self.tick(learner, 2, 101)
            result = self.tick(learner, 3, 100)
            self.assertIsNone(result["new_decision"])
            self.assertIn("LEARNING_WAVE.DRAWDOWN_THRESHOLD", learner.status()["learning_cycle"]["pause_reasons"])

    def test_short_candidate_has_symmetric_cost_hurdle_and_outcome(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(Path(tmp))
            quote = dict(ask_price="100.01", bid_price="100")
            self.assertEqual(
                learner._cycle_side(
                    "MOMENTUM_SHORT_COST_HURDLE", Decimal("-2"), quote,
                ),
                "SHORT",
            )
            self.assertIsNone(learner._cycle_side(
                "MOMENTUM_SHORT_COST_HURDLE", Decimal("-0.01"), quote,
            ))
            winning_result = learner._short_outcome("100", "90", duration_seconds=60)
            losing_result = learner._short_outcome("100", "110", duration_seconds=60)
            winning, losing = winning_result[4], losing_result[4]
            self.assertGreater(winning, 0)
            self.assertLess(losing, 0)
            self.assertGreater(winning_result[3], 0)
            self.assertLess(
                abs(winning-(winning_result[1]-winning_result[2]-winning_result[3])),
                Decimal("1e-25"),
            )

    def test_legacy_memory_migrates_paused_and_retains_pending_and_evaluation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root, cycle_episodes=100)
            for i in range(1, 8):
                self.tick(learner, i, 100+i)
            with closing(sqlite3.connect(learner.database)) as con:
                before = con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
                con.execute("DROP TABLE live_cycle")
                con.commit()
            learner = self.learner(root, cycle_episodes=100)
            self.assertEqual(learner.status()["learning_cycle"]["phase"], "PAUSED")
            for i in range(8, 11):
                self.tick(learner, i, 100+i)
            with closing(sqlite3.connect(learner.database)) as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM decisions").fetchone()[0], before)
                model = json.loads(con.execute("SELECT model_json FROM models ORDER BY version DESC LIMIT 1").fetchone()[0])
                train = con.execute("SELECT COUNT(*) FROM decisions WHERE split='TRAIN' AND episode_json IS NOT NULL").fetchone()[0]
                self.assertEqual(model["trained_episode_count"], train)
            self.assertEqual(learner.status()["learning_cycle"]["phase"], "EVALUATING")

    def test_frozen_configuration_cannot_change_on_restart(self):
        with TemporaryDirectory() as tmp:
            self.learner(Path(tmp))
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                self.learner(Path(tmp), max_drawdown_usdt="200")

    def test_pre_horizon_state_migrates_only_to_zero_horizon(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root)
            with closing(sqlite3.connect(learner.database)) as con:
                state = json.loads(con.execute(
                    "SELECT state FROM live_cycle WHERE id=1"
                ).fetchone()[0])
                state["config"].pop("outcome_horizon_seconds")
                state["config"].pop("signal_lookback_seconds")
                con.execute(
                    "UPDATE live_cycle SET state=? WHERE id=1",
                    (json.dumps(state, sort_keys=True),),
                )
                con.commit()

            migrated = self.learner(root)
            self.assertEqual(
                migrated.status()["learning_cycle"]["config"][
                    "outcome_horizon_seconds"
                ],
                0,
            )
            self.assertEqual(
                migrated.status()["learning_cycle"]["config"][
                    "signal_lookback_seconds"
                ],
                0,
            )
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                self.learner(root, outcome_horizon_seconds=60)

    def test_signal_lookback_uses_only_causal_horizon_reference(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(
                Path(tmp),
                cycle_episodes=100,
                signal_lookback_seconds=180,
            )
            self.assertIsNone(self.tick(learner, 1, 100)["new_decision"])
            self.assertIsNone(self.tick(learner, 2, 101)["new_decision"])
            self.assertIsNone(self.tick(learner, 3, 99)["new_decision"])
            decision = self.tick(learner, 4, 104)["new_decision"]
            self.assertIsNotNone(decision)
            self.assertEqual(decision["feature_momentum"], "4.000")
            self.assertEqual(decision["prediction"], "UP")

    def test_top_book_imbalance_is_separate_restart_safe_causal_feature(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(
                root, cycle_episodes=100, feature_mode="TOP_BOOK_IMBALANCE")
            self.assertIsNone(self.tick(learner, 1, 100)["new_decision"])
            decision = self.tick(learner, 2, 100)["new_decision"]
            self.assertIsNotNone(decision)
            self.assertLess(Decimal(decision["feature_momentum"]), 0)
            self.assertEqual(decision["prediction"], "DOWN")
            self.assertEqual(
                learner.status()["learning_cycle"]["config"]["feature_mode"],
                "TOP_BOOK_IMBALANCE",
            )
            restarted = self.learner(
                root, cycle_episodes=100, feature_mode="TOP_BOOK_IMBALANCE")
            self.assertEqual(restarted.status(), learner.status())
            with self.assertRaisesRegex(ValueError, "configuration differs"):
                self.learner(root, cycle_episodes=100,
                             feature_mode="MID_MOMENTUM")

    def test_time_bound_and_atomic_rollback_do_not_skip_an_observation(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root, cycle_episodes=100, cycle_seconds=60)
            self.tick(learner, 1, 100)
            original = learner.status()
            with patch.object(learner, "_save_cycle", side_effect=RuntimeError("crash before commit")):
                with self.assertRaisesRegex(RuntimeError, "crash"):
                    self.tick(learner, 2, 101)
            learner = self.learner(root, cycle_episodes=100, cycle_seconds=60)
            self.assertEqual(original, learner.status())
            result = self.tick(learner, 2, 101)
            self.assertEqual(result["status"], "PROCESSED")
            self.assertIsNone(result["new_decision"])
            self.assertIn("LEARNING_WAVE.TIME_THRESHOLD", learner.status()["learning_cycle"]["pause_reasons"])

    def test_bounded_outcome_horizon_survives_restart_without_overlapping_decisions(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(
                root,
                cycle_episodes=100,
                outcome_horizon_seconds=180,
            )
            self.tick(learner, 1, 100)
            first = self.tick(learner, 2, 101)
            self.assertIsNotNone(first["new_decision"])
            waiting = self.tick(learner, 3, 102)
            self.assertIsNone(waiting["matured_episode"])
            self.assertIsNone(waiting["new_decision"])

            learner = self.learner(
                root,
                cycle_episodes=100,
                outcome_horizon_seconds=180,
            )
            still_waiting = self.tick(learner, 4, 103)
            self.assertIsNone(still_waiting["matured_episode"])
            self.assertIsNone(still_waiting["new_decision"])
            matured = self.tick(learner, 5, 104)
            self.assertIsNotNone(matured["matured_episode"])
            self.assertEqual(
                matured["matured_episode"]["outcome_available_at"],
                snapshot(5, bid="104", ask="104.01")["available_at"],
            )
            self.assertIsNotNone(matured["new_decision"])
            with closing(sqlite3.connect(learner.database)) as con:
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM decisions "
                        "WHERE outcome_observation_key IS NULL"
                    ).fetchone()[0],
                    1,
                )

    def test_horizon_episodes_remain_training_eligible_after_pause(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(
                Path(tmp),
                cycle_episodes=2,
                outcome_horizon_seconds=180,
            )
            for index in range(1, 12):
                self.tick(learner, index, 100 + index)
            cycle = learner.status()["learning_cycle"]
            self.assertEqual(cycle["phase"], "EVALUATING")
            self.assertEqual(cycle["training_cutoff"], 2)
            self.assertEqual(cycle["candidate_version"], 1)
            self.assertEqual(cycle["analysis"]["episodes"], 2)

    def test_shadow_evaluation_waits_for_same_bounded_horizon_across_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            options = dict(outcome_horizon_seconds=180)
            learner = self.learner(root, **options)
            for index in range(1, 13):
                self.tick(learner, index, 100 + index)
            cycle = learner.status()["learning_cycle"]
            self.assertEqual(cycle["phase"], "EVALUATING")
            self.assertIsNotNone(cycle["evaluation"]["pending"])
            self.assertEqual(cycle["evaluation"]["count"], 0)

            learner = self.learner(root, **options)
            self.tick(learner, 13, 113)
            self.tick(learner, 14, 114)
            self.assertEqual(
                learner.status()["learning_cycle"]["evaluation"]["count"],
                0,
            )
            self.tick(learner, 15, 115)
            evaluation = learner.status()["learning_cycle"]["evaluation"]
            self.assertEqual(evaluation["count"], 1)
            self.assertIsNotNone(evaluation["pending"])

    def test_market_mismatch_is_rejected_without_mutation(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(Path(tmp))
            self.tick(learner, 1, 100)
            before = learner.status()
            other = snapshot(2, bid="101", ask="102")
            other["market"] = "ETH_USDT"
            with self.assertRaisesRegex(ValueError, "market differs"):
                learner.process_snapshot(other)
            self.assertEqual(before, learner.status())
            self.assertEqual(self.tick(learner, 2, 101)["status"], "PROCESSED")

    def test_restart_gap_invalidates_pending_without_fake_pnl_or_training(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root)
            self.tick(learner, 1, 100)
            self.tick(learner, 2, 101)
            learner = self.learner(root)
            result = self.tick(learner, 20, 200)
            self.assertIsNone(result["matured_episode"])
            self.assertIsNone(result["new_decision"])
            with learner._connection() as con:
                invalid = json.loads(con.execute("SELECT episode_json FROM decisions").fetchone()[0])
                self.assertEqual(invalid["status"], "INVALID_OBSERVATION_GAP")
                self.assertNotIn("net_outcome_usdt", invalid)
                self.assertEqual(learner._training_rows(con, 100), [])
            self.assertIsNotNone(self.tick(learner, 21, 201)["new_decision"])

    def test_shadow_gap_does_not_count_or_reuse_long_gap_feature(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(root)
            for i in range(1, 9):
                self.tick(learner, i, 100+i)
            self.assertIsNotNone(learner.status()["learning_cycle"]["evaluation"]["pending"])
            learner = self.learner(root)
            self.tick(learner, 30, 500)
            evaluation = learner.status()["learning_cycle"]["evaluation"]
            self.assertEqual(evaluation["count"], 0)
            self.assertEqual(evaluation["candidate_trades"], 0)
            self.assertEqual(evaluation["invalid_gap_count"], 1)
            self.assertIsNone(evaluation["pending"])
            self.tick(learner, 31, 501)
            self.assertIsNotNone(learner.status()["learning_cycle"]["evaluation"]["pending"])

    def test_positive_shadow_profit_below_minimum_trade_count_cannot_resume(self):
        with TemporaryDirectory() as tmp:
            learner = self.learner(Path(tmp), min_evaluation_trades=3)
            for i in range(1, 11):
                self.tick(learner, i, 100+i)
            cycle = learner.status()["learning_cycle"]
            self.assertGreater(Decimal(cycle["evaluation"]["candidate"]), 0)
            self.assertEqual(cycle["evaluation"]["candidate_trades"], 2)
            self.assertEqual(cycle["phase"], "COOLDOWN")

    def test_paired_abstention_rejects_once_minimum_trades_are_unreachable(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            learner = self.learner(
                root, evaluation_episodes=4, min_evaluation_trades=2
            )
            for i in range(1, 8):
                self.tick(learner, i, 100+i)
            self.assertEqual(
                learner.status()["learning_cycle"]["phase"], "EVALUATING"
            )

            # A flat market makes both the legacy MOMENTUM incumbent and the
            # cost-hurdle candidate abstain.  Stop once too few windows remain
            # for the candidate to satisfy the preregistered minimum trades.
            for i in range(8, 12):
                self.tick(learner, i, 107)
                learner = self.learner(
                    root, evaluation_episodes=4, min_evaluation_trades=2
                )

            cycle = learner.status()["learning_cycle"]
            self.assertEqual(cycle["phase"], "COOLDOWN")
            self.assertEqual(cycle["evaluation"]["count"], 3)
            self.assertEqual(cycle["evaluation"]["no_trade"], "0")
            self.assertEqual(cycle["evaluation"]["candidate_trades"], 0)
            self.assertEqual(cycle["evaluation"]["incumbent_trades"], 0)
            self.assertEqual(
                cycle["reason"],
                "PROSPECTIVE_MINIMUM_TRADE_COUNT_UNREACHABLE",
            )
            self.assertFalse(
                cycle["evaluation"]["qualifies_for_research_simulation"]
            )
            self.assertFalse(cycle["champion_promotion"])
            self.assertFalse(cycle["trading_authority"])
            with closing(sqlite3.connect(learner.database)) as con:
                self.assertEqual(
                    con.execute(
                        "SELECT COUNT(*) FROM live_shadow_episodes"
                    ).fetchone()[0],
                    3,
                )


if __name__ == "__main__":
    unittest.main()

