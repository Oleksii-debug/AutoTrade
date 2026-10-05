"""Exact four-surface searched-coverage composition for WP-20.

This layer proves only that all provider surfaces required by the currently
qualified negative-proof contract have issuer-protected coverage for the same
current acquisition/Q and the same durable UNKNOWN submission.  It does not
inspect provider facts for semantic absence and it does not assert that any
consistency horizon elapsed.

The purpose is to remove another caller-authored ``coverage complete`` input
before the later canonical account-cut/PROVEN_ABSENT issuer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import canonical_json
from .provider_account_absence_coverage import (
    HistoricalUnknownSubmissionBinding,
    ProviderAccountAbsenceCoverageError,
    ProviderAccountSurfaceCoverage,
    require_historical_unknown_submission_authority,
    require_provider_account_surface_coverage_authority,
)
from .provider_account_absence_semantics import (
    QualifiedProviderAccountAbsenceSemantics,
)
from .provider_account_currentness import (
    ProviderAccountCurrentnessError,
    require_current_provider_account_surface_coverage_consumption,
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

_SCHEMA_VERSION = "1.0.0"
_REQUIRED_SURFACES = (
    "ACTIVITIES",
    "EXECUTIONS",
    "OPEN_ORDERS",
    "ORDER_HISTORY",
)


class ProviderAccountCoverageSetError(ValueError):
    """The exact current four-surface provider coverage set is unavailable."""


def _at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderAccountCoverageSetError(
            "at must be exact timezone-aware datetime"
        )
    return value


def _index_exact_sources(
    coverages: tuple[ProviderAccountSurfaceCoverage, ...],
    page_chains: tuple[ProviderAccountPageChain, ...],
) -> tuple[
    dict[str, ProviderAccountSurfaceCoverage],
    dict[str, ProviderAccountPageChain],
]:
    if type(coverages) is not tuple or any(
        type(item) is not ProviderAccountSurfaceCoverage for item in coverages
    ):
        raise TypeError(
            "coverages must be an exact tuple of ProviderAccountSurfaceCoverage"
        )
    if type(page_chains) is not tuple or any(
        type(item) is not ProviderAccountPageChain for item in page_chains
    ):
        raise TypeError(
            "page_chains must be an exact tuple of ProviderAccountPageChain"
        )
    if len(coverages) != len(_REQUIRED_SURFACES) or len(page_chains) != len(
        _REQUIRED_SURFACES
    ):
        raise ProviderAccountCoverageSetError(
            "coverage set requires exactly the four qualified provider surfaces"
        )
    coverage_by_surface: dict[str, ProviderAccountSurfaceCoverage] = {}
    chain_by_surface: dict[str, ProviderAccountPageChain] = {}
    for value in coverages:
        require_provider_account_surface_coverage_authority(value)
        if value.surface in coverage_by_surface:
            raise ProviderAccountCoverageSetError(
                "coverage set contains duplicate provider surface"
            )
        coverage_by_surface[value.surface] = value
    for value in page_chains:
        require_provider_account_page_chain_authority(value)
        if value.surface in chain_by_surface:
            raise ProviderAccountCoverageSetError(
                "coverage set contains duplicate page-chain surface"
            )
        chain_by_surface[value.surface] = value
    if tuple(sorted(coverage_by_surface)) != _REQUIRED_SURFACES:
        raise ProviderAccountCoverageSetError(
            "coverage set does not contain the exact required provider surfaces"
        )
    if tuple(sorted(chain_by_surface)) != _REQUIRED_SURFACES:
        raise ProviderAccountCoverageSetError(
            "page-chain set does not contain the exact required provider surfaces"
        )
    return coverage_by_surface, chain_by_surface


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountRequiredSurfaceCoverageSet:
    """Sealed exact-current four-surface searched coverage composition."""

    provider_scope_digest: str
    account_id: str
    qualification_id: str
    acquisition_id: str
    acquisition_generation: int
    origin_set_digest: str
    absence_semantics_digest: str
    historical_submission_digest: str
    consistency_horizon_rule_id: str
    surfaces_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountCoverageSetError(
            "required surface coverage set must come from canonical issuer"
        )

    @property
    def surfaces(self) -> tuple[dict[str, object], ...]:
        require_provider_account_coverage_set_authority(self)
        decoded = json.loads(self.surfaces_json)
        if type(decoded) is not list:
            raise ProviderAccountCoverageSetError(
                "required surface coverage state is non-canonical"
            )
        return tuple(dict(item) for item in decoded)

    def payload(self) -> dict[str, object]:
        require_provider_account_coverage_set_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "qualification_id": self.qualification_id,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "origin_set_digest": self.origin_set_digest,
            "absence_semantics_digest": self.absence_semantics_digest,
            "historical_submission_digest": self.historical_submission_digest,
            "consistency_horizon_rule_id": self.consistency_horizon_rule_id,
            "surfaces": json.loads(self.surfaces_json),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-required-coverage-set:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_coverage_set_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            tuple[weakref.ReferenceType, ...],
            tuple[weakref.ReferenceType, ...],
            weakref.ReferenceType,
            weakref.ReferenceType,
            weakref.ReferenceType,
            DurableProviderQualificationRegistry,
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "qualification_id",
        "acquisition_id",
        "acquisition_generation",
        "origin_set_digest",
        "absence_semantics_digest",
        "historical_submission_digest",
        "consistency_horizon_rule_id",
        "surfaces_json",
    )

    def material(
        value: ProviderAccountRequiredSurfaceCoverageSet,
    ) -> tuple[object, ...]:
        if type(value) is not ProviderAccountRequiredSurfaceCoverageSet:
            raise ProviderAccountCoverageSetError(
                "exact ProviderAccountRequiredSurfaceCoverageSet is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountRequiredSurfaceCoverageSet,
        *,
        coverages: tuple[ProviderAccountSurfaceCoverage, ...],
        page_chains: tuple[ProviderAccountPageChain, ...],
        origin_set: ProviderAccountOriginBindingSet,
        absence_semantics: QualifiedProviderAccountAbsenceSemantics,
        historical_submission: HistoricalUnknownSubmissionBinding,
        qualification_registry: DurableProviderQualificationRegistry,
    ) -> None:
        prune()
        if id(value) in states and states[id(value)][0]() is not None:
            raise ProviderAccountCoverageSetError(
                "required surface coverage authority identity collision"
            )
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            tuple(weakref.ref(item) for item in coverages),
            tuple(weakref.ref(item) for item in page_chains),
            weakref.ref(origin_set),
            weakref.ref(absence_semantics),
            weakref.ref(historical_submission),
            qualification_registry,
        )

    def require(
        value: ProviderAccountRequiredSurfaceCoverageSet,
    ) -> ProviderAccountRequiredSurfaceCoverageSet:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountCoverageSetError(
                "required surface coverage construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderAccountCoverageSetError(
                "required surface coverage changed after issuance"
            )
        return value

    def require_current(
        value: ProviderAccountRequiredSurfaceCoverageSet,
        *,
        at: datetime,
    ) -> ProviderAccountRequiredSurfaceCoverageSet:
        accepted = require(value)
        point = _at(at)
        state = states.get(id(accepted))
        assert state is not None
        coverages = tuple(ref() for ref in state[2])
        page_chains = tuple(ref() for ref in state[3])
        origin_set = state[4]()
        absence_semantics = state[5]()
        historical_submission = state[6]()
        qualification_registry = state[7]
        if (
            any(item is None for item in coverages)
            or any(item is None for item in page_chains)
            or origin_set is None
            or absence_semantics is None
            or historical_submission is None
        ):
            raise ProviderAccountCoverageSetError(
                "required surface coverage source authority is unavailable"
            )
        try:
            rebuilt = _validate_and_materialize(
                coverages=coverages,  # type: ignore[arg-type]
                page_chains=page_chains,  # type: ignore[arg-type]
                origin_set=origin_set,
                absence_semantics=absence_semantics,
                historical_submission=historical_submission,
                qualification_registry=qualification_registry,
                at=point,
            )
        except (
            ProviderAccountCoverageSetError,
            ProviderAccountCurrentnessError,
            ProviderAccountOriginSetError,
            ProviderAccountPageChainError,
            ProviderAccountAbsenceCoverageError,
        ) as error:
            raise ProviderAccountCoverageSetError(
                "required surface coverage is not exact current authority"
            ) from error
        if rebuilt != accepted.surfaces_json:
            raise ProviderAccountCoverageSetError(
                "required surface coverage no longer matches current sources"
            )
        return accepted

    return register, require, require_current


(
    _register_provider_account_coverage_set_authority,
    require_provider_account_coverage_set_authority,
    require_current_provider_account_coverage_set_authority,
) = _install_coverage_set_authority()
del _install_coverage_set_authority


def _validate_and_materialize(
    *,
    coverages: tuple[ProviderAccountSurfaceCoverage, ...],
    page_chains: tuple[ProviderAccountPageChain, ...],
    origin_set: ProviderAccountOriginBindingSet,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    historical_submission: HistoricalUnknownSubmissionBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> str:
    if type(origin_set) is not ProviderAccountOriginBindingSet:
        raise TypeError("origin_set must be exact ProviderAccountOriginBindingSet")
    if type(absence_semantics) is not QualifiedProviderAccountAbsenceSemantics:
        raise TypeError(
            "absence_semantics must be exact QualifiedProviderAccountAbsenceSemantics"
        )
    if type(historical_submission) is not HistoricalUnknownSubmissionBinding:
        raise TypeError(
            "historical_submission must be exact HistoricalUnknownSubmissionBinding"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    point = _at(at)
    try:
        require_current_provider_account_origin_set_authority(origin_set, at=point)
        require_historical_unknown_submission_authority(historical_submission)
    except (ProviderAccountOriginSetError, ProviderAccountAbsenceCoverageError) as error:
        raise ProviderAccountCoverageSetError(
            "coverage set source authority is not current"
        ) from error
    coverage_by_surface, chain_by_surface = _index_exact_sources(
        coverages,
        page_chains,
    )
    material: list[dict[str, object]] = []
    horizon_rule: str | None = None
    for surface in _REQUIRED_SURFACES:
        coverage = coverage_by_surface[surface]
        page_chain = chain_by_surface[surface]
        require_current_provider_account_surface_coverage_consumption(
            coverage=coverage,
            page_chain=page_chain,
            origin_set=origin_set,
            absence_semantics=absence_semantics,
            historical_submission=historical_submission,
            qualification_registry=qualification_registry,
            at=point,
        )
        if horizon_rule is None:
            horizon_rule = coverage.consistency_horizon_rule_id
        elif coverage.consistency_horizon_rule_id != horizon_rule:
            raise ProviderAccountCoverageSetError(
                "required surfaces do not share one qualified consistency-horizon rule"
            )
        material.append(
            {
                "surface": surface,
                "coverage_digest": coverage.content_digest,
                "page_chain_digest": page_chain.content_digest,
                "search_binding": coverage.search_binding,
                "coverage_start": coverage.coverage_start,
                "coverage_end": coverage.coverage_end,
                "retention_rule_id": coverage.retention_rule_id,
                "consistency_horizon_rule_id": coverage.consistency_horizon_rule_id,
                "page_count": coverage.page_count,
                "latest_provider_observed_at": coverage.latest_provider_observed_at,
            }
        )
    assert horizon_rule is not None
    return canonical_json(material)


def issue_provider_account_required_surface_coverage_set(
    *,
    coverages: tuple[ProviderAccountSurfaceCoverage, ...],
    page_chains: tuple[ProviderAccountPageChain, ...],
    origin_set: ProviderAccountOriginBindingSet,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    historical_submission: HistoricalUnknownSubmissionBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> ProviderAccountRequiredSurfaceCoverageSet:
    """Issue exact four-surface coverage completeness without caller booleans."""

    point = _at(at)
    surfaces_json = _validate_and_materialize(
        coverages=coverages,
        page_chains=page_chains,
        origin_set=origin_set,
        absence_semantics=absence_semantics,
        historical_submission=historical_submission,
        qualification_registry=qualification_registry,
        at=point,
    )
    decoded = json.loads(surfaces_json)
    horizon_rules = {item["consistency_horizon_rule_id"] for item in decoded}
    if len(horizon_rules) != 1:
        raise ProviderAccountCoverageSetError(
            "required surfaces do not share one consistency-horizon rule"
        )
    value = object.__new__(ProviderAccountRequiredSurfaceCoverageSet)
    material = {
        "provider_scope_digest": origin_set.provider_scope_digest,
        "account_id": origin_set.account_id,
        "qualification_id": origin_set.qualification_id,
        "acquisition_id": origin_set.acquisition_id,
        "acquisition_generation": origin_set.acquisition_generation,
        "origin_set_digest": origin_set.content_digest,
        "absence_semantics_digest": absence_semantics.content_digest,
        "historical_submission_digest": historical_submission.content_digest,
        "consistency_horizon_rule_id": next(iter(horizon_rules)),
        "surfaces_json": surfaces_json,
    }
    for name, item in material.items():
        object.__setattr__(value, name, item)
    _register_provider_account_coverage_set_authority(
        value,
        coverages=coverages,
        page_chains=page_chains,
        origin_set=origin_set,
        absence_semantics=absence_semantics,
        historical_submission=historical_submission,
        qualification_registry=qualification_registry,
    )
    return value
