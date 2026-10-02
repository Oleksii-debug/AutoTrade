"""Windows-update regressions under an explicit test-only canonical trust seam.

The full case set lives in ``_windows_update_cases`` so the durable WP-64 fixture
can be reused without duplicating a large test module.  This discovered wrapper
keeps the canonical verifier patched only for each test method; production code
continues to reject caller-selected trust.
"""

from unittest.mock import patch

import mvp.autotrade_mvp.release_candidate as release_candidate_module
import mvp.autotrade_mvp.supply_chain_qualification as supply_chain_module
from mvp.autotrade_mvp.qualification_attestation import (
    verify_qualification_attestation,
)
from mvp.tests import _windows_update_cases


class WindowsUpdatePlanTests(_windows_update_cases.WindowsUpdatePlanTests):
    def setUp(self):
        super().setUp()
        policy = self.trust_policy

        def canonical_verify(receipt_arg, **kwargs):
            return verify_qualification_attestation(
                receipt_arg,
                policy=policy,
                expected_policy_id=policy.policy_id,
                expected_policy_version=policy.policy_version,
                **kwargs,
            )

        self._release_verify_patcher = patch.object(
            release_candidate_module,
            "verify_canonical_qualification_attestation",
            side_effect=canonical_verify,
        )
        self._supply_verify_patcher = patch.object(
            supply_chain_module,
            "verify_canonical_qualification_attestation",
            side_effect=canonical_verify,
        )
        self._release_verify_patcher.start()
        self._supply_verify_patcher.start()

    def tearDown(self):
        self._supply_verify_patcher.stop()
        self._release_verify_patcher.stop()
        super().tearDown()


if __name__ == "__main__":
    import unittest

    unittest.main()
