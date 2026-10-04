import sqlite3
import tempfile
from contextlib import closing
import unittest
from decimal import Decimal
from pathlib import Path

from mvp.autotrade_mvp.authority import (
    AuthorityPolicy,
    AuthorityService,
    InstrumentVersionIdentity,
)
from mvp.autotrade_mvp.authority_persistence import (
    AUTHORITY_AGGREGATE_TYPE,
    AUTHORITY_EVENT_TYPE,
    persist_authority_snapshot,
    restore_authority_snapshot,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
AUTHORITY_ID = "runtime-authority"


class AuthorityPersistenceTests(unittest.TestCase):
    def _policy(self, *, autonomous: bool = False) -> AuthorityPolicy:
        return AuthorityPolicy.create(
            policy_id="policy-1",
            account_id="account-1",
            environments={"PAPER"},
            instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 2)},
            actions={"BUY"},
            max_notional=Decimal("100"),
            valid_from="2026-09-25T00:00:00Z",
            expires_at="2026-09-26T00:00:00Z",
            autonomous=autonomous,
        )

    def _service(self) -> AuthorityService:
        service = AuthorityService()
        service.register_policy(self._policy())
        service.add_confirmation(
            confirmation_id="confirm-1",
            policy_id="policy-1",
            intent_hash="intent-hash",
            account_id="account-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=2,
            action="BUY",
            notional=Decimal("10"),
            expires_at="2026-09-25T02:00:00Z",
        )
        admitted = service._admit_unverified(
            admission_id="admit-1",
            policy_id="policy-1",
            intent_hash="intent-hash",
            account_id="account-1",
            environment="PAPER",
            instrument_id=INSTRUMENT_ID,
            instrument_version=2,
            action="BUY",
            notional=Decimal("10"),
            state_version=7,
            risk_admitted=True,
            now="2026-09-25T01:00:00Z",
            confirmation_id="confirm-1",
        )
        self.assertEqual("ADMITTED", admitted.outcome)
        return service

    def _autonomous_service(self) -> AuthorityService:
        service = AuthorityService()
        service.register_policy(self._policy(autonomous=True))
        return service

    def _seed_legacy_snapshot(
        self,
        store: JournalStore,
        service: AuthorityService,
        *,
        event_id: str = "authority-snapshot-1",
        committed_at: str = "2026-09-25T01:00:01Z",
        aggregate_version: int = 1,
    ):
        """Model a pre-upgrade snapshot without using the retired write path."""
        state = service.export_state()
        payload = {"authority_id": AUTHORITY_ID, "state": state}
        return JournalStore.append_event(
            store,
            {
                "event_id": event_id,
                "event_type": AUTHORITY_EVENT_TYPE,
                "aggregate_type": AUTHORITY_AGGREGATE_TYPE,
                "aggregate_id": AUTHORITY_ID,
                "aggregate_version": str(aggregate_version),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": committed_at,
            },
        )

    def test_restart_preserves_legacy_admission_as_non_dispatchable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            self._seed_legacy_snapshot(store, self._service())

            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertEqual(1, restored.epoch)
            self.assertEqual(
                (False, "financial_evidence_missing"),
                restored.dispatch_allowed(
                    "admit-1",
                    intent_hash="intent-hash",
                    account_id="account-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=2,
                    action="BUY",
                    now="2026-09-25T01:30:00Z",
                ),
            )

    def test_new_store_cannot_bootstrap_nonempty_snapshot_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    self._service(),
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-1",
                    committed_at="2026-09-25T01:00:01Z",
                )
            self.assertEqual([], store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID))

    def test_new_store_cannot_bootstrap_even_empty_snapshot_writer_lineage(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    AuthorityService(),
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-empty",
                    committed_at="2026-09-25T01:00:01Z",
                )
            self.assertEqual([], store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID))

    def test_legacy_policy_mutation_cannot_publish_new_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            self._seed_legacy_snapshot(store, self._service())
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            restored.revoke_policy(
                "policy-1",
                reason="operator-revoked",
                revoked_at="2026-09-25T01:05:00Z",
            )

            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    restored,
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-policy-mint",
                    committed_at="2026-09-25T01:05:01Z",
                )
            self.assertEqual(1, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))

    def test_legacy_confirmation_mutation_cannot_publish_new_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._autonomous_service()
            self._seed_legacy_snapshot(store, service)
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            restored.add_confirmation(
                confirmation_id="confirm-new",
                policy_id="policy-1",
                intent_hash="intent-new",
                account_id="account-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("5"),
                expires_at="2026-09-25T02:00:00Z",
            )

            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    restored,
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-confirmation-mint",
                    committed_at="2026-09-25T01:05:01Z",
                )
            self.assertEqual(1, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))

    def test_legacy_admission_mutation_cannot_publish_new_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._autonomous_service()
            self._seed_legacy_snapshot(store, service)
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            admitted = restored._admit_unverified(
                admission_id="admit-new",
                policy_id="policy-1",
                intent_hash="intent-new",
                account_id="account-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("5"),
                state_version=8,
                risk_admitted=True,
                now="2026-09-25T01:05:00Z",
            )
            self.assertEqual("ADMITTED", admitted.outcome)

            with self.assertRaisesRegex(ValueError, "requires canonical authority"):
                persist_authority_snapshot(
                    store,
                    restored,
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-admission-mint",
                    committed_at="2026-09-25T01:05:01Z",
                )
            self.assertEqual(1, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))

    def test_lost_reply_retry_reuses_original_authority_event_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._service()
            first = self._seed_legacy_snapshot(store, service)
            retried = persist_authority_snapshot(
                store,
                service,
                authority_id=AUTHORITY_ID,
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T09:59:59Z",
            )
            self.assertTrue(first.inserted)
            self.assertFalse(retried.inserted)
            self.assertEqual(1, retried.aggregate_version)
            self.assertEqual(1, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))

    def test_lost_reply_retry_with_changed_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._service()
            self._seed_legacy_snapshot(store, service)
            service.revoke_policy(
                "policy-1",
                reason="operator-revoked",
                revoked_at="2026-09-25T01:05:00Z",
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                persist_authority_snapshot(
                    store,
                    service,
                    authority_id=AUTHORITY_ID,
                    event_id="authority-snapshot-1",
                    committed_at="2026-09-25T01:05:01Z",
                )
            self.assertEqual(1, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))

    def test_historical_lost_reply_retry_survives_newer_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            initial = self._service()
            first = self._seed_legacy_snapshot(store, initial)
            advanced = AuthorityService.restore(initial.export_state())
            advanced.revoke_policy(
                "policy-1",
                reason="operator-revoked",
                revoked_at="2026-09-25T01:05:00Z",
            )
            self._seed_legacy_snapshot(
                store,
                advanced,
                event_id="authority-snapshot-2",
                committed_at="2026-09-25T01:05:01Z",
                aggregate_version=2,
            )

            retry = persist_authority_snapshot(
                store,
                initial,
                authority_id=AUTHORITY_ID,
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T09:59:59Z",
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(first.event_id, retry.event_id)
            self.assertEqual(2, len(store.load_events(AUTHORITY_AGGREGATE_TYPE, AUTHORITY_ID)))
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertEqual(2, restored.epoch)

    def test_confirmation_remains_consumed_after_legacy_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            self._seed_legacy_snapshot(store, self._service())
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            second = restored._admit_unverified(
                admission_id="admit-2",
                policy_id="policy-1",
                intent_hash="intent-hash",
                account_id="account-1",
                environment="PAPER",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("10"),
                state_version=8,
                risk_admitted=True,
                now="2026-09-25T01:10:00Z",
                confirmation_id="confirm-1",
            )
            self.assertEqual("REJECTED", second.outcome)
            self.assertEqual("confirmation_already_used", second.reason)

    def test_missing_durable_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            with self.assertRaisesRegex(ValueError, "missing"):
                restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

    def test_tampered_legacy_journal_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            self._seed_legacy_snapshot(store, self._service())
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"authority_id":"runtime-authority","state":{}}', "authority-snapshot-1"),
                )
                connection.commit()
            with self.assertRaisesRegex(ValueError, "payload hash"):
                restore_authority_snapshot(store, authority_id=AUTHORITY_ID)


if __name__ == "__main__":
    unittest.main()
