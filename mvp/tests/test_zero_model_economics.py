from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import uuid4

from autotrade_research.economics.after_cost import (
    CostBreakdown,
    TradeEconomics,
    qualify_after_cost_edge,
)
from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    allocate_targets,
)
from mvp.autotrade_mvp.model_gateway import (
    BudgetLedger,
    ModelDescriptor,
    ModelRequest,
    RouteStatus,
    RoutingMode,
    RoutingPolicy,
    route_model,
)
from mvp.autotrade_mvp.simulated_provider import SimulatedProvider


NOW = datetime(2026, 9, 24, 20, 30, tzinfo=timezone.utc)


def request(*model_ids, budget="10", remote=True):
    return ModelRequest(
        request_id="zero-model-qualification",
        allowed_model_ids=tuple(model_ids),
        privacy_remote_allowed=remote,
        budget_remaining=Decimal(budget),
        deadline_utc=NOW + timedelta(minutes=1),
    )


def candidate(*, desired="100", price="10", lot="1", cost="0.001"):
    return AllocationCandidate.create(
        symbol="AAA",
        desired_notional=desired,
        price=price,
        lot_size=lot,
        cost_rate=cost,
    )


def policy(*, cash="1000", max_cost="20"):
    return AllocationPolicy.create(
        cash_available=cash,
        max_gross_notional="1000",
        max_net_notional="1000",
        max_symbol_notional="1000",
        max_total_cost=max_cost,
        max_stress_loss="500",
    )


class ZeroModelEconomicsQualificationTests(unittest.TestCase):
    def test_zero_policy_never_reserves_or_selects_a_model(self):
        descriptors = (
            ModelDescriptor(
                model_id="remote-expensive",
                provider_id="remote",
                revision="r1",
                remote=True,
                estimated_cost=Decimal("9.99"),
                latency_ms=100,
                quality_score=Decimal("1"),
            ),
        )
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ZERO,
                allowed_model_ids=("remote-expensive",),
                allow_remote=True,
                maximum_cost=Decimal("100"),
            ),
            request("remote-expensive"),
            descriptors,
            now_utc=NOW,
        )
        self.assertEqual(decision.status, RouteStatus.NO_MODEL)
        self.assertEqual(decision.reserved_cost, Decimal("0"))
        self.assertIsNone(decision.model_id)

    def test_remote_outage_degrades_to_no_model_without_exception(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("remote-missing",),
                allow_remote=True,
                maximum_cost=Decimal("10"),
            ),
            request("remote-missing"),
            (),
            now_utc=NOW,
        )
        self.assertEqual(decision.status, RouteStatus.NO_MODEL)
        allocation = allocate_targets([candidate()], policy())
        self.assertEqual(allocation.status, "ALLOCATED")
        self.assertGreater(allocation.gross_notional, Decimal("0"))

    def test_local_resource_exhaustion_can_fall_back_to_cash(self):
        local = ModelDescriptor(
            model_id="local-heavy",
            provider_id="local",
            revision="r1",
            remote=False,
            estimated_cost=Decimal("0"),
            latency_ms=1000,
            quality_score=Decimal("0.9"),
        )
        decision = route_model(
            RoutingPolicy(
                RoutingMode.LOCAL_ONLY,
                allowed_model_ids=("local-heavy",),
                maximum_cost=Decimal("0"),
                maximum_latency_ms=10,
            ),
            request("local-heavy", budget="0", remote=False),
            (local,),
            now_utc=NOW,
        )
        self.assertEqual(decision.status, RouteStatus.NO_MODEL)
        allocation = allocate_targets(
            [candidate(desired="100", price="100", lot="1")],
            policy(cash="50"),
        )
        self.assertEqual(allocation.status, "NO_INCREASE_FALLBACK")
        self.assertEqual(allocation.gross_notional, Decimal("0"))

    def test_zero_model_after_cost_economics_remains_useful(self):
        trade = TradeEconomics(
            capital_at_risk=Decimal("100"),
            expected_gross_pnl=Decimal("5"),
            worst_case_loss=Decimal("10"),
            costs=CostBreakdown(
                commission=Decimal("1"),
                spread=Decimal("0.5"),
                infrastructure=Decimal("0.5"),
                model_compute=Decimal("0"),
            ),
            minimum_practical_advantage=Decimal("1"),
        )
        result = qualify_after_cost_edge(
            trade,
            max_loss_budget=Decimal("20"),
            minimum_order_notional=Decimal("10"),
            proposed_notional=Decimal("100"),
        )
        self.assertEqual(result.disposition, "ADMISSIBLE_FOR_RESEARCH")
        self.assertEqual(result.total_cost, Decimal("2.0"))
        self.assertEqual(result.expected_net_pnl, Decimal("3.0"))
        self.assertFalse(result.live_authority_granted)

    def test_model_cost_can_destroy_edge_but_zero_model_does_not_hide_it(self):
        zero_model_trade = TradeEconomics(
            capital_at_risk=Decimal("100"),
            expected_gross_pnl=Decimal("3"),
            worst_case_loss=Decimal("10"),
            costs=CostBreakdown(
                commission=Decimal("1"),
                infrastructure=Decimal("0.5"),
                model_compute=Decimal("0"),
            ),
            minimum_practical_advantage=Decimal("1"),
        )
        model_trade = TradeEconomics(
            capital_at_risk=Decimal("100"),
            expected_gross_pnl=Decimal("3"),
            worst_case_loss=Decimal("10"),
            costs=CostBreakdown(
                commission=Decimal("1"),
                infrastructure=Decimal("0.5"),
                model_compute=Decimal("2"),
            ),
            minimum_practical_advantage=Decimal("1"),
        )
        zero_result = qualify_after_cost_edge(
            zero_model_trade,
            max_loss_budget=Decimal("20"),
        )
        model_result = qualify_after_cost_edge(
            model_trade,
            max_loss_budget=Decimal("20"),
        )
        self.assertEqual(zero_result.disposition, "ADMISSIBLE_FOR_RESEARCH")
        self.assertEqual(model_result.disposition, "REJECT")
        self.assertIn("NO_AFTER_COST_PRACTICAL_EDGE", model_result.reason_codes)

    def test_small_capital_minimum_notional_is_fail_closed(self):
        trade = TradeEconomics(
            capital_at_risk=Decimal("5"),
            expected_gross_pnl=Decimal("1"),
            worst_case_loss=Decimal("1"),
            costs=CostBreakdown(model_compute=Decimal("0")),
        )
        result = qualify_after_cost_edge(
            trade,
            max_loss_budget=Decimal("5"),
            minimum_order_notional=Decimal("10"),
            proposed_notional=Decimal("5"),
        )
        self.assertEqual(result.disposition, "REJECT")
        self.assertIn("BELOW_MINIMUM_NOTIONAL", result.reason_codes)

    def test_zero_budget_ledger_cannot_create_hidden_paid_fallback(self):
        ledger = BudgetLedger(Decimal("0"))
        snapshot = ledger.snapshot()
        self.assertEqual(snapshot.ceiling, Decimal("0"))
        self.assertEqual(snapshot.available, Decimal("0"))
        with self.assertRaises(ValueError):
            ledger.reserve("hidden-paid-fallback", Decimal("0.01"))

    def test_deterministic_simulated_financial_path_runs_after_no_model_decision(self):
        decision = route_model(
            RoutingPolicy(RoutingMode.ZERO, maximum_cost=Decimal("0")),
            request(budget="0", remote=False),
            (),
            now_utc=NOW,
        )
        self.assertEqual(decision.status, RouteStatus.NO_MODEL)

        provider = SimulatedProvider(initial_cash="1000", fee_rate="0.001")
        now = NOW.isoformat().replace("+00:00", "Z")
        submission = provider.submit_order(
            attempt_id=str(uuid4()),
            client_order_id="zero-model-order-1",
            instrument_version="AAA:v1",
            side="BUY",
            quantity="2",
            price="10",
            now=now,
            fill_immediately=True,
        )
        self.assertEqual(submission["outcome"], "ACKNOWLEDGED")
        fills = provider.activity_fills()
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["last_quantity"]["value"], "2")
        snapshot = provider.account_snapshot(now=now)
        self.assertEqual(snapshot["positions"][0]["quantity"]["value"], "2")
        self.assertEqual(Decimal(snapshot["balances"][0]["total"]), Decimal("979.98"))


if __name__ == "__main__":
    unittest.main()
