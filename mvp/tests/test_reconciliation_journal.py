from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation import (
    ProviderActivityEvidence,
    ProviderFillEvidence,
    ResourceAvailabilityEvidence,
    SnapshotConsistencyEvidence,
    UnknownSubmission,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    _reconciliation_aggregate_id,
    load_account_resource_availability_evidence,
    load_latest_reconciliation_checkpoint,
    load_latest_reconciliation_checkpoint_for_scope,
    load_submission_resolution_evidence,
    load_reconciliation_checkpoint_for_readiness,
    record_reconciliation_checkpoint,
    unknown_submissions_from_dispatch,
    unresolved_attempt_ids_from_checkpoint,
    unresolved_provider_activity_ids_from_checkpoint,
)


def snapshot(*, provider_id="TEST_PROVIDER", account_id="test-account", environment="PAPER"):
    return SnapshotConsistencyEvidence(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        mode="ATOMIC",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
    )


def fill(*, provider_id="TEST_PROVIDER", account_id="test-account", environment="PAPER"):
    return ProviderFillEvidence.create(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_execution_id="e1",
        client_order_id="c1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-09-24T18:00:00Z",
    )


def availability(*, provider_id="TEST_PROVIDER", account_id="test-account", environment="PAPER"):
    return ResourceAvailabilityEvidence(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        snapshot_id="snapshot-capacity-1",
        query_started_at="2026-09-24T17:00:00Z",
        query_completed_at="2026-09-24T19:00:00Z",
        provider_as_of="2026-09-24T18:59:59Z",
        valid_until="2026-09-24T19:05:00Z",
        available_resources={"CASH:USD": "850", "MARGIN:USD": "1200.50"},
        evidence_refs=("provider:snapshot-capacity-1",),
    )


def reconciliation(**overrides):
    values = dict(
        provider_id="TEST_PROVIDER",
        account_id="test-account",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["e1"],
        provider_fills=[fill()],
        snapshot_consistency=snapshot(),
        coverage_start="2026-09-24T17:00:00Z",
        coverage_end="2026-09-24T19:00:00Z",
        pagination_complete=True,
        provider_activity_provider_id="TEST_PROVIDER",
        provider_activity_account_id="test-account",
    )
    values.update(overrides)
    provider_id = values["provider_id"]
    account_id = values["account_id"]
    environment = values["environment"]
    if "provider_fills" not in overrides:
        values["provider_fills"] = [
            fill(
                provider_id=provider_id,
                account_id=account_id,
                environment=environment,
            )
        ]
    if "snapshot_consistency" not in overrides:
        values["snapshot_consistency"] = snapshot(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
        )
    if "provider_activity_provider_id" not in overrides:
        values["provider_activity_provider_id"] = provider_id
    if "provider_activity_account_id" not in overrides:
        values["provider_activity_account_id"] = account_id
    return reconcile_account(**values)


class ReconciliationJournalTests(unittest.TestCase):
    def test_scoped_checkpoint_identity_cannot_collide_on_separator_characters(self):
        left = _reconciliation_aggregate_id(
            reconciliation_id="rid",
            provider_id="PROVIDER/A",
            account_id="B",
            environment="PAPER",
        )
        right = _reconciliation_aggregate_id(
            reconciliation_id="rid",
            provider_id="PROVIDER",
            account_id="A/B",
            environment="PAPER",
        )
        self.assertNotEqual(left, right)

    def test_checkpoint_round_trip_is_exact_and_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            result = reconciliation(
                provider_cash={"USD": "899.50"},
                resource_availability=availability(),
            )

            first = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            host_id="test-host",
            owner_epoch="epoch-1",
            )
            second = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            host_id="test-host",
            owner_epoch="epoch-1",
            )

            self.assertEqual(first["event_id"], second["event_id"])
            self.assertEqual(
                first["payload"]["cash_differences"]["USD"],
                "-0.50",
            )
            self.assertEqual(
                first["payload"]["resource_availability"],
                {
                    "provider_id": "TEST_PROVIDER",
                    "account_id": "test-account",
                    "environment": "PAPER",
                    "snapshot_id": "snapshot-capacity-1",
                    "query_started_at": "2026-09-24T17:00:00Z",
                    "query_completed_at": "2026-09-24T19:00:00Z",
                    "provider_as_of": "2026-09-24T18:59:59Z",
                    "valid_until": "2026-09-24T19:05:00Z",
                    "available_resources": {
                        "CASH:USD": "850",
                        "MARGIN:USD": "1200.50",
                    },
                    "evidence_refs": ["provider:snapshot-capacity-1"],
                },
            )
            self.assertEqual(
                len(
                    store.load_events(
                        "account_reconciliation",
                        first["aggregate_id"],
                    )
                ),
                1,
            )
            self.assertEqual(len(store.pending_outbox()), 1)

            reopened = JournalStore(path)
            latest = load_latest_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-1",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            )
            self.assertEqual(latest["event_id"], first["event_id"])
            self.assertEqual(
                latest["payload"]["blocking_resources"],
                ["CASH:USD"],
            )
            self.assertEqual(
                latest["payload"]["resource_availability"]["snapshot_id"],
                "snapshot-capacity-1",
            )
            self.assertEqual(
                latest["payload"]["resource_availability"]["available_resources"]["CASH:USD"],
                "850",
            )

    def test_scope_latest_uses_durable_journal_order_not_provider_clock(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            older = record_reconciliation_checkpoint(
                store,
                reconciliation_id="scope-head-a",
                result=reconciliation(resource_availability=availability()),
                observed_at="2026-09-24T19:02:00Z",
                host_id="host-a",
                owner_epoch="epoch-a",
            )
            newer = record_reconciliation_checkpoint(
                store,
                reconciliation_id="scope-head-b",
                result=reconciliation(
                    provider_cash={"USD": "901"},
                    resource_availability=availability(),
                ),
                # The provider clock regresses, but this fact is durably recorded later.
                observed_at="2026-09-24T19:01:00Z",
                host_id="host-b",
                owner_epoch="epoch-b",
            )
            latest = load_latest_reconciliation_checkpoint_for_scope(
                store,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            self.assertNotEqual(older["aggregate_id"], newer["aggregate_id"])
            self.assertGreater(newer["journal_sequence"], older["journal_sequence"])
            self.assertEqual(latest["event_id"], newer["event_id"])

            newest = record_reconciliation_checkpoint(
                store,
                reconciliation_id="scope-head-c",
                result=reconciliation(
                    provider_cash={"USD": "902"},
                    resource_availability=availability(),
                ),
                # Equal provider timestamps are not ambiguous: durable order is unique.
                observed_at="2026-09-24T19:01:00Z",
                host_id="host-c",
                owner_epoch="epoch-c",
            )
            latest = load_latest_reconciliation_checkpoint_for_scope(
                store,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            self.assertGreater(newest["journal_sequence"], newer["journal_sequence"])
            self.assertEqual(latest["event_id"], newest["event_id"])

    def test_owner_transfer_requires_new_readiness_checkpoint(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            result = reconciliation()

            owner_a = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-owner-transfer",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
                host_id="host-a",
                owner_epoch="epoch-a",
            )
            self.assertIsNone(
                load_reconciliation_checkpoint_for_readiness(
                    store,
                    reconciliation_id="acct-owner-transfer",
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    host_id="host-b",
                    owner_epoch="epoch-b",
                )
            )

            owner_b = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-owner-transfer",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
                host_id="host-b",
                owner_epoch="epoch-b",
            )
            self.assertNotEqual(owner_a["event_id"], owner_b["event_id"])
            self.assertEqual(owner_b["aggregate_version"], 2)
            self.assertEqual(
                owner_a["payload"]["checkpoint_owner"],
                {"host_id": "host-a", "owner_epoch": "epoch-a"},
            )
            self.assertEqual(
                owner_b["payload"]["checkpoint_owner"],
                {"host_id": "host-b", "owner_epoch": "epoch-b"},
            )

            reopened = JournalStore(path)
            ready = load_reconciliation_checkpoint_for_readiness(
                reopened,
                reconciliation_id="acct-owner-transfer",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                host_id="host-b",
                owner_epoch="epoch-b",
            )
            self.assertEqual(ready["event_id"], owner_b["event_id"])
            self.assertIsNone(
                load_reconciliation_checkpoint_for_readiness(
                    reopened,
                    reconciliation_id="acct-owner-transfer",
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    host_id="host-a",
                    owner_epoch="epoch-a",
                )
            )

            exact_retry = record_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-owner-transfer",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
                host_id="host-b",
                owner_epoch="epoch-b",
            )
            self.assertEqual(exact_retry["event_id"], owner_b["event_id"])

    def test_fresh_complete_checkpoint_is_exact_cash_availability_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-availability",
                result=reconciliation(resource_availability=availability()),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )

            reopened = JournalStore(path)
            evidence = load_account_resource_availability_evidence(
                reopened,
                checkpoint_event_id=checkpoint["event_id"],
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                resources=("CASH:USD",),
                now="2026-09-24T19:00:30Z",
                max_age_seconds="60",
            )
            self.assertEqual(evidence["availability"], {"CASH:USD": "850"})
            self.assertEqual(
                evidence["checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(evidence["snapshot_mode"], "ATOMIC")
            self.assertEqual(evidence["age_seconds"], "30")

            with self.assertRaisesRegex(ValueError, "stale"):
                load_account_resource_availability_evidence(
                    reopened,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    resources=("CASH:USD",),
                    now="2026-09-24T19:02:00Z",
                    max_age_seconds="60",
                )
            with self.assertRaisesRegex(
                ValueError,
                "not canonically supported",
            ):
                load_account_resource_availability_evidence(
                    reopened,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    resources=("MARGIN:USD",),
                    now="2026-09-24T19:00:30Z",
                    max_age_seconds="60",
                )

            no_explicit_availability = record_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-no-explicit-availability",
                result=reconciliation(),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            with self.assertRaisesRegex(
                ValueError,
                "lacks explicit provider resource availability",
            ):
                load_account_resource_availability_evidence(
                    reopened,
                    checkpoint_event_id=no_explicit_availability["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    resources=("CASH:USD",),
                    now="2026-09-24T19:00:30Z",
                    max_age_seconds="60",
                )

    def test_exact_checkpoint_event_is_submission_resolution_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            unknown = UnknownSubmission.create(
                attempt_id="attempt-authority",
                intent_id="intent-authority",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                client_order_id="c1",
                started_at="2026-09-24T17:30:00Z",
            )
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-resolution-authority",
                result=reconciliation(unknown_submissions=[unknown]),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )

            reopened = JournalStore(path)
            evidence = load_submission_resolution_evidence(
                reopened,
                checkpoint_event_id=checkpoint["event_id"],
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                attempt_id="attempt-authority",
                intent_id="intent-authority",
                client_order_id="c1",
            )
            self.assertEqual(evidence["outcome"], "OBSERVED_EXECUTION")
            self.assertEqual(evidence["provider_execution_ids"], ("e1",))
            self.assertEqual(evidence["provider_order_ids"], ())
            self.assertEqual(
                evidence["checkpoint_event_id"],
                checkpoint["event_id"],
            )
            self.assertEqual(
                evidence["checkpoint_payload_hash"],
                checkpoint["payload_hash"],
            )

    def test_submission_resolution_evidence_fails_closed_on_scope_or_identity_mismatch(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            unknown = UnknownSubmission.create(
                attempt_id="attempt-scoped-authority",
                intent_id="intent-scoped-authority",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                client_order_id="c1",
                started_at="2026-09-24T17:30:00Z",
            )
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-scoped-authority",
                result=reconciliation(unknown_submissions=[unknown]),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )

            with self.assertRaisesRegex(ValueError, "scope mismatch"):
                load_submission_resolution_evidence(
                    store,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="other-account",
                    environment="PAPER",
                    attempt_id="attempt-scoped-authority",
                    intent_id="intent-scoped-authority",
                    client_order_id="c1",
                )
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                load_submission_resolution_evidence(
                    store,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    attempt_id="attempt-scoped-authority",
                    intent_id="wrong-intent",
                    client_order_id="c1",
                )
            with self.assertRaisesRegex(
                ValueError,
                "exactly one resolution",
            ):
                load_submission_resolution_evidence(
                    store,
                    checkpoint_event_id=checkpoint["event_id"],
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                    attempt_id="missing-attempt",
                    intent_id="intent-scoped-authority",
                    client_order_id="c1",
                )

    def test_checkpoint_retains_exact_execution_ids_across_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            unknown = UnknownSubmission.create(
                attempt_id="attempt-checkpoint-execution",
                intent_id="intent-checkpoint-execution",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                client_order_id="c1",
                started_at="2026-09-24T17:30:00Z",
            )
            result = reconciliation(unknown_submissions=[unknown])
            self.assertEqual(
                result.submission_resolutions[0].provider_execution_ids,
                ("e1",),
            )

            record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-execution-identity",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            reopened = JournalStore(path)
            latest = load_latest_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-execution-identity",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )
            resolution = latest["payload"]["submission_resolutions"][0]
            self.assertEqual(resolution["outcome"], "OBSERVED_EXECUTION")
            self.assertEqual(resolution["provider_execution_ids"], ["e1"])
            self.assertEqual(resolution["provider_order_ids"], [])

    def test_changed_checkpoint_appends_new_version(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            first = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=reconciliation(provider_cash={"USD": "899.50"}),
                observed_at="2026-09-24T19:00:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=reconciliation(),
                observed_at="2026-09-24T19:01:00Z",
                host_id="test-host",
                owner_epoch="epoch-1",
            )
            events = store.load_events(
                "account_reconciliation",
                first["aggregate_id"],
            )
            self.assertEqual(
                [event["aggregate_version"] for event in events],
                [1, 2],
            )
            self.assertFalse(events[0]["payload"]["complete"])
            self.assertTrue(events[1]["payload"]["complete"])

    def test_unknown_dispatch_is_rebuilt_from_exact_durable_aggregate(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            aggregate_id = "submission-attempt:scoped-proof"
            prepared_payload = {
                "intent_id": "intent-scoped-1",
                "provider": "TEST_PROVIDER",
                "account_id": "test-account",
                "environment": "PAPER",
                "client_order_id": "client-scoped-1",
                "prepared_at": "2026-09-24T18:00:00Z",
            }
            unknown_payload = {
                "client_order_id": "client-scoped-1",
                "reason": "transport_result_ambiguous",
            }
            for version, event_type, payload in (
                (1, "SubmissionPrepared", prepared_payload),
                (2, "SubmissionUnknown", unknown_payload),
            ):
                store.append_event(
                    {
                        "event_id": f"scoped-dispatch-{version}",
                        "event_type": event_type,
                        "schema_version": "1.0.0",
                        "aggregate_type": "submission_attempt",
                        "aggregate_id": aggregate_id,
                        "aggregate_version": str(version),
                        "host_id": "local-mvp",
                        "owner_epoch": "1",
                        "environment": "PAPER",
                        "occurred_at": "2026-09-24T18:00:00Z",
                        "observed_at": "2026-09-24T18:00:00Z",
                        "committed_at": "2026-09-24T18:00:00Z",
                        "correlation_id": "scoped-dispatch-correlation",
                        "causation_id": None,
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "evidence_refs": [],
                    }
                )

            reopened = JournalStore(path)
            recovered = unknown_submissions_from_dispatch(
                reopened,
                attempt_ids=["attempt-1"],
                aggregate_ids={"attempt-1": aggregate_id},
            )
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].attempt_id, "attempt-1")
            self.assertEqual(recovered[0].client_order_id, "client-scoped-1")

            with self.assertRaises(KeyError):
                unknown_submissions_from_dispatch(
                    reopened,
                    attempt_ids=["attempt-1"],
                )

    def test_checkpoint_recovers_unresolved_attempt_ids(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            unknown = UnknownSubmission.create(
                attempt_id="attempt-unknown",
                intent_id="intent-unknown",
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
                client_order_id="client-unknown",
                started_at="2026-09-24T18:00:00Z",
            )
            result = reconciliation(
                unknown_submissions=[unknown],
                searched_client_order_ids=[],
            )
            self.assertFalse(result.complete)
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-1",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            host_id="test-host",
            owner_epoch="epoch-1",
            )
            self.assertEqual(
                unresolved_attempt_ids_from_checkpoint(
                    checkpoint,
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                ),
                ("attempt-unknown",),
            )

    def test_activity_gap_checkpoint_survives_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            result = reconciliation(
                provider_activities=[
                    ProviderActivityEvidence.create(
                        provider_id="TEST_PROVIDER",
                        account_id="test-account",
                        environment="PAPER",
                        activity_id="manual-cash-1",
                        activity_type="CASH_ADJUSTMENT",
                        origin="MANUAL",
                        occurred_at="2026-09-24T18:15:00Z",
                        currency="USD",
                    )
                ],
            )
            self.assertFalse(result.complete)
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-activity",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            host_id="test-host",
            owner_epoch="epoch-1",
            )
            self.assertEqual(
                checkpoint["payload"]["unexpected_provider_activity_ids"],
                ["manual-cash-1"],
            )
            self.assertEqual(
                checkpoint["payload"]["manual_or_external_activity_ids"],
                ["manual-cash-1"],
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(
                    checkpoint,
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                ),
                ("manual-cash-1",),
            )

            reopened = JournalStore(path)
            latest = load_latest_reconciliation_checkpoint(
                reopened,
                reconciliation_id="acct-activity",
            provider_id="TEST_PROVIDER",
            account_id="test-account",
            environment="PAPER",
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(
                    latest,
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                ),
                ("manual-cash-1",),
            )
            self.assertIn(
                "CASH:USD",
                latest["payload"]["blocking_resources"],
            )

    def test_missing_expected_activity_is_recoverable_from_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            result = reconciliation(
                local_provider_activity_ids=["expected-activity"],
            )
            checkpoint = record_reconciliation_checkpoint(
                store,
                reconciliation_id="acct-missing-activity",
                result=result,
                observed_at="2026-09-24T19:00:00Z",
            host_id="test-host",
            owner_epoch="epoch-1",
            )
            self.assertEqual(
                unresolved_provider_activity_ids_from_checkpoint(
                    checkpoint,
                    provider_id="TEST_PROVIDER",
                    account_id="test-account",
                    environment="PAPER",
                ),
                ("expected-activity",),
            )

    def test_activity_gap_recovery_validates_checkpoint_shape(self):
        with self.assertRaisesRegex(ValueError, "must be a list"):
            unresolved_provider_activity_ids_from_checkpoint(
                {
                    "payload": {
                        "provider_id": "TEST_PROVIDER",
                        "account_id": "test-account",
                        "environment": "PAPER",
                        "unexpected_provider_activity_ids": "not-a-list",
                        "missing_local_provider_activity_ids": [],
                    }
                },
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )


    def test_corrupt_null_identities_fail_closed_in_checkpoint_recovery(self):
        malformed_submission = {
            "payload": {
                "provider_id": "TEST_PROVIDER",
                "account_id": "test-account",
                "environment": "PAPER",
                "submission_resolutions": [
                    {"attempt_id": None, "outcome": "UNKNOWN"}
                ]
            }
        }
        with self.assertRaises(ValueError):
            unresolved_attempt_ids_from_checkpoint(
                malformed_submission,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )

        malformed_activity = {
            "payload": {
                "provider_id": "TEST_PROVIDER",
                "account_id": "test-account",
                "environment": "PAPER",
                "unexpected_provider_activity_ids": [None],
                "missing_local_provider_activity_ids": [],
            }
        }
        with self.assertRaises(ValueError):
            unresolved_provider_activity_ids_from_checkpoint(
                malformed_activity,
                provider_id="TEST_PROVIDER",
                account_id="test-account",
                environment="PAPER",
            )


if __name__ == "__main__":
    unittest.main()
