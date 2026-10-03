from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.securities_borrow import (
    BorrowEvidenceError,
    BorrowRecallEvidence,
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


class SnapshotOnlyStore(ArtifactStore):
    def __init__(self, root):
        super().__init__(root)
        self.snapshot_reads = 0

    def read_authenticated_snapshot(self, artifact_id):
        self.snapshot_reads += 1
        return super().read_authenticated_snapshot(artifact_id)

    def load_manifest(self, artifact_id):
        raise AssertionError("borrow verifier must not perform a split manifest read")

    def read_bytes(self, artifact_id):
        raise AssertionError("borrow verifier must not perform a split object read")


class FailingSnapshotStore(ArtifactStore):
    def read_authenticated_snapshot(self, artifact_id):
        raise ArtifactIntegrityError("injected authenticated-snapshot failure")


class SecuritiesBorrowAuthenticatedSnapshotTests(unittest.TestCase):
    def test_valid_recall_consumes_exactly_one_authenticated_snapshot(self):
        with TemporaryDirectory() as directory:
            producer = ArtifactStore(directory)
            bound = bind_provider_evidence(producer, recall())
            guarded = SnapshotOnlyStore(directory)

            reference = verify_provider_borrow_evidence(bound, guarded)

            self.assertEqual(reference, bound.evidence_ref)
            self.assertEqual(guarded.snapshot_reads, 1)

    def test_snapshot_integrity_failure_precedes_journal_mutation(self):
        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            producer = ArtifactStore(f"{directory}/borrow-provider-artifacts")
            bound = bind_provider_evidence(producer, recall())
            failing = FailingSnapshotStore(
                f"{directory}/borrow-provider-artifacts"
            )
            projection = DurableBorrowRecallProjection(
                journal,
                provider_id=PROVIDER_ID,
                account_id=ACCOUNT_ID,
                environment=ENVIRONMENT,
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                evidence_artifact_store=failing,
            )

            with self.assertRaisesRegex(
                BorrowEvidenceError,
                "verification failed",
            ):
                projection.record_recall(bound)

            self.assertEqual(
                journal.load_events(
                    "securities_borrow_recall",
                    projection.aggregate_id,
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
