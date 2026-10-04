from __future__ import annotations

import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def _event() -> dict[str, object]:
    payload = {"kind": "fill", "quantity": "1"}
    return {
        "event_id": "evt-v7",
        "event_type": "ExecutionFillObserved",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-09-29T00:00:00+00:00",
    }


class V8CheckpointMigrationAuthorityTests(unittest.TestCase):
    def test_v7_upgrade_invalidates_both_derived_checkpoint_families(self) -> None:
        class V7JournalStore(JournalStore):
            SCHEMA_VERSION = 7

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            legacy = V7JournalStore(path)
            legacy.append_event(_event())

            aggregate_state = {"net_quantity": "1"}
            global_state = {"paper-1": "1"}
            aggregate_json = json.dumps(
                aggregate_state,
                sort_keys=True,
                separators=(",", ":"),
            )
            global_json = json.dumps(
                global_state,
                sort_keys=True,
                separators=(",", ":"),
            )

            # Seed the historical v7 state-only digest representation directly.
            # Calling today's save methods through a SCHEMA_VERSION=7 subclass
            # would accidentally apply the newer identity/cut digest contract.
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    INSERT INTO projection_checkpoints(
                        projection_name, aggregate_type, aggregate_id,
                        aggregate_version, state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        "position",
                        "account",
                        "paper-1",
                        1,
                        aggregate_json,
                        payload_digest(aggregate_state),
                        "2026-09-29T00:00:00Z",
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO global_projection_checkpoints(
                        projection_name, journal_sequence,
                        state_json, state_hash, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "portfolio",
                        1,
                        global_json,
                        payload_digest(global_state),
                        "2026-09-29T00:00:00Z",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            upgraded = JournalStore(path)
            self.assertEqual(upgraded.current_schema_version(), JournalStore.SCHEMA_VERSION)
            self.assertEqual(
                [item["event_id"] for item in upgraded.load_events("account", "paper-1")],
                ["evt-v7"],
            )
            self.assertIsNone(
                upgraded.load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )
            )
            self.assertIsNone(
                upgraded.load_global_projection_checkpoint(
                    projection_name="portfolio"
                )
            )

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM projection_checkpoints"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM global_projection_checkpoints"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                    1,
                )
            finally:
                connection.close()

            self.assertTrue(
                upgraded.save_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                    aggregate_version=1,
                    state=aggregate_state,
                )
            )
            self.assertTrue(
                upgraded.save_global_projection_checkpoint(
                    projection_name="portfolio",
                    journal_sequence=1,
                    state=global_state,
                )
            )
            self.assertEqual(
                upgraded.load_projection_checkpoint(
                    projection_name="position",
                    aggregate_type="account",
                    aggregate_id="paper-1",
                )["state"],
                aggregate_state,
            )
            self.assertEqual(
                upgraded.load_global_projection_checkpoint(
                    projection_name="portfolio"
                )["state"],
                global_state,
            )


if __name__ == "__main__":
    unittest.main()
