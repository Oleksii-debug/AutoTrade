from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import OwnerFence, RecoveryController


class RecoveryOwnerJournalCutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(Path(self.directory.name) / "journal.sqlite3")
        self.controller = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )
        self.controller.start("host-a")

    def _append_race_event(self) -> None:
        payload = {"reason": "owner-cut-race-probe"}
        self.store.append_event(
            {
                "event_id": "owner-cut-race-probe-1",
                "event_type": "OwnerCutRaceProbe",
                "aggregate_type": "test_probe",
                "aggregate_id": "owner-cut-race",
                "aggregate_version": "1",
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-04T20:00:00Z",
            }
        )

    def test_owner_advance_commits_when_validated_journal_cut_is_unchanged(self) -> None:
        cut = self.store.current_journal_sequence()

        self.controller._append_durable_owner(
            OwnerFence("host-b", 2),
            expected_journal_sequence=cut,
        )

        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in self.controller.durable_owner_chain()],
            [("host-a", 1), ("host-b", 2)],
        )

    def test_owner_advance_rejects_any_intervening_durable_event(self) -> None:
        cut = self.store.current_journal_sequence()
        self._append_race_event()

        with self.assertRaisesRegex(
            ValueError,
            "journal sequence changed after whole-store validation",
        ):
            self.controller._append_durable_owner(
                OwnerFence("host-b", 2),
                expected_journal_sequence=cut,
            )

        self.assertEqual(
            [(owner.owner_id, owner.epoch) for owner in self.controller.durable_owner_chain()],
            [("host-a", 1)],
        )


if __name__ == "__main__":
    unittest.main()
