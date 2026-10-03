from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import NAMESPACE_URL, uuid5

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp import host_network, production_host
from mvp.autotrade_mvp.journal_taxonomy import (
    NON_FINANCIAL,
    QUALIFICATION_NON_FINANCIAL,
    require_journal_aggregate_descriptor,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.production_host import ProductionHostConfig
from mvp.autotrade_mvp.qualification_attestation import (
    AcceptedQualificationAttestation,
    EvidenceArtifactRef,
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.trusted_chronology import ChronologyScope
import mvp.autotrade_mvp.trusted_chronology_cut as chronology
from mvp.autotrade_mvp.trusted_chronology_cut import (
    TrustedChronologyError,
    accept_trusted_chronology_cut,
    chronology_measurement_requirement,
    prepare_durable_chronology_challenge,
    require_chronology_horizon,
    require_current_trusted_chronology_cut,
)


SOURCE_SHA = "a" * 40
RELEASE_ID = str(uuid5(NAMESPACE_URL, "chronology-release"))
RELEASE_SHA = "sha256:" + "b" * 64
MEASUREMENT_ID = str(uuid5(NAMESPACE_URL, "chronology-measurement"))
ATTESTATION_ID = str(uuid5(NAMESPACE_URL, "chronology-attestation"))
POLICY_ID = "sha256:" + "c" * 64
ROOT_ID = "sha256:" + "d" * 64
ATTESTATION_DIGEST = "sha256:" + "e" * 64
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _wall_ns(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    delta = parsed - EPOCH
    return (
        delta.days * 86_400_000_000_000
        + delta.seconds * 1_000_000_000
        + delta.microseconds * 1_000
    )


class TrustedChronologyCutTests(unittest.TestCase):
    def _state(self, directory: str):
        path = Path(directory) / "journal.sqlite3"
        store = JournalStore(path)
        recovery = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:paper-account",
        )
        recovery.start("host-a")
        config = ProductionHostConfig(
            journal_path=path.resolve(),
            account_id="paper-account",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )
        occurrence = production_host._issue_production_host_runtime_occurrence(
            store,
            config,
        )
        return store, recovery, config, occurrence

    class DummySecurityBoundary:
        pass

    def _runtime(self, directory: str):
        path = Path(directory) / "journal.sqlite3"
        config = ProductionHostConfig(
            journal_path=path.resolve(),
            account_id="paper-account",
            environment="PAPER",
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )
        with (
            patch.object(
                production_host,
                "SecurityBoundary",
                self.DummySecurityBoundary,
            ),
            patch.object(
                host_network,
                "SecurityBoundary",
                self.DummySecurityBoundary,
            ),
            patch.object(
                production_host,
                "AuthenticatedHostServer",
                return_value=Mock(),
            ),
        ):
            return production_host.build_production_host(
                config,
                security_boundary=self.DummySecurityBoundary(),
                principal_resolver=lambda headers, origin: None,
                snapshot_provider=lambda state, principal: {},
            )

    @staticmethod
    def _measurement(attempt, *, lower="2026-10-03T14:00:00Z", upper="2026-10-03T14:00:01Z"):
        return _canonical_bytes(
            {
                "authority_id": "independent-time-authority",
                "challenge_digest": attempt.challenge.challenge_digest,
                "protocol_id": "rfc3161-or-equivalent-v1",
                "protocol_version": "1.0.0",
                "request_nonce": attempt.challenge.request_nonce,
                "response_id": "response-1",
                "schema_version": "1.0.0",
                "utc_lower_bound": lower,
                "utc_upper_bound": upper,
            }
        )

    @staticmethod
    def _dummy_receipt():
        return object.__new__(SignedQualificationAttestation)

    @staticmethod
    def _accepted(attempt, measurement, *, result="PASS", unresolved_limits=(), release_marker="attempt"):
        measurement_sha = "sha256:" + sha256(measurement).hexdigest()
        dynamic_requirement = chronology_measurement_requirement(
            attempt,
            measurement,
        )
        ref = EvidenceArtifactRef(
            artifact_id=MEASUREMENT_ID,
            sha256=measurement_sha,
            media_type="application/json",
            evidence_kind="TRUSTED_CHRONOLOGY_MEASUREMENT",
            source_sha=SOURCE_SHA,
        )
        if release_marker == "attempt":
            release_id = attempt.challenge.release_artifact_id
            release_sha = attempt.challenge.release_artifact_sha256
        elif release_marker == "wrong":
            release_id = str(uuid5(NAMESPACE_URL, "wrong-release"))
            release_sha = "sha256:" + "f" * 64
        else:
            release_id = None
            release_sha = None
        return AcceptedQualificationAttestation(
            attestation_id=ATTESTATION_ID,
            attestation_digest=ATTESTATION_DIGEST,
            policy_id=POLICY_ID,
            policy_version="2026.10",
            trust_root_id=ROOT_ID,
            result=result,
            source_sha=SOURCE_SHA,
            domain="HOST_CLOCK",
            gate="CHRONOLOGY",
            package_id="WP-48",
            protocol_id="trusted-chronology-cut-v1",
            protocol_version="1.0.0",
            requirement_id="independent-utc-chronology-cut",
            requirement_ids=(
                "independent-utc-chronology-cut",
                dynamic_requirement,
            ),
            evidence_refs=(ref,),
            producer_id="independent.qualifier",
            verifier_id="autotrade.qualifier",
            runner_id="runner-1",
            harness_version="1.0.0",
            started_at="2026-10-03T13:59:59Z",
            completed_at="2026-10-03T14:00:01Z",
            signed_at="2026-10-03T14:00:02Z",
            unresolved_limits=tuple(unresolved_limits),
            schema_version="1.0.0",
            verification_method="RSA_PKCS1V15_SHA256",
            release_artifact_id=release_id,
            release_artifact_sha256=release_sha,
            attestation_json="{}",
            signature_b64="AA==",
        )

    def _prepare(
        self,
        store,
        recovery,
        occurrence=None,
        *,
        scope=ChronologyScope.SOURCE_QUALIFICATION,
        runtime=None,
    ):
        del occurrence
        kwargs = {}
        if scope is ChronologyScope.RELEASE_RUNTIME:
            kwargs = {
                "release_artifact_id": RELEASE_ID,
                "release_artifact_sha256": RELEASE_SHA,
                "runtime": runtime,
            }
        with (
            patch.object(chronology.time, "monotonic_ns", return_value=10_000_000_000),
            patch.object(
                chronology.time,
                "time_ns",
                return_value=_wall_ns("2026-10-03T14:00:00Z"),
            ),
        ):
            return prepare_durable_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=scope,
                **kwargs,
            )

    def _accept(
        self,
        store,
        recovery,
        attempt,
        measurement,
        accepted,
        artifact_store,
        *,
        finish_mono=11_000_000_000,
        finish_wall="2026-10-03T14:00:01Z",
        verifier_side_effect=None,
        runtime=None,
    ):
        if verifier_side_effect is None:
            verifier_side_effect = lambda *args, **kwargs: accepted
        with (
            patch.object(chronology.time, "monotonic_ns", return_value=finish_mono),
            patch.object(
                chronology.time,
                "time_ns",
                return_value=_wall_ns(finish_wall),
            ),
            patch.object(
                chronology,
                "verify_canonical_qualification_attestation",
                side_effect=verifier_side_effect,
            ),
        ):
            return accept_trusted_chronology_cut(
                store=store,
                recovery=recovery,
                attempt=attempt,
                measurement_bytes=measurement,
                receipt=self._dummy_receipt(),
                evidence_store=artifact_store,
                evidence_root=str(artifact_store.root),
                runtime=runtime,
            )

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
            return require_current_trusted_chronology_cut(
                store=store,
                recovery=recovery,
                cut=cut,
                evidence_store=artifact_store,
                evidence_root=str(artifact_store.root),
                expected_source_sha=SOURCE_SHA,
                expected_scope=expected_scope,
                expected_release_artifact_id=expected_release_artifact_id,
                expected_release_artifact_sha256=expected_release_artifact_sha256,
                runtime=runtime,
            )

    def test_source_cut_is_runtime_free_and_current(self):
        with TemporaryDirectory() as directory:
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
                ),
                cut,
            )
            require_chronology_horizon(
                cut,
                "2026-10-03T13:59:59Z",
                "2026-10-03T14:00:00Z",
            )
            with self.assertRaisesRegex(PermissionError, "horizon"):
                require_chronology_horizon(
                    cut,
                    "2026-10-03T14:00:00.000001Z",
                )

    def test_release_runtime_cut_binds_exact_delivered_release(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            runtime = self._runtime(directory)
            try:
                runtime_occurrence = runtime.runtime_occurrence
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

                self.assertEqual(cut.release_artifact_id, RELEASE_ID)
                self.assertEqual(cut.release_artifact_sha256, RELEASE_SHA)
                self.assertEqual(
                    cut.runtime_occurrence_id,
                    runtime_occurrence.runtime_occurrence_id,
                )
                self.assertEqual(cut.runtime_host_id, runtime_occurrence.host_id)
                self.assertEqual(
                    self._require_current(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        accepted=accepted,
                        artifact_store=artifacts,
                        expected_scope=ChronologyScope.RELEASE_RUNTIME,
                        expected_release_artifact_id=RELEASE_ID,
                        expected_release_artifact_sha256=RELEASE_SHA,
                        runtime=runtime,
                    ),
                    cut,
                )
                with self.assertRaisesRegex(PermissionError, "release identity"):
                    self._require_current(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        accepted=accepted,
                        artifact_store=artifacts,
                        expected_scope=ChronologyScope.RELEASE_RUNTIME,
                        expected_release_artifact_id=str(
                            uuid5(NAMESPACE_URL, "other-release")
                        ),
                        expected_release_artifact_sha256=RELEASE_SHA,
                        runtime=runtime,
                    )
                with self.assertRaisesRegex(PermissionError, "source/scope"):
                    self._require_current(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        accepted=accepted,
                        artifact_store=artifacts,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                        runtime=runtime,
                    )
            finally:
                runtime.close()

    def test_new_runtime_occurrence_invalidates_prepared_attempt(self):
        with TemporaryDirectory() as directory:
            store, recovery, config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            successor = production_host._issue_production_host_runtime_occurrence(
                store,
                config,
            )
            self.assertNotEqual(
                successor.runtime_occurrence_id,
                occurrence.runtime_occurrence_id,
            )
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(PermissionError, "journal advanced"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_clock_incident_after_prepare_invalidates_attempt_even_after_restore(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            recovery.set_clock_trusted(
                False,
                reason_code="clock-loss",
                evidence_ref="probe:loss",
            )
            recovery.set_clock_trusted(
                True,
                reason_code="clock-restored",
                evidence_ref="probe:restore",
            )
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(PermissionError, "incident generation"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_freshness_limit_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(TrustedChronologyError, "freshness"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                    finish_mono=40_000_000_001,
                    finish_wall="2026-10-03T14:00:30Z",
                )

    def test_external_uncertainty_limit_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(
                attempt,
                lower="2026-10-03T13:59:59Z",
                upper="2026-10-03T14:00:01.000001Z",
            )
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(TrustedChronologyError, "uncertainty"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_local_wall_rollback_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(TrustedChronologyError, "wall clock moved backward"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                    finish_wall="2026-10-03T13:59:59Z",
                )

    def test_external_local_offset_limit_is_fail_closed(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(
                attempt,
                lower="2026-10-03T14:00:07Z",
                upper="2026-10-03T14:00:08Z",
            )
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(TrustedChronologyError, "disagrees"):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_attestation_must_bind_exact_raw_measurement_digest(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            wrong_ref = EvidenceArtifactRef(
                artifact_id=MEASUREMENT_ID,
                sha256="sha256:" + "9" * 64,
                media_type="application/json",
                evidence_kind="TRUSTED_CHRONOLOGY_MEASUREMENT",
                source_sha=SOURCE_SHA,
            )
            accepted = replace(
                accepted,
                evidence_refs=(wrong_ref,),
            )
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                TrustedChronologyError,
                "exactly one raw measurement",
            ):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_release_attestation_mismatch_is_rejected_defensively(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            runtime = self._runtime(directory)
            try:
                attempt = self._prepare(
                    store,
                    recovery,
                    occurrence,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                    runtime=runtime,
                )
                measurement = self._measurement(attempt)
                accepted = self._accepted(
                    attempt,
                    measurement,
                    release_marker="wrong",
                )
                artifacts = ArtifactStore(Path(directory) / "artifacts")
                with self.assertRaisesRegex(
                    TrustedChronologyError,
                    "release identity mismatch",
                ):
                    self._accept(
                        store,
                        recovery,
                        attempt,
                        measurement,
                        accepted,
                        artifacts,
                        runtime=runtime,
                    )
            finally:
                runtime.close()

    def test_concurrent_durable_write_during_verification_prevents_cut(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")

            def raced_verifier(*args, **kwargs):
                payload = {"value": "raced"}
                store.append_event(
                    {
                        "event_id": str(uuid5(NAMESPACE_URL, "chronology-race")),
                        "event_type": "TestChronologyRace",
                        "aggregate_type": "test_chronology_race",
                        "aggregate_id": "race",
                        "aggregate_version": "1",
                        "payload": payload,
                        "payload_hash": payload_digest(payload),
                        "committed_at": "2026-10-03T14:00:00Z",
                    }
                )
                return accepted

            with self.assertRaisesRegex(
                PermissionError,
                "journal advanced",
            ):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                    verifier_side_effect=raced_verifier,
                )
            events = store.load_events_by_aggregate_type("trusted_chronology")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["TrustedChronologyChallengePrepared"],
            )

    def test_source_cut_rejects_release_scoped_accepted_value(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(
                attempt,
                measurement,
                release_marker="wrong",
            )
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            with self.assertRaisesRegex(
                TrustedChronologyError,
                "cannot consume release-scoped",
            ):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_non_pass_or_unresolved_attestation_cannot_mint_cut(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            for accepted in (
                self._accepted(attempt, measurement, result="FAIL"),
                self._accepted(
                    attempt,
                    measurement,
                    result="INCONCLUSIVE",
                    unresolved_limits=("external-authority-unavailable",),
                ),
            ):
                with self.subTest(result=accepted.result):
                    with self.assertRaisesRegex(
                        TrustedChronologyError,
                        "requires PASS",
                    ):
                        self._accept(
                            store,
                            recovery,
                            attempt,
                            measurement,
                            accepted,
                            artifacts,
                        )

    def test_measurement_requirement_detaches_challenge_from_parser_side_effect(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            expected = chronology_measurement_requirement(attempt, measurement)
            original_owner_id = attempt.challenge.owner_id
            real_parser = chronology.parse_challenge_bound_measurement

            def mutating_parser(data, *, challenge):
                transcript = real_parser(data, challenge=challenge)
                object.__setattr__(
                    attempt.challenge,
                    "owner_id",
                    "attacker-controlled-owner",
                )
                return transcript

            try:
                with patch.object(
                    chronology,
                    "parse_challenge_bound_measurement",
                    side_effect=mutating_parser,
                ):
                    observed = chronology_measurement_requirement(
                        attempt,
                        measurement,
                    )
            finally:
                object.__setattr__(
                    attempt.challenge,
                    "owner_id",
                    original_owner_id,
                )

            self.assertEqual(observed, expected)

    def test_accept_detaches_attempt_before_final_frontier_side_effect(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            original_owner_id = attempt.challenge.owner_id
            real_current_sequence = chronology._current_sequence
            current_sequence_calls = 0

            def mutating_current_sequence(selected_store):
                nonlocal current_sequence_calls
                sequence = real_current_sequence(selected_store)
                current_sequence_calls += 1
                if current_sequence_calls == 2:
                    object.__setattr__(
                        attempt.challenge,
                        "owner_id",
                        "attacker-controlled-owner",
                    )
                return sequence

            try:
                with patch.object(
                    chronology,
                    "_current_sequence",
                    side_effect=mutating_current_sequence,
                ):
                    cut = self._accept(
                        store,
                        recovery,
                        attempt,
                        measurement,
                        accepted,
                        artifacts,
                    )
            finally:
                object.__setattr__(
                    attempt.challenge,
                    "owner_id",
                    original_owner_id,
                )

            self.assertGreaterEqual(current_sequence_calls, 2)
            self.assertEqual(cut.owner_id, original_owner_id)
            self.assertIsNone(cut.runtime_occurrence_id)

    def test_current_cut_validation_detaches_caller_cut_before_durable_read(self):
        with TemporaryDirectory() as directory:
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
            original_owner_id = cut.owner_id
            real_load_events = chronology._load_events

            def mutating_load_events(selected_store, aggregate_id):
                events = real_load_events(selected_store, aggregate_id)
                object.__setattr__(
                    cut,
                    "owner_id",
                    "attacker-controlled-owner",
                )
                return events

            try:
                with patch.object(
                    chronology,
                    "_load_events",
                    side_effect=mutating_load_events,
                ):
                    durable = self._require_current(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        accepted=accepted,
                        artifact_store=artifacts,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                    )
            finally:
                object.__setattr__(cut, "owner_id", original_owner_id)

            self.assertEqual(durable.owner_id, original_owner_id)

    def test_prepared_event_rejects_rehashed_nested_challenge_tamper(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            self._prepare(store, recovery, occurrence)
            event = dict(
                store.load_events_by_aggregate_type("trusted_chronology")[0]
            )
            payload = dict(event["payload"])
            nested = dict(payload["challenge"])
            nested["owner_id"] = "attacker-controlled-owner"
            payload["challenge"] = nested
            event["payload"] = payload
            event["payload_hash"] = payload_digest(payload)

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "challenge digest mismatch",
            ):
                chronology._validate_prepared_event(event)

    def test_prepared_event_rejects_rehashed_protocol_limit_tamper(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            self._prepare(store, recovery, occurrence)
            event = dict(
                store.load_events_by_aggregate_type("trusted_chronology")[0]
            )
            payload = dict(event["payload"])
            limits = dict(payload["limits"])
            limits["max_request_elapsed_ns"] = "1"
            payload["limits"] = limits
            event["payload"] = payload
            event["payload_hash"] = payload_digest(payload)

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "limits differ",
            ):
                chronology._validate_prepared_event(event)

    def test_prepared_event_must_be_immediate_challenge_successor(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            self._prepare(store, recovery, occurrence)
            event = dict(
                store.load_events_by_aggregate_type("trusted_chronology")[0]
            )
            event["journal_sequence"] += 1

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "sole post-challenge write",
            ):
                chronology._validate_prepared_event(event)

    def test_measurement_requirement_executes_valid_exact_attempt_snapshot(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)

            requirement = chronology_measurement_requirement(attempt, measurement)

            self.assertTrue(requirement.startswith("trusted-chronology:"))
            self.assertEqual(len(requirement), len("trusted-chronology:") + 64)

    def test_attempt_snapshot_rejects_nonexact_scope_without_property_dispatch(self):
        class ExplosiveScope:
            @property
            def value(self):
                raise AssertionError("hostile scope property must not execute")

        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            object.__setattr__(attempt.challenge, "scope", ExplosiveScope())

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "scope must be exact ChronologyScope",
            ):
                chronology_measurement_requirement(attempt, measurement)

    def test_cut_snapshot_rejects_equality_bearing_field_before_durable_compare(self):
        class ExplosiveEquality:
            def __eq__(self, other):
                raise AssertionError("hostile equality must not execute")

        with TemporaryDirectory() as directory:
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
            object.__setattr__(cut, "owner_id", ExplosiveEquality())

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "owner_id must be canonical",
            ):
                self._require_current(
                    store=store,
                    recovery=recovery,
                    cut=cut,
                    accepted=accepted,
                    artifact_store=artifacts,
                    expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                )

    def test_receipt_completed_at_cannot_predate_measurement_upper_bound(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = replace(
                self._accepted(attempt, measurement),
                completed_at="2026-10-03T14:00:00Z",
            )
            artifacts = ArtifactStore(Path(directory) / "artifacts")

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "receipt predates authorized measurement",
            ):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_receipt_signed_at_cannot_predate_measurement_upper_bound(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = replace(
                self._accepted(attempt, measurement),
                signed_at="2026-10-03T14:00:00Z",
            )
            artifacts = ArtifactStore(Path(directory) / "artifacts")

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "receipt predates authorized measurement",
            ):
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                )

    def test_receipt_completion_equal_measurement_upper_bound_is_admitted(self):
        with TemporaryDirectory() as directory:
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

            self.assertEqual(cut.utc_upper_bound, "2026-10-03T14:00:01Z")

    def test_durable_reverification_rejects_receipt_time_before_measurement(self):
        with TemporaryDirectory() as directory:
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
            stale_acceptance = replace(
                accepted,
                completed_at="2026-10-03T14:00:00Z",
            )

            with self.assertRaisesRegex(
                TrustedChronologyError,
                "receipt predates authorized measurement",
            ):
                self._require_current(
                    store=store,
                    recovery=recovery,
                    cut=cut,
                    accepted=stale_acceptance,
                    artifact_store=artifacts,
                    expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                )

    def test_current_cut_rejects_rehashed_dynamic_requirement_splice(self):
        with TemporaryDirectory() as directory:
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
            events = tuple(
                store.load_events_by_aggregate_type("trusted_chronology")
            )
            accepted_event = dict(events[1])
            payload = dict(accepted_event["payload"])
            original_dynamic_requirement = payload["measurement_requirement_id"]
            payload["measurement_requirement_id"] = "independent-utc-chronology-cut"
            payload["cut_digest"] = chronology._cut_digest(payload)
            accepted_event["payload"] = payload
            accepted_event["payload_hash"] = payload_digest(payload)

            self.assertNotEqual(
                payload["measurement_requirement_id"],
                original_dynamic_requirement,
            )
            self.assertIn(
                payload["measurement_requirement_id"],
                accepted.requirement_ids,
            )
            with patch.object(
                chronology,
                "_load_events",
                return_value=(events[0], accepted_event),
            ):
                with self.assertRaisesRegex(
                    TrustedChronologyError,
                    "measurement requirement differs from durable subject",
                ):
                    self._require_current(
                        store=store,
                        recovery=recovery,
                        cut=cut,
                        accepted=accepted,
                        artifact_store=artifacts,
                        expected_scope=ChronologyScope.SOURCE_QUALIFICATION,
                    )

    def test_validated_aggregate_rejects_rehashed_owner_splice(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            attempt = self._prepare(store, recovery, occurrence)
            measurement = self._measurement(attempt)
            accepted = self._accepted(attempt, measurement)
            artifacts = ArtifactStore(Path(directory) / "artifacts")
            self._accept(
                store,
                recovery,
                attempt,
                measurement,
                accepted,
                artifacts,
            )
            events = tuple(
                store.load_events_by_aggregate_type("trusted_chronology")
            )
            accepted_event = dict(events[1])
            payload = dict(accepted_event["payload"])
            original_challenge_digest = payload["challenge_digest"]
            payload["owner_id"] = "spliced-owner"
            payload["cut_digest"] = chronology._cut_digest(payload)
            accepted_event["payload"] = payload
            accepted_event["payload_hash"] = payload_digest(payload)

            self.assertEqual(
                payload["challenge_digest"],
                original_challenge_digest,
            )
            with self.assertRaisesRegex(
                TrustedChronologyError,
                "differs from prepared challenge",
            ):
                chronology._validated_aggregate((events[0], accepted_event))

    def test_validated_aggregate_rejects_rehashed_release_runtime_splice(self):
        with TemporaryDirectory() as directory:
            store, recovery, _config, occurrence = self._state(directory)
            runtime = self._runtime(directory)
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
                self._accept(
                    store,
                    recovery,
                    attempt,
                    measurement,
                    accepted,
                    artifacts,
                    runtime=runtime,
                )
                events = tuple(
                    store.load_events_by_aggregate_type("trusted_chronology")
                )
                accepted_event = dict(events[1])
                payload = dict(accepted_event["payload"])
                original_challenge_digest = payload["challenge_digest"]
                payload["runtime_host_id"] = "spliced-host"
                payload["cut_digest"] = chronology._cut_digest(payload)
                accepted_event["payload"] = payload
                accepted_event["payload_hash"] = payload_digest(payload)

                self.assertEqual(
                    payload["challenge_digest"],
                    original_challenge_digest,
                )
                with self.assertRaisesRegex(
                    TrustedChronologyError,
                    "differs from prepared challenge",
                ):
                    chronology._validated_aggregate((events[0], accepted_event))
            finally:
                runtime.stop()

    def test_taxonomy_classifies_cut_as_nonfinancial_qualification_evidence(self):
        descriptor = require_journal_aggregate_descriptor("trusted_chronology")
        self.assertEqual(descriptor.domain_classification, NON_FINANCIAL)
        self.assertEqual(
            descriptor.qualification_visibility,
            QUALIFICATION_NON_FINANCIAL,
        )


if __name__ == "__main__":
    unittest.main()
