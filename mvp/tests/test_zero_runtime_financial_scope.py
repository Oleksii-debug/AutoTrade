"""Causal regressions for ZERO financial-scope checkpoint/publication ownership."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    AutonomousRuntimeCheckpointError,
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


class ZeroRuntimeFinancialScopeTests(unittest.TestCase):
    def test_other_account_simulation_state_is_excluded_from_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "checkpoint-account-owner"
            store.append_event(_start_event(run_id=run_id))
            owned = _event(
                aggregate_type="economic_book",
                aggregate_id="owned-economic",
                event_type="EconomicTransactionBooked",
                payload={
                    "provider_id": "SIMULATED",
                    "account_id": "zero-account",
                    "environment": "SIMULATION",
                },
            )
            foreign = _event(
                aggregate_type="economic_book",
                aggregate_id="foreign-economic",
                event_type="EconomicTransactionBooked",
                payload={
                    "provider_id": "SIMULATED",
                    "account_id": "other-account",
                    "environment": "SIMULATION",
                },
            )
            store.append_event(owned)
            before_cut, _loop, before_authority = _runtime_scope_snapshot(
                store, run_id=run_id
            )
            store.append_event(foreign)
            after_cut, _loop, after_authority = _runtime_scope_snapshot(
                store, run_id=run_id
            )
            self.assertEqual(after_cut, before_cut)
            self.assertEqual(after_authority, before_authority)
            self.assertEqual(
                [item["event_id"] for item in after_authority["economic_book"]],
                [owned["event_id"]],
            )

    def test_other_account_same_topic_publication_is_not_acknowledged(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "positive-publication-owner"
            store.append_event(_start_event(run_id=run_id))
            foreign = _event(
                aggregate_type="economic_book",
                aggregate_id="other-account-book",
                event_type="EconomicTransactionBooked",
                payload={
                    "provider_id": "SIMULATED",
                    "account_id": "other-account",
                    "environment": "SIMULATION",
                },
            )
            store.append_event(foreign, outbox_topic="autotrade.economic.events")
            deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(foreign["event_id"])
            self.assertIsNotNone(state)
            self.assertFalse(state["delivered"])

    def test_hostless_owned_economic_publication_uses_run_scope(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "hostless-owned-economic"
            store.append_event(_start_event(run_id=run_id))
            owned = _event(
                aggregate_type="economic_book",
                aggregate_id="zero-account-book",
                event_type="EconomicTransactionBooked",
                payload={
                    "provider_id": "SIMULATED",
                    "account_id": "zero-account",
                    "environment": "SIMULATION",
                },
            )
            store.append_event(owned, outbox_topic="autotrade.economic.events")
            deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(owned["event_id"])
            self.assertIsNotNone(state)
            self.assertTrue(state["delivered"])

    def test_owned_component_wrong_topic_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "misrouted-economic"
            store.append_event(_start_event(run_id=run_id))
            owned = _event(
                aggregate_type="economic_book",
                aggregate_id="zero-account-book",
                event_type="EconomicTransactionBooked",
                payload={
                    "provider_id": "SIMULATED",
                    "account_id": "zero-account",
                    "environment": "SIMULATION",
                },
            )
            store.append_event(owned, outbox_topic="autotrade.submission.events")
            with self.assertRaisesRegex(
                AutonomousRuntimeCheckpointError,
                "topic|routing|publication",
            ):
                deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(owned["event_id"])
            self.assertIsNotNone(state)
            self.assertFalse(state["delivered"])

    def test_no_outbox_component_cannot_gain_publication_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "risk-outbox-rejected"
            store.append_event(_start_event(run_id=run_id))
            risk = _event(
                aggregate_type="risk_decision",
                aggregate_id="risk:sha256:" + "1" * 64,
                event_type="RiskDecisionRecorded",
                payload={
                    "account_id": "zero-account",
                    "environment": "SIMULATION",
                },
            )
            store.append_event(risk, outbox_topic="autotrade.economic.events")
            with self.assertRaisesRegex(
                AutonomousRuntimeCheckpointError,
                "outbox|publication|topic|routing",
            ):
                deliver_autonomous_owned_publications(store, run_id=run_id)
            state = store.outbox_delivery_state(risk["event_id"])
            self.assertIsNotNone(state)
            self.assertFalse(state["delivered"])


if __name__ == "__main__":
    unittest.main()
