import unittest
from decimal import Decimal

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderProjection,
    OrderProjectionConflict,
)


class OrderProjectionTests(unittest.TestCase):
    def test_fill_before_ack_is_economic_truth(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="2")
        self.assertTrue(order.record_fill(fill_id="f1", quantity="1", price="10"))
        self.assertEqual(order.state, "PARTIALLY_FILLED")
        order.acknowledge(provider_order_id="p1")
        self.assertEqual(order.state, "PARTIALLY_FILLED")
        self.assertEqual(order.filled_quantity, Decimal("1"))

    def test_acknowledgement_never_invents_fill(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="2")
        order.acknowledge(provider_order_id="p1")
        self.assertEqual(order.state, "WORKING")
        self.assertEqual(order.filled_quantity, Decimal("0"))

    def test_partial_fill_then_cancel_keeps_executed_quantity(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="5")
        order.record_fill(fill_id="f1", quantity="2", price="11")
        order.cancel()
        snap = order.snapshot()
        self.assertEqual(snap.state, "PARTIALLY_FILLED_CANCELLED")
        self.assertEqual(snap.filled_quantity, Decimal("2"))
        self.assertEqual(snap.open_quantity, Decimal("3"))

    def test_duplicate_fill_is_idempotent_but_conflict_fails(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="2")
        self.assertTrue(order.record_fill(fill_id="f1", quantity="1", price="10"))
        self.assertFalse(order.record_fill(fill_id="f1", quantity="1", price="10"))
        with self.assertRaises(OrderProjectionConflict):
            order.record_fill(fill_id="f1", quantity="1.5", price="10")

    def test_overfill_is_exposed_instead_of_hidden_as_normal_fill(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="1")
        order.record_fill(fill_id="f1", quantity="1.2", price="10")
        snap = order.snapshot()
        self.assertEqual(snap.state, "OVERFILLED")
        self.assertEqual(snap.filled_quantity, Decimal("1.2"))
        self.assertEqual(snap.open_quantity, Decimal("0"))

    def test_late_bust_after_terminal_fill_reopens_quantity(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="1")
        order.record_fill(fill_id="f1", quantity="1", price="10")
        self.assertEqual(order.state, "FILLED")
        self.assertTrue(order.bust_fill("f1"))
        self.assertEqual(order.filled_quantity, Decimal("0"))
        self.assertEqual(order.open_quantity, Decimal("1"))
        self.assertEqual(order.state, "PENDING")

    def test_late_correction_changes_economics_without_new_fill_identity(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="2")
        order.record_fill(fill_id="f1", quantity="1", price="10")
        order.correct_fill(fill_id="f1", quantity="1.5", price="12")
        self.assertEqual(order.filled_quantity, Decimal("1.5"))
        self.assertEqual(order.average_fill_price, Decimal("12"))
        self.assertEqual(order.snapshot().fill_count, 1)

    def test_reject_cannot_erase_late_provider_fill(self):
        order = OrderProjection(client_order_id="c1", requested_quantity="1")
        order.acknowledge(provider_order_id="p1", status="REJECTED")
        self.assertEqual(order.state, "REJECTED")
        order.record_fill(fill_id="f1", quantity="1", price="10")
        self.assertEqual(order.state, "FILLED_AFTER_REJECT")
        self.assertEqual(order.filled_quantity, Decimal("1"))

    def test_two_oco_fills_are_preserved_and_flagged(self):
        first = OrderProjection(client_order_id="a", requested_quantity="1", oco_group_id="g")
        second = OrderProjection(client_order_id="b", requested_quantity="1", oco_group_id="g")
        group = OcoGroupProjection("g")
        group.add(first)
        group.add(second)
        first.record_fill(fill_id="fa", quantity="1", price="10")
        second.record_fill(fill_id="fb", quantity="1", price="11")
        self.assertTrue(group.refresh())
        self.assertEqual(first.state, "OCO_VIOLATION")
        self.assertEqual(second.state, "OCO_VIOLATION")
        self.assertEqual(first.filled_quantity, Decimal("1"))
        self.assertEqual(second.filled_quantity, Decimal("1"))


if __name__ == "__main__":
    unittest.main()
