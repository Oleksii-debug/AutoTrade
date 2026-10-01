from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import (
    AggregatePreconditionFailed,
    ExpectedAggregateHead,
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
)


def envelope(
    *,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
):
    payload = {"event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": "JournalAuthorityAggregatePreconditionEvent",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-01T00:00:00+00:00",
    }


def exact_head(
    store: JournalStore,
    *,
    aggregate_type: str,
    aggregate_id: str,
) -> ExpectedAggregateHead:
    events = store.load_events(aggregate_type, aggregate_id)
    if not events:
        return ExpectedAggregateHead(aggregate_type, aggregate_id, 0)
    latest = events[-1]
    return ExpectedAggregateHead(
        aggregate_type,
        aggregate_id,
        latest["aggregate_version"],
        latest_event_id=latest["event_id"],
        latest_payload_hash=latest["payload_hash"],
    )


class JournalAuthorityAggregatePreconditionTests(unittest.TestCase):
    def store(self, directory: str, name: str) -> JournalStore:
        return JournalStore(Path(directory) / name)

    def test_exact_aggregate_head_composes_with_physical_store_authority(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory, "journal.sqlite3")
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            expected = exact_head(
                store,
                aggregate_type="recovery_owner",
                aggregate_id="paper:acct",
            )

            with journal_store_authority_scope(store, store.store_identity):
                result = store.append_event(
                    envelope(
                        event_id="sending-1",
                        aggregate_type="submission_attempt",
                        aggregate_id="attempt-1",
                        version=1,
                    ),
                    outbox_topic="autotrade.submission.events",
                    aggregate_preconditions=(expected,),
                )

            self.assertTrue(result.inserted)
            self.assertEqual(
                [event["event_id"] for event in store.load_events(
                    "submission_attempt", "attempt-1"
                )],
                ["sending-1"],
            )

    def test_stale_aggregate_head_is_zero_mutation_inside_store_authority_scope(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory, "journal.sqlite3")
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            stale = exact_head(
                store,
                aggregate_type="recovery_owner",
                aggregate_id="paper:acct",
            )
            store.append_event(
                envelope(
                    event_id="owner-2",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=2,
                )
            )
            before_sequence = store.current_journal_sequence()
            before_outbox = store.pending_outbox_count()

            with journal_store_authority_scope(store, store.store_identity):
                with self.assertRaises(AggregatePreconditionFailed):
                    store.append_event(
                        envelope(
                            event_id="sending-stale",
                            aggregate_type="submission_attempt",
                            aggregate_id="attempt-stale",
                            version=1,
                        ),
                        outbox_topic="autotrade.submission.events",
                        aggregate_preconditions=(stale,),
                    )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(store.pending_outbox_count(), before_outbox)
            self.assertEqual(
                store.load_events("submission_attempt", "attempt-stale"),
                [],
            )

    def test_nested_other_store_scope_restores_outer_store_authority(self):
        with TemporaryDirectory() as directory:
            first = self.store(directory, "first.sqlite3")
            second = self.store(directory, "second.sqlite3")

            first.append_event(
                envelope(
                    event_id="first-owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:first",
                    version=1,
                )
            )
            second.append_event(
                envelope(
                    event_id="second-owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:second",
                    version=1,
                )
            )
            first_head = exact_head(
                first,
                aggregate_type="recovery_owner",
                aggregate_id="paper:first",
            )
            second_head = exact_head(
                second,
                aggregate_type="recovery_owner",
                aggregate_id="paper:second",
            )

            with journal_store_authority_scope(first, first.store_identity):
                with journal_store_authority_scope(second, second.store_identity):
                    second_result = second.append_event(
                        envelope(
                            event_id="second-send-1",
                            aggregate_type="submission_attempt",
                            aggregate_id="second-attempt",
                            version=1,
                        ),
                        aggregate_preconditions=(second_head,),
                    )
                first_result = first.append_event(
                    envelope(
                        event_id="first-send-1",
                        aggregate_type="submission_attempt",
                        aggregate_id="first-attempt",
                        version=1,
                    ),
                    aggregate_preconditions=(first_head,),
                )

            self.assertTrue(second_result.inserted)
            self.assertTrue(first_result.inserted)
            self.assertEqual(
                [event["event_id"] for event in first.load_events(
                    "submission_attempt", "first-attempt"
                )],
                ["first-send-1"],
            )
            self.assertEqual(
                [event["event_id"] for event in second.load_events(
                    "submission_attempt", "second-attempt"
                )],
                ["second-send-1"],
            )


if __name__ == "__main__":
    unittest.main()
