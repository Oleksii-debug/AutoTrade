from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from autotrade_runtime.artifacts import ArtifactStore

import _trusted_chronology_cut_cases as _cases
from mvp.autotrade_mvp import production_host
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyRebindingTests(unittest.TestCase):
    """Falsify post-callback module-global authority retargeting."""

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

    def test_runtime_currentness_ignores_helper_rebound_from_signed_verifier(self):
        with _cases.TemporaryDirectory() as directory:
            store, recovery, config, occurrence = self._state(directory)
            runtime = self._runtime(directory)
            patcher = None
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
                forged_runtime_currentness = Mock(return_value=stale_occurrence)
                patcher = patch.object(
                    chronology,
                    "require_current_production_host_runtime_occurrence",
                    forged_runtime_currentness,
                )
                successor = None

                def raced_verifier(*_args, **_kwargs):
                    nonlocal successor
                    successor = production_host._issue_production_host_runtime_occurrence(
                        store,
                        config,
                    )
                    patcher.start()
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
                    self.assertRaises(PermissionError),
                ):
                    chronology.require_current_trusted_chronology_cut(
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
                forged_runtime_currentness.assert_not_called()
            finally:
                if patcher is not None:
                    patcher.stop()
                runtime.close()

    def test_signed_verifier_cannot_rebind_instant_parser_to_launder_future_horizon(self):
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
            manifest = self._measurement_manifest(accepted, measurement)
            real_instant = chronology._instant
            horizon_parser_invoked = False

            def forged_instant(value, *, name):
                nonlocal horizon_parser_invoked
                if name == "covered_utc":
                    horizon_parser_invoked = True
                    return value, datetime.max.replace(tzinfo=timezone.utc)
                if name.startswith("claimed_instant["):
                    horizon_parser_invoked = True
                    return value, datetime.min.replace(tzinfo=timezone.utc)
                return real_instant(value, name=name)

            forged_parser = Mock(side_effect=forged_instant)
            parser_patcher = patch.object(chronology, "_instant", forged_parser)
            patch_started = False

            def raced_verifier(*_args, **_kwargs):
                nonlocal patch_started
                parser_patcher.start()
                patch_started = True
                return accepted

            try:
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
                        "trusted chronology implementation authority changed: _instant",
                    ),
                ):
                    chronology.require_current_trusted_chronology_cut(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        evidence_store=artifacts,
                        evidence_root=str(artifacts.root),
                        expected_source_sha=_cases.SOURCE_SHA,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                        claimed_instants=("2026-10-03T14:00:00.000001Z",),
                    )

                self.assertTrue(patch_started)
                self.assertFalse(horizon_parser_invoked)
            finally:
                if patch_started:
                    parser_patcher.stop()


if __name__ == "__main__":
    unittest.main()
