from decimal import Decimal
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.model_cost_store import ModelCostStore


class ModelCostStoreTests(unittest.TestCase):
    def test_restart_preserves_budget_and_exact_model_identity(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/costs.sqlite3"
            first = ModelCostStore(path, ceiling=Decimal("10"))
            self.assertTrue(
                first.reserve(
                    request_id="req-1",
                    model_id="m1",
                    provider_id="p1",
                    revision="r7",
                    amount=Decimal("1.25"),
                )
            )
            reopened = ModelCostStore(path, ceiling=Decimal("10"))
            record = reopened.get("req-1")
            self.assertEqual(record.model_id, "m1")
            self.assertEqual(record.provider_id, "p1")
            self.assertEqual(record.revision, "r7")
            self.assertEqual(reopened.snapshot()["reserved"], Decimal("1.25"))

    def test_request_identity_is_idempotent_and_conflicts_fail_closed(self):
        with TemporaryDirectory() as directory:
            store = ModelCostStore(f"{directory}/costs.sqlite3", ceiling=Decimal("2"))
            self.assertTrue(
                store.reserve(
                    request_id="req",
                    model_id="m",
                    provider_id="p",
                    revision=None,
                    amount=Decimal("1"),
                )
            )
            self.assertFalse(
                store.reserve(
                    request_id="req",
                    model_id="m",
                    provider_id="p",
                    revision=None,
                    amount=Decimal("1"),
                )
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.reserve(
                    request_id="req",
                    model_id="other",
                    provider_id="p",
                    revision=None,
                    amount=Decimal("1"),
                )

    def test_budget_is_hard_across_restart(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/costs.sqlite3"
            store = ModelCostStore(path, ceiling=Decimal("1"))
            store.reserve(
                request_id="a", model_id="m", provider_id="p", revision=None, amount=Decimal("0.7")
            )
            reopened = ModelCostStore(path, ceiling=Decimal("1"))
            with self.assertRaisesRegex(ValueError, "budget exhausted"):
                reopened.reserve(
                    request_id="b", model_id="m", provider_id="p", revision=None, amount=Decimal("0.31")
                )

    def test_settlement_is_exact_and_replay_safe(self):
        with TemporaryDirectory() as directory:
            store = ModelCostStore(f"{directory}/costs.sqlite3", ceiling=Decimal("5"))
            store.reserve(
                request_id="req", model_id="m", provider_id="p", revision="x", amount=Decimal("2")
            )
            self.assertTrue(
                store.settle(
                    "req", incurred=Decimal("0.8"), estimated_unbilled=Decimal("0.4")
                )
            )
            self.assertFalse(
                store.settle(
                    "req", incurred=Decimal("0.8"), estimated_unbilled=Decimal("0.4")
                )
            )
            with self.assertRaisesRegex(ValueError, "conflicts"):
                store.settle(
                    "req", incurred=Decimal("0.9"), estimated_unbilled=Decimal("0.4")
                )
            snap = store.snapshot()
            self.assertEqual(snap["incurred"], Decimal("0.8"))
            self.assertEqual(snap["estimated_unbilled"], Decimal("0.4"))

    def test_settlement_cannot_exceed_reserved_ceiling(self):
        with TemporaryDirectory() as directory:
            store = ModelCostStore(f"{directory}/costs.sqlite3", ceiling=Decimal("5"))
            store.reserve(
                request_id="req", model_id="m", provider_id="p", revision=None, amount=Decimal("1")
            )
            with self.assertRaisesRegex(ValueError, "exceeds reserved"):
                store.settle("req", incurred=Decimal("1.01"))

    def test_billing_reconciliation_moves_unbilled_to_incurred(self):
        with TemporaryDirectory() as directory:
            store = ModelCostStore(f"{directory}/costs.sqlite3", ceiling=Decimal("5"))
            store.reserve(
                request_id="req", model_id="m", provider_id="p", revision=None, amount=Decimal("2")
            )
            store.settle("req", incurred=Decimal("0.5"), estimated_unbilled=Decimal("0.7"))
            store.reconcile_unbilled("req", billed=Decimal("0.3"))
            record = store.get("req")
            self.assertEqual(record.incurred_cost, Decimal("0.8"))
            self.assertEqual(record.estimated_unbilled, Decimal("0.4"))

    def test_conflicting_ceiling_on_reopen_fails_closed(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/costs.sqlite3"
            ModelCostStore(path, ceiling=Decimal("5"))
            with self.assertRaisesRegex(ValueError, "ceiling conflicts"):
                ModelCostStore(path, ceiling=Decimal("6"))


if __name__ == "__main__":
    unittest.main()
