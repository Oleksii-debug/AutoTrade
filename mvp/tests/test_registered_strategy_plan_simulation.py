from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation


NOW = "2026-10-18T00:00:00Z"
PRICES = ["100", "101", "102", "103"]
SIGNALS = ["99", "100", "101", "102"]
SIDES = ["HOLD", "BUY", "HOLD", "SELL"]
IDENTITY = "a" * 64


def run(path, **kwargs):
    return run_autonomous_simulation(
        PRICES,
        path,
        run_id="registered-plan",
        now=NOW,
        signal_prices=SIGNALS,
        strategy_side_plan=SIDES,
        strategy_plan_sha256=IDENTITY,
        **kwargs,
    )


class RegisteredStrategyPlanSimulationTests(unittest.TestCase):
    def test_canonical_simulator_owns_costed_fills_and_restart(self):
        with TemporaryDirectory() as directory:
            first = run(directory, stop_after_episodes=2)
            self.assertEqual(first["status"], "PAUSED")
            resumed = run(directory)
            self.assertEqual(resumed["status"], "COMPLETED")
            self.assertEqual(resumed["position"], "0")
            self.assertEqual(resumed["cash"], "1001.796")
            self.assertEqual(
                [(row["decision"], row["status"]) for row in resumed["decisions"]],
                [("HOLD", "HOLD"), ("BUY", "FILLED"),
                 ("HOLD", "HOLD"), ("REDUCE", "FILLED")],
            )
            again = run(directory)
            self.assertEqual(again["new_outbound_requests"], 0)
            started = JournalStore(Path(directory) / "journal.sqlite3").load_events(
                "canonical_autonomous_simulation", "registered-plan"
            )[0]["payload"]["protocol"]
            self.assertEqual(started["strategy_side_plan"], SIDES)
            self.assertEqual(started["strategy_plan_sha256"], IDENTITY)

    def test_plan_identity_and_shape_fail_before_mutation_or_resume(self):
        with TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "must match"):
                run_autonomous_simulation(
                    PRICES,
                    state,
                    run_id="registered-plan",
                    now=NOW,
                    signal_prices=SIGNALS,
                    strategy_side_plan=["HOLD"],
                    strategy_plan_sha256=IDENTITY,
                )
            self.assertFalse(state.exists())
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=2)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.current_journal_sequence()
            with self.assertRaisesRegex(ValueError, "identity changed"):
                run_autonomous_simulation(
                    PRICES,
                    directory,
                    run_id="registered-plan",
                    now=NOW,
                    signal_prices=SIGNALS,
                    strategy_side_plan=SIDES,
                    strategy_plan_sha256="b" * 64,
                )
            self.assertEqual(store.current_journal_sequence(), before)

    def test_registered_plan_cannot_be_relabelled_or_see_execution_only_stream(self):
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "separated causal signal"):
                run_autonomous_simulation(
                    PRICES,
                    directory,
                    run_id="registered-plan",
                    now=NOW,
                    strategy_side_plan=SIDES,
                    strategy_plan_sha256=IDENTITY,
                )
        with TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "cannot be relabelled"):
                run_autonomous_simulation(
                    PRICES,
                    directory,
                    run_id="registered-plan",
                    now=NOW,
                    signal_prices=SIGNALS,
                    model_sha256="c" * 64,
                    strategy_side_plan=SIDES,
                    strategy_plan_sha256=IDENTITY,
                )


if __name__ == "__main__":
    unittest.main()

