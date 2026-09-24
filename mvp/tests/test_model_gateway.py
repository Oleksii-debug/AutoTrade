from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.model_gateway import (
    ModelDescriptor,
    ModelRequest,
    RouteStatus,
    RoutingMode,
    RoutingPolicy,
    route_model,
)


NOW = datetime(2026, 9, 24, 16, 30, tzinfo=timezone.utc)


def model(model_id, *, remote, cost, latency=50, quality="0.5"):
    return ModelDescriptor(
        model_id=model_id,
        provider_id="provider-" + model_id,
        revision="r1",
        remote=remote,
        estimated_cost=Decimal(cost),
        latency_ms=latency,
        quality_score=Decimal(quality),
    )


def request(*ids, budget="10", remote=True, deadline=None):
    return ModelRequest(
        request_id="req-1",
        allowed_model_ids=tuple(ids),
        privacy_remote_allowed=remote,
        budget_remaining=Decimal(budget),
        deadline_utc=deadline or NOW + timedelta(minutes=1),
    )


class ModelGatewayTests(unittest.TestCase):
    def test_zero_mode_never_selects_model(self):
        decision = route_model(
            RoutingPolicy(RoutingMode.ZERO, maximum_cost=Decimal("100")),
            request("remote"),
            [model("remote", remote=True, cost="1")],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)
        self.assertEqual(Decimal("0"), decision.reserved_cost)

    def test_local_only_never_falls_back_to_remote(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.LOCAL_ONLY,
                allowed_model_ids=("remote",),
                allow_remote=True,
                maximum_cost=Decimal("10"),
            ),
            request("remote"),
            [model("remote", remote=True, cost="1")],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)

    def test_privacy_denies_remote_even_when_policy_allows_it(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("remote",),
                allow_remote=True,
                maximum_cost=Decimal("10"),
            ),
            request("remote", remote=False),
            [model("remote", remote=True, cost="1")],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)

    def test_budget_is_hard_bound(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("remote",),
                allow_remote=True,
                maximum_cost=Decimal("5"),
            ),
            request("remote", budget="0.99"),
            [model("remote", remote=True, cost="1")],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)

    def test_fixed_mode_selects_only_fixed_model(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.FIXED,
                fixed_model_id="local-b",
                allowed_model_ids=("local-a", "local-b"),
                maximum_cost=Decimal("10"),
            ),
            request("local-a", "local-b"),
            [
                model("local-a", remote=False, cost="0", quality="1"),
                model("local-b", remote=False, cost="0", quality="0.1"),
            ],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.ADMITTED, decision.status)
        self.assertEqual("local-b", decision.model_id)

    def test_dynamic_route_is_deterministic_and_quality_first(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.DYNAMIC,
                allowed_model_ids=("b", "a"),
                maximum_cost=Decimal("10"),
            ),
            request("a", "b"),
            [
                model("a", remote=False, cost="0.2", quality="0.7"),
                model("b", remote=False, cost="0.1", quality="0.8"),
            ],
            now_utc=NOW,
        )
        self.assertEqual("b", decision.model_id)
        self.assertEqual(Decimal("0.1"), decision.reserved_cost)

    def test_latency_ceiling_filters_model(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("slow",),
                maximum_cost=Decimal("10"),
                maximum_latency_ms=100,
            ),
            request("slow"),
            [model("slow", remote=False, cost="0", latency=101)],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)

    def test_expired_deadline_rejects_before_routing(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("local",),
                maximum_cost=Decimal("10"),
            ),
            request("local", deadline=NOW),
            [model("local", remote=False, cost="0")],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.REJECTED, decision.status)
        self.assertEqual("deadline_expired", decision.reason)

    def test_duplicate_descriptor_is_rejected(self):
        with self.assertRaises(ValueError):
            route_model(
                RoutingPolicy(
                    RoutingMode.ALLOWLIST,
                    allowed_model_ids=("x",),
                    maximum_cost=Decimal("10"),
                ),
                request("x"),
                [
                    model("x", remote=False, cost="0"),
                    model("x", remote=False, cost="0"),
                ],
                now_utc=NOW,
            )


if __name__ == "__main__":
    unittest.main()
