from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import (
    JournalStore,
    ProtectedJournalWriter,
    payload_digest,
)


AGGREGATE_TYPE = "authenticated_provider_read"
WRITER_ID = "sha256:" + "a" * 64
OTHER_WRITER_ID = "sha256:" + "b" * 64
NAMESPACE_VERSION = "v1"


def event(
    *,
    event_id: str = "provider-read:attempt-1:prepared",
    aggregate_id: str = "provider-read:attempt-1",
    version: int = 1,
) -> dict[str, object]:
    payload = {"kind": "prepared", "query_digest": "sha256:" + "c" * 64}
    return {
        "event_id": event_id,
        "event_type": "AuthenticatedReadPrepared",
        "aggregate_type": AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-01T01:00:00+00:00",
    }


class ProtectedJournalWriterTests(unittest.TestCase):
    def select(self, store: JournalStore):
        return store.select_protected_writer(
            aggregate_type=AGGREGATE_TYPE,
            namespace_version=NAMESPACE_VERSION,
            writer_authority_id=WRITER_ID,
        )

    def test_invalid_selection_is_zero_mutation(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            with self.assertRaisesRegex(
                TypeError, "canonical non-empty text"
            ):
                store.select_protected_writer(
                    aggregate_type=" " + AGGREGATE_TYPE,
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=WRITER_ID,
                )

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM protected_event_namespaces"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM events"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_generic_append_cannot_write_protected_namespace(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            writer = self.select(store)

            with self.assertRaisesRegex(
                ValueError, "generic append_event cannot write"
            ):
                store.append_event(event())
            self.assertEqual(store.load_events(AGGREGATE_TYPE, "provider-read:attempt-1"), [])

            result = store.append_protected_event(writer, event())
            self.assertTrue(result.inserted)
            loaded = store.load_protected_events(
                writer, "provider-read:attempt-1"
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["writer_authority_id"], WRITER_ID)

            # Even exact idempotent replay may not downgrade through the generic
            # API once the namespace is protected.
            with self.assertRaisesRegex(
                ValueError, "generic append_event cannot write"
            ):
                store.append_event(event())

    def test_generic_command_batch_cannot_bypass_protected_writer(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            self.select(store)

            with self.assertRaisesRegex(
                ValueError, "generic commit_command cannot write"
            ):
                store.commit_command(
                    command_id="cmd-protected-bypass",
                    actor="provider-origin",
                    environment="PAPER",
                    idempotency_key="protected-bypass",
                    request={"action": "WRITE_PROTECTED_EVENT"},
                    result={"status": "MUST_NOT_COMMIT"},
                    state_version=0,
                    events=[(event(), None)],
                )

            connection = sqlite3.connect(path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM command_dedupe"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM events"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

    def test_internal_core_entrypoints_are_not_instance_writer_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            writer = self.select(store)

            with self.assertRaisesRegex(
                RuntimeError, "selected ProtectedJournalWriter"
            ):
                store._append_protected_event(
                    event(),
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=WRITER_ID,
                )
            with self.assertRaisesRegex(
                RuntimeError, "not a public writer authority"
            ):
                store._append_event(
                    event(),
                    outbox_topic=None,
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=WRITER_ID,
                )
            with self.assertRaisesRegex(
                RuntimeError, "product selection"
            ):
                store._register_protected_event_namespace(
                    aggregate_type=AGGREGATE_TYPE,
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=WRITER_ID,
                )
            with self.assertRaisesRegex(
                RuntimeError, "recovery requires selected"
            ):
                store._load_protected_events(
                    aggregate_type=AGGREGATE_TYPE,
                    aggregate_id="provider-read:attempt-1",
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=WRITER_ID,
                )

            # The selected public capability path remains functional.
            store.append_protected_event(writer, event())
            loaded = store.load_protected_events(
                writer, "provider-read:attempt-1"
            )
            self.assertEqual(len(loaded), 1)

    def test_directly_constructed_capability_is_not_selected_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.select(store)
            forged = ProtectedJournalWriter(
                store_identity=store.store_identity,
                aggregate_type=AGGREGATE_TYPE,
                namespace_version=NAMESPACE_VERSION,
                writer_authority_id=WRITER_ID,
            )
            with self.assertRaisesRegex(
                RuntimeError, "was not selected by this JournalStore"
            ):
                store.append_protected_event(forged, event())
            self.assertEqual(
                store.load_events(AGGREGATE_TYPE, "provider-read:attempt-1"),
                [],
            )

    def test_capability_is_bound_to_exact_store_instance_and_scope(self) -> None:
        with TemporaryDirectory() as directory:
            first = JournalStore(Path(directory) / "first.sqlite3")
            second = JournalStore(Path(directory) / "second.sqlite3")
            writer = self.select(first)
            self.select(second)

            with self.assertRaisesRegex(
                RuntimeError, "was not selected by this JournalStore"
            ):
                second.append_protected_event(writer, event())

            object.__setattr__(writer, "namespace_version", "v2")
            with self.assertRaisesRegex(
                RuntimeError, "scope or store generation changed"
            ):
                first.append_protected_event(writer, event())
            self.assertEqual(
                first.load_events(AGGREGATE_TYPE, "provider-read:attempt-1"),
                [],
            )

    def test_conflicting_writer_selection_fails_closed(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.select(store)
            with self.assertRaisesRegex(
                ValueError, "different writer authority"
            ):
                store.select_protected_writer(
                    aggregate_type=AGGREGATE_TYPE,
                    namespace_version=NAMESPACE_VERSION,
                    writer_authority_id=OTHER_WRITER_ID,
                )

    def test_existing_generic_history_cannot_be_promoted(self) -> None:
        class V9JournalStore(JournalStore):
            SCHEMA_VERSION = 9

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            legacy = V9JournalStore(path)
            legacy.append_event(event())
            self.assertEqual(legacy.current_schema_version(), 9)

            upgraded = JournalStore(path)
            self.assertEqual(
                upgraded.current_schema_version(),
                JournalStore.SCHEMA_VERSION,
            )
            with self.assertRaisesRegex(
                ValueError, "existing unqualified journal history"
            ):
                self.select(upgraded)

            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT writer_authority_id FROM events WHERE event_id = ?",
                    ("provider-read:attempt-1:prepared",),
                ).fetchone()
                protected_count = connection.execute(
                    "SELECT COUNT(*) FROM protected_event_namespaces"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertIsNotNone(row)
            self.assertIsNone(row[0])
            self.assertEqual(protected_count, 0)

    def test_restart_requires_new_process_selection_but_rehydrates_history(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first = JournalStore(path)
            first_writer = self.select(first)
            first.append_protected_event(first_writer, event())

            restarted = JournalStore(path)
            restarted_writer = self.select(restarted)
            with self.assertRaisesRegex(
                RuntimeError, "was not selected by this JournalStore"
            ):
                restarted.load_protected_events(
                    first_writer, "provider-read:attempt-1"
                )
            loaded = restarted.load_protected_events(
                restarted_writer, "provider-read:attempt-1"
            )
            self.assertEqual(
                [item["event_id"] for item in loaded],
                ["provider-read:attempt-1:prepared"],
            )
            self.assertEqual(loaded[0]["writer_authority_id"], WRITER_ID)

    def test_sqlite_backup_preserves_writer_metadata_without_generation_alias(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            backup_path = Path(directory) / "backup.sqlite3"
            store = JournalStore(path)
            writer = self.select(store)
            store.append_protected_event(writer, event())

            source = sqlite3.connect(path)
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()
                source.close()

            restored = JournalStore(backup_path)
            restored_writer = self.select(restored)
            loaded = restored.load_protected_events(
                restored_writer, "provider-read:attempt-1"
            )
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["writer_authority_id"], WRITER_ID)

            # A capability selected for the source physical generation is not a
            # capability for the copied database, even with identical rows.
            with self.assertRaisesRegex(
                RuntimeError, "was not selected by this JournalStore"
            ):
                restored.load_protected_events(
                    writer, "provider-read:attempt-1"
                )

    def test_writer_metadata_tamper_is_rejected_by_protected_loader(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            writer = self.select(store)
            store.append_protected_event(writer, event())

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET writer_authority_id = ? WHERE event_id = ?",
                    (
                        OTHER_WRITER_ID,
                        "provider-read:attempt-1:prepared",
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(
                ValueError, "unqualified writer provenance"
            ):
                store.load_protected_events(
                    writer, "provider-read:attempt-1"
                )


if __name__ == "__main__":
    unittest.main()
