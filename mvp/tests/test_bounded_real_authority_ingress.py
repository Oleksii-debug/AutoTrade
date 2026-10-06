from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore
from mvp.autotrade_mvp.bounded_real import (
    ArtifactStoreEvidenceVerifier,
    EvidenceVerification,
    assess_bounded_real_qualification,
    artifact_store_evidence_verifier,
)
from mvp.tests.test_bounded_real import envelope, observations, prerequisites


class BoundedRealAuthorityIngressTests(unittest.TestCase):
    def test_verifier_subclass_is_not_trusted_or_dispatched(self):
        touched: list[str] = []

        class DerivedVerifier(ArtifactStoreEvidenceVerifier):
            @property
            def identity(self):
                touched.append("identity")
                raise AssertionError("derived verifier identity dispatched")

            def verify(self, ref):
                touched.append("verify")
                return EvidenceVerification(valid=True)

            @property
            def store(self):
                touched.append("store")
                raise AssertionError("derived verifier store dispatched")

            @property
            def evidence_root(self):
                touched.append("evidence_root")
                raise AssertionError("derived verifier root dispatched")

        with TemporaryDirectory() as directory:
            verifier = DerivedVerifier(
                ArtifactStore(directory),
                evidence_root=directory,
            )
            result = assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisites(),
                observations=observations(),
                evidence_verifier=verifier,
            )

        self.assertIn(
            "untrusted_immutable_evidence_verifier",
            result.reason_codes,
        )
        self.assertIsNone(result.evidence_verifier_identity)
        self.assertEqual(touched, [])

    def test_canonical_verifier_remains_accepted(self):
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            verifier = artifact_store_evidence_verifier(
                store,
                evidence_root=directory,
            )
            result = assess_bounded_real_qualification(
                envelope=envelope(),
                prerequisite_evidence=prerequisites(),
                observations=observations(),
                evidence_verifier=verifier,
            )
        self.assertNotIn(
            "untrusted_immutable_evidence_verifier",
            result.reason_codes,
        )
        self.assertIsNotNone(result.evidence_verifier_identity)

    def test_text_subclass_is_rejected_before_strip_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile bounded-real text normalization")

        with self.assertRaisesRegex(ValueError, "envelope_id is required"):
            envelope(envelope_id=HostileText("bounded-1"))
        self.assertEqual(touched, [])

    def test_decimal_subclass_is_rejected_before_numeric_dispatch(self):
        touched: list[str] = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                touched.append("is_finite")
                raise AssertionError("hostile decimal finiteness dispatch")

            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile decimal comparison dispatch")

        value = HostileDecimal("1000")
        with self.assertRaisesRegex(
            TypeError,
            "max_capital must use Decimal, string or integer input",
        ):
            envelope(max_capital=value)
        self.assertEqual(touched, [])

    def test_integer_subclass_is_rejected_before_observation_dispatch(self):
        touched: list[str] = []

        class HostileInt(int):
            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile observation integer comparison")

        with self.assertRaisesRegex(
            ValueError,
            "observed_fill_count must be a non-negative integer",
        ):
            observations(observed_fill_count=HostileInt(3))
        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
