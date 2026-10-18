"""Plan-10 causal risk binding tests; no provider or real-order evidence."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.risk import RiskPolicy
from mvp.autotrade_mvp.risk_policy_authority import DurableRiskPolicyRegistry
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation
from tools.historical_risk_context import (
    HistoricalRiskGate,
    causal_historical_risk_cut,
    evaluate_historical_risk,
)


START = datetime(2024, 1, 1, tzinfo=timezone.utc)


def policy(*, daily="500", drawdown="0.20") -> RiskPolicy:
    return RiskPolicy.create(
        max_abs_position="10",
        max_single_notional="1000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss=daily,
        max_drawdown_fraction=drawdown,
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
    )


def completed(episode, *, cash, position, equity):
    return {
        "episode": episode,
        "cash": cash,
        "position": position,
        "equity": equity,
        "reconciled": True,
    }


class HistoricalRiskContextTests(unittest.TestCase):
    def _loss_cut(self):
        return causal_historical_risk_cut(
            completed=[
                completed(1, cash="900", position="1", equity="1000"),
                completed(2, cash="900", position="1", equity="1000"),
            ],
            execution_prices=["100", "100", "80", "999999"],
            execution_times=[START + timedelta(hours=index) for index in range(4)],
            current_episode=3,
            initial_cash="1000",
        )

    def test_daily_loss_enters_canonical_context_and_blocks_new_risk(self):
        cut = self._loss_cut()
        decision = evaluate_historical_risk(
            cut, policy(daily="10", drawdown="1"),
            side="BUY", quantity="1", execution_price="80",
        )

        self.assertEqual(cut.context.daily_pnl, Decimal("-20"))
        self.assertEqual(cut.context.drawdown_fraction, Decimal("0.02"))
        self.assertFalse(decision.admitted)
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertEqual(failed, {"daily_loss"})

    def test_drawdown_enters_canonical_context_and_blocks_new_risk(self):
        cut = self._loss_cut()
        decision = evaluate_historical_risk(
            cut, policy(daily="1000", drawdown="0.01"),
            side="BUY", quantity="1", execution_price="80",
        )

        self.assertFalse(decision.admitted)
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertEqual(failed, {"drawdown"})

    def test_restart_rebuild_is_identical_and_future_prices_are_unread(self):
        original = self._loss_cut()
        restarted = causal_historical_risk_cut(
            completed=[
                completed(1, cash="900", position="1", equity="1000"),
                completed(2, cash="900", position="1", equity="1000"),
            ],
            # Deliberately invalid future values prove the current cut does
            # not parse or validate observations beyond episode 3.
            execution_prices=["100", "100", "80", 100.0],
            execution_times=[
                START, START + timedelta(hours=1), START + timedelta(hours=2),
                datetime(2025, 1, 1),
            ],
            current_episode=3,
            initial_cash="1000",
        )

        self.assertEqual(restarted, original)

    def test_daily_loss_resets_only_from_last_completed_prior_utc_day(self):
        times = [
            datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
            datetime(2024, 1, 2, 0, tzinfo=timezone.utc),
        ]
        cut = causal_historical_risk_cut(
            completed=[completed(1, cash="850", position="1", equity="950")],
            execution_prices=["100", "90"],
            execution_times=times,
            current_episode=2,
            initial_cash="1000",
        )

        self.assertEqual(cut.daily_start_equity, Decimal("950"))
        self.assertEqual(cut.context.daily_pnl, Decimal("-10"))
        self.assertEqual(cut.context.drawdown_fraction, Decimal("0.06"))

    def test_restart_tamper_and_binary_float_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "equity conflicts"):
            causal_historical_risk_cut(
                completed=[completed(1, cash="900", position="1", equity="999")],
                execution_prices=["100", "90"],
                execution_times=[START, START + timedelta(hours=1)],
                current_episode=2,
                initial_cash="1000",
            )
        with self.assertRaisesRegex(TypeError, "exact Decimal"):
            causal_historical_risk_cut(
                completed=[], execution_prices=[100.0], execution_times=[START],
                current_episode=1, initial_cash="1000",
            )

    def test_loop_pauses_before_next_order_and_restart_repeats_same_block(self):
        prices = ["100", "101", "102", "50"]
        times = [START + timedelta(hours=index) for index in range(len(prices))]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = run_autonomous_simulation(
                prices, root, run_id="historical-risk-loop",
                now="2024-01-01T00:00:00Z", stop_after_episodes=3,
            )
            self.assertEqual(first["completed_episodes"], 3)
            self.assertEqual(first["position"], "1")

            reports = []
            for _restart in range(2):
                gate = HistoricalRiskGate(
                    state_dir=root,
                    run_id="historical-risk-loop",
                    execution_prices=prices,
                    signal_prices=prices,
                    execution_times=times,
                    initial_cash="1000",
                    target_quantity="1",
                )
                with patch.object(
                    DurableRiskPolicyRegistry,
                    "resolve_current",
                    return_value=SimpleNamespace(
                        policy=policy(daily="1000", drawdown="0.01")
                    ),
                ):
                    blocked = run_autonomous_simulation(
                        prices, root, run_id="historical-risk-loop",
                        now="2024-01-01T00:00:00Z", stop_after_episodes=4,
                        should_pause=gate,
                    )
                self.assertEqual(blocked["status"], "PAUSED")
                self.assertEqual(blocked["completed_episodes"], 3)
                self.assertEqual(blocked["new_outbound_requests"], 0)
                self.assertEqual(gate.blocked_rules, ("drawdown",))
                reports.append(gate.report())
            self.assertEqual(reports[0], reports[1])

    def test_noop_restart_report_restores_last_completed_predecision(self):
        prices = ["100", "101", "102"]
        times = [START + timedelta(hours=index) for index in range(len(prices))]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first_gate = HistoricalRiskGate(
                state_dir=root,
                run_id="historical-risk-report-restart",
                execution_prices=prices,
                signal_prices=prices,
                execution_times=times,
                initial_cash="1000",
                target_quantity="1",
            )
            result = run_autonomous_simulation(
                prices,
                root,
                run_id="historical-risk-report-restart",
                now="2024-01-01T00:00:00Z",
                stop_after_episodes=3,
                should_pause=first_gate,
            )
            self.assertEqual(result["completed_episodes"], 3)
            first_report = first_gate.report()

            restarted_gate = HistoricalRiskGate(
                state_dir=root,
                run_id="historical-risk-report-restart",
                execution_prices=prices,
                signal_prices=prices,
                execution_times=times,
                initial_cash="1000",
                target_quantity="1",
            )
            self.assertEqual(restarted_gate.report(), first_report)


if __name__ == "__main__":
    unittest.main()
