from pathlib import Path
import tempfile
import unittest

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import ReservationConflict


class _HostileDict(dict):
    callbacks = 0

    @classmethod
    def reset(cls):
        cls.callbacks = 0

    def _called(self):
        type(self).callbacks += 1
        raise AssertionError("hostile mapping callback executed")

    def __getitem__(self, key):
        self._called()

    def __iter__(self):
        self._called()

    def items(self):
        self._called()

    def get(self, key, default=None):
        self._called()


class DurableReservationReplayInertJsonTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JournalStore(Path(self.temp.name) / "journal.sqlite")
        self.book = DurableReservationBook(
            self.store,
            environment="SIMULATION",
            account_id="simulation-account",
        )
        _HostileDict.reset()

    def tearDown(self):
        self.temp.cleanup()

    def test_top_level_event_subclass_is_rejected_before_callbacks(self):
        hostile = _HostileDict()
        dict.__setitem__(hostile, "aggregate_version", 1)

        with self.assertRaisesRegex(ReservationConflict, "exact inert JSON values"):
            self.book._replay([hostile])

        self.assertEqual(_HostileDict.callbacks, 0)

    def test_nested_payload_subclass_is_rejected_before_hashing(self):
        hostile = _HostileDict()
        dict.__setitem__(hostile, "environment", "SIMULATION")
        event = {
            "aggregate_version": 1,
            "event_type": "ReservationMutationCommitted",
            "payload": hostile,
            "payload_hash": "sha256:" + "0" * 64,
        }

        with self.assertRaisesRegex(ReservationConflict, "exact inert JSON values"):
            self.book._replay([event])

        self.assertEqual(_HostileDict.callbacks, 0)

    def test_nested_request_and_snapshot_subclasses_are_rejected_before_hashing(self):
        for field in ("request", "snapshot"):
            with self.subTest(field=field):
                _HostileDict.reset()
                hostile = _HostileDict()
                dict.__setitem__(hostile, "forged", "value")
                payload = {
                    "environment": "SIMULATION",
                    "account_id": "simulation-account",
                    "operation": "RESERVE",
                    "request": {},
                    "snapshot": {},
                    "idempotency_key": "idem",
                    "request_hash": "sha256:" + "0" * 64,
                }
                payload[field] = hostile
                event = {
                    "aggregate_version": 1,
                    "event_type": "ReservationMutationCommitted",
                    "payload": payload,
                    "payload_hash": "sha256:" + "0" * 64,
                }

                with self.assertRaisesRegex(
                    ReservationConflict,
                    "exact inert JSON values",
                ):
                    self.book._replay([event])

                self.assertEqual(_HostileDict.callbacks, 0)

    def test_text_subclass_key_is_rejected_before_semantic_use(self):
        class HostileText(str):
            pass

        payload = {
            HostileText("environment"): "SIMULATION",
            "account_id": "simulation-account",
        }
        event = {
            "aggregate_version": 1,
            "event_type": "ReservationMutationCommitted",
            "payload": payload,
            "payload_hash": "sha256:" + "0" * 64,
        }

        with self.assertRaisesRegex(ReservationConflict, "object keys must be exact text"):
            self.book._replay([event])


if __name__ == "__main__":
    unittest.main()
