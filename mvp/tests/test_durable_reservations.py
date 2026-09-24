from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reservations import (
    InsufficientAvailable,
    ReservationConflict,
)


EVIDENCE = (
    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
    + "a" * 64
)


class DurableReservationBookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite"
        self.store = JournalStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def book(self):
        return DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="paper-account",
        )

    def reserve(self, book, *, amount="70", command="cmd-reserve", idem="idem-reserve"):
        return book.reserve(
            command_id=command,
            idempotency_key=idem,
            reservation_id="r1",
            intent_id="i1",
            requirements={"CASH:USD": amount},
            available={"CASH:USD": "100"},
        )

    def test_restart_reconstructs_active_reservation_from_journal(self):
        first = self.book()
        self.reserve(first)
        self.assertEqual(first.version, 1)
        self.assertEqual(first.total_reserved("CASH:USD"), Decimal("70"))

        restarted = self.book()
        self.assertEqual(restarted.version, 1)
        self.assertEqual(restarted.get("r1").state, "WORKING")
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))

    def test_partial_consumption_and_unknown_survive_restart(self):
        first = self.book()
        self.reserve(first)
        first.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "20"},
        )
        first.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )

        restarted = self.book()
        snapshot = restarted.get("r1")
        self.assertEqual(snapshot.state, "UNKNOWN")
        self.assertEqual(snapshot.consumed["CASH:USD"], Decimal("20"))
        self.assertEqual(snapshot.remaining["CASH:USD"], Decimal("50"))
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("50"))
        self.assertEqual(restarted.version, 3)

    def test_evidenced_terminal_resolution_survives_restart(self):
        first = self.book()
        self.reserve(first)
        first.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )
        first.mark_terminal(
            command_id="cmd-terminal",
            idempotency_key="idem-terminal",
            reservation_id="r1",
            outcome="PROVEN_ABSENT",
            resolution_evidence=EVIDENCE,
        )

        restarted = self.book()
        snapshot = restarted.get("r1")
        self.assertEqual(snapshot.state, "PROVEN_ABSENT")
        self.assertEqual(
            snapshot.resolution_evidence,
            EVIDENCE,
        )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("0"))

    def test_restart_does_not_make_reserved_cash_available_again(self):
        first = self.book()
        self.reserve(first, amount="70")

        restarted = self.book()
        with self.assertRaises(InsufficientAvailable):
            restarted.reserve(
                command_id="cmd-r2",
                idempotency_key="idem-r2",
                reservation_id="r2",
                intent_id="i2",
                requirements={"CASH:USD": "40"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(restarted.version, 1)

    def test_duplicate_consume_idempotency_key_does_not_consume_twice(self):
        book = self.book()
        self.reserve(book, amount="100")
        first = book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "40"},
        )
        second = book.consume(
            command_id="another-command-id-is-ignored-by-idem",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "40"},
        )
        self.assertEqual(first, second)
        self.assertEqual(second.consumed["CASH:USD"], Decimal("40"))
        self.assertEqual(second.remaining["CASH:USD"], Decimal("60"))
        self.assertEqual(book.version, 2)

    def test_idempotency_key_cannot_be_reused_for_different_economics(self):
        book = self.book()
        self.reserve(book, amount="100")
        book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "10"},
        )
        with self.assertRaisesRegex(ReservationConflict, "different"):
            book.consume(
                command_id="cmd-consume-other",
                idempotency_key="idem-consume",
                reservation_id="r1",
                usage={"CASH:USD": "11"},
            )
        self.assertEqual(book.get("r1").consumed["CASH:USD"], Decimal("10"))

    def test_command_and_idempotency_namespaces_are_isolated_per_account(self):
        first = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="account-a",
        )
        second = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="account-b",
        )

        first.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="r-a",
            intent_id="i-a",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )
        second.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="r-b",
            intent_id="i-b",
            requirements={"CASH:USD": "60"},
            available={"CASH:USD": "100"},
        )

        self.assertEqual(first.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(second.total_reserved("CASH:USD"), Decimal("60"))
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                    account_id="account-a",
            ).total_reserved("CASH:USD"),
            Decimal("70"),
        )
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                    account_id="account-b",
            ).total_reserved("CASH:USD"),
            Decimal("60"),
        )

    def test_same_account_isolated_between_paper_and_live(self):
        paper = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="same-account",
        )
        live = DurableReservationBook(
            self.store,
            environment="LIVE",
            account_id="same-account",
        )
        paper.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="paper-r",
            intent_id="paper-i",
            requirements={"CASH:USD": "70"},
            available={"CASH:USD": "100"},
        )
        live.reserve(
            command_id="same-command",
            idempotency_key="same-idempotency",
            reservation_id="live-r",
            intent_id="live-i",
            requirements={"CASH:USD": "30"},
            available={"CASH:USD": "100"},
        )
        self.assertNotEqual(paper.scope_id, live.scope_id)
        self.assertEqual(paper.total_reserved("CASH:USD"), Decimal("70"))
        self.assertEqual(live.total_reserved("CASH:USD"), Decimal("30"))
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="PAPER",
                account_id="same-account",
            ).total_reserved("CASH:USD"),
            Decimal("70"),
        )
        self.assertEqual(
            DurableReservationBook(
                JournalStore(self.path),
                environment="LIVE",
                account_id="same-account",
            ).total_reserved("CASH:USD"),
            Decimal("30"),
        )

    def test_reservation_environment_is_required_and_canonical(self):
        with self.assertRaises(TypeError):
            DurableReservationBook(self.store, account_id="acct")
        with self.assertRaisesRegex(ValueError, "environment must be"):
            DurableReservationBook(
                self.store,
                environment="",
                account_id="acct",
            )
        lower = DurableReservationBook(
            self.store,
            environment=" paper ",
            account_id="acct",
        )
        upper = DurableReservationBook(
            self.store,
            environment="PAPER",
            account_id="acct",
        )
        self.assertEqual(lower.environment, "PAPER")
        self.assertEqual(lower.scope_id, upper.scope_id)

    def test_journal_failure_does_not_mutate_projection(self):
        book = self.book()
        with patch.object(
            self.store,
            "commit_command",
            side_effect=RuntimeError("simulated disk failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "disk failure"):
                self.reserve(book)
        self.assertEqual(book.version, 0)
        self.assertEqual(book.active(), ())
        self.assertEqual(self.book().active(), ())

    def test_tampered_payload_is_rejected_on_restart(self):
        book = self.book()
        self.reserve(book)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            connection.execute(
                "UPDATE events SET payload_json=? WHERE event_id=?",
                (row[1].replace('"70"', '"71"', 1), row[0]),
            )
            connection.commit()

        with self.assertRaisesRegex(ReservationConflict, "payload hash"):
            self.book()

    def test_tampered_snapshot_with_recomputed_hash_still_fails_replay(self):
        book = self.book()
        self.reserve(book)
        with sqlite3.connect(self.path) as connection:
            row = connection.execute(
                "SELECT event_id, payload_json FROM events "
                "WHERE aggregate_type='reservation_book'"
            ).fetchone()
            import json
            from mvp.autotrade_mvp.persistence import payload_digest

            payload = json.loads(row[1])
            payload["snapshot"]["remaining"]["CASH:USD"] = "69"
            replacement = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            connection.execute(
                "UPDATE events SET payload_json=?, payload_hash=? WHERE event_id=?",
                (replacement, payload_digest(payload), row[0]),
            )
            connection.commit()

        with self.assertRaisesRegex(ReservationConflict, "snapshot"):
            self.book()

    def test_unknown_still_cannot_erase_consumed_exposure(self):
        book = self.book()
        self.reserve(book, amount="100")
        book.consume(
            command_id="cmd-consume",
            idempotency_key="idem-consume",
            reservation_id="r1",
            usage={"CASH:USD": "10"},
        )
        book.mark_unknown(
            command_id="cmd-unknown",
            idempotency_key="idem-unknown",
            reservation_id="r1",
        )
        with self.assertRaises(ReservationConflict):
            book.mark_terminal(
                command_id="cmd-absent",
                idempotency_key="idem-absent",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                resolution_evidence=EVIDENCE,
            )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("90"))

    def test_concurrent_same_idempotency_between_reads_is_not_double_applied(self):
        book = self.book()
        self.reserve(book, amount="100")
        original_events = book._events
        calls = 0
        raced = False

        def race_on_second_read():
            nonlocal calls, raced
            calls += 1
            if calls == 2 and not raced:
                raced = True
                competing = DurableReservationBook(
                    JournalStore(self.path),
                    environment="PAPER",
                    account_id="paper-account",
                )
                competing.consume(
                    command_id="cmd-competing-consume",
                    idempotency_key="idem-shared-consume",
                    reservation_id="r1",
                    usage={"CASH:USD": "60"},
                )
            return original_events()

        with patch.object(book, "_events", side_effect=race_on_second_read):
            result = book.consume(
                command_id="cmd-local-consume",
                idempotency_key="idem-shared-consume",
                reservation_id="r1",
                usage={"CASH:USD": "60"},
            )

        self.assertEqual(result.consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(result.remaining["CASH:USD"], Decimal("40"))
        self.assertEqual(book.version, 2)
        restarted = self.book()
        self.assertEqual(restarted.get("r1").consumed["CASH:USD"], Decimal("60"))
        self.assertEqual(restarted.version, 2)

    def test_concurrent_writer_fences_stale_projection_without_corrupting_journal(self):
        book = self.book()
        original_commit = self.store.commit_command
        raced = False

        def race_then_commit(*args, **kwargs):
            nonlocal raced
            if not raced:
                raced = True
                competing = DurableReservationBook(
                    JournalStore(self.path),
                    environment="PAPER",
                    account_id="paper-account",
                )
                competing.reserve(
                    command_id="cmd-race",
                    idempotency_key="idem-race",
                    reservation_id="r-race",
                    intent_id="i-race",
                    requirements={"CASH:USD": "30"},
                    available={"CASH:USD": "100"},
                )
            return original_commit(*args, **kwargs)

        with patch.object(
            self.store,
            "commit_command",
            side_effect=race_then_commit,
        ):
            with self.assertRaisesRegex(ValueError, "aggregate_version must be 2"):
                book.reserve(
                    command_id="cmd-stale",
                    idempotency_key="idem-stale",
                    reservation_id="r-stale",
                    intent_id="i-stale",
                    requirements={"CASH:USD": "80"},
                    available={"CASH:USD": "100"},
                )

        self.assertEqual(
            [item.reservation_id for item in book.active()],
            ["r-race"],
        )
        self.assertEqual(book.total_reserved("CASH:USD"), Decimal("30"))
        self.assertEqual(book.version, 1)
        restarted = self.book()
        self.assertEqual(restarted.version, 1)
        self.assertEqual(
            [item.reservation_id for item in restarted.active()],
            ["r-race"],
        )
        self.assertEqual(restarted.total_reserved("CASH:USD"), Decimal("30"))

    def test_resource_alias_collision_fails_before_journal_mutation(self):
        book = self.book()
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            book.reserve(
                command_id="cmd-alias",
                idempotency_key="idem-alias",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": "10", " CASH:USD ": "20"},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.version, 0)

    def test_terminal_release_requires_immutable_evidence_before_journal_mutation(self):
        book = self.book()
        self.reserve(book)
        before_version = book.version
        before_reserved = book.total_reserved("CASH:USD")
        with self.assertRaisesRegex(ValueError, "immutable artifact"):
            book.mark_terminal(
                command_id="cmd-terminal-weak",
                idempotency_key="idem-terminal-weak",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                resolution_evidence="provider-complete-coverage",
            )
        self.assertEqual(book.version, before_version)
        self.assertEqual(book.total_reserved("CASH:USD"), before_reserved)
        self.assertEqual(self.book().total_reserved("CASH:USD"), before_reserved)

    def test_terminal_release_rejects_noncanonical_evidence_digest(self):
        book = self.book()
        self.reserve(book)
        with self.assertRaisesRegex(ValueError, "canonical lowercase"):
            book.mark_terminal(
                command_id="cmd-terminal-bad-digest",
                idempotency_key="idem-terminal-bad-digest",
                reservation_id="r1",
                outcome="PROVEN_ABSENT",
                resolution_evidence=(
                    "artifact:11111111-1111-4111-8111-111111111111@sha256:"
                    + "A" * 64
                ),
            )
        self.assertEqual(book.version, 1)

    def test_binary_float_inputs_fail_before_journal_mutation(self):
        book = self.book()
        with self.assertRaises(TypeError):
            book.reserve(
                command_id="cmd-float",
                idempotency_key="idem-float",
                reservation_id="r1",
                intent_id="i1",
                requirements={"CASH:USD": 10.5},
                available={"CASH:USD": "100"},
            )
        self.assertEqual(book.version, 0)


if __name__ == "__main__":
    unittest.main()
