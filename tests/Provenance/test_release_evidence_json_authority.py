from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.build_provenance_manifest import release_evidence_snapshot


SOURCE_SHA = "a" * 40
OTHER_SOURCE_SHA = "b" * 40
DIGEST_A = "sha256:" + "1" * 64
DIGEST_B = "sha256:" + "2" * 64
OBSERVED_AT = "2026-10-03T23:00:00Z"


def valid_evidence_json(*, extra: str = "") -> bytes:
    suffix = f",{extra}" if extra else ""
    return (
        "{"
        '"qualified":true,'
        '"schema_version":"1.0.0",'
        f'"source_sha":"{SOURCE_SHA}",'
        '"evidence_refs":['
        "{"
        '"artifact_id":"release-evidence",'
        f'"sha256":"{DIGEST_A}",'
        f'"observed_at":"{OBSERVED_AT}"'
        "}"
        "]"
        f"{suffix}"
        "}"
    ).encode("utf-8")


class ReleaseEvidenceJsonAuthorityTests(unittest.TestCase):
    def write_raw(self, raw: bytes) -> Path:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "evidence.json"
        path.write_bytes(raw)
        return path

    def assert_invalid_json(self, raw: bytes) -> None:
        qualified, reason, snapshot = release_evidence_snapshot(
            self.write_raw(raw),
            label="release evidence",
        )
        self.assertFalse(qualified)
        self.assertEqual(reason, "invalid_json")
        self.assertIsNone(snapshot)

    def test_valid_document_still_returns_the_exact_parsed_snapshot(self):
        raw = valid_evidence_json(extra='"metadata":{"nested":true}')
        qualified, reason, snapshot = release_evidence_snapshot(
            self.write_raw(raw),
            label="release evidence",
        )
        self.assertTrue(qualified)
        self.assertIsNone(reason)
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["source_sha"], SOURCE_SHA)
        self.assertEqual(snapshot["metadata"], {"nested": True})

    def test_duplicate_top_level_authority_key_fails_closed(self):
        raw = (
            "{"
            '"qualified":true,'
            '"schema_version":"1.0.0",'
            f'"source_sha":"{SOURCE_SHA}",'
            f'"source_sha":"{OTHER_SOURCE_SHA}",'
            '"evidence_refs":['
            "{"
            '"artifact_id":"release-evidence",'
            f'"sha256":"{DIGEST_A}",'
            f'"observed_at":"{OBSERVED_AT}"'
            "}"
            "]"
            "}"
        ).encode("utf-8")
        self.assert_invalid_json(raw)

    def test_duplicate_nested_evidence_digest_fails_closed(self):
        raw = (
            "{"
            '"qualified":true,'
            '"schema_version":"1.0.0",'
            f'"source_sha":"{SOURCE_SHA}",'
            '"evidence_refs":['
            "{"
            '"artifact_id":"release-evidence",'
            f'"sha256":"{DIGEST_A}",'
            f'"sha256":"{DIGEST_B}",'
            f'"observed_at":"{OBSERVED_AT}"'
            "}"
            "]"
            "}"
        ).encode("utf-8")
        self.assert_invalid_json(raw)

    def test_duplicate_nested_unconsumed_metadata_key_fails_closed(self):
        raw = valid_evidence_json(extra='"metadata":{"authority":"first","authority":"second"}')
        self.assert_invalid_json(raw)

    def test_non_finite_json_constants_fail_closed_even_when_unconsumed(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                self.assert_invalid_json(
                    valid_evidence_json(extra=f'"metadata":{{"number":{token}}}')
                )


if __name__ == "__main__":
    unittest.main()
