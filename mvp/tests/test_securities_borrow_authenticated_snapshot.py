from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.securities_borrow import (
    BorrowAvailabilityEvidence,
    BorrowEvidenceError,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    DurableBorrowRecallProjection,
    verify_provider_borrow_evidence,
)
from mvp.tests.securities_borrow_evidence_helpers import bind_provider_evidence
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
PROVIDER_ID = "TEST_PROVIDER"
ACCOUNT_ID = "paper-borrow"
ENVIRONMENT = "PAPER"


def availability() -> BorrowAvailabilityEvidence:
    return BorrowAvailabilityEvidence(
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        locate_id="locate-authenticated-snapshot",
        provider_revision="availability-snapshot-r1",
        capacity_quantity="100",
        hard_to_borrow=False,
        observed_at="2026-10-03T17:00:30Z",
        effective_at="2026-10-03T17:00:00Z",
        expires_at="2026-10-03T18:00:00Z",
        evidence_ref="provider:availability-snapshot-r1",
        indicative_rate="0.01",
    )


def recall() -> BorrowRecallEvidence:
    return BorrowRecallEvidence(
        recall_id="recall-authenticated-snapshot",
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        provider_revision="recall-snapshot-r1",
        quantity="3",
        observed_at="2026-10-03T17:00:30Z",
        effective_at="2026-10-03T17:00:00Z",
        deadline="2026-10-03T18:00:00Z",
        evidence_ref="provider:recall-snapshot-r1",
    )


def resolution() -> BorrowRecallResolutionEvidence:
    return BorrowRecallResolutionEvidence(
        resolution_id="resolution-authenticated-snapshot",
        recall_id="recall-authenticated-snapshot",
        provider_id=PROVIDER_ID,
        account_id=ACCOUNT_ID,
        environment=ENVIRONMENT,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        provider_revision="resolution-snapshot-r1",
        resolved_quantity="1",
        observed_at="2026-10-03T17:10:30Z",
        effective_at="2026-10-03T17:10:00Z",
        evidence_ref="provider:resolution-snapshot-r1",
    )


class MaliciousSnapshotStore(ArtifactStore):
    def __init__(self, root):
        super().__init__(root)
        self.override_calls = 0

    def read_authenticated_snapshot(self, artifact_id):
        self.override_calls += 1
        raise AssertionError("subclass snapshot override must not become financial authority")

    def load_manifest(self, artifact_id):
        self.override_calls += 1
        raise AssertionError("subclass manifest override must not become financial authority")

    def read_bytes(self, artifact_id):
        self.override_calls += 1
        raise AssertionError("subclass object override must not become financial authority")


class SecuritiesBorrowAuthenticatedSnapshotTests(unittest.TestCase):
    def test_each_evidence_kind_consumes_one_canonical_authenticated_snapshot(self):
        original_snapshot = ArtifactStore.read_authenticated_snapshot

        for factory in (availability, recall, resolution):
            with self.subTest(evidence_kind=factory.__name__):
                with TemporaryDirectory() as directory:
                    store = ArtifactStore(directory)
                    bound = bind_provider_evidence(store, factory())
                    snapshot_calls = []

                    def traced_snapshot(candidate, artifact_id):
                        snapshot_calls.append(artifact_id)
                        return original_snapshot(candidate, artifact_id)

                    with (
                        patch.object(
                            ArtifactStore,
                            "read_authenticated_snapshot",
                            new=traced_snapshot,
                        ),
                        patch.object(
                            ArtifactStore,
                            "load_manifest",
                            side_effect=AssertionError(
                                "borrow verifier must not perform split manifest read"
                            ),
                        ),
                        patch.object(
                            ArtifactStore,
                            "read_bytes",
                            side_effect=AssertionError(
                                "borrow verifier must not perform split object read"
                            ),
                        ),
                    ):
                        reference = verify_provider_borrow_evidence(bound, store)

                    self.assertEqual(reference, bound.evidence_ref)
                    self.assertEqual(len(snapshot_calls), 1)

    def test_artifact_store_subclass_cannot_supply_financial_evidence(self):
        with TemporaryDirectory() as directory:
            canonical = ArtifactStore(directory)
            bound = bind_provider_evidence(canonical, recall())
            malicious = MaliciousSnapshotStore(directory)

            with self.assertRaisesRegex(
                BorrowEvidenceError,
                "canonical ArtifactStore",
            ):
                verify_provider_borrow_evidence(bound, malicious)

            self.assertEqual(malicious.override_calls, 0)

    def test_snapshot_integrity_failure_precedes_journal_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(f"{directory}/borrow-provider-artifacts")
            bound = bind_provider_evidence(artifacts, recall())
            projection = DurableBorrowRecallProjection(
                journal,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=artifacts,
            )

            def fail_snapshot(candidate, artifact_id):
                raise ArtifactIntegrityError("injected authenticated-snapshot failure")

            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                new=fail_snapshot,
            ):
                with self.assertRaisesRegex(
                    BorrowEvidenceError,
                    "verification failed",
                ):
                    projection.record_recall(bound)

            self.assertEqual(projection.version, 0)
            self.assertEqual(
                journal.load_events(
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )

    def test_restart_replay_rejects_polymorphic_artifact_store_before_dispatch(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            artifact_root = f"{directory}/borrow-provider-artifacts"
            artifacts = ArtifactStore(artifact_root)
            bound = bind_provider_evidence(artifacts, recall())
            projection = DurableBorrowRecallProjection(
                journal,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(projection.record_recall(bound), bound.quantity)

            malicious = MaliciousSnapshotStore(artifact_root)
            with self.assertRaisesRegex(
                BorrowEvidenceError,
                "canonical ArtifactStore",
            ):
                DurableBorrowRecallProjection(
                    JournalStore(journal.path),
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    evidence_artifact_store=malicious,
                )

            self.assertEqual(malicious.override_calls, 0)


if __name__ == "__main__":
    unittest.main()
