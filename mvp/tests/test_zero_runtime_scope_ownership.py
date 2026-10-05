"""Regression tests for exact ZERO checkpoint and publication ownership.

The physical JournalStore may contain PAPER/LIVE, other-host, or other-account
events for the same aggregate types used by autonomous ZERO. Foreign facts must
not change the ZERO runtime checkpoint cut, and ZERO must never acknowledge a
foreign publication merely because it uses a canonical component topic.
"""
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


def _component_event(
    *,
    aggregate_id: str,
    environment: str | None = None,
    host_id: str | None = None,
    payload: dict | None = None,
):
    return _event(
        aggregate_type="submission_attempt",
        aggregate_id=aggregate_id,
        event_type="SubmissionPrepared",
        payload=payload,
        environment=environment,
        host_id=host_id,
    )


def _start_event(
    *,
    run_id: str,
    account_id: str = "zero-account",
    provider_id: str = "SIMULATED",
):
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
                    "account_id": account_id,
                    "provider_id": provider_id,
                    "environment": "SIMULATION",
                    "instrument_version": "TEST@1",
                    "instrument_id": "00000000-0000-0000-0000-000000000001",
                }
            },
            "provider_state": {},
        },
    )


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

    def test_hostless_simulation_authority_policy_remains_checkpoint_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            policy = _event(
                aggregate_type="authority_state",
                aggregate_id="canonical",
                event_type="AuthorityPolicyRegistered",
                payload={
                    "policy_id": "zero-policy",
                    "account_id": "zero-account",
                    "environments": ["SIMULATION"],
                },
            )
            store.append_event(policy)

            cut, _loop, authority = _runtime_scope_snapshot(
                store,
                run_id="hostless-authority-run",
            )

            self.assertEqual(cut["authority_event_counts"]["authority_state"], 1)
            self.assertEqual(
                [item["event_id"] for item in authority["authority_state"]],
                [policy["event_id"]],
            )

    def test_other_account_simulation_publication_is_never_acknowledged(self):
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

    def test_hostless_owned_economic_publication_uses_run_financial_scope(self):
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

    def test_owned_economic_publication_with_wrong_topic_is_rejected(self):
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

    def test_admitted_authority_publication_uses_financial_ready_topic(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "admission-publication"
            store.append_event(_start_event(run_id=run_id))
            admission = _event(
                aggregate_type="authority_state",
                aggregate_id="canonical",
                event_type="AuthorityAdmissionRecorded",
                payload={
                    "account_id": "zero-account",
                    "environment": "SIMULATION",
                    "outcome": "ADMITTED",
                },
            )
            store.append_event(admission, outbox_topic="financial.admission.ready")

            deliver_autonomous_owned_publications(store, run_id=run_id)

            state = store.outbox_delivery_state(admission["event_id"])
            self.assertIsNotNone(state)
            self.assertTrue(state["delivered"])


if __name__ == "__main__":
    unittest.main()
