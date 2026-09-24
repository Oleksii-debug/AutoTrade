from decimal import Decimal
import unittest

from mvp.autotrade_mvp.model_routing import (
    ModelRoute,
    RouteRequest,
    RoutingPolicy,
    select_route,
)


class ModelRoutingTests(unittest.TestCase):
    def setUp(self):
        self.local = ModelRoute(
            route_id="local-a",
            provider="local",
            model="small",
            locality="local",
            allowed_data_classes=frozenset({"public", "private"}),
        )
        self.remote = ModelRoute(
            route_id="remote-a",
            provider="remote",
            model="large",
            locality="remote",
            input_cost_per_million=Decimal("2"),
            output_cost_per_million=Decimal("8"),
            allowed_data_classes=frozenset({"public"}),
        )

    def test_zero_mode_never_selects_model(self):
        decision = select_route(
            [self.local, self.remote],
            RoutingPolicy(mode="zero"),
            RouteRequest("public", 1000, 1000),
        )
        self.assertFalse(decision.execute)
        self.assertIsNone(decision.route_id)
        self.assertEqual(decision.estimated_cost, Decimal("0"))

    def test_local_mode_never_uses_remote(self):
        decision = select_route(
            [self.remote, self.local],
            RoutingPolicy(mode="local", max_request_cost=Decimal("1")),
            RouteRequest("public", 1000, 1000),
        )
        self.assertTrue(decision.execute)
        self.assertEqual(decision.route_id, "local-a")

    def test_private_data_does_not_leak_to_remote(self):
        decision = select_route(
            [self.remote],
            RoutingPolicy(mode="dynamic", max_request_cost=Decimal("10"), allow_remote=True),
            RouteRequest("private", 1000, 1000),
        )
        self.assertFalse(decision.execute)
        self.assertIsNone(decision.route_id)

    def test_budget_is_fail_closed(self):
        decision = select_route(
            [self.remote],
            RoutingPolicy(mode="dynamic", max_request_cost=Decimal("0.001"), allow_remote=True),
            RouteRequest("public", 1000, 1000),
        )
        self.assertFalse(decision.execute)

    def test_dynamic_selects_lowest_cost_then_stable_id(self):
        remote_b = ModelRoute(
            route_id="remote-b",
            provider="remote",
            model="other",
            locality="remote",
            input_cost_per_million=Decimal("1"),
            output_cost_per_million=Decimal("1"),
            allowed_data_classes=frozenset({"public"}),
        )
        decision = select_route(
            [self.remote, remote_b],
            RoutingPolicy(mode="dynamic", max_request_cost=Decimal("1"), allow_remote=True),
            RouteRequest("public", 1000, 1000),
        )
        self.assertEqual(decision.route_id, "remote-b")
        self.assertEqual(decision.estimated_cost, Decimal("0.002"))

    def test_allowlist_rejects_unlisted_route(self):
        decision = select_route(
            [self.local],
            RoutingPolicy(
                mode="allowlist",
                max_request_cost=Decimal("1"),
                allowed_route_ids=frozenset({"other"}),
            ),
            RouteRequest("public", 100, 100),
        )
        self.assertFalse(decision.execute)

    def test_fixed_mode_requires_exact_route(self):
        decision = select_route(
            [self.local, self.remote],
            RoutingPolicy(
                mode="fixed",
                fixed_route_id="remote-a",
                max_request_cost=Decimal("1"),
                allow_remote=True,
            ),
            RouteRequest("public", 1000, 1000),
        )
        self.assertTrue(decision.execute)
        self.assertEqual(decision.route_id, "remote-a")

    def test_negative_token_estimate_is_rejected(self):
        with self.assertRaises(ValueError):
            RouteRequest("public", -1, 0)


if __name__ == "__main__":
    unittest.main()
