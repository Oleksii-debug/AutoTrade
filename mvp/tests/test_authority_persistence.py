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
    persist_authority_snapshot,
    restore_authority_snapshot,
)
from mvp.autotrade_mvp.persistence import JournalStore


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class AuthorityPersistenceTests(unittest.TestCase):
    def _service(self):
        service = AuthorityService()
        service.register_policy(
            AuthorityPolicy.create(
                policy_id="policy-1",
                account_id="account-1",
                environments={"PAPER"},
                instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 2)},
                actions={"BUY"},
                max_notional=Decimal("100"),
                valid_from="2026-09-25T00:00:00Z",
                expires_at="2026-09-26T00:00:00Z",
                autonomous=False,
            )
        )
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

    def test_restart_preserves_legacy_admission_as_non_dispatchable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            persist_authority_snapshot(
                store,
                self._service(),
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:01Z",
            )

            restored = restore_authority_snapshot(
                store, authority_id="runtime-authority"
            )
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
            self.assertEqual(
                (False, "admission_scope_changed"),
                restored.dispatch_allowed(
                    "admit-1",
                    intent_hash="intent-hash",
                    account_id="account-1",
                    environment="PAPER",
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=3,
                    action="BUY",
                    now="2026-09-25T01:30:00Z",
                ),
            )

    def test_lost_reply_retry_reuses_original_authority_event_version(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._service()
            first = persist_authority_snapshot(
                store,
                service,
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:01Z",
            )
            retried = persist_authority_snapshot(
                store,
                service,
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:02Z",
            )
            self.assertTrue(first.inserted)
            self.assertFalse(retried.inserted)
            self.assertEqual(first.aggregate_version, 1)
            self.assertEqual(retried.aggregate_version, 1)
            stored = store.load_events(
                "financial-authority",
                "runtime-authority",
            )
            self.assertEqual(len(stored), 1)
            self.assertEqual(stored[0]["aggregate_version"], 1)
            self.assertEqual(
                stored[0]["payload"]["authority_id"],
                "runtime-authority",
            )

    def test_lost_reply_retry_with_changed_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._service()
            persist_authority_snapshot(
                store,
                service,
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:01Z",
            )
            service.revoke_policy(
                "policy-1",
                reason="operator-revoked",
                revoked_at="2026-09-25T01:05:00Z",
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                persist_authority_snapshot(
                    store,
                    service,
                    authority_id="runtime-authority",
                    event_id="authority-snapshot-1",
                    committed_at="2026-09-25T01:05:01Z",
                )
            self.assertEqual(
                len(
                    store.load_events(
                        "financial-authority",
                        "runtime-authority",
                    )
                ),
                1,
            )

    def test_confirmation_remains_consumed_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite")
            service = self._service()
            persist_authority_snapshot(
                store,
                service,
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:01Z",
            )
            restored = restore_authority_snapshot(
                store, authority_id="runtime-authority"
            )
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
                restore_authority_snapshot(
                    store, authority_id="runtime-authority"
                )

    def test_tampered_journal_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite"
            store = JournalStore(path)
            persist_authority_snapshot(
                store,
                self._service(),
                authority_id="runtime-authority",
                event_id="authority-snapshot-1",
                committed_at="2026-09-25T01:00:01Z",
            )
            with closing(sqlite3.connect(path)) as connection:
                connection.execute(
                    "UPDATE events SET payload_json = ? WHERE event_id = ?",
                    ('{"authority_id":"runtime-authority","state":{}}', "authority-snapshot-1"),
                )
                connection.commit()
            with self.assertRaisesRegex(ValueError, "payload hash"):
                restore_authority_snapshot(
                    store, authority_id="runtime-authority"
                )


if __name__ == "__main__":
    unittest.main()
