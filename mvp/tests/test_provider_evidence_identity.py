import unittest

from mvp.autotrade_mvp.durable_order_projection import _canonical_evidence_refs


ARTIFACT_A = "11111111-1111-4111-8111-111111111111"
ARTIFACT_B = "22222222-2222-4222-8222-222222222222"
DIGEST = "sha256:" + "a" * 64
OBSERVED_AT = "2026-10-04T00:00:00Z"


def evidence_ref(artifact_id: str, source_uri: str) -> dict[str, str]:
    return {
        "artifact_id": artifact_id,
        "sha256": DIGEST,
        "source_uri": source_uri,
        "observed_at": OBSERVED_AT,
    }


class ProviderEvidenceIdentityTests(unittest.TestCase):
    def test_same_artifact_cannot_be_counted_twice_via_different_qualifiers(self):
        with self.assertRaisesRegex(ValueError, "unique artifact_id"):
            _canonical_evidence_refs(
                (
                    evidence_ref(ARTIFACT_A, "https://provider.example/evidence/a"),
                    evidence_ref(ARTIFACT_A, "https://provider.example/evidence/b"),
                )
            )

    def test_distinct_artifacts_remain_distinct_evidence(self):
        refs = _canonical_evidence_refs(
            (
                evidence_ref(ARTIFACT_A, "https://provider.example/evidence/a"),
                evidence_ref(ARTIFACT_B, "https://provider.example/evidence/b"),
            )
        )
        self.assertEqual(
            [ref["artifact_id"] for ref in refs],
            [ARTIFACT_A, ARTIFACT_B],
        )


if __name__ == "__main__":
    unittest.main()
