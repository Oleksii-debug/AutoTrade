from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
