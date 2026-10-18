"""Plan10 5A/5B: learning is bound to CLOSED canonical simulation outcomes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.adaptive_ma import EXPERTS, verify_model
from tools.historical_learning_adapter import train_closed_simulation_outcomes
from tools.train_historical_agent import save_model


START = datetime(2024, 1, 1, 1, tzinfo=timezone.utc)


def decision(episode, *, cash, position, status, fill_id, protocol):
    return {
        "episode": episode,
        "status": status,
        "decision": "BUY" if position != "0" and episode in (1, 4) else
                    "REDUCE" if position == "0" and fill_id else "HOLD",
        "cash": cash,
        "position": position,
        "equity": cash if position == "0" else "1000",
        "reconciled": True,
        "order_id": None if fill_id is None else f"order-{protocol[0]}-{episode}",
        "fill_id": fill_id,
        "reconciliation_event_id": f"reconcile-{protocol[0]}-{episode}",
        "protocol_digest": protocol,
        "emergency": False,
    }


def simulation(hex_character, outcomes, *, execution_count=6):
    protocol = "sha256:" + hex_character * 64
    rows = []
    episode = 0
    cash = 1000
    for index, pnl in enumerate(outcomes, 1):
        episode += 1
        rows.append(decision(
            episode, cash=str(cash - 100), position="1", status="FILLED",
            fill_id=f"open-{hex_character}-{index}", protocol=protocol,
        ))
        episode += 1
        rows.append(decision(
            episode, cash=str(cash - 100), position="1", status="HOLD",
            fill_id=None, protocol=protocol,
        ))
        episode += 1
        cash += pnl
        rows.append(decision(
            episode, cash=str(cash), position="0", status="FILLED",
            fill_id=f"close-{hex_character}-{index}", protocol=protocol,
        ))
    while episode < execution_count:
        episode += 1
        rows.append(decision(
            episode, cash=str(cash), position="0", status="HOLD",
            fill_id=None, protocol=protocol,
        ))
    return {
        "status": "COMPLETED", "environment": "SIMULATION", "mode": "ZERO",
        "run_id": f"candidate-{hex_character}", "protocol_digest": protocol,
        "completed_episodes": len(rows), "cash": str(cash), "position": "0",
        "reconciled": True, "new_outbound_requests": 0, "resumed": False,
        "decisions": rows, "economic_edge_status": "INCONCLUSIVE",
    }


def candidates():
    return [
        {"parameters": {"fast": fast, "slow": slow},
         "dataset_identity_sha256": "d" * 64,
         "execution_count": 6,
         "simulation": simulation(character, outcomes)}
        for (fast, slow), character, outcomes in zip(
            EXPERTS, ("a", "b", "c"), ((10, 5), (-10,), (0,)),
        )
    ]


class HistoricalLearningAdapterTests(unittest.TestCase):
    def train(self, root, *, values=None, seed=7):
        return train_closed_simulation_outcomes(
            candidate_simulations=candidates() if values is None else values,
            memory_path=Path(root) / "learning-memory.sqlite3",
            dataset_sha256="d" * 64, symbol="BTCUSDT", interval="1h",
            first_execution_at=START, initial_cash="1000", seed=seed,
            embargo_seconds=3600, expected_execution_count=6,
        )

    def test_only_closed_outcomes_update_and_replay_is_no_change(self):
        with TemporaryDirectory() as tmp:
            model = self.train(tmp)
            replay = self.train(tmp)
            self.assertEqual(replay, model)
            self.assertEqual(model["selected_parameters"], {"fast": 2, "slow": 3})
            self.assertEqual(
                [row["closed_outcomes"] for row in model["candidates"]],
                [2, 1, 1],
            )
            self.assertEqual(model["learning_audit"]["episode_count"], 4)
            self.assertEqual(model["rng_seed"], 7)
            self.assertEqual(
                model["model_registry"]["routing_status"],
                "FROZEN_CANDIDATE_NOT_PROMOTED",
            )
            con = sqlite3.connect(Path(tmp) / "learning-memory.sqlite3")
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0], 4)
                con.execute("DELETE FROM episodes WHERE rowid=(SELECT min(rowid) FROM episodes)")
                con.commit()
            finally:
                con.close()
            with self.assertRaisesRegex(ValueError, "chronology"):
                self.train(tmp)

    def test_fresh_memory_reproduces_exact_model_bytes(self):
        with TemporaryDirectory() as left, TemporaryDirectory() as right:
            first = self.train(left)
            second = self.train(right)
            self.assertEqual(
                json.dumps(first, sort_keys=True, separators=(",", ":")),
                json.dumps(second, sort_keys=True, separators=(",", ":")),
            )

    def test_open_tail_is_not_reused_as_an_outcome(self):
        with TemporaryDirectory() as tmp:
            values = candidates()
            open_tail = values[2]["simulation"]["decisions"][:2]
            protocol = values[2]["simulation"]["protocol_digest"]
            for episode in range(3, 7):
                open_tail.append(decision(
                    episode, cash="900", position="1", status="HOLD",
                    fill_id=None, protocol=protocol,
                ))
            values[2]["simulation"] = dict(
                values[2]["simulation"], decisions=open_tail,
                completed_episodes=len(open_tail), cash="900", position="1",
            )
            with self.assertRaisesRegex(ValueError, "at least one CLOSED"):
                self.train(tmp, values=values)

    def test_future_label_injection_and_outcome_reuse_are_rejected(self):
        with TemporaryDirectory() as tmp:
            poisoned = candidates()
            poisoned[0]["simulation"]["decisions"][0]["future_close"] = "999999"
            with self.assertRaisesRegex(ValueError, "leakage refused"):
                self.train(tmp, values=poisoned)
        with TemporaryDirectory() as tmp:
            reused = candidates()
            reused[1]["simulation"]["protocol_digest"] = reused[0]["simulation"]["protocol_digest"]
            for row in reused[1]["simulation"]["decisions"]:
                row["protocol_digest"] = reused[0]["simulation"]["protocol_digest"]
            with self.assertRaisesRegex(ValueError, "cannot label multiple"):
                self.train(tmp, values=reused)

    def test_simulation_must_bind_exact_training_dataset(self):
        with TemporaryDirectory() as tmp:
            mismatched = candidates()
            mismatched[0]["dataset_identity_sha256"] = "e" * 64
            with self.assertRaisesRegex(ValueError, "bound to the training dataset"):
                self.train(tmp, values=mismatched)

    def test_frozen_version_seed_and_embargo_are_verified(self):
        with TemporaryDirectory() as tmp:
            model = self.train(tmp)
            last = datetime.fromisoformat(model["training_last_close_utc"])
            self.assertEqual(
                verify_model(
                    model, symbol="BTCUSDT", interval="1h",
                    holdout_start_open=last + timedelta(hours=1),
                )[2],
                model["model_sha256"],
            )
            with self.assertRaisesRegex(ValueError, "embargo overlaps"):
                verify_model(
                    model, symbol="BTCUSDT", interval="1h",
                    holdout_start_open=last + timedelta(minutes=59),
                )
            target = Path(tmp) / "candidate.json"
            save_model(target, model)
            save_model(target, model)
            changed = dict(model, rng_seed=8)
            with self.assertRaisesRegex(ValueError, "existing model differs"):
                save_model(target, changed)

    def test_validation_or_holdout_split_cannot_update(self):
        with TemporaryDirectory() as tmp:
            for split in ("VALIDATION", "HOLDOUT"):
                with self.assertRaisesRegex(ValueError, "forbidden outside the TRAIN"):
                    train_closed_simulation_outcomes(
                        candidate_simulations=candidates(),
                        memory_path=Path(tmp) / f"{split}.sqlite3",
                        dataset_sha256="d" * 64, symbol="BTCUSDT", interval="1h",
                        first_execution_at=START, split_role=split,
                    )

    def test_completed_next_bar_receipt_is_admitted_but_future_time_is_rejected(self):
        values = candidates()
        for item in values:
            for row in item["simulation"]["decisions"]:
                if row["status"] != "FILLED":
                    continue
                available = (START + timedelta(hours=row["episode"])).isoformat().replace(
                    "+00:00", "Z"
                )
                row["historical_execution"] = {
                    "status": "FILLED",
                    "requested_quantity": "1",
                    "executed_quantity": "1",
                    "completed_at": available,
                    "available_at": available,
                    "model_fingerprint": "4" * 64,
                    "bar_sha256": "sha256:" + "5" * 64,
                }
        opening = values[0]["simulation"]["decisions"][0]
        opening["status"] = "PARTIAL"
        opening["historical_execution"].update({
            "status": "PARTIAL",
            "requested_quantity": "2",
            "executed_quantity": "1",
        })
        with TemporaryDirectory() as tmp:
            model = self.train(tmp, values=values)
            self.assertEqual(model["schema"], "autotrade-closed-simulation-training-v2")

        poisoned = values
        poisoned[0]["simulation"]["decisions"][0]["historical_execution"][
            "available_at"
        ] = (START + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
        with TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "expected newly completed bar"):
                self.train(tmp, values=poisoned)


if __name__ == "__main__":
    unittest.main()
