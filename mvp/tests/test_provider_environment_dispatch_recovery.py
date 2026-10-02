from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    provider_domain_submission_attempt_key,
    provider_domain_submission_attempt_keys,
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

        same_domain = provider_domain_submission_attempt_key(
            attempt_id="attempt-1",
            provider_id="KRAKEN",
            environment="PAPER",
            provider_environment="PAPER",
        )
        self.assertNotEqual(same_domain, "attempt-1")
        self.assertNotEqual(same_domain, testnet)

    def test_provider_domain_key_cannot_be_preoccupied_by_crafted_logical_id(self):
        testnet_keys = provider_domain_submission_attempt_keys(
            attempt_id="attempt-1",
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self.assertEqual(len(testnet_keys), 2)
        legacy_testnet_key = testnet_keys[1]

        crafted_other_provider = provider_domain_submission_attempt_key(
            attempt_id=legacy_testnet_key,
            provider_id="KRAKEN",
            environment="PAPER",
            provider_environment="PAPER",
        )
        self.assertNotEqual(crafted_other_provider, testnet_keys[0])
        self.assertNotEqual(crafted_other_provider, legacy_testnet_key)

        bybit_live = provider_domain_submission_attempt_key(
            attempt_id="same-logical-attempt",
            provider_id="BYBIT",
            environment="LIVE",
            provider_environment="MAINNET",
        )
        kraken_live = provider_domain_submission_attempt_key(
            attempt_id="same-logical-attempt",
            provider_id="KRAKEN",
            environment="LIVE",
            provider_environment="LIVE",
        )
        self.assertNotEqual(bybit_live, kraken_live)

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

    def test_unknown_recovery_reads_legacy_provider_domain_identity(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="owner-1",
        )
        logical_attempt = "attempt-legacy-v1"
        current_key, legacy_key = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self.assertNotEqual(current_key, legacy_key)
        dispatcher._append(
            attempt_id=legacy_key,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": logical_attempt,
                "intent_id": "intent-legacy",
                "intent_hash": "hash-legacy",
                "provider": "BYBIT",
                "environment": "PAPER",
                "provider_environment": "TESTNET",
                "account_id": "paper-1",
                "client_order_id": "client-legacy-v1",
                "prepared_at": "2026-09-24T18:00:00Z",
            },
            now="2026-09-24T18:00:00Z",
        )
        dispatcher._append(
            attempt_id=legacy_key,
            event_type="SubmissionUnknown",
            version=2,
            payload={
                "client_order_id": "client-legacy-v1",
                "reason": "legacy-v1-ambiguity",
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

    def test_dual_identity_versions_fail_closed_on_recovery(self):
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="owner-1",
        )
        logical_attempt = "attempt-dual-version"
        keys = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self.assertEqual(len(keys), 2)
        for index, durable_key in enumerate(keys):
            dispatcher._append(
                attempt_id=durable_key,
                event_type="SubmissionPrepared",
                version=1,
                payload={
                    "attempt_id": logical_attempt,
                    "intent_id": "intent-dual",
                    "intent_hash": "hash-dual",
                    "provider": "BYBIT",
                    "environment": "PAPER",
                    "provider_environment": "TESTNET",
                    "account_id": "paper-1",
                    "client_order_id": "client-dual",
                    "prepared_at": "2026-09-24T18:00:00Z",
                },
                now="2026-09-24T18:00:00Z",
            )
            dispatcher._append(
                attempt_id=durable_key,
                event_type="SubmissionUnknown",
                version=2,
                payload={
                    "client_order_id": "client-dual",
                    "reason": f"dual-version-{index}",
                },
                now="2026-09-24T18:00:01Z",
            )

        with self.assertRaisesRegex(
            ValueError,
            "multiple durable identity versions",
        ):
            unknown_submissions_from_dispatch(
                self.store,
                attempt_ids=(logical_attempt,),
                environment="PAPER",
                account_id="paper-1",
                provider_id="BYBIT",
                provider_environment="TESTNET",
            )

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
