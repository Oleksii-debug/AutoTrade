import unittest

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderBookProjection,
    OrderProjection,
)


class OrderProjectionTrustBoundaryTests(unittest.TestCase):
    def order(self, **overrides):
        values = dict(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
            client_order_id="order-1",
            instrument="A@1",
            side="BUY",
            requested_quantity="2",
            oco_group_id="oco-1",
        )
        values.update(overrides)
        return OrderProjection(**values)

    def test_hostile_text_subclass_is_rejected_before_virtual_string_dispatch(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

            def upper(self):
                calls.append("upper")
                raise AssertionError("hostile upper executed")

        with self.assertRaisesRegex(TypeError, "provider_id must be exact text"):
            self.order(provider_id=HostileText("SIMULATED"))
        self.assertEqual(calls, [])

    def test_common_text_boundary_protects_provider_observation_methods(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

        order = self.order()
        with self.assertRaisesRegex(TypeError, "fill_id must be exact text"):
            order.record_fill(
                fill_id=HostileText("fill-1"),
                provider_execution_id="execution-1",
                quantity="1",
                price="100",
            )
        self.assertEqual(calls, [])
        self.assertEqual(order.active_fills, ())
        self.assertEqual(order.fill_history, ())

    def test_book_rejects_order_subclass_before_reading_authoritative_fields(self):
        calls = []

        class DerivedOrder(OrderProjection):
            def __getattribute__(self, name):
                if name in {
                    "provider_id",
                    "account_id",
                    "environment",
                    "client_order_id",
                    "parent_intent_id",
                }:
                    calls.append(name)
                    raise AssertionError("derived order field executed")
                return super().__getattribute__(name)

        forged = object.__new__(DerivedOrder)
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        with self.assertRaisesRegex(TypeError, "order must be exact OrderProjection"):
            book.register(forged)
        self.assertEqual(calls, [])

    def test_oco_group_rejects_order_subclass_before_reading_authoritative_fields(self):
        calls = []

        class DerivedOrder(OrderProjection):
            def __getattribute__(self, name):
                if name in {
                    "oco_group_id",
                    "provider_id",
                    "account_id",
                    "environment",
                    "client_order_id",
                }:
                    calls.append(name)
                    raise AssertionError("derived order field executed")
                return super().__getattribute__(name)

        forged = object.__new__(DerivedOrder)
        group = OcoGroupProjection("oco-1")
        with self.assertRaisesRegex(TypeError, "order must be exact OrderProjection"):
            group.add(forged)
        self.assertEqual(calls, [])

    def test_snapshot_rejects_bool_like_object_before_bool_dispatch(self):
        calls = []

        class BoolLike:
            def __bool__(self):
                calls.append("bool")
                raise AssertionError("hostile bool executed")

        order = self.order()
        with self.assertRaisesRegex(TypeError, "oco_violation must be exact bool"):
            order.snapshot(oco_violation=BoolLike())
        self.assertEqual(calls, [])

    def test_exact_bool_snapshot_override_and_normal_book_flow_remain_supported(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        order = book.create(
            client_order_id="order-1",
            instrument="A@1",
            side="BUY",
            requested_quantity="2",
            oco_group_id="oco-1",
        )
        self.assertFalse(order.snapshot(oco_violation=False).oco_violation)
        self.assertTrue(order.snapshot(oco_violation=True).oco_violation)
        self.assertIs(book.order("order-1"), order)

    def test_exact_order_object_still_registers_idempotently(self):
        book = OrderBookProjection(
            provider_id="SIMULATED",
            account_id="account-1",
            environment="SIMULATION",
        )
        order = self.order()
        self.assertTrue(book.register(order))
        self.assertFalse(book.register(order))


if __name__ == "__main__":
    unittest.main()
