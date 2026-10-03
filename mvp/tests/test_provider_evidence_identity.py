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


    def test_evidence_container_subclasses_are_rejected_without_virtual_dispatch(self):
        touched: list[str] = []

        class HostileList(list):
            def __iter__(self):
                touched.append("list-iter")
                raise AssertionError("hostile evidence sequence must not be iterated")

            def copy(self):
                touched.append("list-copy")
                raise AssertionError("hostile evidence sequence copy must not run")

        with self.assertRaisesRegex(TypeError, "exact list or tuple"):
            _canonical_evidence_refs(
                HostileList(
                    [evidence_ref(ARTIFACT_A, "https://provider.example/evidence/a")]
                )
            )
        self.assertEqual(touched, [])

    def test_evidence_mapping_subclass_is_rejected_without_field_reads(self):
        touched: list[str] = []

        class HostileDict(dict):
            def __iter__(self):
                touched.append("dict-iter")
                raise AssertionError("hostile evidence mapping must not be iterated")

            def get(self, *args, **kwargs):
                touched.append("dict-get")
                raise AssertionError("hostile evidence mapping get must not run")

            def copy(self):
                touched.append("dict-copy")
                raise AssertionError("hostile evidence mapping copy must not run")

        hostile = HostileDict(
            evidence_ref(ARTIFACT_A, "https://provider.example/evidence/a")
        )
        with self.assertRaisesRegex(TypeError, "exact dict"):
            _canonical_evidence_refs((hostile,))
        self.assertEqual(touched, [])

    def test_exact_list_and_dict_snapshot_remain_supported(self):
        refs = _canonical_evidence_refs(
            [evidence_ref(ARTIFACT_A, "https://provider.example/evidence/a")]
        )
        self.assertEqual(refs[0]["artifact_id"], ARTIFACT_A)


if __name__ == "__main__":
    unittest.main()
