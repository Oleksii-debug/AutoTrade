from decimal import Decimal
import unittest

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderBookProjection,
    OrderProjection,
    OrderProjectionConflict,
)


class RegisteredOrderIdentityTests(unittest.TestCase):
    def _registered(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        order = OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="order-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
            oco_group_id="oco-1",
        )
        self.assertTrue(book.register(order))
        return book, order

    def test_registered_identity_mutation_fails_closed_before_book_read(self):
        mutations = (
            ("provider_id", "OTHER"),
            ("account_id", "account-2"),
            ("environment", "PAPER"),
            ("client_order_id", "order-2"),
            ("instrument", "XYZ"),
            ("side", "SELL"),
            ("requested_quantity", Decimal("3")),
            ("oco_group_id", "oco-2"),
            ("parent_intent_id", "parent-1"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                book, order = self._registered()
                object.__setattr__(order, field, value)

                with self.assertRaises(OrderProjectionConflict):
                    book.order("order-1")
                with self.assertRaises(OrderProjectionConflict):
                    book.snapshots()
                with self.assertRaises(OrderProjectionConflict):
                    book.effective_fills()

    def test_registered_identity_mutation_cannot_rekey_book(self):
        book, order = self._registered()
        object.__setattr__(order, "client_order_id", "order-2")

        with self.assertRaises(OrderProjectionConflict):
            book.order("order-1")
        with self.assertRaises(KeyError):
            book.order("order-2")

    def test_registered_identity_mutation_fails_closed_before_oco_read(self):
        mutations = (
            ("provider_id", "OTHER"),
            ("account_id", "attacker-account"),
            ("environment", "PAPER"),
            ("client_order_id", "order-2"),
            ("instrument", "XYZ"),
            ("side", "SELL"),
            ("requested_quantity", Decimal("3")),
            ("oco_group_id", "oco-2"),
            ("parent_intent_id", "parent-1"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                _book, order = self._registered()
                group = OcoGroupProjection("oco-1")
                group.add(order)
                object.__setattr__(order, field, value)

                with self.assertRaises(OrderProjectionConflict):
                    group.refresh()

    def test_non_finite_quantity_tamper_fails_as_projection_conflict(self):
        book, order = self._registered()
        object.__setattr__(order, "requested_quantity", Decimal("sNaN"))

        with self.assertRaises(OrderProjectionConflict):
            book.order("order-1")
        with self.assertRaises(OrderProjectionConflict):
            book.snapshots()

    def test_mutated_registered_object_cannot_be_registered_again(self):
        book, order = self._registered()
        object.__setattr__(order, "client_order_id", "order-2")

        with self.assertRaises(OrderProjectionConflict):
            book.register(order)

    def test_compromised_book_rejects_new_registration_before_mutation(self):
        book, order = self._registered()
        object.__setattr__(order, "instrument", "XYZ")
        candidate = OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="order-2",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
            oco_group_id="oco-1",
        )

        with self.assertRaises(OrderProjectionConflict):
            book.register(candidate)
        with self.assertRaises(KeyError):
            book.order("order-2")

    def test_compromised_oco_group_rejects_new_peer_before_mutation(self):
        _book, order = self._registered()
        group = OcoGroupProjection("oco-1")
        group.add(order)
        object.__setattr__(order, "side", "SELL")
        peer = OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="order-2",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
            oco_group_id="oco-1",
        )

        with self.assertRaises(OrderProjectionConflict):
            group.add(peer)

    def test_unknown_amendment_parent_preserves_keyerror_contract(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        child = OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="child-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
            parent_intent_id="missing-parent",
        )

        with self.assertRaises(KeyError) as raised:
            book.register(child)
        self.assertEqual(raised.exception.args, ("missing-parent",))

    def test_amendment_lookup_fails_closed_when_registered_child_identity_changes(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        parent = book.create(
            client_order_id="parent-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
            oco_group_id="oco-1",
        )
        child = book.create(
            client_order_id="child-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
            oco_group_id="oco-1",
            parent_intent_id=parent.client_order_id,
        )
        self.assertEqual(book.amendment_child("parent-1"), "child-1")

        object.__setattr__(child, "instrument", "XYZ")

        with self.assertRaises(OrderProjectionConflict):
            book.amendment_child("parent-1")


if __name__ == "__main__":
    unittest.main()
