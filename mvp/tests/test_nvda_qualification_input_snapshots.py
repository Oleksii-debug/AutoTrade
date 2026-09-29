from hashlib import sha256
from io import BytesIO
import json
import unittest
import zipfile

from tools.check_nvda_qualification import (
    _load_evidence_once,
    validate_release_artifact_binding,
)


class _FlippingPath:
    """Path-like test double whose bytes change after the first read."""

    def __init__(self, first: bytes, second: bytes):
        self._payloads = (first, second)
        self.read_count = 0

    def is_file(self):
        return True

    def read_bytes(self):
        index = min(self.read_count, len(self._payloads) - 1)
        self.read_count += 1
        return self._payloads[index]


def _release_bundle_bytes(*, source_sha: str = "a" * 40) -> bytes:
    manifest = {
        "schema_version": "1.0.0",
        "product": "AutoTrade",
        "version": "test",
        "source_sha": source_sha,
        "mode": "release",
        "release_eligible": True,
        "trading_authority_granted_by_artifact": False,
        "provenance_sha256": "sha256:" + "c" * 64,
        "provenance_blockers": [],
        "files": [],
    }
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "bundle-manifest.json",
            json.dumps(manifest, sort_keys=True),
        )
    return output.getvalue()


class NvdaQualificationInputSnapshotTests(unittest.TestCase):
    def test_raw_evidence_parse_and_digest_share_one_read_snapshot(self):
        first = json.dumps(
            {
                "schema_version": "1.0.0",
                "source_sha": "a" * 40,
            },
            sort_keys=True,
        ).encode("utf-8")
        second = json.dumps(
            {
                "schema_version": "1.0.0",
                "source_sha": "b" * 40,
            },
            sort_keys=True,
        ).encode("utf-8")
        path = _FlippingPath(first, second)

        parsed, digest = _load_evidence_once(path)

        self.assertEqual(path.read_count, 1)
        self.assertEqual(parsed["source_sha"], "a" * 40)
        self.assertEqual(digest, "sha256:" + sha256(first).hexdigest())

    def test_release_digest_and_manifest_share_one_read_snapshot(self):
        first = _release_bundle_bytes(source_sha="a" * 40)
        second = _release_bundle_bytes(source_sha="b" * 40)
        path = _FlippingPath(first, second)
        evidence = {
            "source_sha": "a" * 40,
            "artifact_sha256": "sha256:" + sha256(first).hexdigest(),
        }

        actual = validate_release_artifact_binding(evidence, path)

        self.assertEqual(path.read_count, 1)
        self.assertEqual(actual, evidence["artifact_sha256"])

    def test_release_substitution_cannot_pair_first_digest_with_second_manifest(self):
        first = _release_bundle_bytes(source_sha="a" * 40)
        second = _release_bundle_bytes(source_sha="b" * 40)
        path = _FlippingPath(first, second)
        evidence = {
            "source_sha": "a" * 40,
            "artifact_sha256": "sha256:" + sha256(first).hexdigest(),
        }

        validate_release_artifact_binding(evidence, path)

        self.assertEqual(path.read_count, 1)


if __name__ == "__main__":
    unittest.main()
