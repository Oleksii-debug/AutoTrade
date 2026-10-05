"""Focused regressions for ZERO aggregate-scope inheritance.

Checkpoint inclusion is fail-closed for ambiguous component history, while
publication ACK requires positive ownership. Submission lifecycle events are
special because durable events after SubmissionPrepared intentionally omit some
financial-scope fields and must inherit only from their exact aggregate.
"""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    _runtime_scope_snapshot,
    deliver_autonomous_owned_publications,
)


_TIME = "2026-10-05T00:00:00Z"


def _event(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    aggregate_version: int = 1,
    payload: dict | None = None,
    environment: str | None = None,
    host_id: str | None = None,
):
    body = {} if payload is None else dict(payload)
    event = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(aggregate_version),
        "payload": body,
        "payload_hash": payload_digest(body),
        "committed_at": _TIME,
    }
    if environment is not None:
        event["environment"] = environment
    if host_id is not None:
        event["host_id"] = host_id
    return event


def _start_event(*, run_id: str):
    return _event(
        aggregate_type="canonical_autonomous_simulation",
        aggregate_id=run_id,
        event_type="AutonomousSimulationStarted",
        environment="SIMULATION",
        host_id="local-simulation",
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


def _append_start(store: JournalStore, *, run_id: str):
    event = _start_event(run_id=run_id)
    store.append_event(event, outbox_topic="autotrade.simulation.events")
    return event


def _submission_prepared(
    *,
    aggregate_id: str,
    account_id: str,
    provider: str = "SIMULATED",
    host_id: str = "local-mvp",
):
    return _event(
        aggregate_type="submission_attempt",
        aggregate_id=aggregate_id,
        event_type="SubmissionPrepared",
        aggregate_version=1,
        environment="SIMULATION",
        host_id=host_id,
        payload={
            "attempt_id": aggregate_id + ":attempt",
            "provider": provider,
            "account_id": account_id,
            "environment": "SIMULATION",
        },
    )


def _submission_sending(
    *,
    aggregate_id: str,
    aggregate_version: int = 2,
    host_id: str = "local-mvp",
):
    return _event(
        aggregate_type="submission_attempt",
        aggregate_id=aggregate_id,
        event_type="SubmissionSending",
        aggregate_version=aggregate_version,
        environment="SIMULATION",
        host_id=host_id,
        payload={"client_order_id": aggregate_id + ":client"},
    )


class ZeroRuntimeAggregateScopeTests(unittest.TestCase):
    def test_owned_sparse_submission_lifecycle_is_checkpoint_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "owned-checkpoint-aggregate"
            _append_start(store, run_id=run_id)
            aggregate_id = "submission-attempt:owned-checkpoint"
            prepared = _submission_prepared(
                aggregate_id=aggregate_id,
                account_id="zero-account",
            )
            sending = _submission_sending(aggregate_id=aggregate_id)
            store.append_event(prepared)
            store.append_event(sending)

            cut, _loop, authority = _runtime_scope_snapshot(store, run_id=run_id)

            self.assertEqual(cut["authority_event_counts"]["submission_attempt"], 2)
            self.assertEqual(
                [event["event_id"] for event in authority["submission_attempt"]],
                [prepared["event_id"], sending["event_id"]],
            )

    def test_foreign_prepared_scope_excludes_sparse_lifecycle_from_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "foreign-checkpoint-aggregate"
            _append_start(store, run_id=run_id)
            aggregate_id = "submission-attempt:foreign-checkpoint"
            prepared = _submission_prepared(
                aggregate_id=aggregate_id,
                account_id="other-account",
            )
            sending = _submission_sending(aggregate_id=aggregate_id)
            store.append_event(prepared)
            store.append_event(sending)

            cut, _loop, authority = _runtime_scope_snapshot(store, run_id=run_id)

            self.assertEqual(cut["authority_event_counts"]["submission_attempt"], 0)
            self.assertEqual(authority["submission_attempt"], [])

    def test_provider_alias_conflict_blocks_entire_submission_publication(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "provider-conflict-aggregate"
            _append_start(store, run_id=run_id)
            aggregate_id = "submission-attempt:provider-conflict"
            prepared = _submission_prepared(
                aggregate_id=aggregate_id,
                account_id="zero-account",
                provider="OTHER_PROVIDER",
            )
            sending = _submission_sending(aggregate_id=aggregate_id)
            for event in (prepared, sending):
                store.append_event(event, outbox_topic="autotrade.submission.events")

            deliver_autonomous_owned_publications(store, run_id=run_id)

            start_state = store.outbox_delivery_state(
                store.load_events("canonical_autonomous_simulation", run_id)[0]["event_id"]
            )
            self.assertIsNotNone(start_state)
            self.assertTrue(start_state["delivered"])
            for event in (prepared, sending):
                state = store.outbox_delivery_state(event["event_id"])
                self.assertIsNotNone(state)
                self.assertFalse(state["delivered"])

    def test_nonlocal_later_host_blocks_entire_submission_publication(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "host-conflict-aggregate"
            _append_start(store, run_id=run_id)
            aggregate_id = "submission-attempt:host-conflict"
            prepared = _submission_prepared(
                aggregate_id=aggregate_id,
                account_id="zero-account",
            )
            sending = _submission_sending(
                aggregate_id=aggregate_id,
                host_id="production-host",
            )
            for event in (prepared, sending):
                store.append_event(event, outbox_topic="autotrade.submission.events")

            deliver_autonomous_owned_publications(store, run_id=run_id)

            for event in (prepared, sending):
                state = store.outbox_delivery_state(event["event_id"])
                self.assertIsNotNone(state)
                self.assertFalse(state["delivered"])


if __name__ == "__main__":
    unittest.main()
