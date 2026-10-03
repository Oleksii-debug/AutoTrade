from __future__ import annotations

from dataclasses import replace
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import trusted_chronology as chronology_module
from mvp.autotrade_mvp.trusted_chronology import (
    ChronologyChallenge,
    ChronologyScope,
    parse_challenge_bound_measurement,
)


class TrustedChronologyChallengeSnapshotTests(unittest.TestCase):
    @staticmethod
    def _challenge() -> ChronologyChallenge:
        challenge = ChronologyChallenge(
            schema_version="1.1.0",
            scope=ChronologyScope.SOURCE_QUALIFICATION,
            source_sha="a" * 40,
            store_identity_digest="sha256:" + ("b" * 64),
            owner_scope="PAPER:account-1",
            owner_id="owner-a",
            owner_epoch=1,
            clock_incident_generation=0,
            journal_sequence=7,
            runtime_host_id="host-a",
            runtime_account_id="account-1",
            runtime_occurrence_id="11111111-1111-1111-1111-111111111111",
            runtime_environment="PAPER",
            release_artifact_id=None,
            release_artifact_sha256=None,
            request_nonce="c" * 64,
            challenge_digest="",
        )
        return replace(
            challenge,
            challenge_digest=chronology_module._challenge_digest(challenge),
        )

    def test_validation_returns_private_snapshot_of_caller_owned_challenge(self):
        challenge = self._challenge()
        validated = chronology_module._validate_challenge(challenge)

        self.assertIsNot(validated, challenge)
        object.__setattr__(challenge, "owner_scope", "LIVE:attacker")
        object.__setattr__(challenge, "source_sha", "d" * 40)

        self.assertEqual(validated.owner_scope, "PAPER:account-1")
        self.assertEqual(validated.source_sha, "a" * 40)
        self.assertEqual(
            validated.challenge_digest,
            chronology_module._challenge_digest(validated),
        )

    def test_measurement_parse_ignores_post_validation_caller_mutation(self):
        challenge = self._challenge()
        original_digest = challenge.challenge_digest
        original_nonce = challenge.request_nonce
        payload = {
            "authority_id": "time-authority-1",
            "challenge_digest": original_digest,
            "protocol_id": "challenge-time-v1",
            "protocol_version": "1.0.0",
            "request_nonce": original_nonce,
            "response_id": "response-1",
            "schema_version": "1.1.0",
            "utc_lower_bound": "2026-10-03T12:00:00Z",
            "utc_upper_bound": "2026-10-03T12:00:00.25Z",
        }

        def mutate_caller_object_after_validation(_data: bytes):
            object.__setattr__(challenge, "request_nonce", "f" * 64)
            object.__setattr__(challenge, "source_sha", "d" * 40)
            return payload

        with patch.object(
            chronology_module,
            "_strict_json_object",
            side_effect=mutate_caller_object_after_validation,
        ):
            transcript = parse_challenge_bound_measurement(
                b"caller-mutation-falsifier",
                challenge=challenge,
            )

        self.assertEqual(challenge.request_nonce, "f" * 64)
        self.assertEqual(transcript.request_nonce, original_nonce)
        self.assertEqual(transcript.challenge_digest, original_digest)


if __name__ == "__main__":
    unittest.main()
