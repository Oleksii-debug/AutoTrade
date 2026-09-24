from decimal import Decimal
import unittest

from mvp.autotrade_mvp.model_routing import (
    ModelBudgetConflict,
    ModelBudgetLedger,
    ModelDescriptor,
    ModelRoutingError,
    ModelRoutingPolicy,
    ModelTask,
    route_model,
)


HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


def model(
    model_id="local-fast",
    *,
    location="LOCAL",
    quality="0.80",
    latency=40,
    input_price="0.000001",
    output_price="0.000002",
    revision="rev-1",
    schemas=("decision.explain.v1",),
    tools=(),
    context=4096,
):
    return ModelDescriptor.create(
        model_id=model_id,
        provider="local-runtime" if location == "LOCAL" else "remote-provider",
        model_name=model_id,
        revision=revision,
        location=location,
        supported_task_schemas=schemas,
        modalities=("TEXT",),
        tool_permissions=tools,
        privacy_region=("EU" if location == "REMOTE" else None),
        license_id="test-license",
        deterministic_limitations=(),
        max_context_tokens=context,
        max_output_tokens=1024,
        expected_latency_ms=latency,
        input_token_price=input_price,
        output_token_price=output_price,
        price_evidence_sha256=HASH_A,
        measured_quality=quality,
        quality_evidence_sha256=HASH_B,
    )


def policy(
    *,
    mode="DYNAMIC",
    allowed=("local-fast",),
    remote_regions=(),
    max_total_cost="1",
    deadline=1000,
    fallback="NO_TRADE",
):
    return ModelRoutingPolicy.create(
        policy_id="policy-1",
        mode=mode,
        allowed_model_ids=allowed,
        allowed_remote_regions=remote_regions,
        max_total_cost=max_total_cost,
        max_input_tokens=3000,
        max_output_tokens=500,
        deadline_ms=deadline,
        fallback=fallback,
    )


def task(*, required_tools=(), input_tokens=100, output_tokens=50):
    return ModelTask.create(
        task_id="task-1",
        task_schema="decision.explain.v1",
        input_hash=HASH_C,
        input_tokens=input_tokens,
        max_output_tokens=output_tokens,
        required_tools=required_tools,
    )


class ModelRoutingTests(unittest.TestCase):
    def test_zero_llm_never_selects_or_reserves_model(self):
        budget = ModelBudgetLedger(ceiling="10")
        decision = route_model(
            policy=policy(mode="ZERO_LLM", allowed=(), fallback="DETERMINISTIC"),
            task=task(),
            models=[model()],
            budget=budget,
        )
        self.assertEqual(decision.outcome, "DETERMINISTIC")
        self.assertEqual(decision.reason, "zero_llm_policy")
        self.assertIsNone(decision.model_id)
        self.assertFalse(decision.authorizes_trading)
        self.assertEqual(budget.snapshot().reserved, Decimal("0"))

    def test_local_only_never_falls_through_to_remote_model(self):
        budget = ModelBudgetLedger(ceiling="10")
        decision = route_model(
            policy=policy(mode="LOCAL_ONLY", allowed=("remote",)),
            task=task(),
            models=[model("remote", location="REMOTE")],
            budget=budget,
        )
        self.assertEqual(decision.outcome, "NO_TRADE")
        self.assertEqual(decision.reason, "no_eligible_model")
        self.assertEqual(budget.snapshot().committed, Decimal("0"))

    def test_dynamic_route_uses_measured_quality_then_cost_and_reserves_exact_decimal(self):
        budget = ModelBudgetLedger(ceiling="1")
        lower = model("low", quality="0.70", input_price="0.000001")
        higher = model(
            "high",
            quality="0.90",
            input_price="0.000003",
            output_price="0.000004",
        )
        decision = route_model(
            policy=policy(allowed=("low", "high")),
            task=task(input_tokens=100, output_tokens=50),
            models=[lower, higher],
            budget=budget,
        )
        self.assertEqual(decision.model_id, "high")
        self.assertEqual(decision.estimated_cost, Decimal("0.000500"))
        self.assertEqual(budget.snapshot().reserved, Decimal("0.000500"))
        self.assertEqual(decision.price_evidence_sha256, HASH_A)
        self.assertEqual(decision.quality_evidence_sha256, HASH_B)
        self.assertFalse(decision.authorizes_trading)

    def test_same_route_is_idempotent_and_does_not_double_reserve(self):
        budget = ModelBudgetLedger(ceiling="1")
        kwargs = dict(
            policy=policy(),
            task=task(),
            models=[model()],
            budget=budget,
        )
        first = route_model(**kwargs)
        reserved = budget.snapshot().reserved
        second = route_model(**kwargs)
        self.assertEqual(second.reservation_id, first.reservation_id)
        self.assertEqual(budget.snapshot().reserved, reserved)

    def test_deadline_context_output_and_tool_constraints_fail_closed(self):
        cases = [
            (
                policy(deadline=10),
                task(),
                model(latency=11),
            ),
            (
                policy(),
                task(input_tokens=3000, output_tokens=500),
                model(context=3000),
            ),
            (
                policy(),
                task(required_tools=("web.search",)),
                model(tools=()),
            ),
        ]
        for route_policy, route_task, descriptor in cases:
            with self.subTest(descriptor=descriptor.model_id, task=route_task.task_id):
                budget = ModelBudgetLedger(ceiling="10")
                decision = route_model(
                    policy=route_policy,
                    task=route_task,
                    models=[descriptor],
                    budget=budget,
                )
                self.assertEqual(decision.outcome, "NO_TRADE")
                self.assertEqual(decision.reason, "no_eligible_model")
                self.assertEqual(budget.snapshot().committed, Decimal("0"))

    def test_policy_token_limit_falls_back_before_model_selection(self):
        budget = ModelBudgetLedger(ceiling="10")
        too_large = task(input_tokens=3001)
        decision = route_model(
            policy=policy(),
            task=too_large,
            models=[model()],
            budget=budget,
        )
        self.assertEqual(decision.reason, "policy_input_token_limit")
        self.assertEqual(decision.outcome, "NO_TRADE")


    def test_remote_model_requires_explicit_privacy_region_allowance(self):
        remote = model("remote", location="REMOTE")
        blocked_budget = ModelBudgetLedger(ceiling="1")
        blocked = route_model(
            policy=policy(allowed=("remote",), remote_regions=()),
            task=task(),
            models=[remote],
            budget=blocked_budget,
        )
        self.assertEqual(blocked.outcome, "NO_TRADE")
        self.assertEqual(blocked_budget.snapshot().committed, Decimal("0"))

        allowed_budget = ModelBudgetLedger(ceiling="1")
        allowed = route_model(
            policy=policy(allowed=("remote",), remote_regions=("EU",)),
            task=task(),
            models=[remote],
            budget=allowed_budget,
        )
        self.assertEqual(allowed.outcome, "MODEL")
        self.assertEqual(allowed.model_id, "remote")

    def test_remote_descriptor_without_privacy_region_is_invalid(self):
        with self.assertRaisesRegex(ModelRoutingError, "privacy_region"):
            ModelDescriptor.create(
                model_id="remote-unsafe",
                provider="remote",
                model_name="remote-unsafe",
                revision="r1",
                location="REMOTE",
                supported_task_schemas={"decision.explain.v1"},
                license_id="test-license",
                max_context_tokens=1000,
                max_output_tokens=100,
                expected_latency_ms=50,
                input_token_price="0",
                output_token_price="0",
                price_evidence_sha256=HASH_A,
                measured_quality="0.5",
                quality_evidence_sha256=HASH_B,
            )

    def test_user_allowlist_is_hard_boundary(self):
        budget = ModelBudgetLedger(ceiling="10")
        better_but_unapproved = model("unapproved", quality="1")
        approved = model("local-fast", quality="0.2")
        decision = route_model(
            policy=policy(allowed=("local-fast",)),
            task=task(),
            models=[better_but_unapproved, approved],
            budget=budget,
        )
        self.assertEqual(decision.model_id, "local-fast")

    def test_budget_ceiling_is_hard_pre_call_gate(self):
        budget = ModelBudgetLedger(ceiling="0.00001")
        decision = route_model(
            policy=policy(max_total_cost="100"),
            task=task(),
            models=[model()],
            budget=budget,
        )
        self.assertEqual(decision.outcome, "NO_TRADE")
        self.assertEqual(decision.reason, "no_eligible_model")
        self.assertEqual(budget.snapshot().committed, Decimal("0"))

    def test_reserved_unbilled_and_incurred_costs_are_separate(self):
        budget = ModelBudgetLedger(ceiling="1")
        decision = route_model(
            policy=policy(),
            task=task(),
            models=[model()],
            budget=budget,
        )
        reservation_id = decision.reservation_id
        self.assertIsNotNone(reservation_id)
        reserved = decision.estimated_cost
        self.assertEqual(budget.snapshot().reserved, reserved)

        budget.mark_unbilled(reservation_id, estimated_cost="0.00030")
        middle = budget.snapshot()
        self.assertEqual(middle.reserved, Decimal("0"))
        self.assertEqual(middle.estimated_unbilled, Decimal("0.00030"))
        self.assertEqual(middle.incurred, Decimal("0"))

        budget.record_billing(reservation_id, billed_cost="0.00025")
        final = budget.snapshot()
        self.assertEqual(final.reserved, Decimal("0"))
        self.assertEqual(final.estimated_unbilled, Decimal("0"))
        self.assertEqual(final.incurred, Decimal("0.00025"))

    def test_actual_billing_overrun_is_recorded_not_erased(self):
        budget = ModelBudgetLedger(ceiling="0.001")
        decision = route_model(
            policy=policy(),
            task=task(),
            models=[model()],
            budget=budget,
        )
        budget.record_billing(decision.reservation_id, billed_cost="0.002")
        self.assertTrue(budget.snapshot().over_budget)
        self.assertEqual(budget.snapshot().available, Decimal("0"))
        with self.assertRaisesRegex(ModelRoutingError, "ceiling"):
            budget.reserve("another", "0.000001")

    def test_unknown_revision_is_explicit_reproducibility_limitation(self):
        budget = ModelBudgetLedger(ceiling="1")
        decision = route_model(
            policy=policy(),
            task=task(),
            models=[model(revision=None)],
            budget=budget,
        )
        self.assertEqual(decision.outcome, "MODEL")
        self.assertEqual(decision.revision, None)
        self.assertEqual(
            decision.reproducibility_limitations,
            ("UNKNOWN_MODEL_REVISION",),
        )

    def test_float_money_truthy_boolean_and_opaque_evidence_are_rejected(self):
        with self.assertRaisesRegex(ModelRoutingError, "exact"):
            ModelBudgetLedger(ceiling=1.5)
        with self.assertRaisesRegex(ModelRoutingError, "integer"):
            task(input_tokens=True)
        with self.assertRaisesRegex(ModelRoutingError, "canonical sha256"):
            ModelDescriptor.create(
                model_id="bad",
                provider="p",
                model_name="m",
                revision="r",
                location="LOCAL",
                supported_task_schemas={"decision.explain.v1"},
                license_id="test-license",
                max_context_tokens=100,
                max_output_tokens=10,
                expected_latency_ms=1,
                input_token_price="0",
                output_token_price="0",
                price_evidence_sha256="price:latest",
                measured_quality="0.5",
                quality_evidence_sha256=HASH_B,
            )

    def test_fixed_and_zero_llm_policy_shapes_are_strict(self):
        with self.assertRaisesRegex(ModelRoutingError, "exactly one"):
            policy(mode="FIXED", allowed=("a", "b"))
        with self.assertRaisesRegex(ModelRoutingError, "cannot contain"):
            policy(mode="ZERO_LLM", allowed=("a",))

    def test_budget_identity_conflicts_fail_closed(self):
        budget = ModelBudgetLedger(ceiling="1")
        self.assertTrue(budget.reserve("call-1", "0.1"))
        self.assertFalse(budget.reserve("call-1", "0.1"))
        with self.assertRaises(ModelBudgetConflict):
            budget.reserve("call-1", "0.2")
        budget.mark_unbilled("call-1")
        with self.assertRaises(ModelBudgetConflict):
            budget.release("call-1")
        budget.record_billing("call-1", billed_cost="0.09")
        budget.record_billing("call-1", billed_cost="0.09")
        with self.assertRaises(ModelBudgetConflict):
            budget.record_billing("call-1", billed_cost="0.08")


if __name__ == "__main__":
    unittest.main()
