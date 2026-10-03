from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from autotrade_runtime.artifacts import ArtifactStore

import _trusted_chronology_cut_cases as _cases
from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyRuntimeGlobalsTests(unittest.TestCase):
    DummySecurityBoundary = _cases.TrustedChronologyCutTests.DummySecurityBoundary
    _state = _cases.TrustedChronologyCutTests._state
    _runtime = _cases.TrustedChronologyCutTests._runtime
    _measurement = staticmethod(_cases.TrustedChronologyCutTests._measurement)
    _dummy_receipt = staticmethod(_cases.TrustedChronologyCutTests._dummy_receipt)
    _accepted = staticmethod(_cases.TrustedChronologyCutTests._accepted)
    _prepare = _cases.TrustedChronologyCutTests._prepare
    _accept = _cases.TrustedChronologyCutTests._accept

    @staticmethod
    def _measurement_manifest(accepted, measurement):
        evidence_ref = accepted.evidence_refs[0]
        return {
            "artifact_id": evidence_ref.artifact_id,
            "sha256": evidence_ref.sha256,
            "media_type": evidence_ref.media_type,
            "metadata": {"evidence_kind": evidence_ref.evidence_kind},
            "source_refs": [f"git:{evidence_ref.source_sha}"],
        }

    def test_signed_verifier_cannot_retarget_runtime_journal_reader_globals(self):
        with _cases.TemporaryDirectory() as directory:
            store, recovery, config, occurrence = self._state(directory)
            runtime = self._runtime(directory)
            patcher = None
            patch_started = False
            try:
                attempt = self._prepare(
                    store,
                    recovery,
                    occurrence,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                    runtime=runtime,
                )
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
                    runtime=runtime,
                )
                stale_occurrence = runtime.runtime_occurrence
                manifest = self._measurement_manifest(accepted, measurement)
                forged_load_events = Mock(return_value=[])
                forged_store_type = type(
                    "ForgedJournalStore",
                    (),
                    {"load_events": staticmethod(forged_load_events)},
                )
                patcher = patch.object(
                    production_host,
                    "JournalStore",
                    forged_store_type,
                )
                successor = None

                def raced_verifier(*_args, **_kwargs):
                    nonlocal successor, patch_started
                    successor = production_host._issue_production_host_runtime_occurrence(
                        store,
                        config,
                    )
                    patcher.start()
                    patch_started = True
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
                    self.assertRaisesRegex(
                        RuntimeError,
                        "trusted chronology production runtime authority changed: JournalStore",
                    ),
                ):
                    verifier = chronology._build_test_current_cut_verifier()
                    verifier(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        evidence_store=artifacts,
                        evidence_root=str(artifacts.root),
                        expected_source_sha=_cases.SOURCE_SHA,
                        expected_scope=ChronologyScope.RELEASE_RUNTIME,
                        expected_release_artifact_id=_cases.RELEASE_ID,
                        expected_release_artifact_sha256=_cases.RELEASE_SHA,
                        runtime=runtime,
                        claimed_instants=("2026-10-03T14:00:00Z",),
                    )

                self.assertIsNotNone(successor)
                self.assertNotEqual(
                    successor.runtime_occurrence_id,
                    stale_occurrence.runtime_occurrence_id,
                )
                forged_load_events.assert_not_called()
            finally:
                if patch_started and patcher is not None:
                    patcher.stop()
                runtime.close()


if __name__ == "__main__":
    unittest.main()
