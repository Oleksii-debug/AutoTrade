"""Focused regressions for durable recovery clock-incident generations."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.journal_taxonomy import (
    FINANCIAL_CONTROL,
    QUALIFICATION_FINANCIAL,
    require_journal_aggregate_descriptor,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import RecoveryController


class RecoveryClockIncidentChronologyTests(unittest.TestCase):
    def _durable(self, directory: str) -> tuple[Path, JournalStore, RecoveryController]:
        path = Path(directory) / "journal.sqlite3"
        store = JournalStore(path)
        controller = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:chronology-account",
        )
        controller.start("host-a")
        return path, store, controller

    def test_loss_is_restart_durable_and_duplicate_loss_does_not_advance(self) -> None:
        with TemporaryDirectory() as directory:
            path, _store, controller = self._durable(directory)
            self.assertTrue(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 0)

            controller.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-probe:1",
            )
            self.assertFalse(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 1)

            controller.set_clock_trusted(
                False,
                reason_code="duplicate-health-report",
                evidence_ref="clock-probe:duplicate",
            )
            self.assertEqual(controller.clock_incident_generation, 1)

            reopened = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:chronology-account",
            )
            self.assertFalse(reopened.clock_trusted)
            self.assertEqual(reopened.clock_incident_generation, 1)

    def test_restore_preserves_generation_and_next_loss_advances(self) -> None:
        with TemporaryDirectory() as directory:
            path, _store, controller = self._durable(directory)
            controller.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-probe:1",
            )
            controller.set_clock_trusted(
                True,
                reason_code="clock-health-requalified",
                evidence_ref="clock-probe:restore-1",
            )
            self.assertTrue(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 1)

            restored = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:chronology-account",
            )
            self.assertTrue(restored.clock_trusted)
            self.assertEqual(restored.clock_incident_generation, 1)

            controller.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed-again",
                evidence_ref="clock-probe:2",
            )
            self.assertEqual(controller.clock_incident_generation, 2)
            reopened_again = RecoveryController(
                owner_store=JournalStore(path),
                owner_scope="PAPER:chronology-account",
            )
            self.assertFalse(reopened_again.clock_trusted)
            self.assertEqual(reopened_again.clock_incident_generation, 2)

    def test_durable_pre_owner_loss_cannot_bypass_incident_journal(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            controller = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:chronology-account",
            )

            self.assertTrue(controller.clock_trusted)
            with self.assertRaises(PermissionError):
                controller.set_clock_trusted(
                    False,
                    reason_code="pre-owner-clock-loss",
                    evidence_ref="clock-probe:pre-owner",
                )

            self.assertTrue(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 0)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "recovery_clock_incident",
                    "PAPER:chronology-account",
                ),
                [],
            )

    def test_restart_cannot_locally_restore_unresolved_durable_incident_without_owner(self) -> None:
        with TemporaryDirectory() as directory:
            path, store, controller = self._durable(directory)
            controller.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-probe:1",
            )
            before = JournalStore.load_events(
                store,
                "recovery_clock_incident",
                "PAPER:chronology-account",
            )
            self.assertEqual(len(before), 1)

            reopened_store = JournalStore(path)
            reopened = RecoveryController(
                owner_store=reopened_store,
                owner_scope="PAPER:chronology-account",
            )
            self.assertFalse(reopened.clock_trusted)
            self.assertEqual(reopened.clock_incident_generation, 1)

            with self.assertRaises(PermissionError):
                reopened.set_clock_trusted(
                    True,
                    reason_code="ownerless-local-requalification",
                    evidence_ref="clock-probe:restore-without-owner",
                )

            self.assertFalse(reopened.clock_trusted)
            self.assertEqual(reopened.clock_incident_generation, 1)
            self.assertEqual(
                JournalStore.load_events(
                    reopened_store,
                    "recovery_clock_incident",
                    "PAPER:chronology-account",
                ),
                before,
            )

    def test_semantically_malformed_incident_chain_fails_closed_on_restart(self) -> None:
        with TemporaryDirectory() as directory:
            path, store, controller = self._durable(directory)
            controller.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-probe:1",
            )
            payload = {
                "schema_version": "1",
                "generation": "2",
                "incident_id": "00000000-0000-0000-0000-000000000000",
                "owner_id": "host-a",
                "owner_epoch": "1",
                "reason_code": "forged-overlap",
                "evidence_ref": "clock-probe:forged",
            }
            JournalStore.append_event(
                store,
                {
                    "event_id": "11111111-1111-1111-1111-111111111111",
                    "event_type": "RecoveryClockTrustLost",
                    "aggregate_type": "recovery_clock_incident",
                    "aggregate_id": "PAPER:chronology-account",
                    "aggregate_version": "2",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-10-03T08:00:00Z",
                },
            )

            with self.assertRaises(RuntimeError):
                RecoveryController(
                    owner_store=JournalStore(path),
                    owner_scope="PAPER:chronology-account",
                )

    @staticmethod
    def _append_owner(
        store: JournalStore,
        *,
        scope: str,
        owner_id: str,
        epoch: int,
    ) -> None:
        payload = {"owner_id": owner_id, "owner_epoch": str(epoch)}
        JournalStore.append_event(
            store,
            {
                "event_id": str(
                    uuid5(
                        NAMESPACE_URL,
                        f"https://test.autotrade.local/recovery-owner/{scope}/{epoch}/{owner_id}",
                    )
                ),
                "event_type": "RecoveryOwnerChanged",
                "aggregate_type": "recovery_owner",
                "aggregate_id": scope,
                "aggregate_version": str(epoch),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-03T00:00:00Z",
            },
        )

    def test_owner_inserted_before_captured_cut_invalidates_stale_controller(self) -> None:
        with TemporaryDirectory() as directory:
            _path, store, controller = self._durable(directory)
            original_current = JournalStore.current_journal_sequence
            injected = False

            def current_with_new_owner(selected_store):
                nonlocal injected
                if not injected:
                    injected = True
                    self._append_owner(
                        selected_store,
                        scope="PAPER:chronology-account",
                        owner_id="host-b",
                        epoch=2,
                    )
                return original_current(selected_store)

            with patch.object(
                JournalStore,
                "current_journal_sequence",
                new=current_with_new_owner,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "current durable recovery owner",
                ):
                    controller.set_clock_trusted(
                        False,
                        reason_code="stale-owner-before-cut",
                        evidence_ref="clock-probe:owner-race",
                    )

            self.assertTrue(injected)
            self.assertTrue(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 0)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "recovery_clock_incident",
                    "PAPER:chronology-account",
                ),
                [],
            )

    def test_post_validation_journal_advance_loses_atomic_frontier_cas(self) -> None:
        with TemporaryDirectory() as directory:
            _path, store, controller = self._durable(directory)
            original_commit = JournalStore.commit_command
            raced = False

            def racing_commit(selected_store, **kwargs):
                nonlocal raced
                if not raced:
                    raced = True
                    payload = {"race": "external-durable-change"}
                    JournalStore.append_event(
                        selected_store,
                        {
                            "event_id": str(
                                uuid5(
                                    NAMESPACE_URL,
                                    "https://test.autotrade.local/clock-cas-race",
                                )
                            ),
                            "event_type": "QualificationEvidenceRecorded",
                            "aggregate_type": "qualification_evidence",
                            "aggregate_id": "clock-cas-race",
                            "aggregate_version": "1",
                            "payload": payload,
                            "payload_hash": payload_digest(payload),
                            "committed_at": "2026-10-03T00:00:00Z",
                        },
                    )
                return original_commit(selected_store, **kwargs)

            with patch.object(
                JournalStore,
                "commit_command",
                new=racing_commit,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "journal sequence changed",
                ):
                    controller.set_clock_trusted(
                        False,
                        reason_code="race-with-durable-change",
                        evidence_ref="clock-probe:cas-race",
                    )

            self.assertTrue(raced)
            self.assertTrue(controller.clock_trusted)
            self.assertEqual(controller.clock_incident_generation, 0)
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "recovery_clock_incident",
                    "PAPER:chronology-account",
                ),
                [],
            )

    def test_incident_family_is_visible_financial_control(self) -> None:
        descriptor = require_journal_aggregate_descriptor("recovery_clock_incident")
        self.assertEqual(descriptor.domain_classification, FINANCIAL_CONTROL)
        self.assertEqual(
            descriptor.qualification_visibility,
            QUALIFICATION_FINANCIAL,
        )
        self.assertTrue(descriptor.is_financial_for_qualification)


if __name__ == "__main__":
    unittest.main()
