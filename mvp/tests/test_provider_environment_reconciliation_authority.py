from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.authority import (
    FinancialAdmissionBinding,
    InstrumentVersionIdentity,
    financial_admission_binding_payload,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    book_external_provider_cash_activity,
)
from mvp.autotrade_mvp.reconciliation import (
    ProviderActivityEvidence,
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    provider_fill_identity_payload,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    load_latest_reconciliation_checkpoint,
    load_latest_reconciliation_checkpoint_for_scope,
    load_submission_resolution_evidence,
    record_reconciliation_checkpoint,
)


PROVIDER = "BYBIT"
ACCOUNT = "paper-1"
ENVIRONMENT = "PAPER"
START = "2026-09-24T18:00:00Z"
END = "2026-09-24T18:01:00Z"


def fill(provider_environment: str | None) -> ProviderFillEvidence:
    return ProviderFillEvidence.create(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=provider_environment,
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="BTCUSDT",
        quantity="1",
        price="100",
        fee_amount="0",
        fee_currency="USDT",
        trade_time="2026-09-24T18:00:30Z",
        side="BUY",
        evidence_refs=("provider-read:sha256:" + "1" * 64,),
    )


def snapshot(provider_environment: str) -> SnapshotConsistencyEvidence:
    return SnapshotConsistencyEvidence(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=provider_environment,
        mode="ATOMIC",
        query_started_at=START,
        query_completed_at=END,
    )


def availability(provider_environment: str) -> ResourceAvailabilityEvidence:
    return ResourceAvailabilityEvidence(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=provider_environment,
        snapshot_id=f"balance-{provider_environment.lower()}",
        query_started_at=START,
        query_completed_at=END,
        valid_until="2026-09-24T18:05:00Z",
        available_resources={"CASH:USDT": "1000"},
        evidence_refs=(f"provider:balance-{provider_environment.lower()}",),
    )


def reconciliation(
    provider_environment: str,
    *,
    unknown_submissions=(),
):
    return reconcile_account(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        provider_environment=provider_environment,
        local_cash={},
        provider_cash={},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        unknown_submissions=unknown_submissions,
        snapshot_consistency=snapshot(provider_environment),
        coverage_start=START,
        coverage_end=END,
        pagination_complete=True,
        resource_availability=availability(provider_environment),
    )


class ProviderEnvironmentReconciliationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")

    def test_legacy_bybit_fill_is_readable_but_has_no_financial_identity(self):
        legacy = fill(None)
        self.assertIsNone(legacy.provider_environment)
        with self.assertRaisesRegex(
            ValueError,
            "provider fill identity requires exact provider_environment",
        ):
            provider_fill_identity_payload(legacy)

    def test_reconciliation_rejects_demo_fill_in_testnet_scope(self):
        with self.assertRaisesRegex(
            ValueError,
            "provider fill evidence provider_environment mismatch",
        ):
            reconcile_account(
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                provider_environment="TESTNET",
                local_cash={},
                provider_cash={},
                local_positions={},
                provider_positions={},
                local_execution_ids=(),
                provider_fills=(fill("DEMO"),),
                snapshot_consistency=snapshot("TESTNET"),
                coverage_start=START,
                coverage_end=END,
                pagination_complete=True,
                resource_availability=availability("TESTNET"),
            )

    def test_testnet_and_demo_have_independent_latest_checkpoint_truth(self):
        testnet = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="same-runtime-scope",
            result=reconciliation("TESTNET"),
            observed_at=END,
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        demo = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="same-runtime-scope",
            result=reconciliation("DEMO"),
            observed_at="2026-09-24T18:01:01Z",
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        self.assertNotEqual(testnet["event_id"], demo["event_id"])

        latest_testnet = load_latest_reconciliation_checkpoint_for_scope(
            self.store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="TESTNET",
        )
        latest_demo = load_latest_reconciliation_checkpoint_for_scope(
            self.store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="DEMO",
        )
        self.assertEqual(latest_testnet["event_id"], testnet["event_id"])
        self.assertEqual(latest_demo["event_id"], demo["event_id"])
        self.assertEqual(
            latest_testnet["payload"]["provider_environment"],
            "TESTNET",
        )
        self.assertEqual(
            latest_demo["payload"]["provider_environment"],
            "DEMO",
        )
        self.assertNotEqual(testnet["aggregate_id"], demo["aggregate_id"])

        # A retry of the earlier domain must resolve to its original event,
        # even after another provider domain has committed the same logical id.
        testnet_retry = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="same-runtime-scope",
            result=reconciliation("TESTNET"),
            observed_at=END,
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        self.assertEqual(testnet_retry["event_id"], testnet["event_id"])

        exact_testnet = load_latest_reconciliation_checkpoint(
            self.store,
            reconciliation_id="same-runtime-scope",
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="TESTNET",
        )
        exact_demo = load_latest_reconciliation_checkpoint(
            self.store,
            reconciliation_id="same-runtime-scope",
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="DEMO",
        )
        self.assertEqual(exact_testnet["event_id"], testnet["event_id"])
        self.assertEqual(exact_demo["event_id"], demo["event_id"])

    def test_submission_resolution_evidence_carries_checkpoint_domain(self):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-1",
            intent_id="intent-1",
            client_order_id="client-1",
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="TESTNET",
            started_at="2026-09-24T18:00:00Z",
        )
        checkpoint = record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="resolution-domain",
            result=reconciliation(
                "TESTNET",
                unknown_submissions=(unknown,),
            ),
            observed_at=END,
            host_id="host-1",
            owner_epoch="epoch-1",
        )
        evidence = load_submission_resolution_evidence(
            self.store,
            checkpoint_event_id=checkpoint["event_id"],
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="TESTNET",
            attempt_id="attempt-1",
            intent_id="intent-1",
            client_order_id="client-1",
        )
        self.assertEqual(evidence["provider_environment"], "TESTNET")

        with self.assertRaisesRegex(
            ValueError,
            "provider_environment|scope",
        ):
            load_submission_resolution_evidence(
                self.store,
                checkpoint_event_id=checkpoint["event_id"],
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                provider_environment="DEMO",
                attempt_id="attempt-1",
                intent_id="intent-1",
                client_order_id="client-1",
            )

    def test_financial_admission_binding_payload_carries_narrow_domain(self):
        binding = FinancialAdmissionBinding(
            admission_id="admission-1",
            intent_id="intent-1",
            reservation_id="reservation-1",
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="TESTNET",
            instrument_symbol="BTCUSDT",
            instrument_version=InstrumentVersionIdentity(
                "00000000-0000-0000-0000-000000000001",
                1,
            ),
            action="ORDER.SUBMIT",
            risk_decision_id="risk-1",
            financial_command_id="command-1",
            request_fingerprint="a" * 64,
            authority_event_id="event-1",
            authority_event_payload_hash="sha256:" + "b" * 64,
            authority_aggregate_version=1,
            authority_journal_sequence=1,
        )
        payload = financial_admission_binding_payload(binding)
        self.assertEqual(payload["schema_version"], "1.1.0")
        self.assertEqual(payload["provider_environment"], "TESTNET")

    def test_external_cash_activity_cannot_cross_provider_domain(self):
        activity = ProviderActivityEvidence.create(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            provider_environment="DEMO",
            activity_id="deposit-1",
            activity_type="DEPOSIT",
            origin="EXTERNAL",
            occurred_at="2026-09-24T18:00:00Z",
            currency="USDT",
            signed_amount=Decimal("10"),
        )
        with self.assertRaisesRegex(
            ValueError,
            "provider activity evidence provider_environment mismatch",
        ):
            book_external_provider_cash_activity(
                self.store,
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
                provider_environment="TESTNET",
                activity=activity,
                observed_at="2026-09-24T18:00:01Z",
            )


if __name__ == "__main__":
    unittest.main()
