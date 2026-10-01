from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


LEGACY_CAPABILITY_EVENT = "CapabilitySnapshotObserved.v1"
CAPABILITY_EVENT_V2 = "CapabilitySnapshotObserved.v2"
COMMITTED_AT = "2026-10-01T00:00:00+00:00"
RETIREMENT_ID = "capability-history-v2"


def envelope(
    *,
    event_id: str,
    event_type: str,
    aggregate_id: str,
    aggregate_version: str,
) -> dict[str, object]:
    payload = {"kind": "test", "event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": event_type,
        "aggregate_type": "capability_history",
        "aggregate_id": aggregate_id,
        "aggregate_version": aggregate_version,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": COMMITTED_AT,
    }


class EventTypeRetirementFenceTests(unittest.TestCase):
    def test_preexisting_schema9_runtime_is_blocked_after_retirement(self):
        class LegacyJournalStore(JournalStore):
            SCHEMA_VERSION = 9

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            legacy = LegacyJournalStore(path)
            legacy.append_event(
                envelope(
                    event_id="legacy-before-cutover",
                    event_type=LEGACY_CAPABILITY_EVENT,
                    aggregate_id="legacy-capability",
                    aggregate_version="1",
                )
            )

            current = JournalStore(path)
            self.assertEqual(current.current_schema_version(), 10)
            self.assertTrue(
                current.retire_event_type(
                    LEGACY_CAPABILITY_EVENT,
                    retirement_id=RETIREMENT_ID,
                )
            )

            # The already-initialized legacy object performs the same INSERT an
            # older binary would perform. The SQLite trigger, not new Python
            # process state, must reject it after the durable cutover.
            with self.assertRaisesRegex(sqlite3.IntegrityError, "event type is retired"):
                legacy.append_event(
                    envelope(
                        event_id="legacy-after-cutover",
                        event_type=LEGACY_CAPABILITY_EVENT,
                        aggregate_id="legacy-capability",
                        aggregate_version="2",
                    )
                )

            # Historical v1 evidence remains readable/auditable.
            historical = current.load_events(
                "capability_history",
                "legacy-capability",
            )
            self.assertEqual(
                [item["event_id"] for item in historical],
                ["legacy-before-cutover"],
            )

            # A distinct v2 aggregate/event family remains writable after the
            # old writer generation has been fenced.
            written = current.append_event(
                envelope(
                    event_id="v2-after-cutover",
                    event_type=CAPABILITY_EVENT_V2,
                    aggregate_id="capability-v2:testnet",
                    aggregate_version="1",
                )
            )
            self.assertTrue(written.inserted)

    def test_retirement_is_exact_idempotent_and_conflicts_on_restamp(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertTrue(
                store.retire_event_type(
                    LEGACY_CAPABILITY_EVENT,
                    retirement_id=RETIREMENT_ID,
                )
            )
            self.assertFalse(
                store.retire_event_type(
                    LEGACY_CAPABILITY_EVENT,
                    retirement_id=RETIREMENT_ID,
                )
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.retire_event_type(
                    LEGACY_CAPABILITY_EVENT,
                    retirement_id="capability-history-v2-restamp",
                )

    def test_retirement_row_cannot_be_updated_or_deleted(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            store.retire_event_type(
                LEGACY_CAPABILITY_EVENT,
                retirement_id=RETIREMENT_ID,
            )

            connection = sqlite3.connect(path)
            try:
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "event type retirement is immutable",
                ):
                    connection.execute(
                        "UPDATE retired_event_types SET retirement_id = ? "
                        "WHERE event_type = ?",
                        ("different-retirement", LEGACY_CAPABILITY_EVENT),
                    )
                connection.rollback()
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "event type retirement is immutable",
                ):
                    connection.execute(
                        "DELETE FROM retired_event_types WHERE event_type = ?",
                        (LEGACY_CAPABILITY_EVENT,),
                    )
            finally:
                connection.rollback()
                connection.close()

    def test_reopen_rejects_missing_retirement_trigger(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            store.retire_event_type(
                LEGACY_CAPABILITY_EVENT,
                retirement_id=RETIREMENT_ID,
            )
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "DROP TRIGGER autotrade_reject_retired_event_type"
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "missing event-type retirement trigger",
            ):
                JournalStore(path)


if __name__ == "__main__":
    unittest.main()
