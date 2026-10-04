"""Cross-bind one financial request to one exact selected provider route.

This module is deliberately pure.  It does not issue financial authority, select
a provider, resolve current capability/qualification state, dispatch a request,
or add another final guard.  It only proves that the immutable #987 financial
request binding names the same provider financial scope, capability C and
accepted provider qualification Q already sealed by the canonical route result.
"""

from __future__ import annotations

from .financial_request_binding import FinancialRequestBindingMaterial
from .provider_selection import SelectedProviderRoute


class ProviderRouteFinancialBindingError(PermissionError):
    """The financially admitted request and selected provider route diverge."""


def require_financial_binding_matches_selected_route(
    binding: FinancialRequestBindingMaterial,
    route: SelectedProviderRoute,
) -> None:
    """Require exact financial/provider identity before provider dispatch.

    The accepted Q identifier is already the content digest of the sealed
    ProviderQualificationIdentity, and FinancialRequestBindingMaterial uses the
    same ``provider-qualification:sha256:...`` namespace.  Equality here is
    therefore an authority identity equality, not a caller label comparison.
    """

    if type(binding) is not FinancialRequestBindingMaterial:
        raise TypeError("binding must be exact FinancialRequestBindingMaterial")
    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")

    candidate = route.candidate
    capability = route.capability
    qualification = route.qualification
    qualification_scope = qualification.scope
    provider_scope = qualification_scope.provider_scope

    if qualification.qualification_id != qualification.identity.content_digest:
        raise ProviderRouteFinancialBindingError(
            "selected provider qualification identity is internally inconsistent"
        )

    route_identity = (
        candidate.provider_id,
        candidate.account_id,
        qualification_scope.provider_scope.runtime_environment,
        candidate.provider_environment,
        candidate.entity_policy_id,
        candidate.product_family,
        candidate.adapter_code_sha,
        candidate.packaged_artifact_digest,
        candidate.protocol_id,
        candidate.protocol_version,
    )
    qualification_identity = (
        provider_scope.provider_id,
        capability.account_id,
        provider_scope.runtime_environment,
        provider_scope.provider_environment,
        provider_scope.entity_policy_id,
        qualification_scope.product_family,
        qualification_scope.adapter_source_git_sha,
        qualification_scope.packaged_artifact_digest,
        qualification_scope.protocol_id,
        qualification_scope.protocol_version,
    )
    if route_identity != qualification_identity:
        raise ProviderRouteFinancialBindingError(
            "selected route static composition differs from accepted qualification"
        )

    capability_identity = (
        capability.provider_id,
        capability.account_id,
        capability.environment,
        capability.provider_environment,
        capability.entity_id,
    )
    candidate_capability_identity = (
        candidate.provider_id,
        candidate.account_id,
        provider_scope.runtime_environment,
        candidate.provider_environment,
        candidate.entity_id,
    )
    if capability_identity != candidate_capability_identity:
        raise ProviderRouteFinancialBindingError(
            "selected route static composition differs from capability authority"
        )

    financial_identity = (
        binding.provider_scope_digest,
        binding.provider_id,
        binding.account_id,
        binding.runtime_environment,
        binding.provider_environment,
        binding.entity_policy_id,
        binding.capability_snapshot_id,
        binding.qualification_identity_digest,
    )
    selected_identity = (
        provider_scope.content_digest,
        candidate.provider_id,
        candidate.account_id,
        provider_scope.runtime_environment,
        candidate.provider_environment,
        candidate.entity_policy_id,
        route.capability_snapshot_id,
        route.qualification_id,
    )
    if financial_identity != selected_identity:
        raise ProviderRouteFinancialBindingError(
            "financial request binding differs from selected provider C/Q authority"
        )
