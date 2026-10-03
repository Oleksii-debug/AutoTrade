"""Adversarial event/cursor ingress at the existing causal replay authority."""
from dataclasses import replace
import unittest
from mvp.autotrade_mvp.replay import CausalReplay, ReplayEvent, ReplayCheckpoint, ReplayError

START = "2026-10-03T00:00:00Z"
AT = "2026-10-03T00:01:00Z"

def event():
    return ReplayEvent(1, AT, "frozen-source@1", {"price": "100", "nested": {"value": 1}})

class ReplayEventSnapshotTests(unittest.TestCase):
    def test_date_only_or_space_separated_time_cannot_select_a_local_timezone_cut(self):
        for value in ("2026-09-01Z", "2026-09-01 00:00:00Z"):
            with self.subTest(value=value), self.assertRaises(ReplayError):
                ReplayEvent(sequence=0, available_at=value, source_version="v1", payload={})
            with self.subTest(start=value), self.assertRaises(ReplayError):
                CausalReplay([event()], start_at=value)
            with self.subTest(checkpoint=value), self.assertRaises(ReplayError):
                ReplayCheckpoint(dataset_digest="a" * 64, cursor=0, clock=value)

    def test_caller_mutation_after_enrollment_cannot_change_dataset_or_visibility(self):
        original = event()
        replay = CausalReplay([original], start_at=START)
        digest = replay.digest
        object.__setattr__(original, "available_at", START)
        object.__setattr__(original, "payload", {"price": "999"})
        self.assertEqual(replay.advance_to(START), ())
        observed = replay.advance_to(AT)[0]
        self.assertEqual(observed.payload["price"], "100")
        self.assertEqual(replay.digest, digest)

    def test_public_event_output_is_detached_from_retained_dataset(self):
        replay = CausalReplay([event()], start_at=START)
        exposed = replay.advance_to(AT)[0]
        object.__setattr__(exposed, "payload", {"price": "999"})
        resumed = CausalReplay([event()], start_at=START, checkpoint=replay.checkpoint())
        self.assertEqual(resumed.digest, replay.digest)
        self.assertEqual(replay._events[0].payload["price"], "100")

    def test_event_subclass_is_rejected_without_virtual_field_reads(self):
        calls=[]
        class HostileEvent(ReplayEvent):
            def __getattribute__(self, name):
                if name in {"sequence", "available_at", "payload"}:
                    calls.append(name)
                return super().__getattribute__(name)
        hostile = HostileEvent(1, AT, "source@1", {})
        calls.clear()
        with self.assertRaises(TypeError):
            CausalReplay([hostile], start_at=START)
        self.assertEqual(calls, [])

    def test_mutated_cursor_is_revalidated_before_resume(self):
        replay=CausalReplay([event()], start_at=START)
        checkpoint=replay.checkpoint()
        object.__setattr__(checkpoint, "cursor", True)
        with self.assertRaises(TypeError):
            CausalReplay([event()], start_at=START, checkpoint=checkpoint)

    def test_nonfinite_event_payload_never_enters_replay_identity(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReplayEvent(1, AT, "source@1", {"value": value})
