from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from research.autotrade_research.artifacts import ArtifactStore

from mvp.tests.test_trusted_chronology_cut import (
    TrustedChronologyCutTests as ChronologyHarness,
)
import mvp.autotrade_mvp.trusted_chronology_cut as chronology
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope


class TrustedChronologyOwnerCallbackRaceTests(unittest.TestCase):
    def test_current_cut_rechecks_local_owner_after_receipt_verifier(self) -> None:
        harness = ChronologyHarness(
            methodName="test_source_cut_is_runtime_free_and_current"
        )
        with harness.subTest("owner callback race"):
            with __import__("tempfile").TemporaryDirectory() as directory:
                store, recovery, _config, occurrence = harness._state(directory)
                attempt = harness._prepare(store, recovery, occurrence)
                measurement = harness._measurement(attempt)
                accepted = harness._accepted(attempt, measurement)
                artifacts = ArtifactStore(Path(directory) / "artifacts")
                cut = harness._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

                current_owner = recovery.owner
                self.assertIsNotNone(current_owner)
                evidence_ref = accepted.evidence_refs[0]
                self.assertEqual(
                    evidence_ref.sha256,
                    "sha256:" + sha256(measurement).hexdigest(),
                )
                manifest = {
                    "artifact_id": evidence_ref.artifact_id,
                    "sha256": evidence_ref.sha256,
                    "media_type": evidence_ref.media_type,
                    "metadata": {"evidence_kind": evidence_ref.evidence_kind},
                    "source_refs": [f"git:{evidence_ref.source_sha}"],
                }

                def raced_verifier(*_args, **_kwargs):
                    owner_type = type(current_owner)
                    recovery.owner = owner_type(
                        owner_id="callback-local-owner",
                        epoch=current_owner.epoch,
                    )
                    return accepted

                with (
                    patch.object(
                        chronology,
                        "parse_signed_qualification_attestation",
                        return_value=harness._dummy_receipt(),
                    ),
                    patch.object(
                        chronology,
                        "verify_canonical_qualification_attestation",
                        side_effect=raced_verifier,
                    ),
                    patch.object(
                        chronology,
                        "trusted_authenticated_reader",
                        return_value=lambda _artifact_id: (manifest, measurement),
                    ),
                    self.assertRaisesRegex(
                        PermissionError,
                        "local recovery owner changed during verification",
                    ),
                ):
                    chronology.require_current_trusted_chronology_cut(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        evidence_store=artifacts,
                        evidence_root=str(artifacts.root),
                        expected_source_sha="a" * 40,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                        claimed_instants=("2026-10-03T14:00:00Z",),
                    )


    def _assert_callback_rebind_is_rejected(self, global_name: str) -> None:
        harness = ChronologyHarness(
            methodName="test_source_cut_is_runtime_free_and_current"
        )
        with __import__("tempfile").TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = harness._state(directory)
            attempt = harness._prepare(store, recovery, occurrence)
            measurement = harness._measurement(attempt)
            accepted = harness._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            cut = harness._accept(
                store,
                recovery,
                attempt,
                measurement,
                accepted,
                artifacts,
            )
            evidence_ref = accepted.evidence_refs[0]
            manifest = {
                "artifact_id": evidence_ref.artifact_id,
                "sha256": evidence_ref.sha256,
                "media_type": evidence_ref.media_type,
                "metadata": {"evidence_kind": evidence_ref.evidence_kind},
                "source_refs": [f"git:{evidence_ref.source_sha}"],
            }

            wrapper_globals = (
                chronology.require_current_trusted_chronology_cut.__globals__
            )
            original_authority = wrapper_globals[global_name]

            def raced_verifier(*_args, **_kwargs):
                wrapper_globals[global_name] = lambda *_a, **_k: None
                return accepted

            try:
                with (
                    patch.object(
                        chronology,
                        "parse_signed_qualification_attestation",
                        return_value=harness._dummy_receipt(),
                    ),
                    patch.object(
                        chronology,
                        "verify_canonical_qualification_attestation",
                        side_effect=raced_verifier,
                    ),
                    patch.object(
                        chronology,
                        "trusted_authenticated_reader",
                        return_value=lambda _artifact_id: (manifest, measurement),
                    ),
                    self.assertRaisesRegex(
                        PermissionError,
                        "trusted chronology verification authority changed",
                    ),
                ):
                    chronology.require_current_trusted_chronology_cut(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        evidence_store=artifacts,
                        evidence_root=str(artifacts.root),
                        expected_source_sha="a" * 40,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                        claimed_instants=("2026-10-03T14:00:00Z",),
                    )
            finally:
                wrapper_globals[global_name] = original_authority

    def test_current_cut_rejects_post_verifier_rebind_during_signed_callback(self) -> None:
        self._assert_callback_rebind_is_rejected(
            "_require_post_verification_currentness"
        )

    def test_current_cut_rejects_horizon_rebind_during_signed_callback(self) -> None:
        self._assert_callback_rebind_is_rejected(
            "_original_require_chronology_horizon"
        )


if __name__ == "__main__":
    unittest.main()
