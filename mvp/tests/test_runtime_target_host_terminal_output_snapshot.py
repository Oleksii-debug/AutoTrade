from __future__ import annotations

import unittest

from mvp.autotrade_mvp.runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBindingError,
    _snapshot_terminal_qualification,
)
from mvp.tests.test_runtime_target_host_chronology_bound_qualification import (
    RuntimeTargetHostChronologyBoundTests,
)


class RuntimeTargetHostTerminalOutputSnapshotTests(unittest.TestCase):
    @staticmethod
    def _helpers() -> RuntimeTargetHostChronologyBoundTests:
        return RuntimeTargetHostChronologyBoundTests(
            "test_requires_release_runtime_scope_before_terminal_verifier"
        )

    def test_terminal_result_detaches_verifier_owned_acceptance(self):
        helpers = self._helpers()
        chronology = helpers._chronology()
        verifier_owned = helpers._qualification()

        result, _current, _horizon, _plan_verify = helpers._verify(
            chronology,
            qualification=verifier_owned,
        )

        self.assertIsNot(result.qualification, verifier_owned)
        self.assertIsNot(
            result.qualification.qualification,
            verifier_owned.qualification,
        )
        self.assertEqual(
            result.qualification.qualification.source_sha,
            verifier_owned.qualification.source_sha,
        )

        object.__setattr__(verifier_owned.qualification, "source_sha", "f" * 40)
        object.__setattr__(
            verifier_owned,
            "target_host_measurement_digest",
            "sha256:" + "0" * 64,
        )

        self.assertEqual(
            result.qualification.qualification.source_sha,
            "a" * 40,
        )
        self.assertEqual(
            result.qualification.target_host_measurement_digest,
            "sha256:" + "3" * 64,
        )

    def test_snapshot_rejects_mutated_mapping_state(self):
        helpers = self._helpers()
        verifier_owned = helpers._qualification()
        object.__setattr__(
            verifier_owned.qualification,
            "evidence_sha256_by_kind",
            {"RUNTIME_TARGET_HOST_BINDING": "sha256:" + "5" * 64},
        )

        with self.assertRaisesRegex(
            RuntimeTargetHostChronologyBindingError,
            "exact immutable mapping state",
        ):
            _snapshot_terminal_qualification(verifier_owned)


if __name__ == "__main__":
    unittest.main()
