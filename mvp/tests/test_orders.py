from decimal import Decimal
import unittest

from mvp.autotrade_mvp.orders import OrderProjection, OrderProjectionConflict


class OrderProjectionTests(unittest.TestCase):
    def test_fill_before_ack_is_economic_truth_and_ack_adds_no_fill(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="10")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="3", price="100",
        )
        before = book.snapshot("i1")
        self.assertEqual(before.operational_state, "PARTIALLY_FILLED")
        self.assertEqual(before.filled_quantity, Decimal("3"))
        self.assertFalse(before.acknowledged)

        book.acknowledge("i1", provider_order_id="provider-order-1")
        after = book.snapshot("i1")
        self.assertTrue(after.acknowledged)
        self.assertEqual(after.filled_quantity, Decimal("3"))
        self.assertEqual(len(book.effective_fills()), 1)

    def test_partial_fill_cancel_and_late_fill_are_preserved(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="10")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="3", price="100",
        )
        book.request_cancel("i1")
        book.confirm_cancel("i1")
        canceled = book.snapshot("i1")
        self.assertEqual(canceled.operational_state, "CANCELED_WITH_LATE_FILL")
        self.assertEqual(canceled.remaining_quantity, Decimal("7"))

        book.observe_fill(
            fill_id="f2", provider_execution_id="exec-2", intent_id="i1",
            side="BUY", quantity="2", price="101",
        )
        late = book.snapshot("i1")
        self.assertEqual(late.filled_quantity, Decimal("5"))
        self.assertEqual(late.remaining_quantity, Decimal("5"))
        self.assertEqual(late.operational_state, "CANCELED_WITH_LATE_FILL")

    def test_duplicate_fill_is_idempotent_and_changed_duplicate_conflicts(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="2")
        kwargs = dict(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="1", price="100",
        )
        self.assertTrue(book.observe_fill(**kwargs))
        self.assertFalse(book.observe_fill(**kwargs))
        with self.assertRaises(OrderProjectionConflict):
            book.observe_fill(**{**kwargs, "price": "101"})
        self.assertEqual(book.filled_quantity("i1"), Decimal("1"))

    def test_same_provider_execution_cannot_create_second_economic_fill(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="2")
        book.observe_fill(
            fill_id="local-f1", provider_execution_id="provider-exec", intent_id="i1",
            side="BUY", quantity="1", price="100",
        )
        self.assertFalse(book.observe_fill(
            fill_id="local-f2", provider_execution_id="provider-exec", intent_id="i1",
            side="BUY", quantity="1", price="100",
        ))
        self.assertEqual(len(book.effective_fills()), 1)

    def test_late_correction_changes_effective_economics_without_deleting_audit_fill(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="2")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="2", price="100",
        )
        self.assertEqual(book.snapshot("i1").operational_state, "FILLED")
        self.assertTrue(book.correct_fill(
            "f1",
            correction_fill_id="f1-r2",
            provider_execution_id="exec-1",
            quantity="2",
            price="101",
            provider_revision="2",
        ))
        effective = book.effective_fills()
        self.assertEqual(len(effective), 1)
        self.assertEqual(effective[0].price, Decimal("101"))
        self.assertEqual(effective[0].correction_of, "f1")
        self.assertFalse(book.correct_fill(
            "f1",
            correction_fill_id="f1-r2",
            provider_execution_id="exec-1",
            quantity="2",
            price="101",
            provider_revision="2",
        ))

    def test_two_oco_fills_are_both_kept_and_surface_breach(self):
        book = OrderProjection()
        book.register_intent(intent_id="take-profit", side="SELL", quantity="1", oco_group="g1")
        book.register_intent(intent_id="stop", side="SELL", quantity="1", oco_group="g1")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="take-profit",
            side="SELL", quantity="1", price="110",
        )
        book.observe_fill(
            fill_id="f2", provider_execution_id="exec-2", intent_id="stop",
            side="SELL", quantity="1", price="90",
        )
        self.assertEqual(len(book.effective_fills()), 2)
        self.assertEqual(
            book.oco_breaches()["g1"],
            ("stop", "take-profit"),
        )

    def test_amendment_lineage_is_single_child_and_does_not_erase_parent(self):
        book = OrderProjection()
        book.register_intent(intent_id="old", side="BUY", quantity="10")
        book.register_intent(
            intent_id="new", side="BUY", quantity="8", parent_intent_id="old"
        )
        self.assertEqual(book.amendment_child("old"), "new")
        with self.assertRaises(OrderProjectionConflict):
            book.register_intent(
                intent_id="other", side="BUY", quantity="7", parent_intent_id="old"
            )
        self.assertEqual(book.snapshot("old").ordered_quantity, Decimal("10"))

    def test_rejection_cannot_erase_an_observed_fill(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="1")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="1", price="100",
        )
        with self.assertRaises(OrderProjectionConflict):
            book.reject("i1")
        self.assertEqual(book.filled_quantity("i1"), Decimal("1"))

    def test_binary_float_fill_is_rejected(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="1")
        with self.assertRaises(TypeError):
            book.observe_fill(
                fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
                side="BUY", quantity=1.0, price="100",
            )


    def test_provider_overfill_is_explicit_not_silently_clamped(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="1")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="1.25", price="100",
        )
        state = book.snapshot("i1")
        self.assertEqual(state.remaining_quantity, Decimal("0"))
        self.assertEqual(state.overfill_quantity, Decimal("0.25"))
        self.assertEqual(state.operational_state, "OVERFILLED")

    def test_correction_cannot_switch_provider_execution_identity(self):
        book = OrderProjection()
        book.register_intent(intent_id="i1", side="BUY", quantity="1")
        book.observe_fill(
            fill_id="f1", provider_execution_id="exec-1", intent_id="i1",
            side="BUY", quantity="1", price="100",
        )
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "provider_execution_id",
        ):
            book.correct_fill(
                "f1",
                correction_fill_id="f1-r2",
                provider_execution_id="exec-other",
                quantity="1",
                price="101",
                provider_revision="2",
            )



if __name__ == "__main__":
    unittest.main()
