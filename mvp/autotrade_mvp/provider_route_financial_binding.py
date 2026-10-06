"""Cross-bind one financial request to one exact selected provider route.

This module is deliberately narrow.  It does not issue financial authority,
select a provider, resolve current capability/qualification state, dispatch a
request, or add another final guard.  It proves that the immutable #987
financial request binding names the same provider financial scope, capability C
and accepted provider qualification Q already sealed by the canonical route
result, and exposes canonical construction inputs used while that binding and
the existing provider transport are prepared.
"""

from __future__ import annotations

import re

from .capabilities import CapabilityRegistry, CapabilitySnapshot
from .financial_request_binding import FinancialRequestBindingMaterial
from .provider_route_dispatch import bind_selected_provider_route_submission_scope
from .provider_selection import SelectedProviderRoute


class ProviderRouteFinancialBindingError(PermissionError):
    """The financially admitted request and selected provider route diverge."""


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderRouteFinancialBindingError(
            f"{name} must be exact non-empty text"
        )
    return value


def build_selected_provider_route_financial_submission_scope(
    route: SelectedProviderRoute,
    *,
    account_id: str,
    runtime_environment: str,
    endpoint: str,
    prepared_request_sha256: str,
    capability_snapshot_ids: tuple[str, ...],
    instrument_versions: tuple[str, ...],
) -> dict[str, object]:
    """Build the exact durable scope whose digest belongs in a financial binding.

    The selected route remains the owner of provider/C/Q/build identity while
    the exact prepared-request axes are supplied by the canonical request
    preparation path.  Keeping both in one scope lets the durable response
    binding prove the same request that crossed the final financial send fence.
    """

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    canonical_account_id = _exact_text(account_id, name="account_id")
    canonical_environment = _exact_text(
        runtime_environment,
        name="runtime_environment",
    )
    canonical_endpoint = _exact_text(endpoint, name="endpoint")
    if not canonical_endpoint.startswith("/") or "://" in canonical_endpoint:
        raise ProviderRouteFinancialBindingError(
            "endpoint must be a canonical provider-relative path"
        )
    if (
        type(prepared_request_sha256) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", prepared_request_sha256) is None
    ):
        raise ProviderRouteFinancialBindingError(
            "prepared_request_sha256 must be a canonical SHA-256 digest"
        )
    if type(capability_snapshot_ids) is not tuple:
        raise TypeError("capability_snapshot_ids must be an exact tuple")
    canonical_capabilities = tuple(
        _exact_text(value, name="capability_snapshot_id")
        for value in capability_snapshot_ids
    )
    if (
        len(canonical_capabilities) != 1
        or canonical_capabilities[0] != route.capability_snapshot_id
    ):
        raise ProviderRouteFinancialBindingError(
            "prepared request capability differs from selected provider route"
        )
    if type(instrument_versions) is not tuple:
        raise TypeError("instrument_versions must be an exact tuple")
    canonical_instruments = tuple(
        _exact_text(value, name="instrument_version")
        for value in instrument_versions
    )
    if len(canonical_instruments) != 1:
        raise ProviderRouteFinancialBindingError(
            "financial submission requires exactly one instrument version"
        )

    candidate = route.candidate
    provider_scope = route.qualification.scope.provider_scope
    if candidate.account_id != canonical_account_id:
        raise ProviderRouteFinancialBindingError(
            "financial submission account differs from selected provider route"
        )
    if provider_scope.runtime_environment != canonical_environment:
        raise ProviderRouteFinancialBindingError(
            "financial submission environment differs from selected provider route"
        )
    return bind_selected_provider_route_submission_scope(
        route,
        {
            "provider_id": candidate.provider_id,
            "account_id": canonical_account_id,
            "environment": canonical_environment,
            "provider_environment": candidate.provider_environment,
            "capability_snapshot_id": route.capability_snapshot_id,
            "endpoint": canonical_endpoint,
            "prepared_request_sha256": prepared_request_sha256,
            "capability_snapshot_ids": list(canonical_capabilities),
            "instrument_versions": list(canonical_instruments),
        },
    )


def build_selected_provider_route_transport_capability_registry(
    route: SelectedProviderRoute,
) -> CapabilityRegistry:
    """Project the sealed route C1 into the existing provider transport registry.

    No new capability is minted here.  ``SelectedProviderRoute.capability`` is
    the exact fresh ``CapabilitySnapshot`` returned by durable selection; the
    legacy provider transport consumes the same snapshot type.  A one-element
    registry therefore preserves C1 identity instead of asking product
    composition to choose another snapshot id or reconstruct capability facts.
    The durable C/Q final guard remains authoritative for supersession/currentness.
    """

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    capability = route.capability
    if type(capability) is not CapabilitySnapshot:
        raise ProviderRouteFinancialBindingError(
            "selected provider route capability is not canonical"
        )
    candidate = route.candidate
    provider_scope = route.qualification.scope.provider_scope
    capability_identity = (
        capability.provider_id,
        capability.account_id,
        capability.entity_id,
        capability.environment,
        capability.provider_environment,
    )
    selected_identity = (
        candidate.provider_id,
        candidate.account_id,
        candidate.entity_id,
        provider_scope.runtime_environment,
        candidate.provider_environment,
    )
    if capability_identity != selected_identity:
        raise ProviderRouteFinancialBindingError(
            "selected provider route capability differs from transport scope"
        )
    registry = CapabilityRegistry()
    registry.add(capability)
    return registry


def build_selected_bybit_transport_authority_inputs(
    route: SelectedProviderRoute,
) -> dict[str, object]:
    """Derive every route-sensitive input accepted by the public Bybit builder."""

    if type(route) is not SelectedProviderRoute:
        raise TypeError("route must be exact SelectedProviderRoute")
    if route.candidate.provider_id != "BYBIT":
        raise ProviderRouteFinancialBindingError(
            "Bybit transport inputs require a selected BYBIT route"
        )
    registry = build_selected_provider_route_transport_capability_registry(route)
    return {
        "provider_environment": route.candidate.provider_environment,
        "capability_snapshot_id": route.capability_snapshot_id,
        "capability_registry": registry,
    }


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
