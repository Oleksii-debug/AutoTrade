from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_call import (
    DurableModelCallOrchestrator,
    ModelCallSpec,
    ModelObservationEvidence,
    PricingEvidenceSnapshot,
    PricingQuote,
)
from mvp.autotrade_mvp.model_gateway import (
    ModelRequest,
    RoutingMode,
    RoutingPolicy,
)
from mvp.autotrade_mvp.model_provider_registry import ModelProviderConfiguration
from mvp.autotrade_mvp.openai_compatible_model_adapter import (
    OpenAICompatibleModelRegistration,
    OpenAICompatibleSettings,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest


class OpenAICompatibleModelGatewayWiringTests(unittest.TestCase):
    def test_registered_adapter_uses_durable_gateway_and_advisory_schema(self):
        prompt = "Observe"
        settings = OpenAICompatibleSettings(
            provider_id="groq",
            profile="owner",
            model_id="vendor/model-1",
            maximum_input_bytes=128,
            maximum_response_bytes=4096,
            maximum_output_tokens=8,
            input_usd_per_million=Decimal("1"),
            output_usd_per_million=Decimal("2"),
            timeout_seconds=9,
        )
        transport_calls = []

        with TemporaryDirectory() as directory:
            journal = JournalStore(Path(directory) / "journal.db")
            budget = DurableModelBudget(
                journal=journal,
                budget_id="registered-openai-compatible-budget",
                ceiling="1",
                environment="SIMULATION",
                clock=lambda: "2026-10-18T00:00:00+00:00",
            )

            def pricing_evidence(call_spec, descriptors):
                quotes = tuple(
                    PricingQuote(
                        provider_id=item.provider_id,
                        model_id=item.model_id,
                        revision=item.revision,
                        estimated_cost=item.estimated_cost,
                    )
                    for item in descriptors
                )
                material = [
                    {
                        "provider_id": item.provider_id,
                        "model_id": item.model_id,
                        "revision": item.revision,
                        "estimated_cost": str(item.estimated_cost),
                    }
                    for item in descriptors
                ]
                return PricingEvidenceSnapshot(
                    evidence_id=call_spec.pricing_evidence_id,
                    evidence_digest=payload_digest({
                        "pricing_evidence_id": call_spec.pricing_evidence_id,
                        "as_of": call_spec.pricing_as_of,
                        "cost_currency": call_spec.cost_currency,
                        "quotes": material,
                    }),
                    as_of=call_spec.pricing_as_of,
                    valid_until="2026-10-18T01:00:00Z",
                    cost_currency=call_spec.cost_currency,
                    quotes=quotes,
                )

            def observation_evidence(observed, binding):
                digest = DurableModelCallOrchestrator._observation_digest(
                    observed,
                    binding,
                )
                return ModelObservationEvidence(
                    attempt_id=binding.attempt_id,
                    evidence_id="test-observation-evidence",
                    evidence_digest=payload_digest({
                        "issuer": "test-model-meter",
                        "observation_digest": digest,
                    }),
                    issuer="test-model-meter",
                    observation_digest=digest,
                )

            orchestrator = DurableModelCallOrchestrator(
                budget=budget,
                clock=lambda: "2026-10-18T00:00:00Z",
                pricing_evidence_resolver=pricing_evidence,
                observation_evidence_resolver=observation_evidence,
                billing_evidence_resolver=lambda *_args: None,
            )
            call_spec = ModelCallSpec(
                job_id="advisory-agent-job",
                input_digest="sha256:" + sha256(prompt.encode()).hexdigest(),
                policy_id="explicit-remote-opt-in-v1",
                pricing_evidence_id="pricing-evidence-v1",
                pricing_as_of="2026-10-18T00:00:00Z",
                result_schema_id="advisory-side-v1",
            )

            def transport(*args):
                attempt_id = orchestrator.attempt_id(call_spec)
                event_types = [
                    event["event_type"]
                    for event in orchestrator._events(attempt_id)
                ]
                self.assertIn("ModelCallStarted", event_types)
                transport_calls.append(args)
                return {
                    "id": "response-1",
                    "usage": {"prompt_tokens": 4, "completion_tokens": 2},
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": "HOLD"},
                    }],
                }

            registration = OpenAICompatibleModelRegistration(
                ModelProviderConfiguration(("groq",)),
                settings,
                revision=None,
                latency_ms=100,
                quality_score=Decimal("0.5"),
                key_resolver=lambda **kwargs: "test-only-key",
                transport=transport,
            )
            descriptor = registration.descriptor
            self.assertEqual(descriptor.estimated_cost, settings.cost_ceiling_usd)
            self.assertTrue(descriptor.remote)

            request = ModelRequest(
                request_id=orchestrator.attempt_id(call_spec),
                allowed_model_ids=(settings.model_id,),
                privacy_remote_allowed=True,
                budget_remaining=Decimal("1"),
                deadline_utc=datetime(2026, 10, 18, tzinfo=timezone.utc)
                + timedelta(hours=1),
            )
            outcome = orchestrator.execute(
                spec=call_spec,
                policy=RoutingPolicy(
                    RoutingMode.FIXED,
                    allowed_model_ids=(settings.model_id,),
                    fixed_model_id=settings.model_id,
                    allow_remote=True,
                    maximum_cost=Decimal("1"),
                ),
                request=request,
                descriptors=(descriptor,),
                call=registration.inference_call(prompt=prompt),
                validate_result=lambda value: value.get("text") in {
                    "BUY", "SELL", "HOLD",
                },
                now_utc=datetime(2026, 10, 18, tzinfo=timezone.utc),
                cancel_requested=lambda: False,
            )

            self.assertEqual(
                outcome.status,
                "OBSERVED_VALID",
                (outcome.reason, orchestrator._events(orchestrator.attempt_id(call_spec))),
            )
            self.assertEqual(outcome.output["text"], "HOLD")
            self.assertEqual(len(transport_calls), 1)
            self.assertEqual(
                budget.snapshot().estimated_unbilled,
                Decimal("0.000008"),
            )


if __name__ == "__main__":
    unittest.main()
