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
        estimated_cost=cost,
        latency_ms=latency,
        quality_score=quality,
    )


def request(*ids, budget="10", remote=True, deadline=None):
    return ModelRequest(
        request_id="req-1",
        allowed_model_ids=tuple(ids),
        privacy_remote_allowed=remote,
        budget_remaining=budget,
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

    def test_fixed_mode_cannot_bypass_policy_allowlist(self):
        decision = route_model(
            RoutingPolicy(
                RoutingMode.FIXED,
                fixed_model_id="fixed-but-not-approved",
                allowed_model_ids=("approved-other",),
                maximum_cost=Decimal("10"),
            ),
            request("fixed-but-not-approved", "approved-other"),
            [
                model(
                    "fixed-but-not-approved",
                    remote=False,
                    cost="0",
                    quality="1",
                ),
                model(
                    "approved-other",
                    remote=False,
                    cost="0",
                    quality="0.1",
                ),
            ],
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.NO_MODEL, decision.status)
        self.assertIsNone(decision.model_id)
        self.assertEqual(decision.reason, "no_admissible_model")

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

    def test_binary_float_model_economics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            model("float-cost", remote=False, cost=0.1)
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            ModelDescriptor(
                model_id="float-quality",
                provider_id="provider",
                revision="r1",
                remote=False,
                estimated_cost=Decimal("0"),
                latency_ms=10,
                quality_score=0.5,
            )
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            RoutingPolicy(
                RoutingMode.ZERO,
                maximum_cost=1.0,
            )
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            request("local", budget=1.0)

    def test_boolean_flags_and_latency_aliases_fail_closed(self):
        with self.assertRaises(TypeError):
            ModelDescriptor(
                model_id="bad-remote",
                provider_id="provider",
                revision="r1",
                remote=1,
                estimated_cost=Decimal("0"),
                latency_ms=10,
                quality_score=Decimal("0.5"),
            )
        with self.assertRaises(ValueError):
            ModelDescriptor(
                model_id="bad-latency",
                provider_id="provider",
                revision="r1",
                remote=False,
                estimated_cost=Decimal("0"),
                latency_ms=True,
                quality_score=Decimal("0.5"),
            )
        with self.assertRaises(TypeError):
            RoutingPolicy("ZERO", maximum_cost=Decimal("0"))
        with self.assertRaises(TypeError):
            RoutingPolicy(RoutingMode.ZERO, allow_remote=1, maximum_cost=Decimal("0"))
        with self.assertRaises(TypeError):
            ModelRequest(
                request_id="bad-privacy",
                allowed_model_ids=(),
                privacy_remote_allowed=1,
                budget_remaining=Decimal("0"),
                deadline_utc=NOW + timedelta(minutes=1),
            )

    def test_authority_identifiers_are_canonical_and_allowlists_are_exact(self):
        descriptor = ModelDescriptor(
            model_id=" local ",
            provider_id=" provider ",
            revision=" r1 ",
            remote=False,
            estimated_cost="0",
            latency_ms=1,
            quality_score="0.5",
        )
        self.assertEqual(descriptor.model_id, "local")
        self.assertEqual(descriptor.provider_id, "provider")
        self.assertEqual(descriptor.revision, "r1")

        policy = RoutingPolicy(
            RoutingMode.ALLOWLIST,
            allowed_model_ids=(" local ",),
            maximum_cost="1",
        )
        req = ModelRequest(
            request_id=" req ",
            allowed_model_ids=(" local ",),
            privacy_remote_allowed=False,
            budget_remaining="1",
            deadline_utc=NOW + timedelta(minutes=1),
        )
        self.assertEqual(policy.allowed_model_ids, ("local",))
        self.assertEqual(req.request_id, "req")
        decision = route_model(policy, req, [descriptor], now_utc=NOW)
        self.assertEqual(RouteStatus.ADMITTED, decision.status)

        with self.assertRaisesRegex(ValueError, "duplicates"):
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("local", " local "),
                maximum_cost="1",
            )
        with self.assertRaises(TypeError):
            ModelRequest(
                request_id="req-list",
                allowed_model_ids=["local"],
                privacy_remote_allowed=False,
                budget_remaining="1",
                deadline_utc=NOW + timedelta(minutes=1),
            )
        with self.assertRaises(ValueError):
            ModelDescriptor(
                model_id="   ",
                provider_id="provider",
                revision="r1",
                remote=False,
                estimated_cost="0",
                latency_ms=1,
                quality_score="0.5",
            )

    def test_cancelled_request_rejects_before_inventory_access(self):
        class ExplodingInventory:
            def __iter__(self):
                raise AssertionError("cancelled request must not inspect model inventory")

        req = ModelRequest(
            request_id="cancelled",
            allowed_model_ids=("remote",),
            privacy_remote_allowed=True,
            budget_remaining="10",
            deadline_utc=NOW + timedelta(minutes=1),
            cancelled=True,
        )
        decision = route_model(
            RoutingPolicy(
                RoutingMode.ALLOWLIST,
                allowed_model_ids=("remote",),
                allow_remote=True,
                maximum_cost="10",
            ),
            req,
            ExplodingInventory(),
            now_utc=NOW,
        )
        self.assertEqual(RouteStatus.REJECTED, decision.status)
        self.assertEqual("request_cancelled", decision.reason)
        self.assertEqual(Decimal("0"), decision.reserved_cost)

        with self.assertRaises(TypeError):
            ModelRequest(
                request_id="bad-cancel",
                allowed_model_ids=(),
                privacy_remote_allowed=False,
                budget_remaining="0",
                deadline_utc=NOW + timedelta(minutes=1),
                cancelled=1,
            )

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


class BudgetLedgerTests(unittest.TestCase):
    def test_budget_ledger_rejects_binary_float_money(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger

        with self.assertRaisesRegex(ValueError, "exact decimal"):
            BudgetLedger(1.0)

        ledger = BudgetLedger(Decimal("2"))
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            ledger.reserve("req", 0.1)

        ledger.reserve("req", Decimal("1"))
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            ledger.settle("req", incurred=0.1)
        self.assertEqual(Decimal("1"), ledger.snapshot().reserved)

        ledger.settle(
            "req",
            incurred=Decimal("0.2"),
            estimated_unbilled=Decimal("0.5"),
        )
        with self.assertRaisesRegex(ValueError, "exact decimal"):
            ledger.reconcile_unbilled(billing_id="bill-float", request_id="req", billed=0.1)

    def test_reservation_is_idempotent(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        ledger.reserve("req", Decimal("1"))
        self.assertEqual(Decimal("1"), ledger.snapshot().reserved)

    def test_reservation_conflict_is_rejected(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        with self.assertRaises(ValueError):
            ledger.reserve("req", Decimal("1.1"))

    def test_budget_exhaustion_is_rejected(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("1"))
        with self.assertRaises(ValueError):
            ledger.reserve("req", Decimal("1.01"))

    def test_settlement_separates_incurred_and_unbilled(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        ledger.settle("req", incurred=Decimal("0.4"), estimated_unbilled=Decimal("0.3"))
        snap = ledger.snapshot()
        self.assertEqual(Decimal("0.4"), snap.incurred)
        self.assertEqual(Decimal("0.3"), snap.estimated_unbilled)
        self.assertEqual(Decimal("1.3"), snap.available)

    def test_settlement_cannot_exceed_reservation(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        with self.assertRaises(ValueError):
            ledger.settle("req", incurred=Decimal("1.01"))
        self.assertEqual(Decimal("1"), ledger.snapshot().reserved)

    def test_unbilled_reconciliation_is_idempotent_by_billing_identity(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        ledger.settle("req", incurred=Decimal("0.2"), estimated_unbilled=Decimal("0.5"))
        ledger.reconcile_unbilled(billing_id="invoice-line-1", request_id="req", billed=Decimal("0.3"))
        first = ledger.snapshot()
        ledger.reconcile_unbilled(billing_id="invoice-line-1", request_id="req", billed=Decimal("0.3"))
        second = ledger.snapshot()
        self.assertEqual(first, second)
        with self.assertRaisesRegex(ValueError, "conflict"):
            ledger.reconcile_unbilled(
                billing_id="invoice-line-1",
                request_id="req",
                billed=Decimal("0.2"),
            )
        self.assertEqual(ledger.snapshot(), second)


    def test_actual_billing_overrun_is_preserved_and_blocks_future_budget(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger

        ledger = BudgetLedger(Decimal("1"))
        ledger.reserve("req", Decimal("0.4"))
        ledger.settle("req", incurred=Decimal("0"), estimated_unbilled=Decimal("0.4"))
        ledger.reconcile_unbilled(
            billing_id="provider-invoice-line-1",
            request_id="req",
            billed=Decimal("1.2"),
        )
        snap = ledger.snapshot()
        self.assertEqual(Decimal("1.2"), snap.incurred)
        self.assertEqual(Decimal("0"), snap.estimated_unbilled)
        self.assertEqual(Decimal("0"), snap.available)
        with self.assertRaisesRegex(ValueError, "budget exhausted"):
            ledger.reserve("another", Decimal("0.01"))

    def test_billing_never_consumes_another_requests_unbilled_estimate(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger

        ledger = BudgetLedger(Decimal("3"))
        ledger.reserve("req-a", Decimal("0.5"))
        ledger.reserve("req-b", Decimal("0.5"))
        ledger.settle("req-a", incurred=Decimal("0"), estimated_unbilled=Decimal("0.5"))
        ledger.settle("req-b", incurred=Decimal("0"), estimated_unbilled=Decimal("0.5"))
        ledger.reconcile_unbilled(
            billing_id="bill-a",
            request_id="req-a",
            billed=Decimal("0.6"),
        )
        snap = ledger.snapshot()
        self.assertEqual(Decimal("0.6"), snap.incurred)
        self.assertEqual(Decimal("0.5"), snap.estimated_unbilled)

    def test_unknown_request_billing_fails_without_mutating_budget(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger

        ledger = BudgetLedger(Decimal("2"))
        before = ledger.snapshot()
        with self.assertRaisesRegex(ValueError, "matching settled request"):
            ledger.reconcile_unbilled(
                billing_id="orphan-bill",
                request_id="missing",
                billed=Decimal("0.2"),
            )
        self.assertEqual(before, ledger.snapshot())

    def test_billing_identity_binds_request_and_amount(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger

        ledger = BudgetLedger(Decimal("2"))
        for request_id in ("a", "b"):
            ledger.reserve(request_id, Decimal("0.5"))
            ledger.settle(
                request_id,
                incurred=Decimal("0"),
                estimated_unbilled=Decimal("0.5"),
            )
        ledger.reconcile_unbilled(
            billing_id="same-provider-line",
            request_id="a",
            billed=Decimal("0.3"),
        )
        stable = ledger.snapshot()
        ledger.reconcile_unbilled(
            billing_id="same-provider-line",
            request_id="a",
            billed=Decimal("0.3"),
        )
        self.assertEqual(stable, ledger.snapshot())
        with self.assertRaisesRegex(ValueError, "conflict"):
            ledger.reconcile_unbilled(
                billing_id="same-provider-line",
                request_id="b",
                billed=Decimal("0.3"),
            )
        self.assertEqual(stable, ledger.snapshot())

    def test_unbilled_reconciliation_moves_cost_to_incurred(self):
        from mvp.autotrade_mvp.model_gateway import BudgetLedger
        ledger = BudgetLedger(Decimal("2"))
        ledger.reserve("req", Decimal("1"))
        ledger.settle("req", incurred=Decimal("0.2"), estimated_unbilled=Decimal("0.5"))
        ledger.reconcile_unbilled(billing_id="bill-1", request_id="req", billed=Decimal("0.3"))
        snap = ledger.snapshot()
        self.assertEqual(Decimal("0.5"), snap.incurred)
        self.assertEqual(Decimal("0.2"), snap.estimated_unbilled)
