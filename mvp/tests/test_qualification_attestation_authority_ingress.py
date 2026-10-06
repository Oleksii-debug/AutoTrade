from tempfile import TemporaryDirectory
import unittest

from research.autotrade_research.artifacts import ArtifactStore
from mvp.autotrade_mvp.qualification_attestation import (
    QualificationAttestation,
    QualificationScope,
    QualificationTrustError,
    QualificationTrustPolicy,
    SignedQualificationAttestation,
    TrustRoot,
    verify_qualification_attestation,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    policy,
    publish,
    root,
    sign,
    verify,
)


class QualificationAttestationAuthorityIngressTests(unittest.TestCase):
    def test_trust_text_subclass_is_rejected_before_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile trust text normalization")

            def upper(self, *args, **kwargs):
                touched.append("upper")
                raise AssertionError("hostile trust text case normalization")

        with self.assertRaises(QualificationTrustError):
            QualificationScope(HostileText("RELEASE"), "FREEZE")
        self.assertEqual(touched, [])

    def test_schema_version_subclass_is_rejected_before_comparison_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile schema text normalization")

            def __eq__(self, other):
                touched.append("eq")
                raise AssertionError("hostile schema comparison")

        trust_root = root()
        with self.assertRaises(QualificationTrustError):
            attestation(trust_root, schema_version=HostileText("1.0.0"))
        self.assertEqual(touched, [])

    def test_public_exponent_subclass_is_rejected_before_numeric_dispatch(self):
        touched: list[str] = []

        class HostileInt(int):
            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile exponent comparison")

            def __mod__(self, other):
                touched.append("mod")
                raise AssertionError("hostile exponent modulo")

        baseline = root()
        with self.assertRaisesRegex(QualificationTrustError, "public_exponent"):
            TrustRoot(
                producer_id=baseline.producer_id,
                verifier_id=baseline.verifier_id,
                public_modulus_hex=baseline.public_modulus_hex,
                public_exponent=HostileInt(65537),
                allowed_scopes=baseline.allowed_scopes,
                valid_from=baseline.valid_from,
            )
        self.assertEqual(touched, [])

    def test_policy_subclass_is_rejected_before_property_dispatch(self):
        touched: list[str] = []

        class DerivedPolicy(QualificationTrustPolicy):
            @property
            def policy_id(self):
                touched.append("policy_id")
                raise AssertionError("derived policy identity dispatched")

        trust_root = root()
        exact_policy = policy(trust_root)
        derived_policy = DerivedPolicy(
            policy_version=exact_policy.policy_version,
            roots=exact_policy.roots,
        )
        signed = attestation(trust_root)
        receipt = SignedQualificationAttestation(signed, sign(signed))
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                TypeError,
                "policy must be QualificationTrustPolicy",
            ):
                verify_qualification_attestation(
                    receipt,
                    policy=derived_policy,
                    evidence_store=store,
                    evidence_root=store.root,
                    expected_policy_id=exact_policy.policy_id,
                    expected_policy_version=exact_policy.policy_version,
                    expected_source_sha=signed.source_sha,
                    expected_domain=signed.domain,
                    expected_gate=signed.gate,
                    expected_package_id=signed.package_id,
                    expected_protocol_id=signed.protocol_id,
                    expected_protocol_version=signed.protocol_version,
                    expected_requirement_id="release-candidate-freeze",
                    expected_release_artifact_id=signed.release_artifact_id,
                    expected_release_artifact_sha256=signed.release_artifact_sha256,
                )
        self.assertEqual(touched, [])

    def test_receipt_subclass_is_rejected_before_attestation_dispatch(self):
        touched: list[str] = []

        class DerivedReceipt(SignedQualificationAttestation):
            def __getattribute__(self, name):
                if name == "attestation":
                    touched.append("attestation")
                return super().__getattribute__(name)

        trust_root = root()
        trust_policy = policy(trust_root)
        signed = attestation(trust_root)
        receipt = DerivedReceipt(signed, sign(signed))
        touched.clear()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish(store)
            with self.assertRaisesRegex(
                TypeError,
                "receipt must be SignedQualificationAttestation",
            ):
                verify(receipt, store, trust_policy)
        self.assertEqual(touched, [])

    def test_signed_receipt_rejects_attestation_subclass(self):
        trust_root = root()
        exact = attestation(trust_root)

        class DerivedAttestation(QualificationAttestation):
            pass

        derived = DerivedAttestation(**exact.__dict__)
        with self.assertRaisesRegex(
            TypeError,
            "attestation must be QualificationAttestation",
        ):
            SignedQualificationAttestation(derived, sign(exact))


if __name__ == "__main__":
    unittest.main()
