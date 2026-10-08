from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import host_network, production_host
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.trusted_chronology import (
    ChronologyScope,
    parse_challenge_bound_measurement,
    prepare_chronology_challenge,
    require_current_chronology_challenge,
)


SOURCE_SHA = "a" * 40


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class TrustedChronologyChallengeTests(unittest.TestCase):
    def _durable_controller(self, root: Path):
        store = JournalStore(root / "journal.sqlite3")
        recovery = RecoveryController(
            owner_store=store,
            owner_scope="PAPER:account-1",
        )
        recovery.start("owner-a")
        return store, recovery

    class DummySecurityBoundary:
        pass

    def _production_runtime(
        self,
        root: Path,
        *,
        account_id: str = "account-1",
        environment: str = "PAPER",
    ):
        config = production_host.ProductionHostConfig(
            journal_path=root / "journal.sqlite3",
            account_id=account_id,
            environment=environment,
            host_id="host-a",
            bind_host="127.0.0.1",
            bind_port=8765,
            public_origin="http://127.0.0.1:8765",
        )
        patches = (
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
        )
        with patches[0], patches[1], patches[2]:
            return production_host.build_production_host(
                config,
                security_boundary=self.DummySecurityBoundary(),
                principal_resolver=lambda headers, origin: None,
                snapshot_provider=lambda state, principal: {},
            )

    def test_challenge_binds_physical_store_owner_incident_and_journal_cut(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )

            self.assertEqual(challenge.owner_scope, "PAPER:account-1")
            self.assertEqual(challenge.owner_id, "owner-a")
            self.assertEqual(challenge.owner_epoch, 1)
            self.assertEqual(challenge.clock_incident_generation, 0)
            self.assertEqual(
                challenge.journal_sequence,
                store.current_journal_sequence(),
            )
            self.assertTrue(challenge.store_identity_digest.startswith("sha256:"))
            self.assertEqual(len(challenge.request_nonce), 64)
            self.assertTrue(challenge.challenge_digest.startswith("sha256:"))
            require_current_chronology_challenge(
                challenge=challenge,
                store=store,
                recovery=recovery,
            )

    def test_broad_local_clock_recovery_cannot_reuse_pre_incident_challenge(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )

            recovery.set_clock_trusted(False, evidence_ref="clock-check-1")
            recovery.set_clock_trusted(True)

            with self.assertRaises(PermissionError):
                require_current_chronology_challenge(
                    challenge=challenge,
                    store=store,
                    recovery=recovery,
                )

    def test_unresolved_durable_incident_cannot_be_overridden_in_process(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            recovery.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-check:durable-open",
            )

            recovery.clock_trusted = True

            with self.assertRaisesRegex(
                PermissionError,
                "durable clock incident is unresolved",
            ):
                prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.SOURCE_QUALIFICATION,
                )

    def test_current_challenge_rechecks_unresolved_durable_incident(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            recovery.set_clock_trusted(
                False,
                reason_code="clock-health-check-failed",
                evidence_ref="clock-check:durable-open",
            )
            recovery.clock_trusted = True

            from mvp.autotrade_mvp import trusted_chronology as chronology_module

            with patch.object(
                chronology_module,
                "_durable_clock_state_at_cut",
                return_value=(1, True),
            ):
                challenge = prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.SOURCE_QUALIFICATION,
                )

            with self.assertRaisesRegex(
                PermissionError,
                "durable clock incident is unresolved",
            ):
                require_current_chronology_challenge(
                    challenge=challenge,
                    store=store,
                    recovery=recovery,
                )

    def test_source_scope_rejects_release_identity_and_release_scope_requires_it(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            with self.assertRaises(ValueError):
                prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.SOURCE_QUALIFICATION,
                    release_artifact_id="11111111-1111-1111-1111-111111111111",
                    release_artifact_sha256="sha256:" + ("b" * 64),
                )
            with self.assertRaises(ValueError):
                prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                )

    def test_release_scope_binds_exact_current_runtime_occurrence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store, recovery = self._durable_controller(root)
            runtime = self._production_runtime(root)
            try:
                occurrence = runtime.runtime_occurrence
                challenge = prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                    release_artifact_id="11111111-1111-4111-8111-111111111111",
                    release_artifact_sha256="sha256:" + ("b" * 64),
                    runtime=runtime,
                )

                self.assertEqual(challenge.schema_version, "1.1.0")
                self.assertEqual(challenge.runtime_host_id, occurrence.host_id)
                self.assertEqual(
                    challenge.runtime_occurrence_id,
                    occurrence.runtime_occurrence_id,
                )
                self.assertEqual(
                    challenge.runtime_occurrence_version,
                    occurrence.aggregate_version,
                )
                self.assertEqual(
                    challenge.runtime_occurrence_journal_sequence,
                    occurrence.journal_sequence,
                )
                self.assertLessEqual(
                    occurrence.journal_sequence,
                    challenge.journal_sequence,
                )
                require_current_chronology_challenge(
                    challenge=challenge,
                    store=store,
                    recovery=recovery,
                    runtime=runtime,
                )
            finally:
                runtime.close()

    def test_release_scope_requires_runtime_and_source_scope_rejects_one(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store, recovery = self._durable_controller(root)
            with self.assertRaisesRegex(
                ValueError,
                "requires canonical production runtime occurrence",
            ):
                prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                    release_artifact_id="11111111-1111-4111-8111-111111111111",
                    release_artifact_sha256="sha256:" + ("b" * 64),
                )

            runtime = self._production_runtime(root)
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "cannot carry runtime occurrence",
                ):
                    prepare_chronology_challenge(
                        store=store,
                        recovery=recovery,
                        source_sha=SOURCE_SHA,
                        scope=ChronologyScope.SOURCE_QUALIFICATION,
                        runtime=runtime,
                    )
            finally:
                runtime.close()

    def test_release_scope_rejects_runtime_from_other_store_generation(self):
        with TemporaryDirectory() as selected_directory, TemporaryDirectory() as other_directory:
            selected_root = Path(selected_directory)
            store, recovery = self._durable_controller(selected_root)
            other_runtime = self._production_runtime(Path(other_directory))
            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "does not share chronology JournalStore generation",
                ):
                    prepare_chronology_challenge(
                        store=store,
                        recovery=recovery,
                        source_sha=SOURCE_SHA,
                        scope=ChronologyScope.RELEASE_RUNTIME,
                        release_artifact_id="11111111-1111-4111-8111-111111111111",
                        release_artifact_sha256="sha256:" + ("b" * 64),
                        runtime=other_runtime,
                    )
            finally:
                other_runtime.close()

    def test_release_scope_rejects_runtime_account_or_environment_splice(self):
        for account_id, environment, message in (
            ("other-account", "PAPER", "account does not match"),
            ("account-1", "SIMULATION", "environment does not match"),
        ):
            with self.subTest(account_id=account_id, environment=environment):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    store, recovery = self._durable_controller(root)
                    runtime = self._production_runtime(
                        root,
                        account_id=account_id,
                        environment=environment,
                    )
                    try:
                        with self.assertRaisesRegex(PermissionError, message):
                            prepare_chronology_challenge(
                                store=store,
                                recovery=recovery,
                                source_sha=SOURCE_SHA,
                                scope=ChronologyScope.RELEASE_RUNTIME,
                                release_artifact_id=(
                                    "11111111-1111-4111-8111-111111111111"
                                ),
                                release_artifact_sha256="sha256:" + ("b" * 64),
                                runtime=runtime,
                            )
                    finally:
                        runtime.close()

    def test_release_challenge_cannot_survive_runtime_occurrence_advance(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store, recovery = self._durable_controller(root)
            runtime = self._production_runtime(root)
            try:
                challenge = prepare_chronology_challenge(
                    store=store,
                    recovery=recovery,
                    source_sha=SOURCE_SHA,
                    scope=ChronologyScope.RELEASE_RUNTIME,
                    release_artifact_id="11111111-1111-4111-8111-111111111111",
                    release_artifact_sha256="sha256:" + ("b" * 64),
                    runtime=runtime,
                )
                production_host._issue_production_host_runtime_occurrence(
                    runtime.journal,
                    runtime.config,
                )
                with self.assertRaises(PermissionError):
                    require_current_chronology_challenge(
                        challenge=challenge,
                        store=store,
                        recovery=recovery,
                        runtime=runtime,
                    )
            finally:
                runtime.close()

    def test_measurement_must_echo_exact_fresh_challenge(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )
            payload = {
                "authority_id": "time-authority-1",
                "challenge_digest": challenge.challenge_digest,
                "protocol_id": "challenge-time-v1",
                "protocol_version": "1.0.0",
                "request_nonce": challenge.request_nonce,
                "response_id": "response-1",
                "schema_version": "1.0.0",
                "utc_lower_bound": "2026-10-01T04:00:00Z",
                "utc_upper_bound": "2026-10-01T04:00:00.25Z",
            }
            parsed = parse_challenge_bound_measurement(
                _canonical_bytes(payload),
                challenge=challenge,
            )

            self.assertEqual(
                parsed.conservative_covered_utc,
                "2026-10-01T04:00:00Z",
            )

            payload["challenge_digest"] = "sha256:" + ("0" * 64)
            with self.assertRaises(PermissionError):
                parse_challenge_bound_measurement(
                    _canonical_bytes(payload),
                    challenge=challenge,
                )

    def test_measurement_rejects_noncanonical_or_reversed_time_evidence(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )
            payload = {
                "authority_id": "time-authority-1",
                "challenge_digest": challenge.challenge_digest,
                "protocol_id": "challenge-time-v1",
                "protocol_version": "1.0.0",
                "request_nonce": challenge.request_nonce,
                "response_id": "response-1",
                "schema_version": "1.0.0",
                "utc_lower_bound": "2026-10-01T04:00:01Z",
                "utc_upper_bound": "2026-10-01T04:00:00Z",
            }
            with self.assertRaises(ValueError):
                parse_challenge_bound_measurement(
                    _canonical_bytes(payload),
                    challenge=challenge,
                )

            payload["utc_lower_bound"] = "2026-10-01T04:00:00Z"
            noncanonical = json.dumps(payload, indent=2).encode("utf-8")
            with self.assertRaisesRegex(ValueError, "not canonical JSON"):
                parse_challenge_bound_measurement(
                    noncanonical,
                    challenge=challenge,
                )

    def test_measurement_utc_fraction_is_canonical_and_bounded(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )
            payload = {
                "authority_id": "time-authority-1",
                "challenge_digest": challenge.challenge_digest,
                "protocol_id": "challenge-time-v1",
                "protocol_version": "1.0.0",
                "request_nonce": challenge.request_nonce,
                "response_id": "response-1",
                "schema_version": "1.0.0",
                "utc_lower_bound": "2026-10-01T04:00:00Z",
                "utc_upper_bound": "2026-10-01T04:00:00.25Z",
            }
            parse_challenge_bound_measurement(
                _canonical_bytes(payload),
                challenge=challenge,
            )

            for hostile in (
                "2026-10-01T04:00:00.250Z",
                "2026-10-01T04:00:00.1234567Z",
                "2026-10-01T04:00:00z",
                "2026-10-01 04:00:00Z",
            ):
                with self.subTest(hostile=hostile):
                    payload["utc_upper_bound"] = hostile
                    with self.assertRaisesRegex(ValueError, "canonical UTC|fractional"):
                        parse_challenge_bound_measurement(
                            _canonical_bytes(payload),
                            challenge=challenge,
                        )

    def test_measurement_resource_envelope_rejects_before_json_decode(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )

            with patch(
                "mvp.autotrade_mvp.trusted_chronology.json.loads",
                side_effect=AssertionError("decoder must not run"),
            ):
                with self.assertRaisesRegex(ValueError, "byte budget"):
                    parse_challenge_bound_measurement(
                        b"{" + (b" " * 4096) + b"}",
                        challenge=challenge,
                    )
                with self.assertRaisesRegex(ValueError, "flat JSON object"):
                    parse_challenge_bound_measurement(
                        b'{"authority_id":{}}',
                        challenge=challenge,
                    )
                with self.assertRaisesRegex(ValueError, "flat JSON object"):
                    parse_challenge_bound_measurement(
                        b'{"authority_id":[]}',
                        challenge=challenge,
                    )

    def test_measurement_lone_surrogate_fails_as_canonical_value_error(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )
            payload = {
                "authority_id": "time-authority-1",
                "challenge_digest": challenge.challenge_digest,
                "protocol_id": "challenge-time-v1",
                "protocol_version": "1.0.0",
                "request_nonce": challenge.request_nonce,
                "response_id": "response-1",
                "schema_version": "1.0.0",
                "utc_lower_bound": "2026-10-01T04:00:00Z",
                "utc_upper_bound": "2026-10-01T04:00:00.25Z",
            }
            hostile = _canonical_bytes(payload).replace(
                b"time-authority-1",
                b"\\ud800",
            )
            with self.assertRaisesRegex(ValueError, "canonically encoded"):
                parse_challenge_bound_measurement(
                    hostile,
                    challenge=challenge,
                )

    def test_challenge_rejects_in_process_owner_scope_rebinding_mid_capture(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            from mvp.autotrade_mvp import trusted_chronology as chronology_module

            original_sequence = chronology_module._current_journal_sequence
            sequence_reads = 0

            def racing_sequence(selected_store):
                nonlocal sequence_reads
                sequence_reads += 1
                value = original_sequence(selected_store)
                if sequence_reads == 2:
                    recovery._owner_scope = "PAPER:other-account"
                return value

            with patch(
                "mvp.autotrade_mvp.trusted_chronology._current_journal_sequence",
                side_effect=racing_sequence,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "owner scope changed during chronology frontier capture",
                ):
                    prepare_chronology_challenge(
                        store=store,
                        recovery=recovery,
                        source_sha=SOURCE_SHA,
                        scope=ChronologyScope.SOURCE_QUALIFICATION,
                    )

    def test_current_challenge_rejects_owner_scope_rebinding_during_final_read(self):
        with TemporaryDirectory() as directory:
            store, recovery = self._durable_controller(Path(directory))
            challenge = prepare_chronology_challenge(
                store=store,
                recovery=recovery,
                source_sha=SOURCE_SHA,
                scope=ChronologyScope.SOURCE_QUALIFICATION,
            )
            from mvp.autotrade_mvp import trusted_chronology as chronology_module

            original_sequence = chronology_module._current_journal_sequence

            def racing_sequence(selected_store):
                value = original_sequence(selected_store)
                recovery._owner_scope = "PAPER:other-account"
                return value

            with patch(
                "mvp.autotrade_mvp.trusted_chronology._current_journal_sequence",
                side_effect=racing_sequence,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "recovery owner generation changed",
                ):
                    require_current_chronology_challenge(
                        challenge=challenge,
                        store=store,
                        recovery=recovery,
                    )

    def test_challenge_requires_nonempty_canonical_owner_scope_suffix(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            for owner_scope in ("PAPER", "PAPER:", "PAPER: account"):
                with self.subTest(owner_scope=owner_scope):
                    recovery = RecoveryController(
                        owner_store=store,
                        owner_scope=owner_scope,
                    )
                    if owner_scope == "PAPER:":
                        # Recovery now rejects an empty account before a trusted
                        # chronology challenge can even be attempted.
                        with self.assertRaisesRegex(ValueError, "account_id is required"):
                            recovery.start("owner-a")
                    else:
                        recovery.start("owner-a")
                        with self.assertRaisesRegex(
                            PermissionError,
                            "environment-scoped recovery owner",
                        ):
                            prepare_chronology_challenge(
                                store=store,
                                recovery=recovery,
                                source_sha=SOURCE_SHA,
                                scope=ChronologyScope.SOURCE_QUALIFICATION,
                            )

    def test_challenge_rejects_shadowed_second_store_on_same_physical_journal(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            owner_store, recovery = self._durable_controller(path)
            selected_store = JournalStore(path / "journal.sqlite3")
            self.assertEqual(
                owner_store.store_identity,
                selected_store.store_identity,
            )

            original_sequence = selected_store.current_journal_sequence
            selected_store.current_journal_sequence = original_sequence
            try:
                with self.assertRaisesRegex(TypeError, "instance state is shadowed"):
                    prepare_chronology_challenge(
                        store=selected_store,
                        recovery=recovery,
                        source_sha=SOURCE_SHA,
                        scope=ChronologyScope.SOURCE_QUALIFICATION,
                    )
            finally:
                del selected_store.current_journal_sequence


if __name__ == "__main__":
    unittest.main()
