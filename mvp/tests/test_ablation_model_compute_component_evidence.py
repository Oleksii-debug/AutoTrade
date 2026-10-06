from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.ablation_model_compute_component_evidence import (
    ResolvedAblationModelComputeComponentEvidence,
    resolve_ablation_model_compute_component_evidence,
    reverify_ablation_model_compute_component_evidence,
)
from mvp.autotrade_mvp.ablation_model_compute_evidence import (
    resolve_ablation_model_compute_cost_fact,
)
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import ModelCallSpec
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.tests.test_model_call import (
    INPUT_DIGEST,
    NOW,
    NOW_TEXT,
    MutableClock,
    descriptor,
    fixed_policy,
    observation,
    orchestrator_for,
    request_for,
)


class AblationModelComputeComponentEvidenceTests(unittest.TestCase):
    def _budget(self, root: Path, *, budget_id: str = "model-policy-budget") -> DurableModelBudget:
        root.mkdir(parents=True, exist_ok=True)
        return DurableModelBudget(
            journal=JournalStore(root / (budget_id + ".sqlite3")),
            budget_id=budget_id,
            ceiling="5",
            environment="PAPER",
            clock=lambda: NOW_TEXT,
        )

    def _observed_call(self, root: Path, *, cost_currency: str = "USD", unbilled: str = "0"):
        budget = self._budget(root)
        calls = orchestrator_for(budget=budget, clock=MutableClock())
        call_spec = ModelCallSpec(
            job_id="research-job-1",
            input_digest=INPUT_DIGEST,
            policy_id="policy-v1",
            pricing_evidence_id="pricing-v1",
            pricing_as_of=NOW_TEXT,
            result_schema_id="schema-v1",
            cost_currency=cost_currency,
        )
        request = request_for(calls, call_spec)
        outcome = calls.execute(
            spec=call_spec,
            policy=fixed_policy(),
            request=request,
            descriptors=[descriptor()],
            call=lambda *_args: observation(
                incurred="0.4",
                unbilled=unbilled,
                billing_id="bill-1",
            ),
            validate_result=lambda _value: True,
            now_utc=NOW,
        )
        self.assertEqual(outcome.status, "OBSERVED_VALID")
        return budget, calls, call_spec, request

    def test_observed_call_upgrades_settled_budget_fact_to_dimensionally_qualified_component(self):
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(Path(directory))
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )

            evidence = resolve_ablation_model_compute_component_evidence(
                budget,
                calls,
                fact,
            )

            self.assertIsInstance(evidence, ResolvedAblationModelComputeComponentEvidence)
            self.assertEqual(evidence.component_name, "model_compute")
            self.assertEqual(str(evidence.amount), "0.4")
            self.assertEqual(evidence.value_unit, "USD")
            self.assertTrue(evidence.dimensionally_qualified)
            self.assertTrue(evidence.terminal_cost_component)
            self.assertEqual(evidence.compute_fact_digest, fact.evidence_digest)
            self.assertGreaterEqual(len(evidence.model_call_event_identities), 3)
            evidence.verify_integrity()

    def test_non_usd_call_preserves_owned_currency_without_implicit_fx(self):
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(
                Path(directory),
                cost_currency="EUR",
            )
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )

            evidence = resolve_ablation_model_compute_component_evidence(
                budget,
                calls,
                fact,
            )

            self.assertEqual(evidence.value_unit, "EUR")
            self.assertEqual(str(evidence.amount), "0.4")

    def test_authenticated_billing_keeps_same_currency_and_matures_unbilled_amount(self):
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(
                Path(directory),
                cost_currency="USD",
                unbilled="0.2",
            )
            calls.reconcile_billing(
                attempt_id=request.request_id,
                billing_id="bill-1",
                billed="0.2",
            )
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=4,
            )

            evidence = resolve_ablation_model_compute_component_evidence(
                budget,
                calls,
                fact,
            )

            self.assertEqual(evidence.value_unit, "USD")
            self.assertEqual(str(evidence.amount), "0.6")
            self.assertTrue(
                any(
                    identity[2] == "ModelBillingEvidenceObserved"
                    for identity in evidence.model_call_event_identities
                )
            )

    def test_direct_budget_mutation_cannot_self_assign_a_currency(self):
        with TemporaryDirectory() as directory:
            budget = self._budget(Path(directory))
            calls = orchestrator_for(budget=budget, clock=MutableClock())
            budget.reserve("legacy-request", "1")
            budget.settle("legacy-request", incurred="0.4", estimated_unbilled="0")
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id="legacy-request",
                expected_aggregate_version=3,
            )

            with self.assertRaisesRegex(
                ValueError,
                "lacks durable model-call authority",
            ):
                resolve_ablation_model_compute_component_evidence(
                    budget,
                    calls,
                    fact,
                )

    def test_different_budget_owner_graph_cannot_relabel_component(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            budget, calls, _spec, request = self._observed_call(root / "first")
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )
            other_budget = self._budget(root / "second", budget_id="other-budget")
            other_calls = orchestrator_for(
                budget=other_budget,
                clock=MutableClock(),
            )

            with self.assertRaisesRegex(
                ValueError,
                "not the same owner graph",
            ):
                resolve_ablation_model_compute_component_evidence(
                    budget,
                    other_calls,
                    fact,
                )

    def test_component_digest_tamper_fails_before_replay(self):
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(Path(directory))
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )
            evidence = resolve_ablation_model_compute_component_evidence(
                budget,
                calls,
                fact,
            )
            object.__setattr__(evidence, "evidence_digest", "sha256:" + "f" * 64)

            with self.assertRaisesRegex(
                ValueError,
                "component digest does not match canonical material",
            ):
                reverify_ablation_model_compute_component_evidence(
                    budget,
                    calls,
                    fact,
                    evidence,
                )

    def test_shadowed_model_call_reader_cannot_retarget_currency_authority(self):
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(Path(directory))
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )
            object.__setattr__(calls, "_events", lambda *_args: [])

            with self.assertRaisesRegex(
                ValueError,
                "shadows canonical methods",
            ):
                resolve_ablation_model_compute_component_evidence(
                    budget,
                    calls,
                    fact,
                )

    def test_shadowed_aggregate_identity_cannot_retarget_currency_authority(self):
        calls_seen: list[str] = []
        with TemporaryDirectory() as directory:
            budget, calls, _spec, request = self._observed_call(Path(directory))
            fact = resolve_ablation_model_compute_cost_fact(
                budget,
                request_id=request.request_id,
                expected_aggregate_version=3,
            )
            object.__setattr__(
                calls,
                "_aggregate_id",
                lambda *_args: calls_seen.append("hostile") or "foreign-aggregate",
            )

            with self.assertRaisesRegex(
                ValueError,
                "shadows canonical methods",
            ):
                resolve_ablation_model_compute_component_evidence(
                    budget,
                    calls,
                    fact,
                )
            self.assertEqual(calls_seen, [])


if __name__ == "__main__":
    unittest.main()
