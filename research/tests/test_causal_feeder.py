from datetime import datetime, timezone
from itertools import permutations
import unittest

from autotrade_research.evaluation.replay.feeder import (
    CausalDataView,
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
    ingested_at=None,
    priority=10,
    sequence=0,
    payload=None,
):
    return CausalEvent.create(
        event_id=event_id,
        kind=kind,
        event_time=event_time,
        available_at=available_at,
        ingested_at=ingested_at or available_at,
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
        self.assertEqual(
            first.ordering_policy_version,
            "available_at/source_priority/source_sequence/event_id:v1",
        )
        self.assertEqual([item.event_id for item in first.events], ["c", "a", "b", "z"])
        self.assertTrue(all(item.schema_version == 1 for item in first.events))
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

    def test_dataset_identity_rejects_noncanonical_uppercase_digest(self):
        with self.assertRaisesRegex(CausalReplayError, "lowercase"):
            CausalDataset.create(
                manifest_sha256="sha256:" + ("A" * 64),
                events=[],
            )

        valid = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[],
        )
        record = CausalFeeder(
            valid,
            start_time="2026-01-01T10:00:00Z",
        ).checkpoint().to_record()
        with self.assertRaisesRegex(CausalReplayError, "lowercase"):
            FeederCheckpoint.from_record(
                {
                    **record,
                    "dataset_sha256": record["dataset_sha256"].upper(),
                }
            )

    def test_direct_dataset_construction_cannot_forge_identity_or_order(self):
        a = event("a", priority=10, sequence=1)
        b = event("b", priority=10, sequence=2)
        valid = CausalDataset.create(manifest_sha256=MANIFEST, events=[a, b])

        with self.assertRaisesRegex(CausalReplayError, "does not match"):
            CausalDataset(
                manifest_sha256=valid.manifest_sha256,
                events=valid.events,
                dataset_sha256="sha256:" + ("2" * 64),
            )
        with self.assertRaisesRegex(CausalReplayError, "deterministic causal order"):
            CausalDataset(
                manifest_sha256=valid.manifest_sha256,
                events=tuple(reversed(valid.events)),
                dataset_sha256=valid.dataset_sha256,
            )
        with self.assertRaisesRegex(CausalReplayError, "duplicate event_id"):
            duplicated = (a, a)
            CausalDataset(
                manifest_sha256=valid.manifest_sha256,
                events=duplicated,
                dataset_sha256="sha256:" + ("3" * 64),
            )

    def test_checkpoint_record_round_trip_is_strict_and_canonical(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[event("one", sequence=1)],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        checkpoint = feeder.checkpoint()
        record = checkpoint.to_record()

        self.assertEqual(FeederCheckpoint.from_record(record), checkpoint)
        self.assertEqual(record["simulation_time"], "2026-01-01T10:00:00Z")

        with self.assertRaisesRegex(CausalReplayError, "keys mismatch"):
            FeederCheckpoint.from_record({**record, "future_cursor": 99})
        with self.assertRaises((TypeError, CausalReplayError)):
            FeederCheckpoint.from_record({**record, "cursor": 1.0})
        with self.assertRaisesRegex(CausalReplayError, "schema_version"):
            FeederCheckpoint.from_record({**record, "schema_version": True})

    def test_all_input_permutations_have_one_order_and_dataset_identity(self):
        raw = [
            event("d", priority=20, sequence=2),
            event("a", priority=10, sequence=2),
            event("c", priority=10, sequence=1),
            event("b", priority=10, sequence=2),
        ]
        expected = CausalDataset.create(manifest_sha256=MANIFEST, events=raw)
        expected_ids = [item.event_id for item in expected.events]
        for candidate in permutations(raw):
            dataset = CausalDataset.create(
                manifest_sha256=MANIFEST,
                events=candidate,
            )
            self.assertEqual(dataset.dataset_sha256, expected.dataset_sha256)
            self.assertEqual([item.event_id for item in dataset.events], expected_ids)

    def test_resume_equivalence_at_every_publication_boundary(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event("one", available_at="2026-01-01T10:00:00Z", sequence=1),
                event("two", available_at="2026-01-01T10:01:00Z", sequence=2),
                event("three", available_at="2026-01-01T10:02:00Z", sequence=3),
                event("four", available_at="2026-01-01T10:03:00Z", sequence=4),
            ],
        )
        cutoffs = [
            "2026-01-01T09:59:00Z",
            "2026-01-01T10:00:00Z",
            "2026-01-01T10:01:00Z",
            "2026-01-01T10:02:00Z",
            "2026-01-01T10:03:00Z",
        ]
        for cutoff in cutoffs:
            baseline = CausalFeeder(dataset, start_time=cutoff)
            checkpoint = baseline.checkpoint()
            expected_tail = baseline.advance_to("2026-01-01T10:04:00Z")
            expected_view = baseline.view()

            resumed = CausalFeeder.restore(dataset=dataset, checkpoint=checkpoint)
            actual_tail = resumed.advance_to("2026-01-01T10:04:00Z")

            self.assertEqual(actual_tail, expected_tail, cutoff)
            self.assertEqual(resumed.view(), expected_view, cutoff)
            self.assertEqual(resumed.checkpoint(), baseline.checkpoint(), cutoff)

    def test_strategy_view_cannot_be_forged_with_future_event(self):
        future = event(
            "future-view",
            event_time="2026-01-01T10:00:00Z",
            available_at="2026-01-01T10:01:00Z",
        )
        with self.assertRaisesRegex(CausalReplayError, "future event"):
            CausalDataView(
                simulation_time=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
                manifest_sha256=MANIFEST,
                dataset_sha256="sha256:" + ("2" * 64),
                events=(future,),
            )

    def test_strategy_view_rejects_duplicate_or_reordered_events(self):
        first = event("first", priority=10, sequence=1)
        second = event("second", priority=10, sequence=2)
        simulation_time = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)

        with self.assertRaisesRegex(CausalReplayError, "duplicate event"):
            CausalDataView(
                simulation_time=simulation_time,
                manifest_sha256=MANIFEST,
                dataset_sha256="sha256:" + ("2" * 64),
                events=(first, first),
            )
        with self.assertRaisesRegex(CausalReplayError, "deterministic causal order"):
            CausalDataView(
                simulation_time=simulation_time,
                manifest_sha256=MANIFEST,
                dataset_sha256="sha256:" + ("2" * 64),
                events=(second, first),
            )

        valid = CausalDataView(
            simulation_time=simulation_time,
            manifest_sha256=MANIFEST,
            dataset_sha256="sha256:" + ("2" * 64),
            events=(first, second),
        )
        self.assertEqual(valid.events, (first, second))

    def test_view_digest_binds_dataset_cutoff_and_resume_equivalence(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[
                event("one", available_at="2026-01-01T10:00:00Z", sequence=1),
                event("two", available_at="2026-01-01T10:01:00Z", sequence=2),
            ],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        first_view = feeder.view()
        checkpoint = feeder.checkpoint()

        self.assertEqual(first_view.manifest_sha256, dataset.manifest_sha256)
        self.assertEqual(first_view.dataset_sha256, dataset.dataset_sha256)

        resumed = CausalFeeder.restore(dataset=dataset, checkpoint=checkpoint)
        self.assertEqual(resumed.view().digest, first_view.digest)

        feeder.advance_to("2026-01-01T10:01:00Z")
        later_view = feeder.view()
        self.assertNotEqual(later_view.digest, first_view.digest)

        same_dataset_same_events_later_cutoff = CausalFeeder(
            dataset,
            start_time="2026-01-01T10:00:30Z",
        ).view()
        self.assertEqual(same_dataset_same_events_later_cutoff.events, first_view.events)
        self.assertEqual(
            same_dataset_same_events_later_cutoff.dataset_sha256,
            first_view.dataset_sha256,
        )
        self.assertNotEqual(
            same_dataset_same_events_later_cutoff.digest,
            first_view.digest,
        )

    def test_restore_rejects_tampered_published_prefix(self):
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[event("one", sequence=1)],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        checkpoint = feeder.checkpoint()
        tampered = FeederCheckpoint(
            schema_version=1,
            manifest_sha256=checkpoint.manifest_sha256,
            dataset_sha256=checkpoint.dataset_sha256,
            simulation_time=checkpoint.simulation_time,
            cursor=checkpoint.cursor,
            published_prefix_sha256="sha256:" + ("2" * 64),
        )
        with self.assertRaisesRegex(CausalReplayError, "prefix digest"):
            CausalFeeder.restore(dataset=dataset, checkpoint=tampered)

    def test_duplicate_event_identity_is_never_silently_deduplicated(self):
        first = event("duplicate", payload={"price": "100"})
        changed = event("duplicate", payload={"price": "101"})
        with self.assertRaisesRegex(CausalReplayError, "duplicate event_id"):
            CausalDataset.create(
                manifest_sha256=MANIFEST,
                events=[first, changed],
            )

    def test_ingest_provenance_is_distinct_from_historical_availability(self):
        delayed_archive_ingest = event(
            "macro-release",
            kind="MACRO",
            event_time="2026-01-01T10:00:00Z",
            available_at="2026-01-01T10:01:00Z",
            ingested_at="2026-09-01T12:00:00Z",
            payload={"value": "2.5"},
        )
        dataset = CausalDataset.create(
            manifest_sha256=MANIFEST,
            events=[delayed_archive_ingest],
        )
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")

        self.assertEqual(feeder.advance_to("2026-01-01T10:00:59Z"), ())
        self.assertEqual(
            [item.event_id for item in feeder.advance_to("2026-01-01T10:01:00Z")],
            ["macro-release"],
        )
        self.assertEqual(
            feeder.view().events[0].ingested_at,
            datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc),
        )

        with self.assertRaisesRegex(CausalReplayError, "ingested_at"):
            event(
                "bad-ingest",
                event_time="2026-01-01T10:00:00Z",
                available_at="2026-01-01T10:01:00Z",
                ingested_at="2026-01-01T10:00:30Z",
            )

    def test_ingest_provenance_changes_dataset_identity_without_changing_cutoff(self):
        early_archive = event(
            "claim",
            kind="NEWS",
            event_time="2026-01-01T10:00:00Z",
            available_at="2026-01-01T10:01:00Z",
            ingested_at="2026-02-01T00:00:00Z",
        )
        late_archive = event(
            "claim",
            kind="NEWS",
            event_time="2026-01-01T10:00:00Z",
            available_at="2026-01-01T10:01:00Z",
            ingested_at="2026-03-01T00:00:00Z",
        )
        first = CausalDataset.create(manifest_sha256=MANIFEST, events=[early_archive])
        second = CausalDataset.create(manifest_sha256=MANIFEST, events=[late_archive])

        self.assertNotEqual(early_archive.digest, late_archive.digest)
        self.assertNotEqual(first.dataset_sha256, second.dataset_sha256)
        self.assertEqual(early_archive.available_at, late_archive.available_at)

    def test_clock_cannot_move_backwards(self):
        dataset = CausalDataset.create(manifest_sha256=MANIFEST, events=[])
        feeder = CausalFeeder(dataset, start_time="2026-01-01T10:00:00Z")
        with self.assertRaisesRegex(CausalReplayError, "backwards"):
            feeder.advance_to("2026-01-01T09:59:59Z")


if __name__ == "__main__":
    unittest.main()
