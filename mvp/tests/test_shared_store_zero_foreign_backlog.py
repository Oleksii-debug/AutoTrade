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
    deliver_autonomous_owned_publications,
)


_HOST_TIME = "2026-10-05T00:00:00Z"
_ZERO_TIME = "2026-10-05T00:00:01Z"


def _event(*, aggregate_type: str, aggregate_id: str, event_type: str, committed_at: str):
    payload = {}
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": committed_at,
    }


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


if __name__ == "__main__":
    unittest.main()
