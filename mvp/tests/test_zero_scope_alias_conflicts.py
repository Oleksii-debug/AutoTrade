"""Adversarial regressions for contradictory ZERO durable scope aliases."""
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
    aggregate_type,
    aggregate_id,
    event_type,
    payload=None,
    environment=None,
    host_id=None,
):
    body = {} if payload is None else dict(payload)
    event = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
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


def _start(store, run_id):
    event = _event(
        aggregate_type="canonical_autonomous_simulation",
        aggregate_id=run_id,
        event_type="AutonomousSimulationStarted",
        environment="SIMULATION",
        host_id="local-simulation",
        payload={
            "protocol": {
                "financial_scope": {
                    "account_id": "zero-account",
                    "provider_id": "SIMULATED",
                    "environment": "SIMULATION",
                }
            }
        },
    )
    store.append_event(event, outbox_topic="autotrade.simulation.events")
    return event


class ZeroScopeAliasConflictTests(unittest.TestCase):
    def _assert_foreign_publication_remains_pending(self, event, topic):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "alias-conflict-run"
            _start(store, run_id)
            store.append_event(event, outbox_topic=topic)
            before, _, _ = _runtime_scope_snapshot(store, run_id=run_id)
            deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(event["event_id"])
            self.assertIsNotNone(state)
            self.assertFalse(state["delivered"])
            after, _, authority = _runtime_scope_snapshot(store, run_id=run_id)
            self.assertEqual(after, before)
            self.assertNotIn(
                event["event_id"],
                {
                    item["event_id"]
                    for items in authority.values()
                    for item in items
                },
            )

    def test_local_envelope_cannot_mask_foreign_payload_environment(self):
        event = _event(
            aggregate_type="economic_book",
            aggregate_id="environment-conflict",
            event_type="EconomicTransactionBooked",
            environment="SIMULATION",
            host_id="local-simulation",
            payload={
                "environment": "PAPER",
                "account_id": "zero-account",
                "provider_id": "SIMULATED",
            },
        )
        self._assert_foreign_publication_remains_pending(
            event,
            "autotrade.economic.events",
        )

    def test_direct_account_cannot_mask_foreign_nested_account(self):
        event = _event(
            aggregate_type="economic_book",
            aggregate_id="account-conflict",
            event_type="EconomicTransactionBooked",
            payload={
                "environment": "SIMULATION",
                "account_id": "zero-account",
                "provider_id": "SIMULATED",
                "scope": {"account_id": "other-account"},
            },
        )
        self._assert_foreign_publication_remains_pending(
            event,
            "autotrade.economic.events",
        )

    def test_provider_id_cannot_mask_conflicting_submission_provider_alias(self):
        event = _event(
            aggregate_type="submission_attempt",
            aggregate_id="provider-conflict",
            event_type="SubmissionPrepared",
            environment="SIMULATION",
            host_id="local-mvp",
            payload={
                "environment": "SIMULATION",
                "account_id": "zero-account",
                "provider_id": "SIMULATED",
                "provider": "FOREIGN",
            },
        )
        self._assert_foreign_publication_remains_pending(
            event,
            "autotrade.submission.events",
        )

    def test_local_envelope_host_cannot_mask_foreign_payload_host(self):
        event = _event(
            aggregate_type="submission_attempt",
            aggregate_id="host-conflict",
            event_type="SubmissionPrepared",
            environment="SIMULATION",
            host_id="local-mvp",
            payload={
                "environment": "SIMULATION",
                "host_id": "production-host",
                "account_id": "zero-account",
                "provider": "SIMULATED",
            },
        )
        self._assert_foreign_publication_remains_pending(
            event,
            "autotrade.submission.events",
        )

    def test_consistent_duplicate_scope_aliases_retain_positive_ownership(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "consistent-alias-run"
            _start(store, run_id)
            event = _event(
                aggregate_type="economic_book",
                aggregate_id="consistent-aliases",
                event_type="EconomicTransactionBooked",
                environment="SIMULATION",
                host_id="local-simulation",
                payload={
                    "environment": "SIMULATION",
                    "host_id": "local-simulation",
                    "account_id": "zero-account",
                    "provider_id": "SIMULATED",
                    "scope": {
                        "account_id": "zero-account",
                        "provider_id": "SIMULATED",
                        "environment": "SIMULATION",
                    },
                    "request": {
                        "account_id": "zero-account",
                        "provider_id": "SIMULATED",
                        "environment": "SIMULATION",
                    },
                },
            )
            store.append_event(event, outbox_topic="autotrade.economic.events")
            deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(event["event_id"])
            self.assertIsNotNone(state)
            self.assertTrue(state["delivered"])


if __name__ == "__main__":
    unittest.main()
