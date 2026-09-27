import tempfile
import unittest
from pathlib import Path

from mvp.autotrade_mvp.authority import AuthorityService
from mvp.autotrade_mvp.authority_persistence import (
    persist_authority_snapshot,
    restore_authority_snapshot,
)
from mvp.autotrade_mvp.persistence import JournalStore


AUTHORITY_ID = "runtime-authority"
ACCOUNT_ID = "account-1"
ENVIRONMENT = "PAPER"
BLOCK_COMMAND = "block-1"
BLOCK_REASON = "reconciliation uncertainty"
BLOCKED_AT = "2026-09-25T01:00:00Z"


class AuthorityPersistenceNewExposureFenceTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.sqlite")

    def _block(self, service: AuthorityService) -> None:
        self.assertTrue(
            service.block_new_exposure(
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                reason=BLOCK_REASON,
                blocked_at=BLOCKED_AT,
                command_id=BLOCK_COMMAND,
            )
        )

    def _restore(self, service: AuthorityService) -> None:
        self.assertTrue(
            service.restore_new_exposure(
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                reason="owner-qualified restore",
                restored_at="2026-09-25T01:10:00Z",
                command_id="restore-1",
                expected_block_command_id=BLOCK_COMMAND,
                expected_block_reason=BLOCK_REASON,
                expected_blocked_at=BLOCKED_AT,
            )
        )

    def test_stale_snapshot_cannot_erase_active_new_exposure_block(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-pre-block",
                committed_at="2026-09-25T00:59:00Z",
            )
            stale = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

            self._block(canonical)
            persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-blocked",
                committed_at="2026-09-25T01:00:01Z",
            )

            with self.assertRaisesRegex(
                ValueError,
                "new-exposure block was removed without exact durable restore",
            ):
                persist_authority_snapshot(
                    store,
                    stale,
                    authority_id=AUTHORITY_ID,
                    event_id="snapshot-stale-erase",
                    committed_at="2026-09-25T01:01:00Z",
                )

            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertTrue(
                restored.is_new_exposure_blocked(ACCOUNT_ID, ENVIRONMENT)
            )

    def test_snapshot_cannot_rewrite_active_block_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            self._block(canonical)
            persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-blocked",
                committed_at="2026-09-25T01:00:01Z",
            )

            tampered = canonical.export_state()
            tampered["new_exposure_blocks"][0]["reason"] = "different reason"
            rewritten = AuthorityService.restore(tampered)

            with self.assertRaisesRegex(
                ValueError,
                "rewrites durable new-exposure block",
            ):
                persist_authority_snapshot(
                    store,
                    rewritten,
                    authority_id=AUTHORITY_ID,
                    event_id="snapshot-rewritten-block",
                    committed_at="2026-09-25T01:02:00Z",
                )

    def test_exact_durable_restore_can_remove_only_the_exact_active_block(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            self._block(canonical)
            persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-blocked",
                committed_at="2026-09-25T01:00:01Z",
            )

            self._restore(canonical)
            result = persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-restored",
                committed_at="2026-09-25T01:10:01Z",
            )
            self.assertTrue(result.inserted)

            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertFalse(
                restored.is_new_exposure_blocked(ACCOUNT_ID, ENVIRONMENT)
            )

    def test_historical_blocked_snapshot_lost_reply_retry_survives_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            self._block(canonical)
            first = persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-blocked",
                committed_at="2026-09-25T01:00:01Z",
            )
            retry_service = AuthorityService.restore(canonical.export_state())

            self._restore(canonical)
            persist_authority_snapshot(
                store,
                canonical,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-restored",
                committed_at="2026-09-25T01:10:01Z",
            )

            retry = persist_authority_snapshot(
                store,
                retry_service,
                authority_id=AUTHORITY_ID,
                event_id="snapshot-blocked",
                committed_at="2026-09-25T09:59:59Z",
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(first.event_id, retry.event_id)
            self.assertEqual(first.aggregate_version, retry.aggregate_version)
            self.assertEqual(
                2,
                len(store.load_events("financial-authority", AUTHORITY_ID)),
            )
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertFalse(
                restored.is_new_exposure_blocked(ACCOUNT_ID, ENVIRONMENT)
            )


if __name__ == "__main__":
    unittest.main()
