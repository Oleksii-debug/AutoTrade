from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.securities_borrow import (
    BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE,
    BorrowAvailabilityEvidence,
    BorrowEvidenceError,
    BorrowRecallEvidence,
    BorrowRecallResolutionEvidence,
    DurableBorrowRecallProjection,
    provider_borrow_evidence_metadata,
    provider_borrow_evidence_receipt,
    verify_provider_borrow_evidence,
)


def availability() -> BorrowAvailabilityEvidence:
    return BorrowAvailabilityEvidence(
        provider_id="TEST_BROKER",
        account_id="acct-1",
        environment="PAPER",
        instrument_id="44444444-4444-4444-8444-444444444444",
        instrument_version=1,
        locate_id="locate-1",
        provider_revision="rev-1",
        capacity_quantity=Decimal("12.5"),
        hard_to_borrow=False,
        observed_at="2026-09-25T05:00:00Z",
        effective_at="2026-09-25T04:59:00Z",
        expires_at="2026-09-25T06:00:00Z",
        evidence_ref="placeholder",
        indicative_rate=Decimal("0.01"),
    )


def bind(store: ArtifactStore, evidence: BorrowAvailabilityEvidence):
    receipt = provider_borrow_evidence_receipt(evidence)
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "autotrade-borrow-evidence:" + canonical_json(receipt),
        )
    )
    manifest = store.publish_bytes(
        artifact_id=artifact_id,
        data=canonical_json(receipt).encode("utf-8"),
        media_type=BORROW_PROVIDER_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        metadata=provider_borrow_evidence_metadata(evidence),
    )
    return replace(
        evidence,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
    ), artifact_id


class SecuritiesBorrowSnapshotAuthorityTests(unittest.TestCase):
    def test_evidence_subclass_is_rejected_before_virtual_receipt_dispatch(self):
        class ForgedAvailability(BorrowAvailabilityEvidence):
            resource_detail_called = False

            def resource_detail(self):
                self.resource_detail_called = True
                raise AssertionError("evidence subclass virtual method must not run")

        base = availability()
        forged = ForgedAvailability(**base.__dict__)

        with self.assertRaisesRegex(
            TypeError,
            "unsupported securities-borrow evidence type",
        ):
            provider_borrow_evidence_receipt(forged)
        self.assertFalse(forged.resource_detail_called)

        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                TypeError,
                "unsupported securities-borrow evidence type",
            ):
                verify_provider_borrow_evidence(forged, store)
        self.assertFalse(forged.resource_detail_called)

    def test_recall_projection_rejects_evidence_subclasses_before_virtual_dispatch(self):
        class ForgedRecall(BorrowRecallEvidence):
            def payload(self):
                raise AssertionError("recall subclass payload must not run")

        class ForgedResolution(BorrowRecallResolutionEvidence):
            def payload(self):
                raise AssertionError("resolution subclass payload must not run")

        with TemporaryDirectory() as directory:
            journal = JournalStore(f"{directory}/journal.sqlite3")
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            projection = DurableBorrowRecallProjection(
                journal,
                provider_id="TEST_BROKER",
                account_id="acct-1",
                environment="PAPER",
                instrument_id="44444444-4444-4444-8444-444444444444",
                instrument_version=1,
                evidence_artifact_store=artifacts,
            )
            recall = ForgedRecall(
                recall_id="recall-1",
                provider_id="TEST_BROKER",
                account_id="acct-1",
                environment="PAPER",
                instrument_id="44444444-4444-4444-8444-444444444444",
                instrument_version=1,
                provider_revision="rev-recall-1",
                quantity=Decimal("1"),
                observed_at="2026-09-25T05:00:00Z",
                effective_at="2026-09-25T04:59:00Z",
                evidence_ref="placeholder",
            )
            with self.assertRaisesRegex(TypeError, "exact BorrowRecallEvidence"):
                projection.record_recall(recall)

            resolution = ForgedResolution(
                resolution_id="resolution-1",
                recall_id="recall-1",
                provider_id="TEST_BROKER",
                account_id="acct-1",
                environment="PAPER",
                instrument_id="44444444-4444-4444-8444-444444444444",
                instrument_version=1,
                provider_revision="rev-resolution-1",
                resolved_quantity=Decimal("1"),
                observed_at="2026-09-25T05:00:00Z",
                effective_at="2026-09-25T04:59:00Z",
                evidence_ref="placeholder",
            )
            with self.assertRaisesRegex(
                TypeError,
                "exact BorrowRecallResolutionEvidence",
            ):
                projection.resolve_recall(resolution)

    def test_artifact_store_subclass_is_rejected_before_virtual_dispatch(self):
        class ForgedArtifactStore(ArtifactStore):
            snapshot_called = False

            def read_authenticated_snapshot(self, artifact_id):
                self.snapshot_called = True
                raise AssertionError("subclass method must not run")

        with TemporaryDirectory() as directory:
            forged = ForgedArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(BorrowEvidenceError, "canonical ArtifactStore"):
                verify_provider_borrow_evidence(availability(), forged)
            self.assertFalse(forged.snapshot_called)

    def test_one_class_qualified_authenticated_snapshot_is_used(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, artifact_id = bind(store, availability())
            original = ArtifactStore.read_authenticated_snapshot
            calls = []

            def counted(instance, requested_artifact_id):
                calls.append((instance, requested_artifact_id))
                return original(instance, requested_artifact_id)

            store.load_manifest = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split manifest read")
            )
            store.read_bytes = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("split payload read")
            )
            store.read_authenticated_snapshot = lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("instance virtual dispatch")
            )
            with patch.object(ArtifactStore, "read_authenticated_snapshot", new=counted):
                canonical_ref = verify_provider_borrow_evidence(evidence, store)

            self.assertEqual(canonical_ref, evidence.evidence_ref)
            self.assertEqual(calls, [(store, artifact_id)])

    def test_snapshot_storage_failure_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(Path(directory) / "artifacts")
            evidence, _ = bind(store, availability())
            for failure in (
                OSError("simulated storage race"),
                ArtifactIntegrityError("simulated integrity failure"),
            ):
                with self.subTest(failure=type(failure).__name__):
                    with patch.object(
                        ArtifactStore,
                        "read_authenticated_snapshot",
                        side_effect=failure,
                    ):
                        with self.assertRaisesRegex(
                            BorrowEvidenceError,
                            "provider borrow evidence verification failed",
                        ):
                            verify_provider_borrow_evidence(evidence, store)


if __name__ == "__main__":
    unittest.main()
