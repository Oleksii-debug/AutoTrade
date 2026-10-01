import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import (
    JournalSequencePreconditionFailed,
    JournalStore,
    payload_digest,
)


def _event(event_id: str, version: int):
    payload = {"event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": "ProtectedWriterQualificationEvent",
        "aggregate_type": "protected-qualification",
        "aggregate_id": "attempt-1",
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-01T00:00:00Z",
    }


class ProtectedWriterV11QualificationTests(unittest.TestCase):
    def _bind(self, store: JournalStore):
        return store.bind_protected_writer(
            aggregate_type="protected-qualification",
            namespace="protected-qualification:v1",
            writer_authority_id="issuer:qualification:v1",
        )

    def test_same_named_writer_guard_trigger_forgery_fails_reopen(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            self._bind(store)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "DROP TRIGGER autotrade_protected_event_writer_guard"
                )
                connection.execute(
                    """
                    CREATE TRIGGER autotrade_protected_event_writer_guard
                    BEFORE INSERT ON events
                    BEGIN
                        SELECT 1;
                    END
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError,
                "protected-writer trigger contract mismatch",
            ):
                JournalStore(path)

    def test_protected_append_keeps_global_journal_cut_precondition(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            capability = self._bind(store)

            inserted = store.append_protected_event(
                capability,
                _event("protected-cut-1", 1),
                expected_journal_sequence=0,
            )
            self.assertTrue(inserted.inserted)

            with self.assertRaises(JournalSequencePreconditionFailed):
                store.append_protected_event(
                    capability,
                    _event("protected-cut-2", 2),
                    expected_journal_sequence=0,
                )
            self.assertIsNone(store.get_event("protected-cut-2"))

    def test_generic_unprotected_event_remains_available(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            payload = {"kind": "ordinary"}
            result = store.append_event(
                {
                    "event_id": "ordinary-1",
                    "event_type": "OrdinaryEvent",
                    "aggregate_type": "ordinary",
                    "aggregate_id": "ordinary-1",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-10-01T00:00:00Z",
                }
            )
            self.assertTrue(result.inserted)


if __name__ == "__main__":
    unittest.main()
