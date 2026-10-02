from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.model_billing_evidence import (
    LOCAL_RUNTIME_METER_SOURCE as BILLING_LOCAL_SOURCE,
    ModelBillingEvidenceAuthority,
    TrustedBillingArtifactReceipt,
)
from mvp.autotrade_mvp.model_budget_journal import DurableModelBudget
from mvp.autotrade_mvp.model_observation_evidence import (
    LOCAL_RUNTIME_METER_SOURCE as OBSERVATION_LOCAL_SOURCE,
    ModelObservationEvidenceAuthority,
    TrustedObservationArtifactReceipt,
)
from mvp.autotrade_mvp.model_pricing_evidence import (
    LOCAL_RUNTIME_SOURCE as PRICING_LOCAL_SOURCE,
    ModelPricingEvidenceAuthority,
    PricingAuthorityScope,
    TrustedPricingArtifactReceipt,
)
from mvp.autotrade_mvp.model_production_composition import (
    ProductionModelCompositionError,
    ProductionModelEvidenceAuthorities,
    build_production_model_orchestrator,
)
from mvp.autotrade_mvp.persistence import JournalStore


PRICING_DIGEST = "sha256:" + "a" * 64
NOW = "2026-09-25T10:00:00Z"


def authorities(directory):
    store = ArtifactStore(Path(directory) / "model-evidence")

    pricing = ModelPricingEvidenceAuthority(
        evidence_root=store.root,
        publication_store=store,
        trusted_receipts=(
            TrustedPricingArtifactReceipt(
                artifact_id="11111111-1111-4111-8111-111111111111",
                object_sha256="sha256:" + "1" * 64,
                source_sha256="sha256:" + "2" * 64,
                issuer="local-pricing-authority",
                source_class=PRICING_LOCAL_SOURCE,
                scopes=(
                    PricingAuthorityScope(
                        provider_id="local-runtime",
                        model_id="model-a",
                        revision="r1",
                        remote=False,
                    ),
                ),
            ),
        ),
    )
    observation = ModelObservationEvidenceAuthority(
        evidence_root=store.root,
        publication_store=store,
        trusted_receipts=(
            TrustedObservationArtifactReceipt(
                artifact_id="22222222-2222-4222-8222-222222222222",
                object_sha256="sha256:" + "3" * 64,
                source_sha256="sha256:" + "4" * 64,
                issuer="local-observation-authority",
                source_class=OBSERVATION_LOCAL_SOURCE,
                attempt_id="model-attempt-a",
                provider_id="local-runtime",
                model_id="model-a",
                revision="r1",
                pricing_evidence_digest=PRICING_DIGEST,
                cost_currency="USD",
                provider_request_id=None,
                provider_response_id=None,
                usage_id="local-usage-a",
                billing_id=None,
            ),
        ),
    )
    billing = ModelBillingEvidenceAuthority(
        evidence_root=store.root,
        publication_store=store,
        trusted_receipts=(
            TrustedBillingArtifactReceipt(
                artifact_id="33333333-3333-4333-8333-333333333333",
                object_sha256="sha256:" + "5" * 64,
                source_sha256="sha256:" + "6" * 64,
                issuer="local-billing-authority",
                source_class=BILLING_LOCAL_SOURCE,
                attempt_id="model-attempt-a",
                billing_id="local-billing-a",
                provider_id="local-runtime",
                model_id="model-a",
                revision="r1",
                cost_currency="USD",
                pricing_evidence_digest=PRICING_DIGEST,
                terminal_state="UNKNOWN",
            ),
        ),
    )
    return ProductionModelEvidenceAuthorities(
        pricing=pricing,
        observation=observation,
        billing=billing,
    )


def budget(directory):
    journal = JournalStore(Path(directory) / "journal.db")
    return DurableModelBudget(
        journal=journal,
        budget_id="production-model-budget",
        ceiling="5",
        environment="PAPER",
        clock=lambda: NOW,
    )


class ProductionModelCompositionTests(unittest.TestCase):
    def test_self_authored_exact_receipts_cannot_enter_production_composition(self):
        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            durable_budget = budget(directory)
            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "independent model-evidence receipt trust is unavailable",
            ):
                build_production_model_orchestrator(
                    budget=durable_budget,
                    clock=lambda: NOW,
                    authorities=evidence,
                    owner_token="production-model-owner",
                )

            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "independent model-evidence receipt trust is unavailable",
            ):
                evidence.build_orchestrator(
                    budget=durable_budget,
                    clock=lambda: NOW,
                    owner_token="production-model-owner",
                )

    def test_arbitrary_callback_cannot_enter_production_authority_bundle(self):
        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "pricing must be exact",
            ):
                ProductionModelEvidenceAuthorities(
                    pricing=lambda *_args: None,
                    observation=evidence.observation,
                    billing=evidence.billing,
                )

    def test_authority_subclass_cannot_extend_production_trust_surface(self):
        class ForgedPricingAuthority(ModelPricingEvidenceAuthority):
            pass

        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            forged = object.__new__(ForgedPricingAuthority)
            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "pricing must be exact",
            ):
                ProductionModelEvidenceAuthorities(
                    pricing=forged,
                    observation=evidence.observation,
                    billing=evidence.billing,
                )

    def test_public_builder_rejects_duck_typed_authority_bundle(self):
        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            durable_budget = budget(directory)

            class DuckBundle:
                pricing = evidence.pricing
                observation = evidence.observation
                billing = evidence.billing

            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "authorities must be exact",
            ):
                build_production_model_orchestrator(
                    budget=durable_budget,
                    clock=lambda: NOW,
                    authorities=DuckBundle(),
                )

    def test_budget_subclass_cannot_expand_production_budget_authority(self):
        class ForgedBudget(DurableModelBudget):
            pass

        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            forged = object.__new__(ForgedBudget)
            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "budget must be exact",
            ):
                evidence.build_orchestrator(
                    budget=forged,
                    clock=lambda: NOW,
                )

    def test_noncallable_clock_is_rejected_before_low_level_composition(self):
        with TemporaryDirectory() as directory:
            evidence = authorities(directory)
            durable_budget = budget(directory)
            with self.assertRaisesRegex(
                ProductionModelCompositionError,
                "clock must be callable",
            ):
                evidence.build_orchestrator(
                    budget=durable_budget,
                    clock="2026-09-25T10:00:00Z",
                )


if __name__ == "__main__":
    unittest.main()
