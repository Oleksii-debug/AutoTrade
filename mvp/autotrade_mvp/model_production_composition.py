"""Canonical production composition for the durable model-call boundary.

The low-level DurableModelCallOrchestrator deliberately accepts injected
resolvers so deterministic tests can exercise failure modes. Production
composition must not expose that callback surface. This module requires the
three concrete retained-evidence authorities and wires them into the one
existing durable lifecycle; it creates no second router, budget, journal or
inference sender.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .model_billing_evidence import ModelBillingEvidenceAuthority
from .model_budget_journal import DurableModelBudget
from .model_call import DurableModelCallOrchestrator
from .model_observation_evidence import ModelObservationEvidenceAuthority
from .model_pricing_evidence import ModelPricingEvidenceAuthority


class ProductionModelCompositionError(TypeError):
    """Raised when production composition is given a non-canonical authority."""


@dataclass(frozen=True, slots=True)
class ProductionModelEvidenceAuthorities:
    """The exact concrete evidence authorities admitted to production wiring."""

    pricing: ModelPricingEvidenceAuthority
    observation: ModelObservationEvidenceAuthority
    billing: ModelBillingEvidenceAuthority

    def __post_init__(self) -> None:
        if type(self.pricing) is not ModelPricingEvidenceAuthority:
            raise ProductionModelCompositionError(
                "pricing must be exact ModelPricingEvidenceAuthority"
            )
        if type(self.observation) is not ModelObservationEvidenceAuthority:
            raise ProductionModelCompositionError(
                "observation must be exact ModelObservationEvidenceAuthority"
            )
        if type(self.billing) is not ModelBillingEvidenceAuthority:
            raise ProductionModelCompositionError(
                "billing must be exact ModelBillingEvidenceAuthority"
            )

    def build_orchestrator(
        self,
        *,
        budget: DurableModelBudget,
        clock: Callable[[], str],
        started_lease_seconds: int = 60,
        owner_token: str | None = None,
    ) -> DurableModelCallOrchestrator:
        """Build the sole durable model-call boundary with sealed resolver types."""

        if type(budget) is not DurableModelBudget:
            raise ProductionModelCompositionError(
                "budget must be exact DurableModelBudget"
            )
        if not callable(clock):
            raise ProductionModelCompositionError("clock must be callable")
        orchestrator = DurableModelCallOrchestrator(
            budget=budget,
            clock=clock,
            pricing_evidence_resolver=self.pricing,
            observation_evidence_resolver=self.observation,
            billing_evidence_resolver=self.billing,
            started_lease_seconds=started_lease_seconds,
            owner_token=owner_token,
        )
        # Fail closed if a future constructor rewrite silently stops preserving
        # exact authority object identity at the durable boundary.
        if (
            orchestrator.pricing_evidence_resolver is not self.pricing
            or orchestrator.observation_evidence_resolver is not self.observation
            or orchestrator.billing_evidence_resolver is not self.billing
        ):
            raise ProductionModelCompositionError(
                "durable model-call boundary did not retain exact evidence authorities"
            )
        return orchestrator


def build_production_model_orchestrator(
    *,
    budget: DurableModelBudget,
    clock: Callable[[], str],
    authorities: ProductionModelEvidenceAuthorities,
    started_lease_seconds: int = 60,
    owner_token: str | None = None,
) -> DurableModelCallOrchestrator:
    """Public production entrypoint; arbitrary callbacks are not accepted."""

    if type(authorities) is not ProductionModelEvidenceAuthorities:
        raise ProductionModelCompositionError(
            "authorities must be exact ProductionModelEvidenceAuthorities"
        )
    return authorities.build_orchestrator(
        budget=budget,
        clock=clock,
        started_lease_seconds=started_lease_seconds,
        owner_token=owner_token,
    )
