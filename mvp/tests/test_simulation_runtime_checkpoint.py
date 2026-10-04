from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accounting import book_external_cash_flow
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.replay import CompositeReplayCheckpoint, REQUIRED_RUNTIME_COMPONENTS
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider
from mvp.autotrade_mvp.simulation_runtime_checkpoint import checkpoint_path
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    PROVIDER,
    run_autonomous_simulation,
)


NOW = "2026-10-03T00:00:00Z"
PRICES = ["100", "101", "103", "102", "100", "100", "101", "103"]


def run(directory, **kwargs):
    return run_autonomous_simulation(
        list(PRICES),
        directory,
        run_id="wp12-runtime-checkpoint",
        now=NOW,
        **kwargs,
    )


class AutonomousRuntimeCheckpointTests(unittest.TestCase):
    def test_pause_persists_complete_composite_cut_and_resume_matches_fresh_run(self):
        with TemporaryDirectory() as continuous, TemporaryDirectory() as restarted:
            expected = run(continuous)
            partial = run(restarted, stop_after_episodes=3)
            self.assertEqual(partial["status"], "PAUSED")

            path = checkpoint_path(restarted)
            self.assertTrue(path.is_file())
            checkpoint = CompositeReplayCheckpoint.from_canonical_json(
                path.read_text(encoding="utf-8")
            )
            self.assertEqual(checkpoint.replay.cursor, 3)
            self.assertEqual(
                set(checkpoint.runtime_components),
                set(REQUIRED_RUNTIME_COMPONENTS),
            )
            self.assertEqual(checkpoint.schema_version, "4.0.0")

            resumed = run(restarted)
            self.assertEqual(resumed["status"], "COMPLETED")
            self.assertEqual(resumed["decisions"], expected["decisions"])
            self.assertEqual(resumed["cash"], expected["cash"])
            self.assertEqual(resumed["position"], expected["position"])
            self.assertEqual(resumed["economic_edge_status"], "INCONCLUSIVE")

            final_checkpoint = CompositeReplayCheckpoint.from_canonical_json(
                path.read_text(encoding="utf-8")
            )
            self.assertEqual(final_checkpoint.replay.cursor, len(PRICES))

    def test_same_terminal_cut_can_be_reopened_without_transport_or_mutation(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("checkpoint-only resume cannot send"),
            ):
                result = run(directory, stop_after_episodes=3)
            self.assertEqual(result["status"], "PAUSED")
            self.assertEqual(result["new_outbound_requests"], 0)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_tampered_checkpoint_fails_before_provider_restore_or_journal_mutation(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            path = checkpoint_path(directory)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["runtime_components"]["strategy_state"] = "f" * 64
            path.write_text(
                json.dumps(
                    value,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "from_state",
                side_effect=AssertionError("invalid checkpoint cannot restore provider"),
            ):
                with self.assertRaisesRegex(ValueError, "runtime checkpoint"):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_missing_checkpoint_fails_closed_before_provider_restore(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=2)
            checkpoint_path(directory).unlink()
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "from_state",
                side_effect=AssertionError("missing checkpoint cannot restore provider"),
            ):
                with self.assertRaisesRegex(ValueError, "missing its runtime checkpoint"):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_foreign_financial_journal_mutation_invalidates_checkpoint_before_restore(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economic = DurableProviderEconomicBook(
                store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            )
            economic.append(
                book_external_cash_flow(
                    transaction_id="wp12-foreign-transaction",
                    cause_event_id="wp12-foreign-cause",
                    currency="USD",
                    amount="1",
                )
            )
            mutated = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "from_state",
                side_effect=AssertionError("stale checkpoint cannot restore provider"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "does not match current authorities",
                ):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), mutated)

    def test_unknown_episode_does_not_mint_a_new_terminal_checkpoint(self):
        with TemporaryDirectory() as directory:
            first = run(directory, stop_after_episodes=2)
            self.assertEqual(first["status"], "PAUSED")
            path = checkpoint_path(directory)
            before = path.read_bytes()

            unknown = run(directory, fault_at_episode=3)
            self.assertEqual(unknown["status"], "UNKNOWN")
            self.assertEqual(path.read_bytes(), before)

            store = JournalStore(Path(directory) / "journal.sqlite3")
            cut = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "from_state",
                side_effect=AssertionError("sticky UNKNOWN cannot recreate provider"),
            ):
                again = run(directory, fault_at_episode=3)
            self.assertEqual(again["status"], "UNKNOWN")
            self.assertEqual(store.whole_store_state_cut(), cut)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
