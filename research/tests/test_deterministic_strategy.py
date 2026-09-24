from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
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
    def test_direct_observation_cannot_bypass_exact_causal_invariants(self):
        with self.assertRaises(TypeError):
            CausalObservation(
                event_id="direct-float",
                symbol="AAA",
                available_at=BASE,
                price=100.1,
            )
        with self.assertRaises(ValueError):
            CausalObservation(
                event_id="direct-naive",
                symbol="AAA",
                available_at=datetime(2026, 1, 1),
                price=Decimal("100"),
            )
        with self.assertRaises(ValueError):
            CausalObservation(
                event_id="direct-zero",
                symbol="AAA",
                available_at=BASE,
                price=Decimal("0"),
            )

        normalized = CausalObservation(
            event_id=" event-direct ",
            symbol=" AAA ",
            available_at=BASE.astimezone(timezone(timedelta(hours=2))),
            price="100.00",
        )
        self.assertEqual(normalized.event_id, "event-direct")
        self.assertEqual(normalized.symbol, "AAA")
        self.assertEqual(normalized.available_at, BASE)
        self.assertEqual(normalized.price, Decimal("100.00"))

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


    def test_snapshot_remembers_evicted_event_for_restart_idempotency(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second, third = obs(0, "100"), obs(1, "101"), obs(2, "102")
        for item in (first, second, third):
            strategy.ingest(item, simulation_time=item.available_at)

        restored = ReturnThresholdBaseline.restore(strategy.snapshot())
        self.assertFalse(restored.ingest(first, simulation_time=third.available_at))
        proposal = restored.propose(symbol="AAA", decision_time=third.available_at)
        self.assertEqual(proposal.evidence_event_ids, ("event-1", "event-2"))

    def test_snapshot_remembers_evicted_event_content_and_rejects_conflict(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second, third = obs(0, "100"), obs(1, "101"), obs(2, "102")
        for item in (first, second, third):
            strategy.ingest(item, simulation_time=item.available_at)

        restored = ReturnThresholdBaseline.restore(strategy.snapshot())
        conflicting = CausalObservation.create(
            event_id=first.event_id,
            symbol=first.symbol,
            available_at=first.available_at,
            price="999",
        )
        with self.assertRaisesRegex(ValueError, "different observation content"):
            restored.ingest(conflicting, simulation_time=third.available_at)

    def test_snapshot_rejects_seen_event_that_conflicts_with_retained_history(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second = obs(0, "100"), obs(1, "101")
        for item in (first, second):
            strategy.ingest(item, simulation_time=item.available_at)
        payload = json.loads(strategy.snapshot())
        payload["seen_events"][first.event_id]["price"] = "999"

        with self.assertRaisesRegex(ValueError, "conflicts with retained history"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_snapshot_rejects_missing_seen_event_for_retained_history(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        first, second = obs(0, "100"), obs(1, "101")
        for item in (first, second):
            strategy.ingest(item, simulation_time=item.available_at)
        payload = json.loads(strategy.snapshot())
        del payload["seen_events"][first.event_id]

        with self.assertRaisesRegex(ValueError, "missing retained history"):
            ReturnThresholdBaseline.restore(
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
            )

    def test_schema_v1_snapshot_remains_readable(self):
        snapshot_v1 = json.dumps(
            {
                "schema_version": 1,
                "lookback": 2,
                "threshold": "0.01",
                "proposal_quantity": "1",
                "history": {
                    "AAA": [
                        {
                            "event_id": "event-0",
                            "available_at": BASE.isoformat(),
                            "price": "100",
                        },
                        {
                            "event_id": "event-1",
                            "available_at": (BASE + timedelta(minutes=1)).isoformat(),
                            "price": "101",
                        },
                    ]
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        restored = ReturnThresholdBaseline.restore(snapshot_v1)
        proposal = restored.propose(
            symbol="AAA",
            decision_time=BASE + timedelta(minutes=1),
        )
        self.assertEqual(proposal.evidence_event_ids, ("event-0", "event-1"))

    def test_out_of_order_availability_is_rejected(self):
        strategy = ReturnThresholdBaseline(lookback=2, threshold="0.01", proposal_quantity="1")
        later = obs(2, "102")
        earlier = obs(1, "101")
        strategy.ingest(later, simulation_time=later.available_at)
        with self.assertRaises(ValueError):
            strategy.ingest(earlier, simulation_time=later.available_at)


if __name__ == "__main__":
    unittest.main()
