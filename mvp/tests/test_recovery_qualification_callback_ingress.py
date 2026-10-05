from types import MappingProxyType
import unittest

from mvp.autotrade_mvp.recovery_qualification import (
    RecoveryQualificationPolicy,
    RecoveryScenario,
    qualify_recovery_release,
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


class RecoveryQualificationCallbackIngressTests(unittest.TestCase):
    def test_polymorphic_inputs_fail_before_caller_callbacks(self):
        calls = []

        class HostileStr(str):
            def strip(self):
                calls.append("strip")
                raise AssertionError("caller string callback must not execute")

            def lower(self):
                calls.append("lower")
                raise AssertionError("caller string callback must not execute")

        class HostileTuple(tuple):
            def __iter__(self):
                calls.append("tuple-iter")
                raise AssertionError("caller tuple iterator must not execute")

        class HostileDict(dict):
            def items(self):
                calls.append("dict-items")
                raise AssertionError("caller mapping callback must not execute")

        class HostileList(list):
            def __iter__(self):
                calls.append("list-iter")
                raise AssertionError("caller evidence iterator must not execute")

        with self.assertRaisesRegex(ValueError, "canonical lowercase"):
            policy(source_sha=HostileStr(SOURCE_SHA))
        with self.assertRaisesRegex(ValueError, "canonical sha256"):
            policy(artifact=HostileStr(ARTIFACT_SHA))
        with self.assertRaisesRegex(ValueError, "protocol_id"):
            policy(protocol_id=HostileStr(PROTOCOL_ID))

        with self.assertRaisesRegex(TypeError, "tuple"):
            evidence(
                RecoveryScenario.NETWORK_LOSS,
                tests_run=HostileTuple(
                    REQUIRED_TESTS[RecoveryScenario.NETWORK_LOSS]
                ),
            )

        limits = HostileDict(
            {scenario: 60_000 for scenario in RecoveryScenario}
        )
        with self.assertRaisesRegex(TypeError, "mapping"):
            RecoveryQualificationPolicy(
                source_sha=SOURCE_SHA,
                release_artifact_id=RELEASE_ARTIFACT_ID,
                release_artifact_sha256=ARTIFACT_SHA,
                evidence_schema_version=EVIDENCE_SCHEMA,
                protocol_id=PROTOCOL_ID,
                max_downtime_ms=limits,
                required_tests=dict(REQUIRED_TESTS),
            )

        with self.assertRaisesRegex(TypeError, "sequence"):
            qualify_recovery_release(
                policy=policy(),
                evidence=HostileList(complete_evidence()),
            )
        self.assertEqual(calls, [])

    def test_canonical_immutable_mappings_remain_admitted(self):
        limits = MappingProxyType(
            {scenario: 60_000 for scenario in RecoveryScenario}
        )
        required = MappingProxyType(dict(REQUIRED_TESTS))
        value = RecoveryQualificationPolicy(
            source_sha=SOURCE_SHA,
            release_artifact_id=RELEASE_ARTIFACT_ID,
            release_artifact_sha256=ARTIFACT_SHA,
            evidence_schema_version=EVIDENCE_SCHEMA,
            protocol_id=PROTOCOL_ID,
            max_downtime_ms=limits,
            required_tests=required,
        )
        self.assertEqual(
            set(value.max_downtime_ms),
            set(RecoveryScenario),
        )
        self.assertEqual(
            value.required_tests[RecoveryScenario.POWER_LOSS],
            REQUIRED_TESTS[RecoveryScenario.POWER_LOSS],
        )


if __name__ == "__main__":
    unittest.main()
