import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    AutonomousRuntimeCheckpointError,
    deliver_autonomous_owned_publications,
)


_TIME = "2026-10-05T00:00:00Z"
_TOPIC = "autotrade.simulation.events"


def _loop_event(*, run_id: str, event_type: str, version: int, payload: dict):
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "aggregate_type": "canonical_autonomous_simulation",
        "aggregate_id": run_id,
        "aggregate_version": str(version),
        "environment": "SIMULATION",
        "host_id": "local-simulation",
        "owner_epoch": "1",
        "occurred_at": _TIME,
        "observed_at": _TIME,
        "committed_at": _TIME,
        "correlation_id": str(uuid4()),
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }


def _start_event(run_id: str):
    return _loop_event(
        run_id=run_id,
        event_type="AutonomousSimulationStarted",
        version=1,
        payload={
            "protocol_digest": "sha256:" + "0" * 64,
            "protocol": {
                "financial_scope": {
                    "account_id": "zero-account",
                    "provider_id": "SIMULATED",
                    "environment": "SIMULATION",
                    "instrument_version": "TEST@1",
                    "instrument_id": "00000000-0000-0000-0000-000000000001",
                }
            },
            "provider_state": {},
        },
    )


class ZeroMissingLoopPublicationTests(unittest.TestCase):
    def test_missing_nonterminal_loop_publication_fails_closed_before_any_ack(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            run_id = "missing-progress-publication"
            started = _start_event(run_id)
            progressed = _loop_event(
                run_id=run_id,
                event_type="AutonomousEpisodeProgressed",
                version=2,
                payload={
                    "episode": 1,
                    "protocol_digest": "sha256:" + "0" * 64,
                    "provider_state": {},
                },
            )
            store.append_event(started, outbox_topic=_TOPIC)
            store.append_event(progressed, outbox_topic=_TOPIC)

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "DELETE FROM outbox WHERE event_id = ?",
                    (progressed["event_id"],),
                )
                connection.commit()
            finally:
                connection.close()

            self.assertIsNone(store.outbox_delivery_state(progressed["event_id"]))
            started_state = store.outbox_delivery_state(started["event_id"])
            self.assertIsNotNone(started_state)
            self.assertFalse(started_state["delivered"])

            with self.assertRaisesRegex(
                AutonomousRuntimeCheckpointError,
                "loop.*publication.*missing|publication.*missing",
            ):
                deliver_autonomous_owned_publications(store, run_id=run_id)

            # Detection is preflight: a later missing loop publication must not
            # cause an earlier publication to be acknowledged partially.
            started_state = store.outbox_delivery_state(started["event_id"])
            self.assertIsNotNone(started_state)
            self.assertFalse(started_state["delivered"])


if __name__ == "__main__":
    unittest.main()
