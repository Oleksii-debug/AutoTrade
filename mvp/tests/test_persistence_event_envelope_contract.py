"""Canonical EventEnvelope admission regressions for WP-05."""

from __future__ import annotations

from hashlib import sha256
import json
import sqlite3
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
            persisted = store.get_event(envelope["event_id"])
            self.assertIsNotNone(persisted)
            expected = dict(envelope)
            expected["aggregate_version"] = 1
            expected["journal_sequence"] = 1
            self.assertEqual(persisted, expected)

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

    def test_claimed_event_envelope_rejects_noncanonical_uuid_lexical_forms(self):
        canonical_uuid = "11111111-1111-4111-8111-111111111111"
        noncanonical = (
            "{" + canonical_uuid + "}",
            "urn:uuid:" + canonical_uuid,
            canonical_uuid.replace("-", ""),
        )
        for field in ("event_id", "correlation_id", "causation_id"):
            for bad_value in noncanonical:
                with self.subTest(field=field, value=bad_value), TemporaryDirectory() as directory:
                    envelope = canonical_event()
                    envelope[field] = bad_value
                    with self.assertRaisesRegex(ValueError, "UUID string"):
                        JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_claimed_event_envelope_accepts_uppercase_hyphenated_uuid(self):
        with TemporaryDirectory() as directory:
            envelope = canonical_event()
            envelope["event_id"] = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
            envelope["correlation_id"] = "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB"
            envelope["causation_id"] = "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC"
            self.assertTrue(
                JournalStore(f"{directory}/journal.sqlite3").append_event(envelope).inserted
            )

    def test_evidence_ref_rejects_noncanonical_uuid_lexical_form(self):
        with TemporaryDirectory() as directory:
            envelope = canonical_event()
            envelope["evidence_refs"] = [
                {
                    "artifact_id": "{33333333-3333-4333-8333-333333333333}",
                    "sha256": "sha256:" + "0" * 64,
                    "observed_at": "2026-10-06T13:30:00Z",
                }
            ]
            with self.assertRaisesRegex(ValueError, "EvidenceRef artifact_id"):
                JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_evidence_ref_rejects_noncanonical_uri_lexical_forms(self):
        invalid = (
            "https://exa mple.test/evidence",
            "https://example.test/%ZZ",
            "scheme:\\ncontrol",
        )
        for source_uri in invalid:
            with self.subTest(source_uri=source_uri), TemporaryDirectory() as directory:
                envelope = canonical_event()
                envelope["evidence_refs"] = [
                    {
                        "artifact_id": "33333333-3333-4333-8333-333333333333",
                        "sha256": "sha256:" + "0" * 64,
                        "observed_at": "2026-10-06T13:30:00Z",
                        "source_uri": source_uri,
                    }
                ]
                with self.assertRaisesRegex(ValueError, "absolute URI"):
                    JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_evidence_ref_accepts_rfc3986_absolute_uri_forms(self):
        valid = (
            "https://example.test/evidence%20bundle?q=1#cut",
            "urn:isbn:0451450523",
            "file:///C:/Program%20Files/AutoTrade/evidence.json",
        )
        for source_uri in valid:
            with self.subTest(source_uri=source_uri), TemporaryDirectory() as directory:
                envelope = canonical_event()
                envelope["event_id"] = {
                    "https://example.test/evidence%20bundle?q=1#cut": "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                    "urn:isbn:0451450523": "BBBBBBBB-BBBB-4BBB-8BBB-BBBBBBBBBBBB",
                    "file:///C:/Program%20Files/AutoTrade/evidence.json": "CCCCCCCC-CCCC-4CCC-8CCC-CCCCCCCCCCCC",
                }[source_uri]
                envelope["evidence_refs"] = [
                    {
                        "artifact_id": "33333333-3333-4333-8333-333333333333",
                        "sha256": "sha256:" + "0" * 64,
                        "observed_at": "2026-10-06T13:30:00Z",
                        "source_uri": source_uri,
                    }
                ]
                self.assertTrue(
                    JournalStore(f"{directory}/journal.sqlite3").append_event(envelope).inserted
                )

    def test_claimed_event_envelope_accepts_schema_valid_lowercase_time_separator(self):
        with TemporaryDirectory() as directory:
            envelope = canonical_event()
            envelope["occurred_at"] = "2026-10-06t13:30:00Z"
            envelope["observed_at"] = "2026-10-06t13:30:01Z"
            envelope["committed_at"] = "2026-10-06t13:30:02Z"
            self.assertTrue(
                JournalStore(f"{directory}/journal.sqlite3").append_event(envelope).inserted
            )

    def test_claimed_event_envelope_still_rejects_lowercase_terminal_z(self):
        with TemporaryDirectory() as directory:
            envelope = canonical_event()
            envelope["occurred_at"] = "2026-10-06T13:30:00z"
            with self.assertRaisesRegex(ValueError, "ending Z"):
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

    def test_claimed_event_envelope_rejects_null_for_nonnullable_optional_fields(self):
        for field in (
            "provider_id",
            "account_id",
            "provider_event_id",
            "source_resolution",
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                envelope = canonical_event()
                envelope[field] = None
                with self.assertRaisesRegex(ValueError, field):
                    JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_evidence_ref_rejects_null_for_nonnullable_optional_fields(self):
        for field in ("source_uri", "rights_id"):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                envelope = canonical_event()
                envelope["evidence_refs"] = [
                    {
                        "artifact_id": "33333333-3333-4333-8333-333333333333",
                        "sha256": "sha256:" + "0" * 64,
                        "observed_at": "2026-10-06T13:30:00Z",
                        field: None,
                    }
                ]
                with self.assertRaisesRegex(ValueError, field):
                    JournalStore(f"{directory}/journal.sqlite3").append_event(envelope)

    def test_claimed_event_envelope_is_schema_revalidated_on_read(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            envelope = canonical_event()
            store.append_event(envelope)

            tampered = dict(envelope)
            tampered["unexpected"] = "rehash-does-not-make-schema-valid"
            raw = json.dumps(
                tampered,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE events SET envelope_json = ?, envelope_hash = ? "
                    "WHERE event_id = ?",
                    (raw, digest, envelope["event_id"]),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(ValueError, "unsupported fields"):
                JournalStore(path).get_event(envelope["event_id"])

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
