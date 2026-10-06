from __future__ import annotations

from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.securities_borrow as securities_borrow_module

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
        quantity_unit="share",
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
        quantity_unit="share",
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
        quantity_unit="share",
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
                "exact canonical ArtifactStore",
            ):
                verify_provider_borrow_evidence(bound, malicious)

            self.assertEqual(malicious.override_calls, 0)

    def test_verifier_ignores_late_module_global_retargets(self):
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(directory)
            bound = bind_provider_evidence(artifacts, recall())

            with (
                patch.object(
                    securities_borrow_module,
                    "provider_borrow_evidence_receipt",
                    side_effect=AssertionError(
                        "late receipt helper must not reinterpret provider evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "provider_borrow_evidence_metadata",
                    side_effect=AssertionError(
                        "late metadata helper must not reinterpret provider evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_immutable_evidence_ref",
                    side_effect=AssertionError(
                        "late reference helper must not retarget artifact authority"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "strict_json_loads",
                    side_effect=AssertionError(
                        "late JSON parser must not reinterpret authenticated bytes"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "canonical_json",
                    side_effect=AssertionError(
                        "late canonical renderer must not redefine authenticated bytes"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "ArtifactStore",
                    new=object,
                ),
                patch.object(
                    BorrowRecallEvidence,
                    "payload",
                    side_effect=AssertionError(
                        "late evidence method replacement must not reinterpret economics"
                    ),
                ),
            ):
                reference = verify_provider_borrow_evidence(bound, artifacts)

            self.assertEqual(reference, bound.evidence_ref)

    def test_verifier_ignores_late_revalidation_helper_retargets(self):
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(directory)
            bound = bind_provider_evidence(artifacts, recall())

            with (
                patch.object(
                    securities_borrow_module,
                    "_text",
                    side_effect=AssertionError(
                        "late text helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_environment",
                    side_effect=AssertionError(
                        "late environment helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_provider_environment",
                    side_effect=AssertionError(
                        "late provider-domain helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_instrument_id",
                    side_effect=AssertionError(
                        "late instrument helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_version",
                    side_effect=AssertionError(
                        "late version helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_decimal",
                    side_effect=AssertionError(
                        "late decimal helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_instant",
                    side_effect=AssertionError(
                        "late instant helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_dt",
                    side_effect=AssertionError(
                        "late chronology helper must not reinterpret authenticated evidence"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_provider_environment_payload",
                    side_effect=AssertionError(
                        "late provider-domain renderer must not rewrite authenticated bytes"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "_decimal_text",
                    side_effect=AssertionError(
                        "late decimal renderer must not rewrite authenticated bytes"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "normalize_provider_environment",
                    side_effect=AssertionError(
                        "late provider-domain normalizer must not execute"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "parse_bounded_exact_decimal",
                    side_effect=AssertionError(
                        "late exact-decimal parser must not execute"
                    ),
                ),
                patch.object(
                    securities_borrow_module,
                    "canonical_decimal_text",
                    side_effect=AssertionError(
                        "late exact-decimal renderer must not execute"
                    ),
                ),
                patch.object(securities_borrow_module, "datetime", new=object),
                patch.object(securities_borrow_module, "timezone", new=object),
                patch.object(securities_borrow_module, "Decimal", new=object),
                patch.object(securities_borrow_module, "UUID", new=object),
            ):
                reference = verify_provider_borrow_evidence(bound, artifacts)

            self.assertEqual(reference, bound.evidence_ref)

    def test_verifier_fails_closed_before_retargeted_post_init_dispatch(self):
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(directory)
            bound = bind_provider_evidence(artifacts, recall())
            calls = []

            def forged_post_init(_self):
                calls.append("called")
                raise AssertionError(
                    "retargeted evidence post-init must not execute"
                )

            with patch.object(
                BorrowRecallEvidence,
                "__post_init__",
                new=forged_post_init,
            ):
                with self.assertRaisesRegex(
                    BorrowEvidenceError,
                    "implementation changed after verifier binding",
                ):
                    verify_provider_borrow_evidence(bound, artifacts)

            self.assertEqual(calls, [])

    def test_malformed_snapshot_representation_fails_closed(self):
        with TemporaryDirectory() as directory:
            artifacts = ArtifactStore(directory)
            bound = bind_provider_evidence(artifacts, recall())
            artifact_id = bound.evidence_ref[len("artifact:"):].split(
                "@sha256:",
                1,
            )[0]
            manifest, raw = ArtifactStore.read_authenticated_snapshot(
                artifacts,
                artifact_id,
            )
            cases = (
                ("manifest-type", [], raw),
                ("raw-type", manifest, bytearray(raw)),
                (
                    "artifact-identity",
                    {**manifest, "artifact_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"},
                    raw,
                ),
                (
                    "manifest-hash",
                    {**manifest, "manifest_hash": "sha256:" + "g" * 64},
                    raw,
                ),
            )
            for name, candidate_manifest, candidate_raw in cases:
                with self.subTest(case=name):
                    with patch.object(
                        ArtifactStore,
                        "read_authenticated_snapshot",
                        return_value=(candidate_manifest, candidate_raw),
                    ):
                        with self.assertRaisesRegex(
                            BorrowEvidenceError,
                            "verification failed",
                        ):
                            verify_provider_borrow_evidence(bound, artifacts)

            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=OSError("injected descriptor failure"),
            ):
                with self.assertRaisesRegex(
                    BorrowEvidenceError,
                    "verification failed",
                ):
                    verify_provider_borrow_evidence(bound, artifacts)

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
                quantity_unit="share",
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
                quantity_unit="share",
                evidence_artifact_store=artifacts,
            )
            self.assertEqual(projection.record_recall(bound), bound.quantity)

            malicious = MaliciousSnapshotStore(artifact_root)
            with self.assertRaisesRegex(
                TypeError,
                "exact canonical ArtifactStore",
            ):
                DurableBorrowRecallProjection(
                    JournalStore(journal.path),
                    provider_id=PROVIDER_ID,
                    account_id=ACCOUNT_ID,
                    environment=ENVIRONMENT,
                    instrument_id=INSTRUMENT_ID,
                    instrument_version=1,
                    quantity_unit="share",
                    evidence_artifact_store=malicious,
                )

            self.assertEqual(malicious.override_calls, 0)


if __name__ == "__main__":
    unittest.main()
