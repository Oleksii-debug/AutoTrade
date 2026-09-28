import unittest

from mvp.autotrade_mvp.replay import (
    CausalReplay,
    CompositeReplayCheckpoint,
    ReplayCheckpoint,
    ReplayError,
    ReplayEvent,
    dataset_digest,
    resume_from_composite_checkpoint,
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

    def _runtime_components(self, **overrides):
        names = (
            "pending_event_queue",
            "rng_state",
            "strategy_state",
            "portfolio_accounting_state",
            "execution_state",
            "accrual_state",
            "policy_state",
            "instrument_state",
            "provider_state",
            "experiment_state",
        )
        values = {
            name: __import__("hashlib").sha256(name.encode("utf-8")).hexdigest()
            for name in names
        }
        values.update(overrides)
        return values

    @staticmethod
    def _component_resolver(values):
        snapshot = dict(values)

        def resolve(name):
            return snapshot[name]

        return resolve

    def test_composite_checkpoint_rejects_changed_rng_before_next_event(self):
        events = [
            event(1, "2026-09-24T10:00:00Z", 1),
            event(2, "2026-09-24T10:01:00Z", 2),
        ]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        replay.advance_to("2026-09-24T10:00:00Z")
        components = self._runtime_components()
        checkpoint = replay.composite_checkpoint(
            runtime_component_resolver=self._component_resolver(components),
            build_sha="a" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )

        changed = dict(components)
        changed["rng_state"] = "f" * 64
        with self.assertRaisesRegex(ReplayError, "runtime state cut"):
            resume_from_composite_checkpoint(
                events,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
                runtime_component_resolver=self._component_resolver(changed),
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )

    def test_composite_checkpoint_requires_all_runtime_authorities(self):
        replay = CausalReplay(
            [event(1, "2026-09-24T10:00:00Z", 1)],
            start_at="2026-09-24T09:59:00Z",
        )
        incomplete = self._runtime_components()
        del incomplete["execution_state"]
        with self.assertRaisesRegex(ReplayError, "runtime component authority failed for execution_state"):
            replay.composite_checkpoint(
                runtime_component_resolver=self._component_resolver(incomplete),
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )

    def test_resume_does_not_accept_caller_supplied_component_digest_mapping(self):
        events = [event(1, "2026-09-24T10:00:00Z", 1)]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        components = self._runtime_components()
        checkpoint = replay.composite_checkpoint(
            runtime_component_resolver=self._component_resolver(components),
            build_sha="a" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )
        with self.assertRaisesRegex(TypeError, "runtime_components"):
            resume_from_composite_checkpoint(
                events,
                start_at="2026-09-24T09:59:00Z",
                checkpoint=checkpoint,
                runtime_components=dict(checkpoint.runtime_components),
                build_sha="a" * 64,
                protocol_ref="protocol:walk-forward-v1",
            )

    def test_composite_checkpoint_exact_resume_preserves_suffix_and_identity(self):
        events = [
            event(1, "2026-09-24T10:00:00Z", 1),
            event(2, "2026-09-24T10:00:00Z", 2),
            event(3, "2026-09-24T10:01:00Z", 3),
        ]
        uninterrupted = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        uninterrupted.advance_to("2026-09-24T10:00:00Z")
        components = self._runtime_components()
        checkpoint = uninterrupted.composite_checkpoint(
            runtime_component_resolver=self._component_resolver(components),
            build_sha="b" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )
        self.assertEqual(
            checkpoint.fingerprint,
            CompositeReplayCheckpoint(
                replay=checkpoint.replay,
                runtime_components=dict(reversed(tuple(components.items()))),
                build_sha="b" * 64,
                protocol_ref="protocol:walk-forward-v1",
            ).fingerprint,
        )

        resumed = resume_from_composite_checkpoint(
            events,
            start_at="2026-09-24T09:59:00Z",
            checkpoint=checkpoint,
            runtime_component_resolver=self._component_resolver(components),
            build_sha="b" * 64,
            protocol_ref="protocol:walk-forward-v1",
        )
        suffix_a = uninterrupted.advance_to("2026-09-24T10:02:00Z")
        suffix_b = resumed.advance_to("2026-09-24T10:02:00Z")
        self.assertEqual(
            [(item.sequence, dict(item.payload)) for item in suffix_a],
            [(item.sequence, dict(item.payload)) for item in suffix_b],
        )

    def test_composite_checkpoint_rejects_build_or_protocol_drift(self):
        events = [event(1, "2026-09-24T10:00:00Z", 1)]
        replay = CausalReplay(events, start_at="2026-09-24T09:59:00Z")
        components = self._runtime_components()
        checkpoint = replay.composite_checkpoint(
            runtime_component_resolver=self._component_resolver(components),
            build_sha="c" * 64,
            protocol_ref="protocol:registered-v1",
        )
        for build_sha, protocol_ref in (
            ("d" * 64, "protocol:registered-v1"),
            ("c" * 64, "protocol:post-hoc-v2"),
        ):
            with self.subTest(build_sha=build_sha, protocol_ref=protocol_ref):
                with self.assertRaisesRegex(ReplayError, "runtime state cut"):
                    resume_from_composite_checkpoint(
                        events,
                        start_at="2026-09-24T09:59:00Z",
                        checkpoint=checkpoint,
                        runtime_component_resolver=self._component_resolver(components),
                        build_sha=build_sha,
                        protocol_ref=protocol_ref,
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

    def test_payload_is_deeply_frozen_and_digest_is_stable(self):
        payload = {"nested": {"a": 1}, "items": [{"b": 2}]}
        item = ReplayEvent(
            sequence=1,
            available_at="2026-09-24T10:00:00Z",
            source_version="v1",
            payload=payload,
        )
        payload["nested"]["a"] = 9
        payload["items"][0]["b"] = 8
        self.assertEqual(item.payload["nested"]["a"], 1)
        self.assertEqual(item.payload["items"][0]["b"], 2)

        with self.assertRaises(TypeError):
            item.payload["nested"]["a"] = 7
        with self.assertRaises(TypeError):
            item.payload["items"][0]["b"] = 7
        with self.assertRaises(TypeError):
            item.payload["items"][0] = {"b": 7}

        one = dataset_digest([item])
        two = dataset_digest(
            [
                ReplayEvent(
                    sequence=1,
                    available_at="2026-09-24T10:00:00Z",
                    source_version="v1",
                    payload={"nested": {"a": 1}, "items": [{"b": 2}]},
                )
            ]
        )
        self.assertEqual(one, two)

        replay = CausalReplay(
            [item],
            start_at="2026-09-24T09:59:00Z",
        )
        before = replay.digest
        emitted = replay.advance_to("2026-09-24T10:00:00Z")[0]
        self.assertEqual(emitted.payload["nested"]["a"], 1)
        self.assertEqual(emitted.payload["items"][0]["b"], 2)
        self.assertEqual(replay.digest, before)

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
