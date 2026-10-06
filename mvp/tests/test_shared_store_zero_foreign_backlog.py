"""Regression for shared-store publication ownership beyond the global outbox page.

A large Host UI backlog is foreign to ZERO runtime authority. It must neither be
acknowledged by ZERO nor hide a later ZERO-owned publication from its delivery
and checkpoint preflight.
"""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    AutonomousRuntimeCheckpointError,
    deliver_autonomous_owned_publications,
)


_HOST_TIME = "2026-10-05T00:00:00Z"
_ZERO_TIME = "2026-10-05T00:00:01Z"


def _event(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    committed_at: str,
    environment: str | None = None,
    host_id: str | None = None,
):
    payload = {}
    event = {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed_at,
    }
    if environment is not None:
        event["environment"] = environment
    if host_id is not None:
        event["host_id"] = host_id
    return event


class SharedStoreZeroForeignBacklogTests(unittest.TestCase):
    def test_foreign_host_backlog_larger_than_global_page_does_not_hide_zero_publication(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")

            # Keep all foreign publications strictly before the ZERO row in the
            # canonical pending ordering. The current global page is 1000 rows.
            store._now = lambda: _HOST_TIME
            for index in range(1000):
                host_event = _event(
                    aggregate_type="HOST_CONTROL",
                    aggregate_id=f"foreign-host-{index}",
                    event_type="COMMAND_ACCEPTED",
                    committed_at=_HOST_TIME,
                )
                store.append_event(host_event, outbox_topic="ui.host-events")

            run_id = "zero-after-large-host-backlog"
            store._now = lambda: _ZERO_TIME
            zero_event = _event(
                aggregate_type="canonical_autonomous_simulation",
                aggregate_id=run_id,
                event_type="AutonomousEpisodeProgressed",
                committed_at=_ZERO_TIME,
            )
            store.append_event(
                zero_event,
                outbox_topic="autotrade.simulation.events",
            )

            self.assertEqual(store.pending_outbox_count(), 1001)

            # ZERO owns only its exact simulation publication. Foreign Host UI
            # rows must not consume the ownership scan's bounded window.
            deliver_autonomous_owned_publications(store, run_id=run_id)

            zero_state = store.outbox_delivery_state(
                zero_event["event_id"],
                topic="autotrade.simulation.events",
            )
            self.assertIsNotNone(zero_state)
            self.assertTrue(zero_state["delivered"])
            self.assertEqual(store.pending_outbox_count(), 1000)
            self.assertTrue(
                all(
                    item["topic"] == "ui.host-events"
                    for item in store.pending_outbox(limit=1000)
                )
            )

    def test_foreign_same_component_type_backlog_cannot_consume_zero_scan_budget(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")

            # These rows deliberately use a ZERO-relevant aggregate type, so an
            # aggregate-type-only SQL filter still fails. PAPER + production-host
            # makes them foreign to the exact local SIMULATION authority.
            store._now = lambda: _HOST_TIME
            for index in range(1000):
                foreign = _event(
                    aggregate_type="submission_attempt",
                    aggregate_id=f"foreign-paper-submission-{index}",
                    event_type="SubmissionPrepared",
                    committed_at=_HOST_TIME,
                    environment="PAPER",
                    host_id="production-host",
                )
                store.append_event(
                    foreign,
                    outbox_topic="autotrade.submission.events",
                )

            run_id = "zero-after-same-type-backlog"
            store._now = lambda: _ZERO_TIME
            zero_event = _event(
                aggregate_type="canonical_autonomous_simulation",
                aggregate_id=run_id,
                event_type="AutonomousEpisodeProgressed",
                committed_at=_ZERO_TIME,
                environment="SIMULATION",
                host_id="local-simulation",
            )
            store.append_event(
                zero_event,
                outbox_topic="autotrade.simulation.events",
            )

            deliver_autonomous_owned_publications(store, run_id=run_id)

            zero_state = store.outbox_delivery_state(zero_event["event_id"])
            self.assertIsNotNone(zero_state)
            self.assertTrue(zero_state["delivered"])
            self.assertEqual(store.pending_outbox_count(), 1000)
            pending = store.pending_outbox(limit=1000)
            self.assertTrue(
                all(item["topic"] == "autotrade.submission.events" for item in pending)
            )
            self.assertTrue(
                all(
                    item["payload"].get("environment") == "PAPER"
                    and item["payload"].get("host_id") == "production-host"
                    for item in pending
                )
            )

    def test_owned_loop_event_with_wrong_topic_is_not_false_acknowledged(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "zero-misrouted-loop-publication"
            zero_event = _event(
                aggregate_type="canonical_autonomous_simulation",
                aggregate_id=run_id,
                event_type="AutonomousEpisodeProgressed",
                committed_at=_ZERO_TIME,
                environment="SIMULATION",
                host_id="local-simulation",
            )
            # Canonical loop writes use autotrade.simulation.events. A durable
            # row routed elsewhere is not successful ZERO publication evidence.
            store.append_event(zero_event, outbox_topic="ui.host-events")

            with self.assertRaisesRegex(
                AutonomousRuntimeCheckpointError,
                "topic|routing|publication",
            ):
                deliver_autonomous_owned_publications(store, run_id=run_id)

            state = store.outbox_delivery_state(zero_event["event_id"])
            self.assertIsNotNone(state)
            self.assertEqual(state["topic"], "ui.host-events")
            self.assertFalse(state["delivered"])
            self.assertEqual(store.pending_outbox_count(), 1)

    def test_delivered_owned_loop_event_with_wrong_topic_still_fails_authority_check(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            run_id = "zero-delivered-misrouted-loop-publication"
            zero_event = _event(
                aggregate_type="canonical_autonomous_simulation",
                aggregate_id=run_id,
                event_type="AutonomousEpisodeProgressed",
                committed_at=_ZERO_TIME,
                environment="SIMULATION",
                host_id="local-simulation",
            )
            store.append_event(zero_event, outbox_topic="ui.host-events")
            state = store.outbox_delivery_state(zero_event["event_id"])
            self.assertIsNotNone(state)
            store.mark_outbox_delivered(
                state["outbox_id"],
                expected_envelope_hash=state["envelope_hash"],
            )

            # Delivery status is not authority: a misrouted ZERO loop row must
            # remain invalid evidence during later recovery/checkpoint scans.
            with self.assertRaisesRegex(
                AutonomousRuntimeCheckpointError,
                "topic|routing|publication",
            ):
                deliver_autonomous_owned_publications(store, run_id=run_id)

            delivered = store.outbox_delivery_state(zero_event["event_id"])
            self.assertIsNotNone(delivered)
            self.assertTrue(delivered["delivered"])
            self.assertEqual(delivered["topic"], "ui.host-events")
            self.assertEqual(store.pending_outbox_count(), 0)


if __name__ == "__main__":
    unittest.main()
