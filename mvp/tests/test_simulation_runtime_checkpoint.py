from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
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

    def test_identical_semantics_in_distinct_journal_backings_keep_distinct_provenance(self):
        with TemporaryDirectory() as first, TemporaryDirectory() as second:
            run(first, stop_after_episodes=3)
            run(second, stop_after_episodes=3)
            left = CompositeReplayCheckpoint.from_canonical_json(
                checkpoint_path(first).read_text(encoding="utf-8")
            )
            right = CompositeReplayCheckpoint.from_canonical_json(
                checkpoint_path(second).read_text(encoding="utf-8")
            )
            self.assertEqual(left.replay, right.replay)
            self.assertNotEqual(left.runtime_cut_id, right.runtime_cut_id)
            self.assertNotEqual(left.fingerprint, right.fingerprint)

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

    def test_runtime_authority_key_is_product_generated_and_replacement_invalidates_resume(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            key_path = Path(directory) / ".autonomous-runtime-authority.key"
            self.assertTrue(key_path.is_file())
            original = key_path.read_bytes()
            self.assertEqual(len(original), 32)

            key_path.write_bytes(b"x" * 32)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("rebound authority cannot send"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "does not match current authorities",
                ):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_runtime_checkpoint_module_exposes_no_reusable_signing_oracle(self):
        from mvp.autotrade_mvp import simulation_runtime_checkpoint as checkpoint_module

        self.assertFalse(hasattr(checkpoint_module, "_sign_material"))
        self.assertFalse(hasattr(checkpoint_module, "_verify_material"))

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
                with self.assertRaisesRegex(ValueError, "missing or has unsafe runtime checkpoint evidence"):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), before)

    def test_foreign_durable_journal_mutation_invalidates_checkpoint_before_restore(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=3)
            store = JournalStore(Path(directory) / "journal.sqlite3")
            payload = {"probe": "post-checkpoint-durable-mutation"}
            store.append_event(
                {
                    "event_id": "wp12-post-checkpoint-probe",
                    "event_type": "Wp12DurableMutationProbe",
                    "schema_version": "1.0.0",
                    "aggregate_type": "wp12_test_probe",
                    "aggregate_id": "probe",
                    "aggregate_version": "1",
                    "host_id": "test",
                    "owner_epoch": "1",
                    "environment": ENVIRONMENT,
                    "occurred_at": NOW,
                    "observed_at": NOW,
                    "committed_at": NOW,
                    "correlation_id": "wp12-probe",
                    "causation_id": None,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "evidence_refs": [],
                }
            )
            mutated = store.whole_store_state_cut()
            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("stale checkpoint cannot send"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "does not match current authorities",
                ):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), mutated)

    def test_crash_after_durable_completion_before_checkpoint_fails_closed(self):
        with TemporaryDirectory() as directory:
            run(directory, stop_after_episodes=2)
            path = checkpoint_path(directory)
            before_checkpoint = path.read_bytes()

            with patch(
                "mvp.autotrade_mvp.simulation_runtime_checkpoint."
                "persist_autonomous_runtime_checkpoint",
                side_effect=RuntimeError("checkpoint persistence crash"),
            ):
                with self.assertRaisesRegex(RuntimeError, "checkpoint persistence crash"):
                    run(directory, stop_after_episodes=3)

            store = JournalStore(Path(directory) / "journal.sqlite3")
            completed = store.load_events(
                "canonical_autonomous_simulation",
                "wp12-runtime-checkpoint",
            )
            self.assertEqual(
                [event["event_type"] for event in completed[-2:]],
                ["AutonomousEpisodeStarted", "AutonomousEpisodeCompleted"],
            )
            self.assertEqual(path.read_bytes(), before_checkpoint)
            durable_cut = store.whole_store_state_cut()

            with patch.object(
                SimulatedProvider,
                "transport_send",
                side_effect=AssertionError("stale checkpoint cannot send"),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "runtime checkpoint identity differs|does not match current authorities",
                ):
                    run(directory)
            self.assertEqual(store.whole_store_state_cut(), durable_cut)

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
