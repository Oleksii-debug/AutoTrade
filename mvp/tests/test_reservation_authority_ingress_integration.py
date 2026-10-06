from contextlib import closing
from pathlib import Path
from types import MappingProxyType
import json
import sqlite3
import tempfile
import unittest

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import (
    JournalStore,
    _event_envelope_digest,
    canonical_json,
    payload_digest,
)
from mvp.autotrade_mvp.reservations import (
    ACTIVE_STATES,
    HELD_STATES,
    POST_BUST_HOLD_STATE,
    TERMINAL_STATES,
    ReservationBook,
    ReservationConflict,
)


class _HostileText(str):
    def strip(self, *_args, **_kwargs):
        raise AssertionError("hostile strip dispatched")

    def upper(self, *_args, **_kwargs):
        raise AssertionError("hostile upper dispatched")


class _HostileMapping(dict):
    def __bool__(self):
        raise AssertionError("hostile mapping truth dispatched")

    def __len__(self):
        raise AssertionError("hostile mapping length dispatched")

    def __iter__(self):
        raise AssertionError("hostile mapping iteration dispatched")

    def items(self):
        raise AssertionError("hostile mapping items dispatched")

    def get(self, *_args, **_kwargs):
        raise AssertionError("hostile mapping get dispatched")


class ReservationAuthorityIngressIntegrationTests(unittest.TestCase):
    def test_state_authorities_are_immutable_without_losing_post_bust_hold(self):
        with self.assertRaises(AttributeError):
            ACTIVE_STATES.add("FORGED")
        with self.assertRaises(AttributeError):
            TERMINAL_STATES.add("FORGED")
        with self.assertRaises(AttributeError):
            HELD_STATES.add("FORGED")
        self.assertIn(POST_BUST_HOLD_STATE, HELD_STATES)
        self.assertTrue(ACTIVE_STATES.issubset(HELD_STATES))

    def test_core_rejects_hostile_text_before_callback(self):
        book = ReservationBook()
        with self.assertRaisesRegex(ValueError, "reservation_id is required"):
            book.reserve(
                reservation_id=_HostileText("r-hostile"),
                intent_id="i-hostile",
                requirements={"CASH:USD": "10"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.active(), ())

    def test_core_exact_map_boundary_covers_newer_fill_and_bust_paths(self):
        book = ReservationBook()
        hostile = _HostileMapping()
        dict.__setitem__(hostile, "CASH:USD", "1")

        for candidate in (hostile, MappingProxyType(hostile)):
            with self.subTest(stage="reserve", mapping=type(candidate).__name__):
                with self.assertRaisesRegex(
                    TypeError, "resource amounts must use an exact dict"
                ):
                    book.reserve(
                        reservation_id="r-reject",
                        intent_id="i-reject",
                        requirements=candidate,
                        available={"CASH:USD": "100"},
                    )
                self.assertEqual(book.active(), ())

        original = book.reserve(
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": "10"},
            available={"CASH:USD": "100"},
        )
        for method_name, kwargs in (
            ("consume", {}),
            (
                "consume_and_mark_filled",
                {"resolution_evidence": "journal:order-fill:test"},
            ),
            ("restore_consumption", {}),
        ):
            for candidate in (hostile, MappingProxyType(hostile)):
                with self.subTest(
                    stage=method_name, mapping=type(candidate).__name__
                ):
                    method = getattr(book, method_name)
                    with self.assertRaisesRegex(
                        TypeError, "resource amounts must use an exact dict"
                    ):
                        method("r1", candidate, **kwargs)
                    self.assertEqual(book.get("r1"), original)

    def test_durable_hostile_map_is_zero_journal_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            book = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-account",
            )
            hostile = _HostileMapping()
            dict.__setitem__(hostile, "CASH:USD", "10")

            for candidate in (hostile, MappingProxyType(hostile)):
                with self.subTest(stage="reserve", mapping=type(candidate).__name__):
                    with self.assertRaisesRegex(
                        TypeError, "resource amounts must use an exact dict"
                    ):
                        book.reserve(
                            command_id="cmd-hostile-" + type(candidate).__name__,
                            idempotency_key="idem-hostile-" + type(candidate).__name__,
                            reservation_id="r-hostile-" + type(candidate).__name__,
                            intent_id="i-hostile-" + type(candidate).__name__,
                            requirements=candidate,
                            available={"CASH:USD": "100"},
                        )
                    self.assertEqual(store.current_journal_sequence(), 0)

            snapshot = book.reserve(
                command_id="cmd-r1",
                idempotency_key="idem-r1",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "10"},
                available={"CASH:USD": "100"},
            )
            before_sequence = store.current_journal_sequence()
            for candidate in (hostile, MappingProxyType(hostile)):
                with self.subTest(stage="consume", mapping=type(candidate).__name__):
                    with self.assertRaisesRegex(
                        TypeError, "resource amounts must use an exact dict"
                    ):
                        book.consume(
                            command_id="cmd-consume-" + type(candidate).__name__,
                            idempotency_key="idem-consume-" + type(candidate).__name__,
                            reservation_id="r1",
                            usage=candidate,
                        )
                    self.assertEqual(store.current_journal_sequence(), before_sequence)
                    self.assertEqual(book.get("r1"), snapshot)

            restarted = DurableReservationBook(
                JournalStore(path),
                environment="PAPER",
                account_id="paper-account",
            )
            self.assertEqual(restarted.get("r1"), snapshot)

    def _assert_rehashed_scope_tamper_rejected(self, field, replacement, pattern):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            book = DurableReservationBook(
                store,
                environment="PAPER",
                account_id="paper-account",
            )
            book.reserve(
                command_id="cmd-r1",
                idempotency_key="idem-r1",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "10"},
                available={"CASH:USD": "100"},
            )

            with closing(sqlite3.connect(path)) as connection:
                row = connection.execute(
                    "SELECT event_id, payload_json, envelope_json FROM events "
                    "WHERE aggregate_type='reservation_book'"
                ).fetchone()
                payload = json.loads(row[1])
                payload[field] = replacement
                payload_json = canonical_json(payload)
                envelope = json.loads(row[2])
                envelope["payload"] = payload
                envelope["payload_hash"] = payload_digest(payload)
                envelope_json = canonical_json(envelope)
                connection.execute(
                    "UPDATE events SET payload_json=?, payload_hash=?, "
                    "envelope_json=?, envelope_hash=? WHERE event_id=?",
                    (
                        payload_json,
                        payload_digest(payload),
                        envelope_json,
                        _event_envelope_digest(envelope_json),
                        row[0],
                    ),
                )
                connection.commit()

            restarted = DurableReservationBook(
                JournalStore(path),
                environment="PAPER",
                account_id="paper-account",
            )
            with self.assertRaisesRegex(ReservationConflict, pattern):
                restarted.get("r1")

    def test_replay_rejects_rehashed_cross_environment_scope(self):
        self._assert_rehashed_scope_tamper_rejected(
            "environment", "LIVE", "environment.*scope"
        )

    def test_replay_rejects_rehashed_cross_account_scope(self):
        self._assert_rehashed_scope_tamper_rejected(
            "account_id", "other-account", "account.*scope"
        )


if __name__ == "__main__":
    unittest.main()
