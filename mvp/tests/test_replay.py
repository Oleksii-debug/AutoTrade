import unittest

from mvp.autotrade_mvp.replay import (
    CausalReplay,
    ReplayCheckpoint,
    ReplayError,
    ReplayEvent,
    dataset_digest,
)


def event(sequence, available_at, value, source_version="v1"):
    return ReplayEvent(
        sequence=sequence,
        available_at=available_at,
        source_version=source_version,
        payload={"value": value},
    )


class CausalReplayTests(unittest.TestCase):
    def test_future_data_is_not_exposed_before_availability(self):
        replay = CausalReplay(
            [
                event(1, "2026-09-24T10:00:00Z", 1),
                event(2, "2026-09-24T10:01:00Z", 2),
            ],
            start_at="2026-09-24T09:59:00Z",
        )

        first = replay.advance_to("2026-09-24T10:00:30Z")
        self.assertEqual([item.sequence for item in first], [1])
        self.assertIsNone(replay.peek_next())
        self.assertEqual(replay.remaining(), 1)

    def test_same_time_events_use_sequence_as_deterministic_tiebreaker(self):
        replay = CausalReplay(
            [
                event(4, "2026-09-24T10:00:00Z", 4),
                event(2, "2026-09-24T10:00:00Z", 2),
                event(3, "2026-09-24T10:00:00Z", 3),
            ],
            start_at="2026-09-24T09:59:00Z",
        )
        visible = replay.advance_to("2026-09-24T10:00:00Z")
        self.assertEqual([item.sequence for item in visible], [2, 3, 4])

    def test_checkpoint_resume_produces_identical_suffix(self):
        events = [
            event(1, "2026-09-24T10:00:00Z", 1),
            event(2, "2026-09-24T10:01:00Z", 2),
            event(3, "2026-09-24T10:02:00Z", 3),
        ]
        first = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        first.advance_to("2026-09-24T10:00:30Z")
        checkpoint = first.checkpoint()

        resumed = CausalReplay(
            events,
            start_at="2026-09-24T09:59:00Z",
            checkpoint=checkpoint,
        )
        suffix_a = first.advance_to("2026-09-24T10:03:00Z")
        suffix_b = resumed.advance_to("2026-09-24T10:03:00Z")
        self.assertEqual(
            [(item.sequence, dict(item.payload)) for item in suffix_a],
            [(item.sequence, dict(item.payload)) for item in suffix_b],
        )

    def test_checkpoint_rejects_changed_dataset(self):
        original = [event(1, "2026-09-24T10:00:00Z", 1)]
        replay = CausalReplay(original, start_at="2026-09-24T09:59:00Z")
        checkpoint = replay.checkpoint()

        changed = [event(1, "2026-09-24T10:00:00Z", 99)]
        with self.assertRaisesRegex(ReplayError, "dataset digest"):
            CausalReplay(
                changed,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
            )

    def test_checkpoint_cannot_skip_event_from_the_future(self):
        events = [
            event(1, "2026-09-24T10:00:00Z", 1),
            event(2, "2026-09-24T10:01:00Z", 2),
        ]
        checkpoint = ReplayCheckpoint(
            dataset_digest=dataset_digest(events),
            cursor=2,
            clock="2026-09-24T10:00:00Z",
        )
        with self.assertRaisesRegex(ReplayError, "unavailable at checkpoint clock"):
            CausalReplay(
                events,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
            )

    def test_checkpoint_may_preserve_visible_but_not_yet_consumed_event(self):
        events = [
            event(1, "2026-09-24T10:00:00Z", 1),
            event(2, "2026-09-24T10:01:00Z", 2),
        ]
        checkpoint = ReplayCheckpoint(
            dataset_digest=dataset_digest(events),
            cursor=0,
            clock="2026-09-24T10:00:00Z",
        )
        replay = CausalReplay(
            events,
            start_at="2026-09-24T09:59:00Z",
            checkpoint=checkpoint,
        )
        self.assertEqual(
            [item.sequence for item in replay.advance_to("2026-09-24T10:00:00Z")],
            [1],
        )

    def test_clock_cannot_move_backwards(self):
        replay = CausalReplay(
            [event(1, "2026-09-24T10:00:00Z", 1)],
            start_at="2026-09-24T09:59:00Z",
        )
        replay.advance_to("2026-09-24T10:00:00Z")
        with self.assertRaisesRegex(ReplayError, "cannot move backwards"):
            replay.advance_to("2026-09-24T09:59:59Z")

    def test_duplicate_sequence_is_rejected(self):
        with self.assertRaisesRegex(ReplayError, "sequence values must be unique"):
            CausalReplay(
                [
                    event(1, "2026-09-24T10:00:00Z", 1),
                    event(1, "2026-09-24T10:01:00Z", 2),
                ],
                start_at="2026-09-24T09:59:00Z",
            )

    def test_payload_is_frozen_and_digest_is_stable(self):
        payload = {"nested": {"a": 1}}
        item = ReplayEvent(
            sequence=1,
            available_at="2026-09-24T10:00:00Z",
            source_version="v1",
            payload=payload,
        )
        payload["nested"]["a"] = 9
        self.assertEqual(item.payload["nested"]["a"], 1)

        one = dataset_digest([item])
        two = dataset_digest(
            [
                ReplayEvent(
                    sequence=1,
                    available_at="2026-09-24T10:00:00Z",
                    source_version="v1",
                    payload={"nested": {"a": 1}},
                )
            ]
        )
        self.assertEqual(one, two)

    def test_invalid_checkpoint_cursor_is_rejected(self):
        events = [event(1, "2026-09-24T10:00:00Z", 1)]
        checkpoint = ReplayCheckpoint(
            dataset_digest=dataset_digest(events),
            cursor=2,
            clock="2026-09-24T10:00:00Z",
        )
        with self.assertRaisesRegex(ReplayError, "cursor exceeds"):
            CausalReplay(
                events,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
            )


if __name__ == "__main__":
    unittest.main()
