from __future__ import annotations

from hashlib import sha256
import unittest

from autotrade_mvp.runtime_target_host_qualification import (
    CAMPAIGN_EVIDENCE_KIND,
    RuntimeTargetHostProvenance,
    RuntimeTargetHostQualificationError,
    _read_bound_payload,
)


SOURCE_SHA = "a" * 40
PAYLOAD_ID = "00000000-0000-4000-8000-000000000001"
RELEASE_ID = "00000000-0000-4000-8000-000000000002"
RAW = b"independent-target-host-measurement"
RAW_SHA256 = "sha256:" + sha256(RAW).hexdigest()
DIGEST = "sha256:" + ("b" * 64)


def _provenance(
    *,
    payload_artifact_id: str = PAYLOAD_ID,
    payload_sha256: str = RAW_SHA256,
) -> RuntimeTargetHostProvenance:
    return RuntimeTargetHostProvenance(
        evidence_kind=CAMPAIGN_EVIDENCE_KIND,
        source_sha=SOURCE_SHA,
        scenario_id="pressure-campaign",
        spec_digest=DIGEST,
        configuration_hash=DIGEST,
        host_fingerprint=DIGEST,
        workload_profile_hash=DIGEST,
        journal_store_identity_digest=DIGEST,
        release_artifact_id=RELEASE_ID,
        release_artifact_sha256=DIGEST,
        collector_id="campaign-collector",
        collector_version="1.0.0",
        payload_artifact_id=payload_artifact_id,
        payload_sha256=payload_sha256,
    )


class RuntimeTargetHostPayloadIndependenceTests(unittest.TestCase):
    def test_forbidden_artifact_identity_fails_before_read(self) -> None:
        reads: list[str] = []

        def reader(artifact_id: str) -> tuple[dict[str, object], bytes]:
            reads.append(artifact_id)
            return {}, RAW

        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "artifact is not independent",
        ):
            _read_bound_payload(
                reader,
                _provenance(),
                forbidden_artifact_ids={PAYLOAD_ID},
                forbidden_sha256=set(),
            )

        self.assertEqual(reads, [])

    def test_forbidden_digest_fails_before_read(self) -> None:
        reads: list[str] = []

        def reader(artifact_id: str) -> tuple[dict[str, object], bytes]:
            reads.append(artifact_id)
            return {}, RAW

        with self.assertRaisesRegex(
            RuntimeTargetHostQualificationError,
            "bytes are not independent",
        ):
            _read_bound_payload(
                reader,
                _provenance(),
                forbidden_artifact_ids=set(),
                forbidden_sha256={RAW_SHA256},
            )

        self.assertEqual(reads, [])

    def test_independent_authenticated_bytes_are_accepted(self) -> None:
        reads: list[str] = []

        def reader(artifact_id: str) -> tuple[dict[str, object], bytes]:
            reads.append(artifact_id)
            return {"artifact_id": artifact_id}, RAW

        result = _read_bound_payload(
            reader,
            _provenance(),
            forbidden_artifact_ids={RELEASE_ID},
            forbidden_sha256={DIGEST},
        )

        self.assertEqual(result, RAW)
        self.assertEqual(reads, [PAYLOAD_ID])


if __name__ == "__main__":
    unittest.main()
