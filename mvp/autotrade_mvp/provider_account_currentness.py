"""Current-consumption validation for WP-20 provider account evidence.

This module is a composition fence, not another provider reader, page-chain
issuer, coverage issuer, clock, or reconciliation engine.  It joins the exact
historical evidence values already issued by the canonical WP-20 components to
the current acquisition/Q authority at the instant a financial consumer wants
to use them.

It deliberately does not decide that the provider consistency horizon elapsed,
issue an accepted account cut, produce ``PROVEN_ABSENT``, or release UNKNOWN.
Those later authorities must consume this validator plus the independent trusted
chronology owned by WP-48/#1018.
"""
from __future__ import annotations

from datetime import datetime

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .provider_account_absence_coverage import (
    HistoricalUnknownSubmissionBinding,
    ProviderAccountAbsenceCoverageError,
    ProviderAccountSurfaceCoverage,
    require_historical_unknown_submission_authority,
    require_provider_account_surface_coverage_authority,
)
from .provider_account_absence_semantics import (
    ProviderAccountAbsenceSemanticsError,
    QualifiedProviderAccountAbsenceSemantics,
    require_provider_account_absence_rule,
)
from .provider_account_origin_set import (
    ProviderAccountOriginBindingSet,
    ProviderAccountOriginSetError,
    require_current_provider_account_origin_set_authority,
)
from .provider_account_page_chain import (
    ProviderAccountPageChain,
    ProviderAccountPageChainError,
    require_provider_account_page_chain_authority,
)


class ProviderAccountCurrentnessError(ValueError):
    """Historical WP-20 evidence is not valid for current financial use."""


def _at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderAccountCurrentnessError(
            "at must be exact timezone-aware datetime"
        )
    return value


def require_current_provider_account_page_chain_consumption(
    *,
    page_chain: ProviderAccountPageChain,
    origin_set: ProviderAccountOriginBindingSet,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> ProviderAccountPageChain:
    """Revalidate one sealed page chain against current acquisition and Q.

    The caller cannot provide a ``current`` boolean.  Every input is an exact
    issuer-protected object, and all cross-object identities are rechecked before
    the historical page graph is admitted for financial consumption.
    """

    if type(page_chain) is not ProviderAccountPageChain:
        raise TypeError("page_chain must be exact ProviderAccountPageChain")
    if type(origin_set) is not ProviderAccountOriginBindingSet:
        raise TypeError("origin_set must be exact ProviderAccountOriginBindingSet")
    if type(absence_semantics) is not QualifiedProviderAccountAbsenceSemantics:
        raise TypeError(
            "absence_semantics must be exact QualifiedProviderAccountAbsenceSemantics"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    point = _at(at)

    try:
        require_provider_account_page_chain_authority(page_chain)
        require_current_provider_account_origin_set_authority(
            origin_set,
            at=point,
        )
    except (ProviderAccountPageChainError, ProviderAccountOriginSetError) as error:
        raise ProviderAccountCurrentnessError(
            "page chain is not backed by exact current acquisition authority"
        ) from error

    try:
        rule = require_provider_account_absence_rule(
            absence_semantics,
            surface=page_chain.surface,
            endpoint=page_chain.endpoint,
            data_entitlement=page_chain.data_entitlement,
            qualification_registry=qualification_registry,
            at=point,
        )
    except ProviderAccountAbsenceSemanticsError as error:
        raise ProviderAccountCurrentnessError(
            "page chain is not backed by exact current provider-Q authority"
        ) from error

    if (
        page_chain.provider_scope_digest != origin_set.provider_scope_digest
        or page_chain.account_id != origin_set.account_id
        or page_chain.acquisition_id != origin_set.acquisition_id
        or page_chain.acquisition_generation != origin_set.acquisition_generation
        or page_chain.qualification_id != origin_set.qualification_id
        or page_chain.origin_set_digest != origin_set.content_digest
        or page_chain.provider_scope_digest != absence_semantics.provider_scope_digest
        or page_chain.qualification_id != absence_semantics.qualification_id
        or page_chain.absence_semantics_digest != absence_semantics.content_digest
        or page_chain.query_scope_rule_id != rule["query_scope_rule_id"]
        or page_chain.pagination_rule_id != rule["pagination_rule_id"]
    ):
        raise ProviderAccountCurrentnessError(
            "page chain no longer matches exact current acquisition/Q source authority"
        )
    return page_chain


def require_current_provider_account_surface_coverage_consumption(
    *,
    coverage: ProviderAccountSurfaceCoverage,
    page_chain: ProviderAccountPageChain,
    origin_set: ProviderAccountOriginBindingSet,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    historical_submission: HistoricalUnknownSubmissionBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> ProviderAccountSurfaceCoverage:
    """Revalidate one searched-send coverage component for current use.

    This validates current acquisition/Q identity and the exact durable UNKNOWN
    that the coverage searched.  It does *not* infer or accept consistency
    horizon satisfaction.
    """

    if type(coverage) is not ProviderAccountSurfaceCoverage:
        raise TypeError("coverage must be exact ProviderAccountSurfaceCoverage")
    if type(historical_submission) is not HistoricalUnknownSubmissionBinding:
        raise TypeError(
            "historical_submission must be exact HistoricalUnknownSubmissionBinding"
        )
    point = _at(at)
    try:
        require_provider_account_surface_coverage_authority(coverage)
        require_historical_unknown_submission_authority(historical_submission)
    except ProviderAccountAbsenceCoverageError as error:
        raise ProviderAccountCurrentnessError(
            "surface coverage historical construction authority is unavailable"
        ) from error

    require_current_provider_account_page_chain_consumption(
        page_chain=page_chain,
        origin_set=origin_set,
        absence_semantics=absence_semantics,
        qualification_registry=qualification_registry,
        at=point,
    )

    try:
        rule = require_provider_account_absence_rule(
            absence_semantics,
            surface=coverage.surface,
            endpoint=coverage.endpoint,
            data_entitlement=coverage.data_entitlement,
            qualification_registry=qualification_registry,
            at=point,
        )
    except ProviderAccountAbsenceSemanticsError as error:
        raise ProviderAccountCurrentnessError(
            "surface coverage is not backed by exact current provider-Q authority"
        ) from error

    if (
        coverage.page_chain_digest != page_chain.content_digest
        or coverage.historical_submission_digest
        != historical_submission.content_digest
        or coverage.provider_scope_digest != page_chain.provider_scope_digest
        or coverage.account_id != page_chain.account_id
        or coverage.qualification_id != page_chain.qualification_id
        or coverage.absence_semantics_digest != absence_semantics.content_digest
        or coverage.surface != page_chain.surface
        or coverage.endpoint != page_chain.endpoint
        or coverage.data_entitlement != page_chain.data_entitlement
        or coverage.query_scope_rule_id != page_chain.query_scope_rule_id
        or coverage.pagination_rule_id != page_chain.pagination_rule_id
        or coverage.query_scope_rule_id != rule["query_scope_rule_id"]
        or coverage.pagination_rule_id != rule["pagination_rule_id"]
        or coverage.retention_rule_id != rule["retention_rule_id"]
        or coverage.consistency_horizon_rule_id
        != rule["consistency_horizon_rule_id"]
    ):
        raise ProviderAccountCurrentnessError(
            "surface coverage no longer matches exact current source authority"
        )
    return coverage
