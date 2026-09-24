from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    ReturnThresholdBaseline,
    run_baseline,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def obs(i, price, *, available=None):
    return CausalObservation.create(
        event_id=f"event-{i}",
        symbol="AAA",
        available_at=available or BASE + timedelta(minutes=i),
        price=price,
    )


class DeterministicStrategyTests(unittest.TestCase):
    def test_future_observation_is_rejected(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        with self.assertRaises(ValueError):
            strategy.ingest(obs(1, "101"), simulation_time=BASE)

    def test_zero_model_path_never_claims_edge(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="2")
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "102")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "BUY")
        self.assertEqual(proposal.quantity, Decimal("2"))
        self.assertEqual(proposal.model_calls, 0)
        self.assertEqual(proposal.economic_edge_claim, "UNPROVEN")

    def test_insufficient_history_fails_to_hold_not_fabricated_signal(self):
        strategy = ReturnThresholdBaseline(lookback=3, threshold="0.01", proposal_quantity="1")
        proposal = run_baseline(
            strategy,
            [obs(0, "100"), obs(1, "110")],
            decision_time=BASE + timedelta(minutes=1),
            symbol="AAA",
        )
        self.assertEqual(proposal.action, "HOLD")
        self.assertEqual(proposal.quantity, Decimal("0"))

    def test_snapshot_resume_matches_uninterrupted_state(self):
        uninterrupted = ReturnThresholdBaseline(lookback=3, threshold="0.01", proposal_quantity="1")
        for item in [obs(0, "100"), obs(1, "101")]:
            uninterrupted.ingest(item, simulation_time=item.available_at)
        restored = ReturnThresholdBaseline.restore(uninterrupted.snapshot())

        final = obs(2, "103")
        uninterrupted.ingest(final, simulation_time=final.available_at)
        restored.ingest(final, simulation_time=final.available_at)

        a = uninterrupted.propose(symbol="AAA", decision_time=final.available_at)
        b = restored.propose(symbol="AAA", decision_time=final.available_at)
        self.assertEqual(a, b)

    def test_duplicate_event_is_idempotent(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        item = obs(0, "100")
        self.assertTrue(strategy.ingest(item, simulation_time=item.available_at))
        self.assertFalse(strategy.ingest(item, simulation_time=item.available_at))

    def test_duplicate_event_id_with_changed_price_fails_closed(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        original = obs(0, "100")
        conflicting = CausalObservation.create(
            event_id=original.event_id,
            symbol=original.symbol,
            available_at=original.available_at,
            price="101",
        )
        strategy.ingest(original, simulation_time=original.available_at)
        with self.assertRaisesRegex(ValueError, "different observation content"):
            strategy.ingest(conflicting, simulation_time=conflicting.available_at)

    def test_duplicate_event_id_cannot_move_between_symbols(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        original = obs(0, "100")
        conflicting = CausalObservation.create(
            event_id=original.event_id,
            symbol="BBB",
            available_at=original.available_at,
            price=original.price,
        )
        strategy.ingest(original, simulation_time=original.available_at)
        with self.assertRaisesRegex(ValueError, "different observation content"):
            strategy.ingest(conflicting, simulation_time=conflicting.available_at)

    def test_out_of_order_availability_is_rejected(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        later = obs(2, "102")
        earlier = obs(1, "101")
        strategy.ingest(later, simulation_time=later.available_at)
        with self.assertRaises(ValueError):
            strategy.ingest(earlier, simulation_time=later.available_at)


if __name__ == "__main__":
    unittest.main()
