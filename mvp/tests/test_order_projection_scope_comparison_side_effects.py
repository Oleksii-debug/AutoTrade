import unittest

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderBookProjection,
    OrderProjection,
    OrderProjectionConflict,
)


class HostileEquality:
    def __init__(self, calls):
        self.calls = calls

    def __eq__(self, other):
        self.calls.append(other)
        raise AssertionError("hostile equality executed inside aggregate seal")

    def __ne__(self, other):
        self.calls.append(other)
        raise AssertionError("hostile inequality executed inside aggregate seal")


class AggregateScopeComparisonSideEffectTests(unittest.TestCase):
    @staticmethod
    def _oco_order():
        return OrderProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="order-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
            oco_group_id="oco-1",
        )

    def test_book_scope_type_fails_closed_before_hostile_equality(self):
        calls = []
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        object.__setattr__(book, "provider_id", HostileEquality(calls))

        with self.assertRaises(OrderProjectionConflict):
            book.snapshots()
        self.assertEqual(calls, [])

    def test_oco_group_scope_type_fails_closed_before_hostile_equality(self):
        calls = []
        group = OcoGroupProjection("oco-1")
        object.__setattr__(group, "group_id", HostileEquality(calls))

        with self.assertRaises(OrderProjectionConflict):
            group.refresh()
        self.assertEqual(calls, [])

    def test_oco_nested_scope_type_fails_closed_before_hostile_equality(self):
        calls = []
        group = OcoGroupProjection("oco-1")
        group.add(self._oco_order())
        object.__setattr__(
            group,
            "_scope",
            (HostileEquality(calls), "account-1", "SIMULATION"),
        )

        with self.assertRaises(OrderProjectionConflict):
            group.refresh()
        self.assertEqual(calls, [])

    def test_registration_identity_shape_fails_closed_before_hostile_equality(self):
        calls = []
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        book.create(
            client_order_id="order-1",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        object.__setattr__(
            book,
            "_registered_identities",
            {"order-1": HostileEquality(calls)},
        )

        with self.assertRaises(OrderProjectionConflict):
            book.snapshots()
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
