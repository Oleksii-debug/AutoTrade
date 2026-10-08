import unittest

from mvp.autotrade_mvp.lifecycle_cost import (
    LifecycleCostError,
    LifecycleCostRequirements,
)


class LifecycleCostCompletenessTests(unittest.TestCase):
    def test_empty_required_kind_set_cannot_claim_complete_cost_model(self):
        with self.assertRaisesRegex(
            LifecycleCostError,
            "at least one required cost kind must be declared",
        ):
            LifecycleCostRequirements(requirements_ref="requirements:test:v1", required_kinds=())


if __name__ == "__main__":
    unittest.main()
