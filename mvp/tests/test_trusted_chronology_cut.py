from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore

import _trusted_chronology_cut_cases as _cases
import mvp.autotrade_mvp.trusted_chronology_cut as chronology
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope


class TrustedChronologyCutTests(_cases.TrustedChronologyCutTests):
    """Run the full predecessor suite plus fail-closed horizon composition cases."""

    def _require_current(
        self,
        *,
        store,
        recovery,
        cut,
        accepted,
        artifact_store,
        expected_scope,
        runtime=None,
        expected_release_artifact_id=None,
        expected_release_artifact_sha256=None,
        claimed_instants=(),
    ):
        with (
            patch.object(
                chronology,
                "parse_signed_qualification_attestation",
                return_value=self._dummy_receipt(),
            ),
            patch.object(
                chronology,
                "verify_canonical_qualification_attestation",
                return_value=accepted,
            ),
        ):
            return chronology.require_current_trusted_chronology_cut(
                store=store,
                recovery=recovery,
                cut=cut,
                evidence_store=artifact_store,
                evidence_root=str(artifact_store.root),
                expected_source_sha=_cases.SOURCE_SHA,
                expected_scope=expected_scope,
                expected_release_artifact_id=expected_release_artifact_id,
                expected_release_artifact_sha256=expected_release_artifact_sha256,
                runtime=runtime,
                claimed_instants=claimed_instants,
            )

    def test_source_cut_is_runtime_free_and_current(self):
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

            self.assertEqual(cut.scope, ChronologyScope.SOURCE_QUALIFICATION)
            self.assertIsNone(cut.release_artifact_id)
            self.assertIsNone(cut.runtime_occurrence_id)
            self.assertIsNone(cut.runtime_host_id)
            self.assertEqual(cut.covered_utc, "2026-10-03T14:00:00Z")
            events = store.load_events_by_aggregate_type("trusted_chronology")
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "TrustedChronologyChallengePrepared",
                    "TrustedChronologyCutAccepted",
                ],
            )
            self.assertEqual(
                self._require_current(
                    store=store,
                    recovery=recovery,
                    cut=cut,
                    accepted=accepted,
                    artifact_store=artifacts,
                    expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                    claimed_instants=(
                        "2026-10-03T13:59:59Z",
                        "2026-10-03T14:00:00Z",
                    ),
                ),
                cut,
            )
            with self.assertRaisesRegex(PermissionError, "horizon"):
                self._require_current(
                    store=store,
                    recovery=recovery,
                    cut=cut,
                    accepted=accepted,
                    artifact_store=artifacts,
                    expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                    claimed_instants=("2026-10-03T14:00:00.000001Z",),
                )

    def test_standalone_horizon_never_admits_caller_owned_cut(self):
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

            with self.assertRaisesRegex(PermissionError, "standalone chronology horizon"):
                chronology.require_chronology_horizon(
                    cut,
                    "2026-10-03T14:00:00Z",
                )

    def test_forged_future_horizon_fails_durable_equality_before_claim(self):
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
            forged = replace(
                cut,
                covered_utc="2026-10-03T15:00:00Z",
                utc_lower_bound="2026-10-03T15:00:00Z",
                utc_upper_bound="2026-10-03T15:00:01Z",
            )

            with self.assertRaisesRegex(PermissionError, "differs from durable authority"):
                self._require_current(
                    store=store,
                    recovery=recovery,
                    cut=forged,
                    accepted=accepted,
                    artifact_store=artifacts,
                    expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                    claimed_instants=("2026-10-03T14:30:00Z",),
                )
