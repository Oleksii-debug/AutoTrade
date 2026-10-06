from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    _reconciliation_aggregate_id,
    load_latest_reconciliation_checkpoint_for_scope,
    load_reconciliation_checkpoint_for_readiness,
    load_submission_resolution_evidence,
    reconciliation_payload,
    record_reconciliation_checkpoint,
)


class ReconciliationNegativeResolutionAuthorityTests(unittest.TestCase):
    def _diagnostic_result(
        self,
        *,
        provider_id: str = "BYBIT",
        environment: str = "PAPER",
        prove_absence: bool = True,
    ):
        unknown = UnknownSubmission.create(
            attempt_id="attempt-negative-authority",
            intent_id="intent-negative-authority",
            client_order_id="client-negative-authority",
            provider_id=provider_id,
            account_id="paper-account",
            environment=environment,
            started_at="2026-10-05T08:00:00Z",
        )
        coverage = tuple(
            CoverageSurfaceEvidence(
                provider_id=provider_id,
                account_id="paper-account",
                environment=environment,
                surface=surface,
                coverage_start="2026-10-05T07:55:00Z",
                coverage_end="2026-10-05T08:10:00Z",
                pagination_complete=True,
                consistency_horizon_satisfied=True,
                provider_semantics_exclude_execution=prove_absence,
            )
            for surface in (
                "OPEN_ORDERS",
                "ORDER_HISTORY",
                "EXECUTIONS",
                "ACTIVITIES",
            )
        )
        result = reconcile_account(
            provider_id=provider_id,
            account_id="paper-account",
            environment=environment,
            local_cash={},
            provider_cash={},
            local_positions={},
            provider_positions={},
            local_execution_ids=(),
            provider_fills=(),
            snapshot_consistency=SnapshotConsistencyEvidence(
                provider_id=provider_id,
                account_id="paper-account",
                environment=environment,
                mode="ATOMIC",
                query_started_at="2026-10-05T08:00:00Z",
                query_completed_at="2026-10-05T08:05:00Z",
            ),
            unknown_submissions=(unknown,),
            searched_client_order_ids=(unknown.client_order_id,),
            coverage_start="2026-10-05T07:55:00Z",
            coverage_end="2026-10-05T08:10:00Z",
            pagination_complete=True,
            absence_coverage=coverage,
        )
        expected = "PROVEN_ABSENT" if prove_absence else "UNKNOWN"
        self.assertEqual(result.submission_resolutions[0].outcome, expected)
        self.assertEqual(result.complete, prove_absence)
        return result, unknown

    def _append_legacy_negative(
        self,
        store: JournalStore,
        *,
        reconciliation_id: str = "legacy-negative",
    ):
        result, unknown = self._diagnostic_result()
        payload = reconciliation_payload(
            result,
            observed_at="2026-10-05T08:10:00Z",
        )
        payload["checkpoint_owner"] = {
            "host_id": "legacy-host",
            "owner_epoch": "1",
        }
        aggregate_id = _reconciliation_aggregate_id(
            reconciliation_id=reconciliation_id,
            provider_id=result.provider_id,
            account_id=result.account_id,
            environment=result.environment,
        )
        event_id = "legacy-real-provider-proven-absent-" + reconciliation_id
        store.append_event(
            {
                "event_id": event_id,
                "event_type": "AccountReconciled",
                "schema_version": "1.0.0",
                "aggregate_type": "account_reconciliation",
                "aggregate_id": aggregate_id,
                "aggregate_version": "1",
                "host_id": "legacy-host",
                "owner_epoch": "1",
                "environment": result.environment,
                "occurred_at": "2026-10-05T08:10:00Z",
                "observed_at": "2026-10-05T08:10:00Z",
                "committed_at": "2026-10-05T08:10:00Z",
                "correlation_id": event_id,
                "causation_id": None,
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "evidence_refs": [],
            }
        )
        return event_id, result, unknown

    def test_real_provider_diagnostic_absence_cannot_be_published_as_financial_truth(self):
        result, _unknown = self._diagnostic_result()
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            before = store.current_journal_sequence()
            with self.assertRaisesRegex(
                ValueError,
                "PROVEN_ABSENT.*coverage authority",
            ):
                record_reconciliation_checkpoint(
                    store,
                    reconciliation_id="real-provider-negative",
                    result=result,
                    observed_at="2026-10-05T08:10:00Z",
                    host_id="host-a",
                    owner_epoch="1",
                )
            self.assertEqual(store.current_journal_sequence(), before)
            self.assertEqual(
                store.load_events_by_aggregate_type("account_reconciliation"),
                [],
            )

    def test_legacy_real_provider_negative_checkpoint_is_rejected_on_read(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            event_id, result, unknown = self._append_legacy_negative(store)

            with self.assertRaisesRegex(
                ValueError,
                "PROVEN_ABSENT.*coverage authority",
            ):
                load_submission_resolution_evidence(
                    store,
                    checkpoint_event_id=event_id,
                    provider_id=result.provider_id,
                    account_id=result.account_id,
                    environment=result.environment,
                    attempt_id=unknown.attempt_id,
                    intent_id=unknown.intent_id,
                    client_order_id=unknown.client_order_id,
                )
            with self.assertRaisesRegex(
                ValueError,
                "PROVEN_ABSENT.*coverage authority",
            ):
                load_reconciliation_checkpoint_for_readiness(
                    store,
                    reconciliation_id="legacy-negative",
                    provider_id=result.provider_id,
                    account_id=result.account_id,
                    environment=result.environment,
                    host_id="legacy-host",
                    owner_epoch="1",
                )

    def test_newer_safe_checkpoint_can_supersede_legacy_negative_scope_head(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            self._append_legacy_negative(store, reconciliation_id="old-unsafe")
            safe_result, _unknown = self._diagnostic_result(prove_absence=False)
            safe = record_reconciliation_checkpoint(
                store,
                reconciliation_id="new-safe",
                result=safe_result,
                observed_at="2026-10-05T08:11:00Z",
                host_id="host-new",
                owner_epoch="2",
            )
            current = load_latest_reconciliation_checkpoint_for_scope(
                store,
                provider_id=safe_result.provider_id,
                account_id=safe_result.account_id,
                environment=safe_result.environment,
            )
            self.assertEqual(current["event_id"], safe["event_id"])
            self.assertEqual(
                current["payload"]["submission_resolutions"][0]["outcome"],
                "UNKNOWN",
            )

    def test_synthetic_paper_provider_keeps_non_provider_test_path(self):
        result, unknown = self._diagnostic_result(provider_id="SIMULATED")
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="synthetic-negative",
                result=result,
                observed_at="2026-10-05T08:10:00Z",
                host_id="host-a",
                owner_epoch="1",
            )
            evidence = load_submission_resolution_evidence(
                store,
                checkpoint_event_id=checkpoint["event_id"],
                provider_id=result.provider_id,
                account_id=result.account_id,
                environment=result.environment,
                attempt_id=unknown.attempt_id,
                intent_id=unknown.intent_id,
                client_order_id=unknown.client_order_id,
            )
            self.assertEqual(evidence["outcome"], "PROVEN_ABSENT")


if __name__ == "__main__":
    unittest.main()
