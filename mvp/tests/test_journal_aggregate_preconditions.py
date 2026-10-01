from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import (
    ExpectedAggregateHead,
    JournalStore,
    payload_digest,
)


def envelope(
    *,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    version: int,
    payload: dict | None = None,
):
    value = {"value": event_id} if payload is None else payload
    return {
        "event_id": event_id,
        "event_type": "AggregatePreconditionTestEvent",
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": value,
        "payload_hash": payload_digest(value),
        "committed_at": "2026-09-30T19:40:00+00:00",
    }


class JournalAggregatePreconditionTests(unittest.TestCase):
    def store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.sqlite3")

    def owner_head(self, store: JournalStore) -> ExpectedAggregateHead:
        event = store.load_events("recovery_owner", "paper:acct")[-1]
        return ExpectedAggregateHead(
            "recovery_owner",
            "paper:acct",
            event["aggregate_version"],
            latest_event_id=event["event_id"],
            latest_payload_hash=event["payload_hash"],
        )

    def test_append_event_accepts_exact_head_while_unrelated_aggregate_advances(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            expected = self.owner_head(store)
            store.append_event(
                envelope(
                    event_id="unrelated-1",
                    aggregate_type="provider_account",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )

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

    def test_append_event_expected_journal_sequence_is_atomic_zero_mutation_fence(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-cut",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            owner = self.owner_head(store)
            cut = store.current_journal_sequence()
            store.append_event(
                envelope(
                    event_id="concurrent-unknown",
                    aggregate_type="submission_attempt",
                    aggregate_id="other-attempt",
                    version=1,
                    payload={"status": "UNKNOWN"},
                )
            )
            before_sequence = store.current_journal_sequence()
            before_outbox = store.pending_outbox_count()

            with self.assertRaisesRegex(
                ValueError,
                "journal sequence changed before append",
            ):
                store.append_event(
                    envelope(
                        event_id="sending-after-stale-cut",
                        aggregate_type="submission_attempt",
                        aggregate_id="attempt-cut",
                        version=1,
                    ),
                    outbox_topic="autotrade.submission.events",
                    aggregate_preconditions=(owner,),
                    expected_journal_sequence=cut,
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(store.pending_outbox_count(), before_outbox)
            self.assertEqual(
                store.load_events("submission_attempt", "attempt-cut"),
                [],
            )

    def test_append_event_stale_head_is_zero_mutation(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            stale = self.owner_head(store)
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

            with self.assertRaisesRegex(ValueError, "aggregate head precondition failed"):
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

    def test_append_event_head_identity_mismatch_is_zero_mutation(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            actual = self.owner_head(store)
            wrong = ExpectedAggregateHead(
                actual.aggregate_type,
                actual.aggregate_id,
                actual.aggregate_version,
                latest_event_id="wrong-event",
                latest_payload_hash=actual.latest_payload_hash,
            )

            with self.assertRaisesRegex(ValueError, "event identity precondition failed"):
                store.append_event(
                    envelope(
                        event_id="sending-wrong-head",
                        aggregate_type="submission_attempt",
                        aggregate_id="attempt-wrong-head",
                        version=1,
                    ),
                    aggregate_preconditions=(wrong,),
                )

            self.assertEqual(
                store.load_events("submission_attempt", "attempt-wrong-head"),
                [],
            )

    def test_empty_head_precondition_requires_aggregate_to_remain_absent(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            empty = ExpectedAggregateHead("recovery_owner", "paper:acct", 0)
            first = store.append_event(
                envelope(
                    event_id="bootstrap-1",
                    aggregate_type="bootstrap",
                    aggregate_id="one",
                    version=1,
                ),
                aggregate_preconditions=(empty,),
            )
            self.assertTrue(first.inserted)

            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            with self.assertRaisesRegex(ValueError, "aggregate head precondition failed"):
                store.append_event(
                    envelope(
                        event_id="bootstrap-2",
                        aggregate_type="bootstrap",
                        aggregate_id="two",
                        version=1,
                    ),
                    aggregate_preconditions=(empty,),
                )
            self.assertEqual(store.load_events("bootstrap", "two"), [])

    def test_exact_append_replay_is_receipt_not_fresh_precondition_acquisition(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            stale = self.owner_head(store)
            candidate = envelope(
                event_id="sending-1",
                aggregate_type="submission_attempt",
                aggregate_id="attempt-1",
                version=1,
            )
            first = store.append_event(
                candidate,
                aggregate_preconditions=(stale,),
            )
            self.assertTrue(first.inserted)

            store.append_event(
                envelope(
                    event_id="owner-2",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=2,
                )
            )
            replay = store.append_event(
                candidate,
                aggregate_preconditions=(stale,),
            )
            self.assertFalse(replay.inserted)
            self.assertEqual(
                [event["event_id"] for event in store.load_events(
                    "submission_attempt", "attempt-1"
                )],
                ["sending-1"],
            )

    def test_commit_command_stale_head_rolls_back_command_events_and_outbox(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            stale = self.owner_head(store)
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

            with self.assertRaisesRegex(ValueError, "aggregate head precondition failed"):
                store.commit_command(
                    command_id="send-command-stale",
                    actor="dispatcher",
                    environment="PAPER",
                    idempotency_key="send-key-stale",
                    request={"attempt_id": "attempt-stale"},
                    result={"status": "SENDING"},
                    state_version=1,
                    events=[
                        (
                            envelope(
                                event_id="sending-command-stale",
                                aggregate_type="submission_attempt",
                                aggregate_id="attempt-stale",
                                version=1,
                            ),
                            "autotrade.submission.events",
                        )
                    ],
                    aggregate_preconditions=(stale,),
                )

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(store.pending_outbox_count(), before_outbox)
            self.assertEqual(
                store.load_events("submission_attempt", "attempt-stale"),
                [],
            )

    def test_commit_command_exact_head_succeeds_despite_unrelated_journal_change(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            store.append_event(
                envelope(
                    event_id="owner-1",
                    aggregate_type="recovery_owner",
                    aggregate_id="paper:acct",
                    version=1,
                )
            )
            expected = self.owner_head(store)
            store.append_event(
                envelope(
                    event_id="unrelated-1",
                    aggregate_type="qualification",
                    aggregate_id="provider-a",
                    version=1,
                )
            )

            result, inserted, appended = store.commit_command(
                command_id="send-command-1",
                actor="dispatcher",
                environment="PAPER",
                idempotency_key="send-key-1",
                request={"attempt_id": "attempt-1"},
                result={"status": "SENDING"},
                state_version=1,
                events=[
                    (
                        envelope(
                            event_id="sending-command-1",
                            aggregate_type="submission_attempt",
                            aggregate_id="attempt-1",
                            version=1,
                        ),
                        "autotrade.submission.events",
                    )
                ],
                aggregate_preconditions=(expected,),
            )

            self.assertTrue(inserted)
            self.assertEqual(result, {"status": "SENDING"})
            self.assertEqual(len(appended), 1)
            self.assertTrue(appended[0].inserted)

    def test_precondition_shape_is_bounded_exact_and_duplicate_free(self):
        with TemporaryDirectory() as directory:
            store = self.store(directory)
            event = envelope(
                event_id="never-written",
                aggregate_type="submission_attempt",
                aggregate_id="never",
                version=1,
            )
            head = ExpectedAggregateHead("recovery_owner", "paper:acct", 0)

            with self.assertRaisesRegex(TypeError, "exact tuple"):
                store.append_event(event, aggregate_preconditions=[head])
            with self.assertRaisesRegex(ValueError, "repeat"):
                store.append_event(event, aggregate_preconditions=(head, head))
            with self.assertRaisesRegex(ValueError, "more than 16"):
                store.append_event(
                    event,
                    aggregate_preconditions=tuple(
                        ExpectedAggregateHead("owner", f"scope-{index}", 0)
                        for index in range(17)
                    ),
                )

            class DerivedHead(ExpectedAggregateHead):
                pass

            with self.assertRaisesRegex(TypeError, "exact ExpectedAggregateHead"):
                store.append_event(
                    event,
                    aggregate_preconditions=(
                        DerivedHead("recovery_owner", "paper:acct", 0),
                    ),
                )

            poisoned = ExpectedAggregateHead("recovery_owner", "paper:acct", 0)
            object.__setattr__(poisoned, "aggregate_version", True)
            with self.assertRaisesRegex(ValueError, "aggregate_version"):
                store.append_event(
                    event,
                    aggregate_preconditions=(poisoned,),
                )

            self.assertEqual(store.load_events("submission_attempt", "never"), [])


if __name__ == "__main__":
    unittest.main()
