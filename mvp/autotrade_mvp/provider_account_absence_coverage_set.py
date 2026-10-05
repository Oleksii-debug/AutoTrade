"""Canonical all-surface provider-account absence coverage bundle.

This WP-20/#697 layer composes the four independently issued
ProviderAccountSurfaceCoverage authorities for one historical possible send.
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

_SCHEMA_VERSION = "1.0.0"
_REQUIRED_SURFACES = (
    "ACTIVITIES",
    "EXECUTIONS",
    "OPEN_ORDERS",
    "ORDER_HISTORY",
)
_SHARED_FIELDS = (
    "provider_scope_digest",
    "account_id",
    "qualification_id",
    "absence_semantics_digest",
    "historical_submission_digest",
    "query_scope_rule_id",
    "pagination_rule_id",
    "consistency_horizon_rule_id",
)


class ProviderAccountAbsenceCoverageSetError(ValueError):
    """The four-surface coverage graph is incomplete or internally inconsistent."""


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountAbsenceCoverageSet:
    """Sealed exact four-surface coverage graph for one historical UNKNOWN."""

    provider_scope_digest: str
    account_id: str
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
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
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
    ) -> None:
        prune()
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            tuple(weakref.ref(item) for item in coverages),
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
        source_refs = state[2]
        if len(source_refs) != len(_REQUIRED_SURFACES):
            raise ProviderAccountAbsenceCoverageSetError(
                "coverage set source authority is incomplete"
            )
        for source_ref in source_refs:
            source = source_ref()
            if source is None:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source authority is unavailable"
                )
            try:
                require_provider_account_surface_coverage_authority(source)
            except ProviderAccountAbsenceCoverageError as error:
                raise ProviderAccountAbsenceCoverageSetError(
                    "coverage set source authority changed"
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
) -> ProviderAccountAbsenceCoverageSet:
    """Compose exactly four issuer-protected surface coverages, never a verdict."""

    if type(coverages) is not tuple:
        raise ProviderAccountAbsenceCoverageSetError(
            "coverages must be an exact tuple"
        )
    if len(coverages) != len(_REQUIRED_SURFACES):
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

    actual_surfaces = tuple(sorted(by_surface))
    if actual_surfaces != _REQUIRED_SURFACES:
        missing = sorted(set(_REQUIRED_SURFACES) - set(actual_surfaces))
        extra = sorted(set(actual_surfaces) - set(_REQUIRED_SURFACES))
        raise ProviderAccountAbsenceCoverageSetError(
            "coverage set surface universe mismatch"
            f"; missing={','.join(missing) or '-'}"
            f"; extra={','.join(extra) or '-'}"
        )

    reference = by_surface[_REQUIRED_SURFACES[0]]
    for field in _SHARED_FIELDS:
        expected = getattr(reference, field)
        for surface in _REQUIRED_SURFACES[1:]:
            if getattr(by_surface[surface], field) != expected:
                raise ProviderAccountAbsenceCoverageSetError(
                    f"coverage set {field} differs across provider surfaces"
                )

    entries: list[dict[str, object]] = []
    ordered_sources: list[ProviderAccountSurfaceCoverage] = []
    for surface in _REQUIRED_SURFACES:
        coverage = by_surface[surface]
        ordered_sources.append(coverage)
        entries.append(
            {
                "surface": surface,
                "coverage_digest": coverage.content_digest,
                "page_chain_digest": coverage.page_chain_digest,
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
    for field in _SHARED_FIELDS:
        object.__setattr__(value, field, getattr(reference, field))
    object.__setattr__(value, "coverages_json", coverages_json)
    _register_provider_account_absence_coverage_set_authority(
        value,
        tuple(ordered_sources),
    )
    return value
