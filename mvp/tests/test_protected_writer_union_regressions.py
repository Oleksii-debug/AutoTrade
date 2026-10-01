import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


_COMMITTED_AT = "2026-10-01T00:00:00Z"


def _event(*, event_id: str, aggregate_type: str, aggregate_id: str = "agg-1"):
    payload = {"value": "protected"}
    return {
        "event_id": event_id,
        "event_type": "ProtectedWriterRegressionEvent",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": _COMMITTED_AT,
    }


class ProtectedWriterUnionRegressionTests(unittest.TestCase):
    def test_registered_protected_namespace_rejects_direct_sql_insert(self):
        """The durable DB boundary, not only Python dispatch, must fence writers."""
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            store.bind_protected_writer(
                aggregate_type="protected-direct",
                namespace="protected-direct:v1",
                writer_authority_id="issuer:direct:v1",
            )

            connection = sqlite3.connect(path)
            try:
                failure = None
                try:
                    connection.execute(
                        """
                        INSERT INTO events(
                            event_id, event_type, aggregate_type, aggregate_id,
                            aggregate_version, payload_json, payload_hash,
                            committed_at, envelope_json, envelope_hash,
                            journal_sequence, writer_namespace,
                            writer_authority_id, writer_authority_hash,
                            writer_provenance_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            "forged-direct",
                            "ProtectedWriterRegressionEvent",
                            "protected-direct",
                            "agg-1",
                            1,
                            '{"value":"forged"}',
                            "sha256:" + "1" * 64,
                            _COMMITTED_AT,
                            '{"forged":true}',
                            "sha256:" + "2" * 64,
                            1,
                            None,
                            None,
                            None,
                            None,
                        ),
                    )
                    connection.commit()
                except sqlite3.DatabaseError as error:
                    failure = error
                    connection.rollback()
                self.assertIsNotNone(
                    failure,
                    "protected aggregate accepted a writer-blind direct SQL event",
                )
            finally:
                connection.close()

            self.assertIsNone(store.get_event("forged-direct"))

    def test_issued_capability_scope_cannot_be_rewritten_to_sibling_namespace(self):
        """Possessing capability A must not permit escalation to registered scope B."""
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            capability_a = store.bind_protected_writer(
                aggregate_type="protected-a",
                namespace="protected-a:v1",
                writer_authority_id="issuer:a:v1",
            )
            capability_b = store.bind_protected_writer(
                aggregate_type="protected-b",
                namespace="protected-b:v1",
                writer_authority_id="issuer:b:v1",
            )

            for name in (
                "aggregate_type",
                "namespace",
                "writer_authority_id",
                "writer_authority_hash",
            ):
                object.__setattr__(capability_a, name, getattr(capability_b, name))

            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                store.append_protected_event(
                    capability_a,
                    _event(
                        event_id="scope-substitution",
                        aggregate_type="protected-b",
                    ),
                )
            self.assertIsNone(store.get_event("scope-substitution"))

    def test_protected_event_cannot_publish_through_writer_blind_outbox(self):
        """Outbox publication must carry/verify provenance or fail before mutation."""
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            capability = store.bind_protected_writer(
                aggregate_type="protected-outbox",
                namespace="protected-outbox:v1",
                writer_authority_id="issuer:outbox:v1",
            )

            with self.assertRaises((TypeError, ValueError, RuntimeError)):
                store.append_protected_event(
                    capability,
                    _event(
                        event_id="protected-outbox-event",
                        aggregate_type="protected-outbox",
                    ),
                    outbox_topic="financial.protected",
                )

            self.assertIsNone(store.get_event("protected-outbox-event"))
            connection = sqlite3.connect(path)
            try:
                count = connection.execute(
                    "SELECT COUNT(*) FROM outbox WHERE event_id = ?",
                    ("protected-outbox-event",),
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
