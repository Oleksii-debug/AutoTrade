from decimal import Decimal
import unittest

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderBookProjection,
    OrderProjection,
    OrderProjectionConflict,
)


class AggregateOrderSealTests(unittest.TestCase):
    @staticmethod
    def _order(*, client_order_id="order-1", provider_id="SIMULATED", oco_group_id="oco-1"):
        return OrderProjection(
            provider_id=provider_id,
            account_id="account-1",
            environment="SIMULATION",
            client_order_id=client_order_id,
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
            oco_group_id=oco_group_id,
        )

    def test_book_scope_mutation_cannot_authorize_mixed_provider_registration(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        book.register(self._order())
        object.__setattr__(book, "provider_id", "OTHER")

        candidate = self._order(client_order_id="order-2", provider_id="OTHER")
        with self.assertRaises(OrderProjectionConflict):
            book.register(candidate)
        with self.assertRaises(OrderProjectionConflict):
            book.snapshots()

    def test_book_registration_seal_table_replacement_cannot_launder_order_mutation(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        order = self._order()
        book.register(order)
        object.__setattr__(order, "instrument", "XYZ")

        forged_identity = (
            "SIMULATED",
            "account-1",
            "SIMULATION",
            "order-1",
            "XYZ",
            "BUY",
            Decimal("2"),
            "oco-1",
            None,
        )
        object.__setattr__(book, "_registered_identities", {"order-1": forged_identity})

        with self.assertRaises(OrderProjectionConflict):
            book.order("order-1")
        with self.assertRaises(OrderProjectionConflict):
            book.effective_fills()

    def test_oco_group_id_mutation_cannot_authorize_different_group_peer(self):
        group = OcoGroupProjection("oco-1")
        group.add(self._order())
        object.__setattr__(group, "group_id", "oco-2")

        peer = self._order(client_order_id="order-2", oco_group_id="oco-2")
        with self.assertRaises(OrderProjectionConflict):
            group.add(peer)
        with self.assertRaises(OrderProjectionConflict):
            group.refresh()

    def test_oco_scope_mutation_cannot_authorize_cross_provider_peer(self):
        group = OcoGroupProjection("oco-1")
        group.add(self._order())
        object.__setattr__(group, "_scope", ("OTHER", "account-1", "SIMULATION"))

        peer = self._order(client_order_id="order-2", provider_id="OTHER")
        with self.assertRaises(OrderProjectionConflict):
            group.add(peer)
        with self.assertRaises(OrderProjectionConflict):
            group.refresh()

    def test_oco_registration_seal_replacement_cannot_launder_order_mutation(self):
        group = OcoGroupProjection("oco-1")
        order = self._order()
        group.add(order)
        object.__setattr__(order, "side", "SELL")

        forged_identity = (
            "SIMULATED",
            "account-1",
            "SIMULATION",
            "order-1",
            "ABC",
            "SELL",
            Decimal("2"),
            "oco-1",
            None,
        )
        object.__setattr__(group, "_registered_identities", {"order-1": forged_identity})

        with self.assertRaises(OrderProjectionConflict):
            group.refresh()


if __name__ == "__main__":
    unittest.main()
