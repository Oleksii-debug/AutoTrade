"""Exact activation-episode binding for pending financial confirmations."""

from __future__ import annotations

from . import pending_intent_financial_binding_impl as _impl


PendingIntentFinancialBindingError = _impl.PendingIntentFinancialBindingError
PendingIntentFinancialBinding = _impl.PendingIntentFinancialBinding


class DurablePendingIntentFinancialBindingRegistry(
    _impl.DurablePendingIntentFinancialBindingRegistry
):
    """Retained binding registry plus exact RiskPolicy episode validation."""

    def resolve_current(
        self,
        pending_intent_id: str,
        *,
        resolved_risk_policy,
        reservation_requirements,
    ) -> PendingIntentFinancialBinding:
        binding = super().resolve_current(
            pending_intent_id,
            resolved_risk_policy=resolved_risk_policy,
            reservation_requirements=reservation_requirements,
        )
        resolved = _impl._resolved_policy_snapshot(resolved_risk_policy)
        if (
            resolved.registration_event_id
            != binding.risk_policy_registration_event_id
            or resolved.activation_event_id
            != binding.risk_policy_activation_event_id
        ):
            raise PendingIntentFinancialBindingError(
                "current risk policy activation episode no longer matches "
                "operator-confirmed envelope"
            )
        return binding


def __getattr__(name: str):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(dir(_impl)))
