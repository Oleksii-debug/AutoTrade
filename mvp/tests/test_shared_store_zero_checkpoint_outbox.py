"""Shared JournalStore regressions for ZERO checkpoint and Host UI outbox ownership.

The provider-free worker and Host intentionally share one JournalStore. Host
control traffic is not ZERO runtime state and ZERO is not a UI publisher.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.durable_host_api import JournalBackedHostCommandStore
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.simulation_runtime_checkpoint import checkpoint_path
from mvp.autotrade_mvp.simulation_session import (
    ACCOUNT,
    ENVIRONMENT,
    run_autonomous_simulation,
)


_PRICES = ["100", "101", "103", "102", "100"]
_NOW = "2026-10-05T00:00:00Z"


class SharedStoreZeroCheckpointOutboxTests(unittest.TestCase):
    def _host_accept(self, store: JournalStore):
        host = JournalBackedHostCommandStore(
            store,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            session_validator=lambda session, actor, origin, action: True,
            request_origin_provider=lambda: "http://127.0.0.1:8765",
            now=lambda: _NOW,
        )
        command_id = str(uuid4())
        accepted = host.submit({
            "command_id": command_id,
            "idempotency_key": command_id,
            "expected_state_version": str(host.state_version),
            "actor": "local-owner",
            "session": "shared-store-test-session",
            "account_id": ACCOUNT,
            "environment": ENVIRONMENT,
            "action": "BLOCK_NEW_EXPOSURE",
            "payload": {},
        })
        self.assertEqual(accepted.status, "ACCEPTED")
        rows = [
            item for item in store.pending_outbox(limit=1000)
            if item["topic"] == "ui.host-events"
        ]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def _assert_host_publication_still_pending(self, store: JournalStore, row):
        state = store.outbox_delivery_state(
            row["event_id"],
            topic="ui.host-events",
        )
        self.assertIsNotNone(state)
        self.assertFalse(state["delivered"])
        self.assertEqual(state["outbox_id"], row["outbox_id"])
        self.assertEqual(state["envelope_hash"], row["envelope_hash"])

    def test_foreign_host_event_does_not_stale_zero_checkpoint_or_gain_ui_delivery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = run_autonomous_simulation(
                _PRICES,
                root,
                run_id="shared-store-host-event",
                now=_NOW,
                stop_after_episodes=1,
            )
            self.assertEqual(first["completed_episodes"], 1)

            store = JournalStore(root / "journal.sqlite3")
            host_row = self._host_accept(store)

            resumed = run_autonomous_simulation(
                _PRICES,
                root,
                run_id="shared-store-host-event",
                now=_NOW,
                stop_after_episodes=2,
            )
            self.assertEqual(resumed["completed_episodes"], 2)
            self._assert_host_publication_still_pending(store, host_row)

    def test_completion_checkpoint_repair_ignores_later_host_state_and_keeps_ui_pending(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = run_autonomous_simulation(
                _PRICES,
                root,
                run_id="shared-store-repair",
                now=_NOW,
                stop_after_episodes=1,
            )
            self.assertEqual(first["completed_episodes"], 1)
            checkpoint_path(root).unlink()

            store = JournalStore(root / "journal.sqlite3")
            host_row = self._host_accept(store)

            resumed = run_autonomous_simulation(
                _PRICES,
                root,
                run_id="shared-store-repair",
                now=_NOW,
                stop_after_episodes=2,
            )
            self.assertEqual(resumed["completed_episodes"], 2)
            self.assertTrue(checkpoint_path(root).is_file())
            self._assert_host_publication_still_pending(store, host_row)


if __name__ == "__main__":
    unittest.main()
