"""Regression tests for exact ZERO checkpoint component ownership.

The physical JournalStore may contain PAPER/LIVE or other-host events for the
same aggregate types used by autonomous ZERO. Those foreign facts must not
change the ZERO runtime checkpoint cut, while genuine local SIMULATION facts
must remain authority-bearing.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_runtime_checkpoint import _runtime_scope_snapshot


_TIME = "2026-10-05T00:00:00Z"


def _component_event(
    *,
    aggregate_id: str,
    environment: str | None = None,
    host_id: str | None = None,
    payload: dict | None = None,
):
    body = {} if payload is None else dict(payload)
    event = {
        "event_id": str(uuid4()),
        "event_type": "SubmissionPrepared",
        "aggregate_type": "submission_attempt",
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "payload": body,
        "payload_hash": payload_digest(body),
        "committed_at": _TIME,
    }
    if environment is not None:
        event["environment"] = environment
    if host_id is not None:
        event["host_id"] = host_id
    return event


class ZeroRuntimeScopeOwnershipTests(unittest.TestCase):
    def test_foreign_same_component_type_does_not_change_zero_runtime_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "zero-runtime-scope"
            owned = _component_event(
                aggregate_id="owned-submission",
                environment="SIMULATION",
                host_id="local-simulation",
            )
            store.append_event(owned)

            before_cut, before_loop, before_authority = _runtime_scope_snapshot(
                store,
                run_id=run_id,
            )

            foreign = _component_event(
                aggregate_id="foreign-paper-submission",
                environment="PAPER",
                host_id="production-host",
            )
            store.append_event(foreign)

            after_cut, after_loop, after_authority = _runtime_scope_snapshot(
                store,
                run_id=run_id,
            )

            self.assertEqual(after_cut, before_cut)
            self.assertEqual(after_loop, before_loop)
            self.assertEqual(after_authority, before_authority)
            submissions = after_authority["submission_attempt"]
            self.assertEqual([item["event_id"] for item in submissions], [owned["event_id"]])
            self.assertNotIn(
                foreign["event_id"],
                {item["event_id"] for item in submissions},
            )

    def test_payload_scope_fallback_remains_owned_when_envelope_scope_is_absent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            owned = _component_event(
                aggregate_id="payload-scoped-submission",
                payload={
                    "environment": "SIMULATION",
                    "host_id": "local-simulation",
                },
            )
            store.append_event(owned)

            cut, _loop, authority = _runtime_scope_snapshot(
                store,
                run_id="payload-scope-run",
            )

            self.assertEqual(cut["authority_event_counts"]["submission_attempt"], 1)
            self.assertEqual(
                [item["event_id"] for item in authority["submission_attempt"]],
                [owned["event_id"]],
            )

    def test_envelope_foreign_scope_cannot_be_overridden_by_payload(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            foreign = _component_event(
                aggregate_id="foreign-envelope-submission",
                environment="PAPER",
                host_id="production-host",
                payload={
                    "environment": "SIMULATION",
                    "host_id": "local-simulation",
                },
            )
            store.append_event(foreign)

            cut, _loop, authority = _runtime_scope_snapshot(
                store,
                run_id="envelope-authority-run",
            )

            self.assertEqual(cut["authority_event_counts"]["submission_attempt"], 0)
            self.assertEqual(authority["submission_attempt"], [])

    def test_genuine_zero_component_mutation_changes_runtime_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "owned-mutation-run"
            before_cut, _before_loop, _before_authority = _runtime_scope_snapshot(
                store,
                run_id=run_id,
            )

            owned = _component_event(
                aggregate_id="new-owned-submission",
                environment="SIMULATION",
                host_id="local-simulation",
            )
            store.append_event(owned)

            after_cut, _after_loop, after_authority = _runtime_scope_snapshot(
                store,
                run_id=run_id,
            )

            self.assertNotEqual(after_cut, before_cut)
            self.assertEqual(after_cut["authority_event_counts"]["submission_attempt"], 1)
            self.assertEqual(
                [item["event_id"] for item in after_authority["submission_attempt"]],
                [owned["event_id"]],
            )


if __name__ == "__main__":
    unittest.main()
