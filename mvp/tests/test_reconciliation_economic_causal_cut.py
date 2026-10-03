from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    book_external_provider_cash_activity,
)
from mvp.autotrade_mvp.reconciliation import (
    ProviderActivityEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    load_account_resource_availability_evidence,
    record_reconciliation_checkpoint,
)


PROVIDER_ID = "ALPACA"
ACCOUNT_ID = "causal-cut-account"
ENVIRONMENT = "PAPER"


def _snapshot(*, started_at: str, completed_at: str) -> SnapshotConsistencyEvidence:
    return SnapshotConsistencyEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        mode="ATOMIC",
        query_started_at=started_at,
        query_completed_at=completed_at,
    )


def _availability(
    *,
    snapshot_id: str,
    started_at: str,
    completed_at: str,
) -> ResourceAvailabilityEvidence:
    return ResourceAvailabilityEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        snapshot_id=snapshot_id,
        query_started_at=started_at,
        query_completed_at=completed_at,
        provider_as_of=completed_at,
        valid_until="2026-09-24T19:05:00Z",
        available_resources={"CASH:USD": "850"},
        evidence_refs=(f"provider:{snapshot_id}",),
    )


def _checkpoint_result(
    *,
    snapshot_id: str,
    started_at: str,
    completed_at: str,
):
    return reconcile_account(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        local_cash={"USD": "0"},
        provider_cash={"USD": "0"},
        local_positions={},
        provider_positions={},
        local_execution_ids=(),
        provider_fills=(),
        snapshot_consistency=_snapshot(
            started_at=started_at,
            completed_at=completed_at,
        ),
        coverage_start=started_at,
        coverage_end=completed_at,
        pagination_complete=True,
        resource_availability=_availability(
            snapshot_id=snapshot_id,
            started_at=started_at,
            completed_at=completed_at,
        ),
    )


def _record_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str,
    snapshot_id: str,
    started_at: str,
    completed_at: str,
    observed_at: str,
):
    return record_reconciliation_checkpoint(
        store,
        reconciliation_id=reconciliation_id,
        result=_checkpoint_result(
            snapshot_id=snapshot_id,
            started_at=started_at,
            completed_at=completed_at,
        ),
        observed_at=observed_at,
        host_id="test-host",
        owner_epoch="epoch-1",
    )


def _book_external_cash(store: JournalStore) -> None:
    activity = ProviderActivityEvidence.create(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        activity_id="deposit-after-provider-cut",
        activity_type="DEPOSIT",
        origin="EXTERNAL",
        occurred_at="2026-09-24T19:00:05Z",
        currency="USD",
        signed_amount="25",
    )
    _, inserted = book_external_provider_cash_activity(
        store,
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        provider_environment=ENVIRONMENT,
        activity=activity,
        observed_at="2026-09-24T19:00:10Z",
    )
    if not inserted:
        raise AssertionError("causal-cut fixture must create one economic event")


class ReconciliationEconomicCausalCutTests(unittest.TestCase):
    def test_current_cash_authority_requires_provider_cut_after_economic_truth(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")

            old = _record_checkpoint(
                store,
                reconciliation_id="provider-cut-before-deposit",
                snapshot_id="capacity-before-deposit",
                started_at="2026-09-24T18:59:50Z",
                completed_at="2026-09-24T19:00:00Z",
                observed_at="2026-09-24T19:00:00Z",
            )
            _book_external_cash(store)

            # The economic event is newer than the reconciliation checkpoint.
            # The current local book must not borrow capital authority from the
            # older provider snapshot merely because both facts are valid alone.
            with self.assertRaisesRegex(
                ValueError,
                "availability checkpoint predates economic financial truth",
            ):
                load_account_resource_availability_evidence(
                    store,
                    checkpoint_event_id=old["event_id"],
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    provider_environment=ENVIRONMENT,
                    resources=("CASH:USD",),
                    now="2026-09-24T19:00:30Z",
                    max_age_seconds="60",
                    require_latest_scope=True,
                )

            relabelled_old_cut = _record_checkpoint(
                store,
                reconciliation_id="old-provider-cut-after-deposit",
                snapshot_id="capacity-old-relabelled",
                started_at="2026-09-24T18:59:50Z",
                completed_at="2026-09-24T19:00:00Z",
                observed_at="2026-09-24T19:00:20Z",
            )
            with self.assertRaisesRegex(
                ValueError,
                "resource availability snapshot predates economic financial truth",
            ):
                load_account_resource_availability_evidence(
                    store,
                    checkpoint_event_id=relabelled_old_cut["event_id"],
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    provider_environment=ENVIRONMENT,
                    resources=("CASH:USD",),
                    now="2026-09-24T19:00:30Z",
                    max_age_seconds="60",
                    require_latest_scope=True,
                )

            fresh = _record_checkpoint(
                store,
                reconciliation_id="provider-cut-after-deposit",
                snapshot_id="capacity-after-deposit",
                started_at="2026-09-24T19:00:15Z",
                completed_at="2026-09-24T19:00:20Z",
                observed_at="2026-09-24T19:00:20Z",
            )
            evidence = load_account_resource_availability_evidence(
                store,
                checkpoint_event_id=fresh["event_id"],
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                provider_environment=ENVIRONMENT,
                resources=("CASH:USD",),
                now="2026-09-24T19:00:30Z",
                max_age_seconds="60",
                require_latest_scope=True,
            )
            self.assertEqual(evidence["availability"], {"CASH:USD": "850"})


if __name__ == "__main__":
    unittest.main()
