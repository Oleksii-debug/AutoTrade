import tempfile
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
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


AUTHORITY_ID = "runtime-authority"
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class AuthorityPersistenceCanonicalProjectionTests(unittest.TestCase):
    def _store(self, directory: str) -> JournalStore:
        return JournalStore(Path(directory) / "journal.sqlite")

    def _policy(self, *, autonomous: bool, environment: str = "PAPER") -> AuthorityPolicy:
        return AuthorityPolicy.create(
            policy_id="policy-1",
            account_id="account-1",
            environments={environment},
            instruments={InstrumentVersionIdentity(INSTRUMENT_ID, 2)},
            actions={"BUY"},
            max_notional=Decimal("100"),
            valid_from="2026-09-25T00:00:00Z",
            expires_at="2026-09-26T00:00:00Z",
            autonomous=autonomous,
        )

    def _snapshot(
        self,
        store: JournalStore,
        service: AuthorityService,
        event_id: str,
        committed_at: str,
    ):
        return persist_authority_snapshot(
            store,
            service,
            authority_id=AUTHORITY_ID,
            event_id=event_id,
            committed_at=committed_at,
        )

    def test_canonical_revocation_without_intermediate_snapshot_blocks_stale_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            canonical.register_policy(self._policy(autonomous=True))
            self._snapshot(
                store,
                canonical,
                "snapshot-before-revocation",
                "2026-09-25T01:00:00Z",
            )
            stale = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

            canonical.revoke_policy(
                "policy-1",
                reason="operator-revoked",
                revoked_at="2026-09-25T01:05:00Z",
            )

            with self.assertRaisesRegex(ValueError, "revocations facts were removed"):
                self._snapshot(
                    store,
                    stale,
                    "snapshot-stale-after-revocation",
                    "2026-09-25T01:06:00Z",
                )
            self.assertEqual(
                1,
                len(store.load_events("financial-authority", AUTHORITY_ID)),
            )

            # The already-published pre-revocation snapshot is also too stale to
            # restart safely once canonical authority has advanced.
            with self.assertRaisesRegex(ValueError, "revocations facts were removed"):
                restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

            self._snapshot(
                store,
                canonical,
                "snapshot-after-revocation",
                "2026-09-25T01:06:01Z",
            )
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            self.assertEqual(2, restored.epoch)

    def test_canonical_confirmation_consumption_without_intermediate_snapshot_cannot_be_erased(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            canonical.register_policy(
                self._policy(autonomous=False, environment="SIMULATION")
            )
            canonical.add_confirmation(
                confirmation_id="confirm-1",
                policy_id="policy-1",
                intent_hash="intent-hash",
                account_id="account-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("10"),
                expires_at="2026-09-25T02:00:00Z",
            )
            self._snapshot(
                store,
                canonical,
                "snapshot-before-admission",
                "2026-09-25T01:00:00Z",
            )
            stale = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

            admitted = canonical._admit_unverified(
                admission_id="admit-1",
                policy_id="policy-1",
                intent_hash="intent-hash",
                account_id="account-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("10"),
                state_version=7,
                risk_admitted=True,
                now="2026-09-25T01:05:00Z",
                confirmation_id="confirm-1",
            )
            self.assertEqual("ADMITTED", admitted.outcome)

            with self.assertRaisesRegex(ValueError, "admissions facts were removed"):
                self._snapshot(
                    store,
                    stale,
                    "snapshot-stale-after-admission",
                    "2026-09-25T01:06:00Z",
                )
            self.assertEqual(
                1,
                len(store.load_events("financial-authority", AUTHORITY_ID)),
            )
            with self.assertRaisesRegex(ValueError, "admissions facts were removed"):
                restore_authority_snapshot(store, authority_id=AUTHORITY_ID)

            self._snapshot(
                store,
                canonical,
                "snapshot-after-admission",
                "2026-09-25T01:06:01Z",
            )
            restored = restore_authority_snapshot(store, authority_id=AUTHORITY_ID)
            retry = restored._admit_unverified(
                admission_id="admit-2",
                policy_id="policy-1",
                intent_hash="intent-hash",
                account_id="account-1",
                environment="SIMULATION",
                instrument_id=INSTRUMENT_ID,
                instrument_version=2,
                action="BUY",
                notional=Decimal("10"),
                state_version=8,
                risk_admitted=True,
                now="2026-09-25T01:10:00Z",
                confirmation_id="confirm-1",
            )
            self.assertEqual("REJECTED", retry.outcome)
            self.assertEqual("confirmation_already_used", retry.reason)

    def test_snapshot_cannot_invent_noncanonical_authority_fact_once_canonical_exists(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            canonical = AuthorityService(store)
            canonical.register_policy(self._policy(autonomous=True))
            self._snapshot(
                store,
                canonical,
                "snapshot-canonical",
                "2026-09-25T01:00:00Z",
            )

            forged = AuthorityService.restore(canonical.export_state())
            forged.add_confirmation(
                confirmation_id="forged-confirmation",
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

            with self.assertRaisesRegex(
                ValueError,
                "facts without canonical authority",
            ):
                self._snapshot(
                    store,
                    forged,
                    "snapshot-forged-confirmation",
                    "2026-09-25T01:01:00Z",
                )
            self.assertEqual(
                1,
                len(store.load_events("financial-authority", AUTHORITY_ID)),
            )

    def test_projection_reuses_canonical_transition_validation_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = self._store(directory)
            payload = {
                "policy_id": "missing-policy",
                "reason": "invalid history",
                "revoked_at": "2026-09-25T01:00:00Z",
            }
            store.append_event(
                {
                    "event_id": "invalid-canonical-revocation",
                    "event_type": "AuthorityPolicyRevoked",
                    "aggregate_type": "authority_state",
                    "aggregate_id": "canonical",
                    "aggregate_version": "1",
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "committed_at": "2026-09-25T01:00:00Z",
                }
            )

            with self.assertRaisesRegex(
                ValueError,
                "canonical authority journal cannot be projected",
            ):
                self._snapshot(
                    store,
                    AuthorityService(),
                    "snapshot-invalid-canonical-history",
                    "2026-09-25T01:00:01Z",
                )
            self.assertEqual(
                0,
                len(store.load_events("financial-authority", AUTHORITY_ID)),
            )


if __name__ == "__main__":
    unittest.main()
