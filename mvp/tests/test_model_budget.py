from decimal import Decimal
import unittest

from mvp.autotrade_mvp.model_budget import ModelBudget


class ModelBudgetTests(unittest.TestCase):
    def test_reservation_is_idempotent(self):
        budget = ModelBudget("10")
        self.assertTrue(budget.reserve("req-1", "3"))
        self.assertFalse(budget.reserve("req-1", "3"))
        self.assertEqual(budget.snapshot().reserved, Decimal("3"))
        self.assertEqual(budget.snapshot().available, Decimal("7"))

    def test_conflicting_reservation_fails(self):
        budget = ModelBudget("10")
        budget.reserve("req-1", "3")
        with self.assertRaisesRegex(ValueError, "different amount"):
            budget.reserve("req-1", "4")

    def test_ceiling_is_hard_bound(self):
        budget = ModelBudget("5")
        budget.reserve("req-1", "4")
        with self.assertRaisesRegex(ValueError, "ceiling"):
            budget.reserve("req-2", "2")

    def test_settlement_separates_incurred_and_unbilled(self):
        budget = ModelBudget("10")
        budget.reserve("req-1", "5")
        state = budget.settle("req-1", incurred="2.5", estimated_unbilled="1")
        self.assertEqual(state.reserved, Decimal("0"))
        self.assertEqual(state.incurred, Decimal("2.5"))
        self.assertEqual(state.estimated_unbilled, Decimal("1"))
        self.assertEqual(state.available, Decimal("6.5"))

    def test_settlement_cannot_exceed_reservation(self):
        budget = ModelBudget("10")
        budget.reserve("req-1", "2")
        with self.assertRaisesRegex(ValueError, "exceeds reserved"):
            budget.settle("req-1", incurred="2", estimated_unbilled="0.1")

    def test_cancel_releases_only_unused_reservation(self):
        budget = ModelBudget("4")
        budget.reserve("req-1", "3")
        self.assertEqual(budget.cancel("req-1"), Decimal("3"))
        self.assertEqual(budget.cancel("req-1"), Decimal("0"))
        self.assertEqual(budget.snapshot().available, Decimal("4"))

    def test_confirmed_billing_resolves_estimate_without_double_counting(self):
        budget = ModelBudget("10")
        budget.reserve("req-1", "5")
        budget.settle("req-1", incurred="2", estimated_unbilled="2")
        state = budget.confirm_billed("1.5", from_estimated_unbilled="2")
        self.assertEqual(state.incurred, Decimal("3.5"))
        self.assertEqual(state.estimated_unbilled, Decimal("0"))
        self.assertEqual(state.committed, Decimal("3.5"))

    def test_invalid_values_are_rejected(self):
        with self.assertRaises(ValueError):
            ModelBudget("-1")
        budget = ModelBudget("5")
        with self.assertRaises(ValueError):
            budget.reserve("", "1")
        with self.assertRaises(ValueError):
            budget.reserve("req", "NaN")


if __name__ == "__main__":
    unittest.main()
