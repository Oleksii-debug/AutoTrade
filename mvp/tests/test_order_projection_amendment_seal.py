import unittest

from mvp.autotrade_mvp.order_projection import (
    OrderBookProjection,
    OrderProjection,
    OrderProjectionConflict,
)


class OrderProjectionAmendmentSealTests(unittest.TestCase):
    @staticmethod
    def _order(*, client_order_id: str, parent_intent_id: str | None = None):
        return OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id=client_order_id,
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
            parent_intent_id=parent_intent_id,
        )

    def test_amendment_index_mutation_cannot_authorize_second_child(self):
        """The one-child amendment invariant must survive caller-held table mutation."""

        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        parent = self._order(client_order_id="parent")
        first_child = self._order(
            client_order_id="child-1",
            parent_intent_id="parent",
        )
        second_child = self._order(
            client_order_id="child-2",
            parent_intent_id="parent",
        )

        book.register(parent)
        book.register(first_child)
        self.assertEqual(book.amendment_child("parent"), "child-1")

        # `_amend_children` remains a caller-held derived index, but its exact
        # contents must match the retained registration cut.
        book._amend_children.clear()

        with self.assertRaises(OrderProjectionConflict):
            book.amendment_child("parent")
        with self.assertRaises(OrderProjectionConflict):
            book.register(second_child)

    def test_amendment_index_retarget_cannot_hide_registered_child(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        parent = self._order(client_order_id="parent")
        first_child = self._order(
            client_order_id="child-1",
            parent_intent_id="parent",
        )
        unrelated = self._order(client_order_id="unrelated")

        book.register(parent)
        book.register(first_child)
        book.register(unrelated)
        book._amend_children["parent"] = "unrelated"

        with self.assertRaises(OrderProjectionConflict):
            book.amendment_child("parent")

    def test_amendment_index_extra_entry_is_rejected(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        parent = self._order(client_order_id="parent")
        book.register(parent)
        book._amend_children["forged-parent"] = "parent"

        with self.assertRaises(OrderProjectionConflict):
            book.order("parent")

    def test_amendment_index_requires_exact_builtin_dict(self):
        class DictSubclass(dict):
            pass

        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        parent = self._order(client_order_id="parent")
        book.register(parent)
        book._amend_children = DictSubclass(book._amend_children)

        with self.assertRaises(OrderProjectionConflict):
            book.amendment_child("parent")


if __name__ == "__main__":
    unittest.main()
