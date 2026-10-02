from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    provider_domain_submission_attempt_key,
    provider_domain_submission_attempt_keys,
    stable_client_order_id,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
    unknown_submissions_from_dispatch,
)
from mvp.autotrade_mvp.recovery import RecoveryController


class ProviderEnvironmentDispatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")

    def _append_recoverable_unknown(
        self,
        *,
        logical_attempt: str,
        durable_attempt: str,
        provider_environment: str,
        client_order_id: str | None = None,
    ) -> tuple[str, str]:
        client = client_order_id or f"client-{logical_attempt}"
        intent = f"intent-{logical_attempt}"
        dispatcher = GuardedDispatcher(
            self.store,
            environment="PAPER",
            account_id="paper-1",
            owner_token="sender-original",
            owner_epoch=1,
        )
        dispatcher._append(
            attempt_id=durable_attempt,
            event_type="SubmissionPrepared",
            version=1,
            payload={
                "attempt_id": logical_attempt,
                "intent_id": intent,
                "intent_hash": f"hash-{logical_attempt}",
                "provider": "BYBIT",
                "environment": "PAPER",
                "provider_environment": provider_environment,
                "account_id": "paper-1",
                "client_order_id": client,
                "owner_token": "sender-original",
                "owner_epoch": 1,
                "prepared_at": "2026-09-24T18:00:00Z",
            },
            now="2026-09-24T18:00:00Z",
        )
        dispatcher._append(
            attempt_id=durable_attempt,
            event_type="SubmissionUnknown",
            version=2,
            payload={
                "client_order_id": client,
                "reason": "restart-ambiguity",
            },
            now="2026-09-24T18:00:01Z",
        )
        return intent, client

    @staticmethod
    def _absence_result(
        *,
        provider_environment: str,
        logical_attempt: str,
        intent_id: str,
        client_order_id: str,
    ):
        unknown = UnknownSubmission.create(
            attempt_id=logical_attempt,
            intent_id=intent_id,
            client_order_id=client_order_id,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment=provider_environment,
            started_at="2026-09-24T18:00:00Z",
        )
        absence_coverage = tuple(
            CoverageSurfaceEvidence(
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
                provider_environment=provider_environment,
                surface=surface,
                coverage_start="2026-09-24T17:59:00Z",
                coverage_end="2026-09-24T18:05:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                provider_semantics_exclude_execution=True,
            )
            for surface in (
                "OPEN_ORDERS",
                "ORDER_HISTORY",
                "EXECUTIONS",
                "ACTIVITIES",
            )
        )
        return reconcile_account(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment=provider_environment,
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id="BYBIT",
                account_id="paper-1",
                environment="PAPER",
                provider_environment=provider_environment,
                mode="ATOMIC",
                query_started_at="2026-09-24T18:01:00Z",
                query_completed_at="2026-09-24T18:02:00Z",
            ),
            unknown_submissions=(unknown,),
            searched_client_order_ids=(client_order_id,),
            coverage_start="2026-09-24T17:59:00Z",
            coverage_end="2026-09-24T18:05:00Z",
            pagination_complete=True,
            absence_coverage=absence_coverage,
        )

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


    def test_recovery_reconstructs_v2_provider_domain_unknown(self):
        logical_attempt = "recover-v2"
        current_key, _legacy_key = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self._append_recoverable_unknown(
            logical_attempt=logical_attempt,
            durable_attempt=current_key,
            provider_environment="TESTNET",
        )
        recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )

        recovered = recovery.recover_durable_submission_uncertainty(
            environment="PAPER",
            account_id="paper-1",
        )

        self.assertEqual(recovered, (logical_attempt,))
        self.assertEqual(recovery.unresolved_attempts, {logical_attempt})

    def test_recovery_reads_legacy_v1_provider_domain_unknown(self):
        logical_attempt = "recover-v1"
        _current_key, legacy_key = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self._append_recoverable_unknown(
            logical_attempt=logical_attempt,
            durable_attempt=legacy_key,
            provider_environment="TESTNET",
        )
        recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )

        recovered = recovery.recover_durable_submission_uncertainty(
            environment="PAPER",
            account_id="paper-1",
        )

        self.assertEqual(recovered, (logical_attempt,))
        self.assertEqual(recovery.unresolved_attempts, {logical_attempt})

    def test_recovery_rejects_dual_v1_v2_provider_domain_state_atomically(self):
        logical_attempt = "recover-dual"
        durable_keys = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        self.assertEqual(len(durable_keys), 2)
        for durable_key in durable_keys:
            self._append_recoverable_unknown(
                logical_attempt=logical_attempt,
                durable_attempt=durable_key,
                provider_environment="TESTNET",
            )
        recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "multiple durable identity versions",
        ):
            recovery.recover_durable_submission_uncertainty(
                environment="PAPER",
                account_id="paper-1",
            )

        self.assertEqual(recovery.unresolved_attempts, set())
        self.assertEqual(recovery._unresolved_send_attempts, set())
        self.assertEqual(recovery._unresolved_send_bindings, {})
        self.assertEqual(recovery._recovered_unknown_identities, {})

    def test_recovery_does_not_use_demo_resolution_for_testnet_unknown(self):
        logical_attempt = "recover-domain-isolation"
        current_key, _legacy_key = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        intent_id, client_order_id = self._append_recoverable_unknown(
            logical_attempt=logical_attempt,
            durable_attempt=current_key,
            provider_environment="TESTNET",
        )
        demo_result = self._absence_result(
            provider_environment="DEMO",
            logical_attempt=logical_attempt,
            intent_id=intent_id,
            client_order_id=client_order_id,
        )
        self.assertEqual(
            demo_result.submission_resolutions[0].outcome,
            "PROVEN_ABSENT",
        )
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="demo-resolution",
            result=demo_result,
            observed_at="2026-09-24T18:05:00Z",
            host_id="host-restarted",
            owner_epoch="1",
        )

        recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )
        recovery.start("host-restarted")

        self.assertEqual(recovery.unresolved_attempts, {logical_attempt})

    def test_recovery_accepts_matching_testnet_terminal_resolution(self):
        logical_attempt = "recover-domain-match"
        current_key, _legacy_key = provider_domain_submission_attempt_keys(
            attempt_id=logical_attempt,
            provider_id="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        intent_id, client_order_id = self._append_recoverable_unknown(
            logical_attempt=logical_attempt,
            durable_attempt=current_key,
            provider_environment="TESTNET",
        )
        testnet_result = self._absence_result(
            provider_environment="TESTNET",
            logical_attempt=logical_attempt,
            intent_id=intent_id,
            client_order_id=client_order_id,
        )
        self.assertEqual(
            testnet_result.submission_resolutions[0].outcome,
            "PROVEN_ABSENT",
        )
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="testnet-resolution",
            result=testnet_result,
            observed_at="2026-09-24T18:05:00Z",
            host_id="host-restarted",
            owner_epoch="1",
        )

        recovery = RecoveryController(
            owner_store=self.store,
            owner_scope="PAPER:paper-1",
        )
        recovery.start("host-restarted")

        self.assertEqual(recovery.unresolved_attempts, set())


if __name__ == "__main__":
    unittest.main()
