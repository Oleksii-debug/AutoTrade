"""Strict empty-result exclusion proof for WP-20 negative reconciliation.

This is the first bounded semantic-exclusion path above the exact required
surface coverage set.  It intentionally supports only the strongest trivial
provider result: every page on every required surface contains zero rows.

That conservative rule is sufficient to exclude a matching provider fact
without inventing a row-matching semantic for broad account activity results.
If any row exists on any surface, this issuer fails closed; a future qualified
row-matching rule may safely widen that case.

This module still does not assert consistency-horizon expiry, issue an accepted
account cut, produce PROVEN_ABSENT, or release UNKNOWN reservations.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import weakref

from .persistence import canonical_json
from .provider_account_absence_coverage import (
    HistoricalUnknownSubmissionBinding,
    ProviderAccountAbsenceCoverageError,
    require_historical_unknown_submission_authority,
)
from .provider_account_coverage_set import (
    ProviderAccountCoverageSetError,
    ProviderAccountRequiredSurfaceCoverageSet,
    require_current_provider_account_coverage_set_authority,
    require_provider_account_coverage_set_authority,
)
from .provider_account_page_chain import (
    ProviderAccountPageChain,
    ProviderAccountPageChainError,
    require_provider_account_page_chain_authority,
)

_SCHEMA_VERSION = "1.0.0"
_RULE_ID = "STRICT_EMPTY_PROVIDER_RESULT_SET_V1"
_REQUIRED_SURFACES = (
    "ACTIVITIES",
    "EXECUTIONS",
    "OPEN_ORDERS",
    "ORDER_HISTORY",
)


class ProviderAccountEmptyExclusionError(ValueError):
    """Strict four-surface empty-result exclusion authority is unavailable."""


def _at(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderAccountEmptyExclusionError(
            "at must be exact timezone-aware datetime"
        )
    return value


def _index_page_chains(
    page_chains: tuple[ProviderAccountPageChain, ...],
) -> dict[str, ProviderAccountPageChain]:
    if type(page_chains) is not tuple or any(
        type(item) is not ProviderAccountPageChain for item in page_chains
    ):
        raise TypeError(
            "page_chains must be an exact tuple of ProviderAccountPageChain"
        )
    if len(page_chains) != len(_REQUIRED_SURFACES):
        raise ProviderAccountEmptyExclusionError(
            "empty exclusion requires exactly four provider page chains"
        )
    indexed: dict[str, ProviderAccountPageChain] = {}
    for chain in page_chains:
        try:
            require_provider_account_page_chain_authority(chain)
        except ProviderAccountPageChainError as error:
            raise ProviderAccountEmptyExclusionError(
                "page-chain construction authority is unavailable"
            ) from error
        if chain.surface in indexed:
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion contains duplicate provider surface"
            )
        indexed[chain.surface] = chain
    if tuple(sorted(indexed)) != _REQUIRED_SURFACES:
        raise ProviderAccountEmptyExclusionError(
            "empty exclusion lacks one or more required provider surfaces"
        )
    return indexed


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountEmptyResultExclusionProof:
    """Sealed current proof that every required provider result set is empty."""

    provider_scope_digest: str
    account_id: str
    qualification_id: str
    acquisition_id: str
    acquisition_generation: int
    required_coverage_set_digest: str
    historical_submission_digest: str
    client_order_id: str
    exclusion_rule_id: str
    consistency_horizon_rule_id: str
    surfaces_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountEmptyExclusionError(
            "empty-result exclusion proof must come from canonical issuer"
        )

    @property
    def surfaces(self) -> tuple[dict[str, object], ...]:
        require_provider_account_empty_exclusion_authority(self)
        value = json.loads(self.surfaces_json)
        if type(value) is not list:
            raise ProviderAccountEmptyExclusionError(
                "empty-result exclusion state is non-canonical"
            )
        return tuple(dict(item) for item in value)

    def payload(self) -> dict[str, object]:
        require_provider_account_empty_exclusion_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "qualification_id": self.qualification_id,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "required_coverage_set_digest": self.required_coverage_set_digest,
            "historical_submission_digest": self.historical_submission_digest,
            "client_order_id": self.client_order_id,
            "exclusion_rule_id": self.exclusion_rule_id,
            "consistency_horizon_rule_id": self.consistency_horizon_rule_id,
            "surfaces": json.loads(self.surfaces_json),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-empty-exclusion:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _validate_material(
    *,
    coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
    page_chains: tuple[ProviderAccountPageChain, ...],
    historical_submission: HistoricalUnknownSubmissionBinding,
    at: datetime,
) -> str:
    if type(coverage_set) is not ProviderAccountRequiredSurfaceCoverageSet:
        raise TypeError(
            "coverage_set must be exact ProviderAccountRequiredSurfaceCoverageSet"
        )
    if type(historical_submission) is not HistoricalUnknownSubmissionBinding:
        raise TypeError(
            "historical_submission must be exact HistoricalUnknownSubmissionBinding"
        )
    point = _at(at)
    try:
        require_current_provider_account_coverage_set_authority(
            coverage_set,
            at=point,
        )
        require_historical_unknown_submission_authority(historical_submission)
    except (ProviderAccountCoverageSetError, ProviderAccountAbsenceCoverageError) as error:
        raise ProviderAccountEmptyExclusionError(
            "empty exclusion source authority is not exact current authority"
        ) from error
    if coverage_set.historical_submission_digest != historical_submission.content_digest:
        raise ProviderAccountEmptyExclusionError(
            "coverage set does not bind the exact searched historical submission"
        )
    indexed = _index_page_chains(page_chains)
    coverage_components = {
        item["surface"]: item for item in coverage_set.surfaces
    }
    if tuple(sorted(coverage_components)) != _REQUIRED_SURFACES:
        raise ProviderAccountEmptyExclusionError(
            "coverage set component surface set is non-canonical"
        )

    material: list[dict[str, object]] = []
    for surface in _REQUIRED_SURFACES:
        chain = indexed[surface]
        component = coverage_components[surface]
        if (
            chain.content_digest != component.get("page_chain_digest")
            or chain.provider_scope_digest != coverage_set.provider_scope_digest
            or chain.account_id != coverage_set.account_id
            or chain.qualification_id != coverage_set.qualification_id
            or chain.acquisition_id != coverage_set.acquisition_id
            or chain.acquisition_generation != coverage_set.acquisition_generation
        ):
            raise ProviderAccountEmptyExclusionError(
                "page chain does not match exact required coverage component"
            )
        pages = chain.pages
        if not pages:
            raise ProviderAccountEmptyExclusionError(
                "required provider surface has no sealed page evidence"
            )
        total_rows = 0
        for page in pages:
            count = page.get("item_count")
            if type(count) is not int or count < 0:
                raise ProviderAccountEmptyExclusionError(
                    "provider page row cardinality is non-canonical"
                )
            total_rows += count
        if total_rows != 0:
            raise ProviderAccountEmptyExclusionError(
                "strict empty-result exclusion cannot admit non-empty provider rows"
            )
        material.append(
            {
                "surface": surface,
                "page_chain_digest": chain.content_digest,
                "page_count": len(pages),
                "total_rows": total_rows,
            }
        )
    return canonical_json(material)


def _install_empty_exclusion_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            weakref.ReferenceType,
            tuple[weakref.ReferenceType, ...],
            weakref.ReferenceType,
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "qualification_id",
        "acquisition_id",
        "acquisition_generation",
        "required_coverage_set_digest",
        "historical_submission_digest",
        "client_order_id",
        "exclusion_rule_id",
        "consistency_horizon_rule_id",
        "surfaces_json",
    )

    def material(value: ProviderAccountEmptyResultExclusionProof) -> tuple[object, ...]:
        if type(value) is not ProviderAccountEmptyResultExclusionProof:
            raise ProviderAccountEmptyExclusionError(
                "exact ProviderAccountEmptyResultExclusionProof is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountEmptyResultExclusionProof,
        *,
        coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
        page_chains: tuple[ProviderAccountPageChain, ...],
        historical_submission: HistoricalUnknownSubmissionBinding,
    ) -> None:
        prune()
        if id(value) in states and states[id(value)][0]() is not None:
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion authority identity collision"
            )
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            weakref.ref(coverage_set),
            tuple(weakref.ref(chain) for chain in page_chains),
            weakref.ref(historical_submission),
        )

    def require(
        value: ProviderAccountEmptyResultExclusionProof,
    ) -> ProviderAccountEmptyResultExclusionProof:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion proof changed after issuance"
            )
        return value

    def require_current(
        value: ProviderAccountEmptyResultExclusionProof,
        *,
        at: datetime,
    ) -> ProviderAccountEmptyResultExclusionProof:
        accepted = require(value)
        state = states.get(id(accepted))
        assert state is not None
        coverage_set = state[2]()
        page_chains = tuple(ref() for ref in state[3])
        historical_submission = state[4]()
        if (
            coverage_set is None
            or historical_submission is None
            or any(chain is None for chain in page_chains)
        ):
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion source authority is unavailable"
            )
        rebuilt = _validate_material(
            coverage_set=coverage_set,
            page_chains=page_chains,  # type: ignore[arg-type]
            historical_submission=historical_submission,
            at=_at(at),
        )
        if rebuilt != accepted.surfaces_json:
            raise ProviderAccountEmptyExclusionError(
                "empty exclusion proof no longer matches current sources"
            )
        return accepted

    return register, require, require_current


(
    _register_provider_account_empty_exclusion_authority,
    require_provider_account_empty_exclusion_authority,
    require_current_provider_account_empty_exclusion_authority,
) = _install_empty_exclusion_authority()
del _install_empty_exclusion_authority


def issue_provider_account_empty_result_exclusion(
    *,
    coverage_set: ProviderAccountRequiredSurfaceCoverageSet,
    page_chains: tuple[ProviderAccountPageChain, ...],
    historical_submission: HistoricalUnknownSubmissionBinding,
    at: datetime,
) -> ProviderAccountEmptyResultExclusionProof:
    """Issue conservative four-surface fact exclusion from zero provider rows."""

    point = _at(at)
    surfaces_json = _validate_material(
        coverage_set=coverage_set,
        page_chains=page_chains,
        historical_submission=historical_submission,
        at=point,
    )
    value = object.__new__(ProviderAccountEmptyResultExclusionProof)
    fields = {
        "provider_scope_digest": coverage_set.provider_scope_digest,
        "account_id": coverage_set.account_id,
        "qualification_id": coverage_set.qualification_id,
        "acquisition_id": coverage_set.acquisition_id,
        "acquisition_generation": coverage_set.acquisition_generation,
        "required_coverage_set_digest": coverage_set.content_digest,
        "historical_submission_digest": historical_submission.content_digest,
        "client_order_id": historical_submission.client_order_id,
        "exclusion_rule_id": _RULE_ID,
        "consistency_horizon_rule_id": coverage_set.consistency_horizon_rule_id,
        "surfaces_json": surfaces_json,
    }
    for name, item in fields.items():
        object.__setattr__(value, name, item)
    _register_provider_account_empty_exclusion_authority(
        value,
        coverage_set=coverage_set,
        page_chains=page_chains,
        historical_submission=historical_submission,
    )
    return value
