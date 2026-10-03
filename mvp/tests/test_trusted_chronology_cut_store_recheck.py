from __future__ import annotations

from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore

import _trusted_chronology_cut_cases as _cases
import mvp.autotrade_mvp.trusted_chronology_cut as chronology
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope


class TrustedChronologyCutStoreRecheckTests(_cases.TrustedChronologyCutTests):
    """Preserve the authority-line store-recheck regression through chronology merge."""

    def test_store_identity_is_rechecked_after_receipt_callback(self) -> None:
        with _cases.TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            cut = self._accept(
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
            original_store_digest = chronology.journal_store_identity_digest
            raced = {"store_changed": False}
            changed_store_digest = "sha256:" + sha256(
                b"post-verification-foreign-journal-store"
            ).hexdigest()

            def observed_store_digest(identity):
                if raced["store_changed"]:
                    return changed_store_digest
                return original_store_digest(identity)

            def raced_verifier(*_args, **_kwargs):
                raced["store_changed"] = True
                return accepted

            with (
                patch.object(
                    chronology,
                    "parse_signed_qualification_attestation",
                    return_value=self._dummy_receipt(),
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
                patch.object(
                    chronology,
                    "journal_store_identity_digest",
                    side_effect=observed_store_digest,
                ),
            ):
                verifier = chronology._build_test_current_cut_verifier()
                with self.assertRaises(PermissionError) as caught:
                    verifier(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        evidence_store=artifacts,
                        evidence_root=str(artifacts.root),
                        expected_source_sha=_cases.SOURCE_SHA,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                        claimed_instants=("2026-10-03T14:00:00Z",),
                    )

            self.assertTrue(raced["store_changed"])
            self.assertIn("JournalStore", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
