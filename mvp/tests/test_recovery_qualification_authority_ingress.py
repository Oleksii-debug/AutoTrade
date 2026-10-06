import unittest

import mvp.autotrade_mvp.recovery_qualification as recovery_module
from mvp.autotrade_mvp.qualification_attestation import (
    SignedQualificationAttestation,
)
from mvp.autotrade_mvp.recovery_qualification import (
    RecoveryQualificationPolicy,
    qualify_recovery_release,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    root as attestation_root,
    sign,
)
from mvp.tests.test_recovery_qualification import (
    ARTIFACT_SHA,
    EVIDENCE_SCHEMA,
    PROTOCOL_ID,
    RELEASE_ARTIFACT_ID,
    REQUIRED_TESTS,
    SOURCE_SHA,
    complete_evidence,
    evidence,
    policy,
)


class RecoveryQualificationAuthorityIngressTests(unittest.TestCase):
    def test_text_subclass_is_rejected_before_normalization_dispatch(self):
        touched: list[str] = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                touched.append("strip")
                raise AssertionError("hostile recovery text normalization")

            def lower(self, *args, **kwargs):
                touched.append("lower")
                raise AssertionError("hostile recovery text case normalization")

        with self.assertRaisesRegex(ValueError, "canonical lowercase"):
            policy(source_sha=HostileText(SOURCE_SHA))
        self.assertEqual(touched, [])

    def test_integer_subclass_is_rejected_before_numeric_dispatch(self):
        touched: list[str] = []

        class HostileInt(int):
            def __lt__(self, other):
                touched.append("lt")
                raise AssertionError("hostile recovery integer comparison")

        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            evidence(
                recovery_module.RecoveryScenario.POWER_LOSS,
                downtime_ms=HostileInt(1),
            )
        self.assertEqual(touched, [])

    def test_mapping_subclass_is_rejected_before_items_dispatch(self):
        touched: list[str] = []

        class HostileMapping(dict):
            def items(self):
                touched.append("items")
                raise AssertionError("hostile recovery mapping iteration")

        with self.assertRaisesRegex(TypeError, "max_downtime_ms must be an exact dict"):
            RecoveryQualificationPolicy(
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                max_downtime_ms=HostileMapping(
                    {
                        scenario: 60_000
                        for scenario in recovery_module.RecoveryScenario
                    }
                ),
                required_tests=dict(REQUIRED_TESTS),
            )
        self.assertEqual(touched, [])

    def test_tuple_subclass_is_rejected_before_iteration_dispatch(self):
        touched: list[str] = []

        class HostileTuple(tuple):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile recovery tuple iteration")

        with self.assertRaisesRegex(TypeError, "must be an exact tuple"):
            evidence(
                recovery_module.RecoveryScenario.NETWORK_LOSS,
                tests_run=HostileTuple(
                    REQUIRED_TESTS[recovery_module.RecoveryScenario.NETWORK_LOSS]
                ),
            )
        self.assertEqual(touched, [])

    def test_sequence_subclass_is_rejected_before_iteration_dispatch(self):
        touched: list[str] = []

        class HostileList(list):
            def __iter__(self):
                touched.append("iter")
                raise AssertionError("hostile recovery evidence iteration")

        values = HostileList(complete_evidence())
        with self.assertRaisesRegex(TypeError, "evidence must be an exact list or tuple"):
            qualify_recovery_release(
                policy=policy(),
                evidence=values,
            )
        self.assertEqual(touched, [])

    def test_policy_subclass_is_rejected_before_attribute_dispatch(self):
        touched: list[str] = []

        class DerivedPolicy(RecoveryQualificationPolicy):
            def __getattribute__(self, name):
                if name == "source_sha":
                    touched.append("source_sha")
                return super().__getattribute__(name)

        exact = policy()
        derived = DerivedPolicy(
            source_sha=exact.source_sha,
            release_artifact_id=exact.release_artifact_id,
            release_artifact_sha256=exact.release_artifact_sha256,
            evidence_schema_version=exact.evidence_schema_version,
            protocol_id=exact.protocol_id,
            max_downtime_ms=dict(exact.max_downtime_ms),
            required_tests=dict(exact.required_tests),
        )
        touched.clear()
        with self.assertRaisesRegex(
            TypeError,
            "policy must be RecoveryQualificationPolicy",
        ):
            qualify_recovery_release(
                policy=derived,
                evidence=[],
            )
        self.assertEqual(touched, [])

    def test_receipt_subclass_is_rejected_before_trust_dispatch(self):
        trust_root = attestation_root()
        signed = attestation(trust_root)

        class DerivedReceipt(SignedQualificationAttestation):
            pass

        receipt = DerivedReceipt(signed, sign(signed))
        with self.assertRaisesRegex(
            TypeError,
            "qualification_receipt must be SignedQualificationAttestation",
        ):
            qualify_recovery_release(
                policy=policy(),
                evidence=[],
                qualification_receipt=receipt,
            )

    def test_snapshot_mapping_subclass_is_rejected_before_get_dispatch(self):
        touched: list[str] = []

        class HostileManifest(dict):
            def get(self, *args, **kwargs):
                touched.append("get")
                raise AssertionError("hostile recovery manifest lookup")

        matched = recovery_module._store_artifact_matches(
            lambda _artifact_id: (HostileManifest(), b"payload"),
            artifact_id=RELEASE_ARTIFACT_ID,
            artifact_sha256=ARTIFACT_SHA,
            media_type="application/vnd.autotrade.release-artifact",
            source_sha=SOURCE_SHA,
            metadata={},
        )
        self.assertFalse(matched)
        self.assertEqual(touched, [])

    def test_canonical_list_and_dict_inputs_remain_supported(self):
        current_policy = policy()
        decision = qualify_recovery_release(
            policy=current_policy,
            evidence=complete_evidence(),
        )
        self.assertFalse(decision.authorizes_trading)
        self.assertEqual(
            set(decision.measured_downtime_ms),
            set(recovery_module.RecoveryScenario),
        )


if __name__ == "__main__":
    unittest.main()
