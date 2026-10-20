from datetime import datetime, timedelta, timezone
import unittest

from mvp.autotrade_mvp.replay import CausalReplay, ReplayEvent
from mvp.autotrade_mvp.simulation_runtime_checkpoint import (
    AutonomousRuntimeCheckpointError,
    _events,
    _immutable_replay_events,
    _replay,
)


class CheckpointEventPopulationTests(unittest.TestCase):
    def setUp(self):
        _immutable_replay_events.cache_clear()
        self.protocol = {
            "prices": ["100", "101", "99"],
            "start_time": "2026-10-18T00:00:00Z",
            "source_build_identity": "a" * 40,
        }

    def tearDown(self):
        _immutable_replay_events.cache_clear()

    def test_cold_warm_and_restart_match_uncached_replay_at_every_cursor(self):
        start = datetime(2026, 10, 18, tzinfo=timezone.utc)
        events = tuple(ReplayEvent(
            sequence=i,
            available_at=(start + timedelta(seconds=i)).isoformat().replace("+00:00", "Z"),
            source_version="a" * 40,
            payload={"episode": i + 1, "price": price},
        ) for i, price in enumerate(self.protocol["prices"]))
        for cursor in range(4):
            expected = CausalReplay(events, start_at="2026-10-17T23:59:59.999999Z")
            if cursor:
                expected.advance_to(events[cursor - 1].available_at)
            for _ in range(2):
                actual = _replay(self.protocol, completed_episodes=cursor)
                self.assertEqual(actual.checkpoint(), expected.checkpoint())
                self.assertEqual(actual.remaining(), expected.remaining())
            _immutable_replay_events.cache_clear()
            self.assertEqual(_replay(self.protocol, completed_episodes=cursor).checkpoint(),
                             expected.checkpoint())

    def test_mutated_inputs_cannot_reuse_old_population_or_bypass_validation(self):
        previous = _events(self.protocol)
        for field, changed in (("prices", ["100", "102", "99"]),
                               ("source_build_identity", "b" * 40),
                               ("start_time", "2026-10-19T00:00:00Z")):
            updated = {**self.protocol, field: changed}
            self.assertNotEqual(_events(updated), previous)
        self.protocol["prices"][1] = "103"
        self.assertNotEqual(_events(self.protocol), previous)
        self.protocol["prices"][1] = 103
        with self.assertRaises(AutonomousRuntimeCheckpointError):
            _events(self.protocol)

    def test_population_is_immutable_and_single_entry_bounded(self):
        events = _events(self.protocol)
        self.assertIs(_events(self.protocol), events)
        with self.assertRaises(TypeError):
            events[0].payload["price"] = "0"
        _events({**self.protocol, "prices": ["42"]})
        self.assertEqual(_immutable_replay_events.cache_info().currsize, 1)
        self.assertEqual(_events(self.protocol), events)


if __name__ == "__main__":
    unittest.main()
