"""Canonical all-surface provider-account absence coverage bundle.

This WP-20/#697 layer composes the four independently issued
ProviderAccountSurfaceCoverage authorities for one historical possible send and
cross-binds each coverage to its exact ProviderAccountPageChain.  The four
chains must come from one serialized provider-account acquisition generation.

It deliberately does not prove that the qualified consistency horizon elapsed,
does not inspect provider rows, and cannot issue PROVEN_ABSENT or release an
UNKNOWN reservation.  Those remain later account-cut/negative-proof work.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import weakref

from .persistence import canonical_json
from .provider_account_absence_coverage import (
    ProviderAccountAbsenceCoverageError,
    ProviderAccountSurfaceCoverage,
    require_provider_account_surface_coverage_authority,
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
_COVERAGE_SHARED_FIELDS = (
    "provider_scope_digest",
    "account_id",
    "qualification_id",
    "absence_semantics_digest",
    "historical_submission_digest",
    "query_scope_rule_id",
    "pagination_rule_id",
    "consistency_horizon_rule_id",
)
_CHAIN_SHARED_WITH_COVERAGE = (
    "provider_scope_digest",
    "account_id",
    "qualification_id",
    "absence_semantics_digest",
    "query_scope_rule_id",
    "pagination_rule_id",
)


class ProviderAccountAbsenceCoverageSetError(ValueError):
    """The four-surface coverage graph is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountAbsenceCoverageSet:
    """Sealed exact four-surface coverage graph for one historical UNKNOWN."""

    provider_scope_digest: str
    account_id: str
    acquisition_id: str
    acquisition_generation: int
    qualification_id: str
    absence_semantics_digest: str
    historical_submission_digest: str
    query_scope_rule_id: str
    pagination_rule_id: str
    consistency_horizon_rule_id: str
    coverages_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountAbsenceCoverageSetError(
            "coverage set must come from canonical all-surface issuer"
        )

    @property
    def coverages(self) -> tuple[dict[str, object], ...]:
        require_provider_account_absence_coverage_set_authority(self)
        try:
            raw = json.loads(self.coverages_json)
        except (json.JSONDecodeError, RecursionError) as error:
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set serialization is malformed"
            ) from error
        if (
            type(raw) is not list
            or len(raw) != len(_REQUIRED_SURFACES)
            or any(type(item) is not dict for item in raw)
        ):
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set serialization is non-canonical"
            )
        return tuple(dict(item) for item in raw)

    def payload(self) -> dict[str, object]:
        require_provider_account_absence_coverage_set_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "qualification_id": self.qualification_id,
            "absence_semantics_digest": self.absence_semantics_digest,
            "historical_submission_digest": self.historical_submission_digest,
            "query_scope_rule_id": self.query_scope_rule_id,
            "pagination_rule_id": self.pagination_rule_id,
            "consistency_horizon_rule_id": self.consistency_horizon_rule_id,
            "coverages": json.loads(self.coverages_json),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-absence-coverage-set:sha256:" + sha256(
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
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "acquisition_id",
        "acquisition_generation",
        "qualification_id",
        "absence_semantics_digest",
        "historical_submission_digest",
        "query_scope_rule_id",
        "pagination_rule_id",
        "consistency_horizon_rule_id",
        "coverages_json",
    )

    def material(value: ProviderAccountAbsenceCoverageSet) -> tuple[object, ...]:
        if type(value) is not ProviderAccountAbsenceCoverageSet:
            raise ProviderAccountAbsenceCoverageSetError(
                "exact ProviderAccountAbsenceCoverageSet is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountAbsenceCoverageSet,
        coverages: tuple[ProviderAccountSurfaceCoverage, ...],
        page_chains: tuple[ProviderAccountPageChain, ...],
    ) -> None:
        prune()
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            tuple(weakref.ref(item) for item in coverages),
            tuple(weakref.ref(item) for item in page_chains),
        )

    def require(
        value: ProviderAccountAbsenceCoverageSet,
    ) -> ProviderAccountAbsenceCoverageSet:
        current = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set construction authority is unavailable"
            )
        if state[1] != current:
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set changed after issuance"
            )
        coverage_refs, chain_refs = state[2], state[3]
        if (
            len(coverage_refs) != len(_REQUIRED_SURFACES)
            or len(chain_refs) != len(_REQUIRED_SURFACES)
        ):
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set source authority is incomplete"
            )
        for source_ref in coverage_refs:
            source = source_ref()
            if source is None:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source coverage authority is unavailable"
                )
            try:
                require_provider_account_surface_coverage_authority(source)
            except ProviderAccountAbsenceCoverageError as error:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source coverage authority changed"
                ) from error
        for source_ref in chain_refs:
            source = source_ref()
            if source is None:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source page-chain authority is unavailable"
                )
            try:
                require_provider_account_page_chain_authority(source)
            except ProviderAccountPageChainError as error:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source page-chain authority changed"
                ) from error
        return value

    return register, require


(
    _register_provider_account_absence_coverage_set_authority,
    require_provider_account_absence_coverage_set_authority,
) = _install_coverage_set_authority()
del _install_coverage_set_authority


def issue_provider_account_absence_coverage_set(
    *,
    coverages: tuple[ProviderAccountSurfaceCoverage, ...],
    page_chains: tuple[ProviderAccountPageChain, ...],
) -> ProviderAccountAbsenceCoverageSet:
    """Compose exactly four source-bound surface coverages, never a verdict."""

    if type(coverages) is not tuple or type(page_chains) is not tuple:
        raise ProviderAccountAbsenceCoverageSetError(
            "coverages and page_chains must be exact tuples"
        )
    if (
        len(coverages) != len(_REQUIRED_SURFACES)
        or len(page_chains) != len(_REQUIRED_SURFACES)
    ):
        raise ProviderAccountAbsenceCoverageSetError(
            "coverage set requires exactly four provider surfaces"
        )

    by_surface: dict[str, ProviderAccountSurfaceCoverage] = {}
    for coverage in coverages:
        if type(coverage) is not ProviderAccountSurfaceCoverage:
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set requires exact ProviderAccountSurfaceCoverage values"
            )
        try:
            accepted = require_provider_account_surface_coverage_authority(coverage)
        except ProviderAccountAbsenceCoverageError as error:
            raise ProviderAccountAbsenceCoverageSetError(
                "surface coverage authority is unavailable"
            ) from error
        surface = accepted.surface
        if surface in by_surface:
            raise ProviderAccountAbsenceCoverageSetError(
                f"duplicate provider surface coverage: {surface}"
            )
        by_surface[surface] = accepted

    chains_by_surface: dict[str, ProviderAccountPageChain] = {}
    for page_chain in page_chains:
        if type(page_chain) is not ProviderAccountPageChain:
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set requires exact ProviderAccountPageChain values"
            )
        try:
            accepted_chain = require_provider_account_page_chain_authority(page_chain)
        except ProviderAccountPageChainError as error:
            raise ProviderAccountAbsenceCoverageSetError(
                "page-chain authority is unavailable"
            ) from error
        surface = accepted_chain.surface
        if surface in chains_by_surface:
            raise ProviderAccountAbsenceCoverageSetError(
                f"duplicate provider surface page chain: {surface}"
            )
        chains_by_surface[surface] = accepted_chain

    actual_surfaces = tuple(sorted(by_surface))
    actual_chain_surfaces = tuple(sorted(chains_by_surface))
    if (
        actual_surfaces != _REQUIRED_SURFACES
        or actual_chain_surfaces != _REQUIRED_SURFACES
    ):
        raise ProviderAccountAbsenceCoverageSetError(
            "coverage set surface universe mismatch"
        )

    reference = by_surface[_REQUIRED_SURFACES[0]]
    for field in _COVERAGE_SHARED_FIELDS:
        expected = getattr(reference, field)
        for surface in _REQUIRED_SURFACES[1:]:
            if getattr(by_surface[surface], field) != expected:
                raise ProviderAccountAbsenceCoverageSetError(
                    f"coverage set {field} differs across provider surfaces"
                )

    reference_chain = chains_by_surface[_REQUIRED_SURFACES[0]]
    acquisition_id = reference_chain.acquisition_id
    acquisition_generation = reference_chain.acquisition_generation
    for surface in _REQUIRED_SURFACES:
        coverage = by_surface[surface]
        page_chain = chains_by_surface[surface]
        if coverage.page_chain_digest != page_chain.content_digest:
            raise ProviderAccountAbsenceCoverageSetError(
                f"coverage set page-chain binding mismatch for {surface}"
            )
        for field in _CHAIN_SHARED_WITH_COVERAGE:
            if getattr(coverage, field) != getattr(page_chain, field):
                raise ProviderAccountAbsenceCoverageSetError(
                    f"coverage/page-chain {field} mismatch for {surface}"
                )
        if (
            coverage.endpoint != page_chain.endpoint
            or coverage.data_entitlement != page_chain.data_entitlement
        ):
            raise ProviderAccountAbsenceCoverageSetError(
                f"coverage/page-chain endpoint scope mismatch for {surface}"
            )
        if (
            page_chain.acquisition_id != acquisition_id
            or page_chain.acquisition_generation != acquisition_generation
        ):
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set acquisition generation differs across provider surfaces"
            )

    entries: list[dict[str, object]] = []
    ordered_coverages: list[ProviderAccountSurfaceCoverage] = []
    ordered_chains: list[ProviderAccountPageChain] = []
    for surface in _REQUIRED_SURFACES:
        coverage = by_surface[surface]
        page_chain = chains_by_surface[surface]
        ordered_coverages.append(coverage)
        ordered_chains.append(page_chain)
        entries.append(
            {
                "surface": surface,
                "coverage_digest": coverage.content_digest,
                "page_chain_digest": page_chain.content_digest,
                "origin_set_digest": page_chain.origin_set_digest,
                "endpoint": coverage.endpoint,
                "data_entitlement": coverage.data_entitlement,
                "retention_rule_id": coverage.retention_rule_id,
                "search_binding": coverage.search_binding,
                "coverage_start": coverage.coverage_start,
                "coverage_end": coverage.coverage_end,
                "page_count": coverage.page_count,
                "latest_provider_observed_at": coverage.latest_provider_observed_at,
            }
        )
    coverages_json = canonical_json(entries)

    value = object.__new__(ProviderAccountAbsenceCoverageSet)
    for field in _COVERAGE_SHARED_FIELDS:
        object.__setattr__(value, field, getattr(reference, field))
    object.__setattr__(value, "acquisition_id", acquisition_id)
    object.__setattr__(value, "acquisition_generation", acquisition_generation)
    object.__setattr__(value, "coverages_json", coverages_json)
    _register_provider_account_absence_coverage_set_authority(
        value,
        tuple(ordered_coverages),
        tuple(ordered_chains),
    )
    return value
