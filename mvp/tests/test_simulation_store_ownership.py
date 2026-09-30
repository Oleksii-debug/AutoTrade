"""Whole-store ownership regressions for canonical simulation."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.simulation_session as simulation_session
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_session import run_canonical_simulation


BUY = ["100", "101", "103"]
SOURCE_SHA = "a" * 40
NOW = "2026-09-30T12:00:00Z"
LATER = "2026-09-30T12:00:01Z"


def foreign_event() -> dict:
    payload = {"foreign": True}
    return {
        "event_id": "foreign-sequence-one",
        "event_type": "ForeignBusinessState",
        "schema_version": "1.0.0",
        "aggregate_type": "foreign_business_state",
        "aggregate_id": "foreign",
        "aggregate_version": "1",
        "host_id": "foreign-host",
        "owner_epoch": "1",
        "environment": "SIMULATION",
        "occurred_at": NOW,
        "observed_at": NOW,
        "committed_at": NOW,
        "correlation_id": "foreign-correlation",
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


class SimulationStoreOwnershipTests(unittest.TestCase):
    def test_fresh_session_owner_is_first_global_business_authority(self):
        with TemporaryDirectory() as directory:
            result = run_canonical_simulation(
                BUY, directory, episode_id="owner-first", source_sha=SOURCE_SHA,
                now=NOW,
            )
            store = JournalStore(Path(directory) / "journal.sqlite3")
            owners = store.load_events(
                simulation_session._OWNER_AGGREGATE_TYPE,
                simulation_session._OWNER_AGGREGATE_ID,
            )

            self.assertEqual(len(owners), 1)
            owner = owners[0]
            self.assertEqual(owner["event_type"], "SimulationSessionOwned")
            self.assertEqual(owner["aggregate_version"], 1)
            self.assertEqual(owner["journal_sequence"], 1)
            self.assertEqual(owner["payload"]["session_id"], result["session_id"])
            self.assertEqual(owner["payload"]["creation_id"], result["creation_id"])
            self.assertEqual(owner["payload"]["creation_identity"]["evidence_time"], NOW)
            self.assertEqual(
                owner["payload"]["creation_identity"]["transport_fault_profile"],
                "NONE",
            )

    def test_owner_only_crash_restarts_from_persisted_creation_cut(self):
        with TemporaryDirectory() as directory:
            original_claim = simulation_session._claim_owner

            def crash_after_owner(*args, **kwargs):
                owner = original_claim(*args, **kwargs)
                self.assertEqual(owner["journal_sequence"], 1)
                raise RuntimeError("crash-after-owner")

            with patch.object(simulation_session, "_claim_owner", crash_after_owner):
                with self.assertRaisesRegex(RuntimeError, "crash-after-owner"):
                    run_canonical_simulation(
                        BUY, directory, episode_id="owner-crash",
                        source_sha=SOURCE_SHA, now=NOW, fault_after_send=False,
                    )

            store = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(store.current_journal_sequence(), 1)
            self.assertEqual(
                store.load_events("canonical_simulation_session", "single-episode"),
                [],
            )
            owner = store.load_events(
                simulation_session._OWNER_AGGREGATE_TYPE,
                simulation_session._OWNER_AGGREGATE_ID,
            )[0]

            resumed = run_canonical_simulation(
                BUY, directory, episode_id="owner-crash", source_sha=SOURCE_SHA,
                now=LATER, fault_after_send=True,
            )

            self.assertTrue(resumed["resumed"])
            self.assertEqual(resumed["evidence_time"], NOW)
            self.assertEqual(resumed["session_id"], owner["payload"]["session_id"])
            self.assertEqual(resumed["creation_id"], owner["payload"]["creation_id"])
            self.assertEqual(resumed["new_outbound_requests"], 1)
            self.assertEqual(
                owner["payload"]["creation_identity"]["transport_fault_profile"],
                "NONE",
            )

    def test_command_only_business_state_cannot_be_adopted(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.record_command(
                command_id="foreign-command",
                actor="foreign",
                environment="SIMULATION",
                idempotency_key="foreign-idempotency",
                request={"foreign": True},
                result={"accepted": True},
                state_version=0,
            )
            self.assertEqual(store.current_journal_sequence(), 0)

            with self.assertRaisesRegex(ValueError, "foreign durable business authority"):
                run_canonical_simulation(
                    BUY, directory, episode_id="command-foreign",
                    source_sha=SOURCE_SHA, now=NOW,
                )

            self.assertEqual(
                store.load_events(
                    simulation_session._OWNER_AGGREGATE_TYPE,
                    simulation_session._OWNER_AGGREGATE_ID,
                ),
                [],
            )
            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"), []
            )

    def test_late_matching_owner_at_sequence_two_is_rejected(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            store.append_event(foreign_event())
            protocol, protocol_id = simulation_session._protocol_identity(SOURCE_SHA)
            creation_identity, creation_id = simulation_session._creation_identity(
                NOW, False
            )
            input_hash = payload_digest({
                "schema_version": "1.0.0",
                "prices": BUY,
            })
            session_id = simulation_session._session_identity(
                episode_id="late-owner",
                input_hash=input_hash,
                protocol_id=protocol_id,
                creation_id=creation_id,
            )
            payload = simulation_session._owner_payload(
                episode_id="late-owner",
                input_hash=input_hash,
                session_id=session_id,
                source_sha=SOURCE_SHA,
                protocol_id=protocol_id,
                protocol=protocol,
                creation_id=creation_id,
                creation_identity=creation_identity,
                decision="BUY",
            )
            envelope = {
                "event_id": simulation_session._uuid(
                    "SimulationSessionOwned", session_id
                ),
                "event_type": "SimulationSessionOwned",
                "schema_version": "1.0.0",
                "aggregate_type": simulation_session._OWNER_AGGREGATE_TYPE,
                "aggregate_id": simulation_session._OWNER_AGGREGATE_ID,
                "aggregate_version": "1",
                "host_id": "local-simulation",
                "owner_epoch": "1",
                "environment": "SIMULATION",
                "occurred_at": NOW,
                "observed_at": NOW,
                "committed_at": NOW,
                "correlation_id": simulation_session._uuid(
                    "correlation", session_id
                ),
                "causation_id": None,
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "evidence_refs": [],
            }
            store.append_event(envelope)
            owner = store.load_events(
                simulation_session._OWNER_AGGREGATE_TYPE,
                simulation_session._OWNER_AGGREGATE_ID,
            )[0]
            self.assertEqual(owner["journal_sequence"], 2)

            with self.assertRaisesRegex(ValueError, "not the first store authority"):
                run_canonical_simulation(
                    BUY, directory, episode_id="late-owner",
                    source_sha=SOURCE_SHA, now=LATER,
                )

            self.assertEqual(
                store.load_events_by_aggregate_type("submission_attempt"), []
            )


if __name__ == "__main__":
    unittest.main()
