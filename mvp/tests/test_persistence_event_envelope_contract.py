"""Canonical EventEnvelope admission regressions for WP-05."""

from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def canonical_event() -> dict[str, object]:
    payload = {"kind": "fill", "quantity": "1"}
    return {
        "event_id": "11111111-1111-4111-8111-111111111111",
        "event_type": "ExecutionFillObserved",
        "schema_version": "1.0.0",
        "aggregate_type": "account",
        "aggregate_id": "paper-1",
        "aggregate_version": "1",
        "host_id": "host-local-1",
        "owner_epoch": "1",
        "environment": "SIMULATION",
        "occurred_at": "2026-10-06T13:30:00Z",
        "observed_at": "2026-10-06T13:30:01Z",
        "committed_at": "2026-10-06T13:30:02Z",
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class CanonicalEventEnvelopeAdmissionTests(unittest.TestCase):
    def test_claimed_canonical_event_envelope_is_accepted_exactly(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            envelope = canonical_event()
            result = store.append_event(envelope, outbox_topic="events")
            self.assertTrue(result.inserted)
            self.assertEqual(store.get_event(envelope["event_id"]), {
                **envelope,
                "journal_sequence": 1,
            })

    def test_claimed_event_envelope_rejects_missing_and_unknown_fields(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            missing = canonical_event()
            missing.pop("owner_epoch")
            with self.assertRaisesRegex(ValueError, "missing required fields"):
                JournalStore(path).append_event(missing)

            extra = canonical_event()
            extra["unexpected"] = "forbidden"
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                JournalStore(path).append_event(extra)

            self.assertEqual(JournalStore(path).current_journal_sequence(), 0)

    def test_claimed_event_envelope_rejects_noncanonical_scalar_contracts(self):
        cases = {
            "event_id": "not-a-uuid",
            "aggregate_version": "01",
            "owner_epoch": "+1",
            "environment": "simulation",
            "occurred_at": "2026-10-06T13:30:00+00:00",
            "correlation_id": "not-a-uuid",
            "payload_hash": "sha256:BAD",
        }
        for field, bad_value in cases.items():
            with self.subTest(field=field), TemporaryDirectory() as directory:
                envelope = canonical_event()
                envelope[field] = bad_value
                with self.assertRaises(ValueError):
                    JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_claimed_event_envelope_rejects_malformed_evidence_ref(self):
        with TemporaryDirectory() as directory:
            envelope = canonical_event()
            envelope["evidence_refs"] = [
                {
                    "artifact_id": "33333333-3333-4333-8333-333333333333",
                    "sha256": "sha256:" + "0" * 64,
                    "observed_at": "2026-10-06T13:30:00Z",
                    "unexpected": "forbidden",
                }
            ]
            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_commit_command_validates_each_claimed_canonical_event_before_mutation(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            envelope = canonical_event()
            envelope["host_id"] = ""
            with self.assertRaisesRegex(ValueError, "host_id"):
                store.commit_command(
                    command_id="44444444-4444-4444-8444-444444444444",
                    actor="test",
                    environment="SIMULATION",
                    idempotency_key="canonical-event-batch",
                    request={"action": "TEST"},
                    result={"status": "REJECTED"},
                    state_version=0,
                    events=[(envelope, "events")],
                )
            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(store.pending_outbox(), [])

    def test_legacy_internal_envelope_without_schema_claim_remains_migratable(self):
        with TemporaryDirectory() as directory:
            payload = {"kind": "legacy"}
            legacy = {
                "event_id": "legacy-event-id",
                "event_type": "LegacyInternalFact",
                "aggregate_type": "legacy",
                "aggregate_id": "one",
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-06T13:30:00Z",
            }
            store = JournalStore(f"{directory}/journal.sqlite3")
            self.assertTrue(store.append_event(legacy).inserted)
            self.assertEqual(store.current_journal_sequence(), 1)


if __name__ == "__main__":
    unittest.main()
