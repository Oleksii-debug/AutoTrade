"""Direct whole-store CAS tests for canonical JournalStore mutations."""

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


def event(event_id="evt-cas-1", aggregate_id="paper-cas", version=1):
    payload = {"kind": "cas-proof", "event_id": event_id}
    return {
        "event_id": event_id,
        "event_type": "CasProofObserved",
        "aggregate_type": "account",
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "committed_at": "2026-10-03T18:30:00Z",
    }


def foreign_result_only_command(store: JournalStore, suffix: str) -> None:
    saved, inserted = store.record_command(
        command_id=f"foreign-command-{suffix}",
        actor="foreign-writer",
        environment="SIMULATION",
        idempotency_key=f"foreign-key-{suffix}",
        request={"foreign": suffix},
        result={"foreign": suffix},
        state_version=0,
    )
    if not inserted or saved != {"foreign": suffix}:
        raise AssertionError("foreign command injection failed")


class JournalWholeStoreCasTests(unittest.TestCase):
    def test_append_event_rejects_command_only_writer_at_same_journal_sequence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            cut = store.whole_store_state_cut()
            self.assertEqual(cut["journal_sequence"], 0)
            foreign_result_only_command(store, "append")

            with self.assertRaisesRegex(
                ValueError,
                "whole-store state changed after bootstrap validation",
            ):
                store.append_event(
                    event(),
                    expected_journal_sequence=cut["journal_sequence"],
                    expected_whole_store_counts=cut["counts"],
                )

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(store.load_events("account", "paper-cas"), [])

    def test_commit_command_rejects_command_only_writer_at_same_journal_sequence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            cut = store.whole_store_state_cut()
            foreign_result_only_command(store, "commit")

            with self.assertRaisesRegex(
                ValueError,
                "whole-store state changed after financial evidence validation",
            ):
                store.commit_command(
                    command_id="candidate-command",
                    actor="candidate",
                    environment="SIMULATION",
                    idempotency_key="candidate-key",
                    request={"candidate": True},
                    result={"status": "ACCEPTED"},
                    state_version=1,
                    events=[(event(), None)],
                    expected_journal_sequence=cut["journal_sequence"],
                    expected_whole_store_counts=cut["counts"],
                )

            self.assertEqual(store.current_journal_sequence(), 0)
            self.assertEqual(store.load_events("account", "paper-cas"), [])
            self.assertEqual(
                store.whole_store_state_counts()["command_dedupe"],
                1,
            )

    def test_outbox_delivery_rejects_command_only_writer_at_same_journal_sequence(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            store.append_event(event(), outbox_topic="events")
            pending = store.pending_outbox()[0]
            cut = store.whole_store_state_cut()
            foreign_result_only_command(store, "delivery")

            with self.assertRaisesRegex(
                ValueError,
                "whole-store state changed after bootstrap validation",
            ):
                store.mark_outbox_delivered(
                    pending["outbox_id"],
                    expected_envelope_hash=pending["envelope_hash"],
                    expected_journal_sequence=cut["journal_sequence"],
                    expected_whole_store_counts=cut["counts"],
                )

            self.assertEqual(len(store.pending_outbox()), 1)
            self.assertEqual(
                store.pending_outbox()[0]["outbox_id"],
                pending["outbox_id"],
            )

    def test_exact_append_replay_survives_stale_whole_store_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            first = store.append_event(event(), outbox_topic="events")
            self.assertTrue(first.inserted)
            cut = store.whole_store_state_cut()
            foreign_result_only_command(store, "append-replay")

            replay = store.append_event(
                event(),
                outbox_topic="events",
                expected_journal_sequence=cut["journal_sequence"],
                expected_whole_store_counts=cut["counts"],
            )
            self.assertFalse(replay.inserted)
            self.assertEqual(store.current_journal_sequence(), 1)
            self.assertEqual(len(store.load_events("account", "paper-cas")), 1)

    def test_exact_command_replay_survives_stale_whole_store_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            start = store.whole_store_state_cut()
            saved, inserted, _ = store.commit_command(
                command_id="owned-command",
                actor="owner",
                environment="SIMULATION",
                idempotency_key="owned-key",
                request={"owned": True},
                result={"status": "ACCEPTED"},
                state_version=1,
                events=[(event(), None)],
                expected_journal_sequence=start["journal_sequence"],
                expected_whole_store_counts=start["counts"],
            )
            self.assertTrue(inserted)
            self.assertEqual(saved, {"status": "ACCEPTED"})
            stale = store.whole_store_state_cut()
            foreign_result_only_command(store, "command-replay")

            saved, inserted, appended = store.commit_command(
                command_id="ignored-replay-command-id",
                actor="owner",
                environment="SIMULATION",
                idempotency_key="owned-key",
                request={"owned": True},
                result={"status": "MUST_NOT_REPLACE"},
                state_version=999,
                events=[(event(), None)],
                expected_journal_sequence=stale["journal_sequence"],
                expected_whole_store_counts=stale["counts"],
            )
            self.assertFalse(inserted)
            self.assertEqual(appended, ())
            self.assertEqual(saved, {"status": "ACCEPTED"})
            self.assertEqual(store.current_journal_sequence(), 1)

    def test_expected_whole_store_counts_rejects_mapping_subclasses(self):
        class CountsSubclass(dict):
            pass

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            cut = store.whole_store_state_cut()
            hostile = CountsSubclass(cut["counts"])

            with self.assertRaisesRegex(
                TypeError,
                "exact dict",
            ):
                store.append_event(
                    event(),
                    expected_journal_sequence=cut["journal_sequence"],
                    expected_whole_store_counts=hostile,
                )
            self.assertEqual(store.current_journal_sequence(), 0)

    def test_expected_whole_store_counts_requires_exact_table_shape(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            cut = store.whole_store_state_cut()
            incomplete = dict(cut["counts"])
            incomplete.pop("command_dedupe")

            with self.assertRaisesRegex(
                ValueError,
                "exact business-state tables",
            ):
                store.append_event(
                    event(),
                    expected_journal_sequence=cut["journal_sequence"],
                    expected_whole_store_counts=incomplete,
                )
            self.assertEqual(store.current_journal_sequence(), 0)


if __name__ == "__main__":
    unittest.main()
