from datetime import datetime, timezone
import unittest

from autotrade_research.evaluation.replay.feeder import (
    CausalDataset,
    CausalEvent,
    CausalFeeder,
    CausalReplayError,
    FeederCheckpoint,
)


MANIFEST = "sha256:" + ("1" * 64)


def event(
    event_id,
    *,
    kind="TRADE",
    event_time="2026-01-01T10:00:00Z",
    available_at="2026-01-01T10:00:00Z",
    priority=10,
    sequence=0,
    payload=None,
):
    return CausalEvent.create(
        event_id=event_id,
        kind=kind,
        event_time=event_time,
        available_at=available_at,
        source_priority=priority,
        source_sequence=sequence,
        payload=payload or {"price": "100.25"},
    )


class CausalFeederTests(unittest.TestCase):
    def test_future_event_is_invisible_until_evidenced_availability(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event(
                    "news-1",
                    kind="NEWS",
                    event_time="2026-01-01T10:00:00Z",
                    available_at="2026-01-01T10:02:00Z",
                    payload={"headline": "released later"},
                )
            ],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T09:59:00Z")

        self.assertEqual(feeder.advance_to("2026-01-01T10:01:59Z"), ())
        self.assertEqual(feeder.view().events, ())
        published = feeder.advance_to("2026-01-01T10:02:00Z")
        self.assertEqual([item.event_id for item in published], ["news-1"])
        self.assertEqual([item.event_id for item in feeder.view().events], ["news-1"])

    def test_same_time_order_is_stable_and_independent_of_input_order(self):
        raw = [
            event("z", priority=20, sequence=0),
            event("b", priority=10, sequence=2),
            event("a", priority=10, sequence=2),
            event("c", priority=10, sequence=1),
        ]
        first = CausalDataset.create(manifest_sha256=MANIFEST, events=raw)
        second = CausalDataset.create(manifest_sha256=MANIFEST, events=reversed(raw))

        self.assertEqual(first.dataset_sha256, second.dataset_sha256)
        self.assertEqual([item.event_id for item in first.events], ["c", "a", "b", "z"])
        feeder = CausalFeeder(first, start_time="2026-01-01T09:00:00Z")
        self.assertEqual(
            [item.event_id for item in feeder.advance_next_time()],
            ["c", "a", "b", "z"],
        )

    def test_checkpoint_resume_matches_uninterrupted_publication(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event("one", available_at="2026-01-01T10:00:00Z", sequence=1),
                event("two", available_at="2026-01-01T10:01:00Z", sequence=2),
                event("three", available_at="2026-01-01T10:02:00Z", sequence=3),
            ],
        )
        baseline = CausalFeeder(dataset, start_time="2026-01-01T09:00:00Z")
        baseline.advance_to("2026-01-01T10:01:00Z")
        checkpoint = baseline.checkpoint()

        uninterrupted_tail = baseline.advance_to("2026-01-01T10:03:00Z")
        uninterrupted_view = baseline.view()

        resumed = CausalFeeder.restore(dataset=dataset, checkpoint=checkpoint)
        resumed_tail = resumed.advance_to("2026-01-01T10:03:00Z")
        resumed_view = resumed.view()

        self.assertEqual(resumed_tail, uninterrupted_tail)
        self.assertEqual(resumed_view, uninterrupted_view)
        self.assertEqual(resumed.checkpoint(), baseline.checkpoint())

    def test_bar_close_cannot_be_published_before_final_availability(self):
        bar = event(
            "bar-1",
            kind="BAR_CLOSE",
            event_time="2026-01-01T10:00:00Z",
            available_at="2026-01-01T10:00:01Z",
            payload={"close": "101.5", "interval": "1m"},
        )
        dataset = CausalDataset.create(manifest_sha256=MANIFEST, events=[bar])
        feeder = CausalFeeder(dataset, start_time="2026-01-01T09:59:00Z")

        self.assertEqual(feeder.advance_to("2026-01-01T10:00:00Z"), ())
        self.assertEqual(
            [item.event_id for item in feeder.advance_to("2026-01-01T10:00:01Z")],
            ["bar-1"],
        )
        with self.assertRaises(CausalReplayError):
            event(
                "impossible-bar",
                kind="BAR_CLOSE",
                event_time="2026-01-01T10:00:00Z",
                available_at="2026-01-01T09:59:59Z",
            )

    def test_payload_rejects_binary_float_and_is_immutable(self):
        with self.assertRaises(TypeError):
            event("float-price", payload={"price": 100.25})

        safe = event("safe", payload={"price": "100.25", "levels": [{"size": "2"}]})
        with self.assertRaises(TypeError):
            safe.payload["price"] = "99"
        with self.assertRaises(TypeError):
            safe.payload["levels"][0]["size"] = "3"

    def test_restore_rejects_wrong_dataset_or_causally_invalid_cursor(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event("one", available_at="2026-01-01T10:00:00Z", sequence=1),
                event("two", available_at="2026-01-01T10:01:00Z", sequence=2),
            ],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T09:00:00Z")
        feeder.advance_to("2026-01-01T10:00:00Z")
        checkpoint = feeder.checkpoint()

        changed = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event("one", available_at="2026-01-01T10:00:00Z", sequence=1),
                event("two-changed", available_at="2026-01-01T10:01:00Z", sequence=2),
            ],
        )
        with self.assertRaisesRegex(CausalReplayError, "dataset digest"):
            CausalFeeder.restore(dataset=changed, checkpoint=checkpoint)

        invalid = FeederCheckpoint(
            schema_version=1,
            manifest_sha256=dataset.manifest_sha256,
            dataset_sha256=dataset.dataset_sha256,
            simulation_time=datetime(2026, 1, 1, 10, 1, tzinfo=timezone.utc),
            cursor=1,
            published_prefix_sha256=checkpoint.published_prefix_sha256,
        )
        with self.assertRaisesRegex(CausalReplayError, "omits an event"):
            CausalFeeder.restore(dataset=dataset, checkpoint=invalid)

    def test_start_view_contains_complete_causal_prefix_only(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event(
                    "past",
                    event_time="2026-01-01T09:58:00Z",
                    available_at="2026-01-01T09:59:00Z",
                    sequence=1,
                ),
                event("future", available_at="2026-01-01T10:01:00Z", sequence=2),
            ],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        self.assertEqual([item.event_id for item in feeder.view().events], ["past"])
        self.assertEqual(feeder.published_count, 1)

    def test_clock_cannot_move_backwards(self):
        dataset = CausalDataset.create(manifest_sha256=MANIFEST, events=[])
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        with self.assertRaisesRegex(CausalReplayError, "backwards"):
            feeder.advance_to("2026-01-01T09:59:59Z")


if __name__ == "__main__":
    unittest.main()
