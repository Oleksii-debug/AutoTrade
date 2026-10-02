from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    provider_domain_submission_attempt_key,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation_journal import (
    unknown_submissions_from_dispatch,
)


class ProviderEnvironmentDispatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")

    def test_narrow_provider_domains_partition_attempt_storage_identity(self):
        testnet = provider_domain_submission_attempt_key(
            attempt_id="attempt-1",
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        demo = provider_domain_submission_attempt_key(
            attempt_id="attempt-1",
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet, demo)

        legacy_compatible = provider_domain_submission_attempt_key(
            attempt_id="attempt-1",
            provider_id="KRAKEN",
            environment="PAPER",
            provider_environment="PAPER",
        )
        self.assertEqual(legacy_compatible, "attempt-1")

    def test_narrow_provider_domains_partition_client_order_identity(self):
        testnet = stable_client_order_id(
            "BYBIT",
            "intent-1",
            environment="PAPER",
            account_id="paper-1",
            provider_environment="TESTNET",
        )
        demo = stable_client_order_id(
            "BYBIT",
            "intent-1",
            environment="PAPER",
            account_id="paper-1",
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet, demo)

    def test_unknown_recovery_preserves_provider_domain(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="owner-1",
        )
        logical_attempt = "attempt-1"
        durable_attempt = provider_domain_submission_attempt_key(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        dispatcher._append(
            attempt_id=durable_attempt,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": logical_attempt,
                "intent_id": "intent-1",
                "intent_hash": "hash-1",
                "provider": "BYBIT",
                "environment": "PAPER",
                "provider_environment": "TESTNET",
                "account_id": "paper-1",
                "client_order_id": "client-1",
                "prepared_at": "2026-09-24T18:00:00Z",
            },
            now="2026-09-24T18:00:00Z",
        )
        dispatcher._append(
            attempt_id=durable_attempt,
            event_type="SubmissionUnknown",
            version=2,
            payload={
                "client_order_id": "client-1",
                "reason": "test-ambiguity",
            },
            now="2026-09-24T18:00:01Z",
        )

        recovered = unknown_submissions_from_dispatch(
            self.store,
            attempt_ids=(logical_attempt,),
            environment="PAPER",
            account_id="paper-1",
            provider_id="BYBIT",
            provider_environment="TESTNET",
        )
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].attempt_id, logical_attempt)
        self.assertEqual(recovered[0].provider_environment, "TESTNET")

    def test_legacy_bybit_submission_is_quarantined_on_recovery(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="owner-1",
        )
        dispatcher._append(
            attempt_id="legacy-attempt",
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": "legacy-attempt",
                "intent_id": "intent-1",
                "intent_hash": "hash-1",
                "provider": "BYBIT",
                "environment": "PAPER",
                "account_id": "paper-1",
                "client_order_id": "client-legacy",
                "prepared_at": "2026-09-24T18:00:00Z",
            },
            now="2026-09-24T18:00:00Z",
        )
        dispatcher._append(
            attempt_id="legacy-attempt",
            event_type="SubmissionUnknown",
            version=2,
            payload={
                "client_order_id": "client-legacy",
                "reason": "legacy-ambiguity",
            },
            now="2026-09-24T18:00:01Z",
        )
        with self.assertRaisesRegex(
            ValueError,
            "legacy BYBIT SubmissionPrepared lacks exact provider_environment",
        ):
            unknown_submissions_from_dispatch(
                self.store,
                attempt_ids=("legacy-attempt",),
                aggregate_ids={
                    "legacy-attempt": dispatcher._aggregate_id("legacy-attempt")
                },
            )


if __name__ == "__main__":
    unittest.main()
