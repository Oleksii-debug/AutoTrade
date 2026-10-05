"""Exact provider-row facts for WP-20 searched account surfaces.

This module consumes the already issuer-protected page-chain and searched-window
coverage components and inspects the exact qualified provider observations that
created those pages. It answers only one bounded question: did the retained
provider result rows contain the durable UNKNOWN submission's exact
``orderLinkId``?

A ``NO_MATCHING_ROW_OBSERVED`` state is deliberately *not* PROVEN_ABSENT. It
carries no consistency-horizon authority and cannot release a reservation. A
later accepted account-cut issuer must compose all required surfaces, current
origin/acquisition authority, provider-Q semantics, and independent chronology.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import weakref

from .persistence import canonical_json
from .provider_account_absence_coverage import (
    HistoricalUnknownSubmissionBinding,
    ProviderAccountAbsenceCoverageError,
    ProviderAccountSurfaceCoverage,
    require_historical_unknown_submission_authority,
    require_provider_account_surface_coverage_authority,
)
from .provider_account_page_chain import (
    ProviderAccountPageChain,
    ProviderAccountPageChainError,
    require_provider_account_page_chain_authority,
)
from .provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginObservation,
    require_provider_origin_response_binding_authority,
)
from .provider_route_reads import (
    ProviderRouteReadError,
    _require_qualified_provider_response_authority,
)

_SCHEMA_VERSION = "1.0.0"
_DIRECT_TARGET_SURFACES = frozenset({"OPEN_ORDERS", "ORDER_HISTORY", "EXECUTIONS"})
_ACCOUNT_WINDOW_SURFACES = frozenset({"ACTIVITIES"})
_SUPPORTED_SURFACES = _DIRECT_TARGET_SURFACES | _ACCOUNT_WINDOW_SURFACES
_NO_MATCH = "NO_MATCHING_ROW_OBSERVED"
_MATCH = "MATCHING_ROW_OBSERVED"


class ProviderAccountSurfaceFactsError(ValueError):
    """Exact retained provider row facts are unavailable or inconsistent."""


def _source_authority(
    observation: ProviderOriginObservation,
) -> AuthenticatedReadResponseBinding:
    if type(observation) is not ProviderOriginObservation:
        raise TypeError("observations must contain exact ProviderOriginObservation")
    binding = observation.response_binding
    if type(binding) is not AuthenticatedReadResponseBinding:
        raise ProviderAccountSurfaceFactsError(
            "provider observation lacks exact durable response binding"
        )
    try:
        require_provider_origin_response_binding_authority(binding)
        _require_qualified_provider_response_authority(
            observation.qualified_observation
        )
    except (ProviderOriginError, ProviderRouteReadError) as error:
        raise ProviderAccountSurfaceFactsError(
            "provider observation construction authority is unavailable"
        ) from error
    if binding.execution_class != "DIRECT_PROVIDER_WIRE":
        raise ProviderAccountSurfaceFactsError(
            "provider row facts require DIRECT_PROVIDER_WIRE evidence"
        )
    return binding


def _result_rows(observation: ProviderOriginObservation) -> tuple[object, ...]:
    payload = observation.payload
    if not hasattr(payload, "get"):
        raise ProviderAccountSurfaceFactsError(
            "provider row payload lacks canonical success envelope"
        )
    ret_code = payload.get("retCode")
    if type(ret_code) is not int or ret_code != 0:
        raise ProviderAccountSurfaceFactsError(
            "provider row payload is not an exact successful result"
        )
    result = payload.get("result")
    if not hasattr(result, "get"):
        raise ProviderAccountSurfaceFactsError(
            "provider row result must be an exact object"
        )
    rows = result.get("list")
    if type(rows) is not tuple:
        raise ProviderAccountSurfaceFactsError(
            "provider row result list is unavailable"
        )
    if "nextPageCursor" not in result:
        raise ProviderAccountSurfaceFactsError(
            "provider row result lacks explicit cursor state"
        )
    cursor = result.get("nextPageCursor")
    if type(cursor) is not str:
        raise ProviderAccountSurfaceFactsError(
            "provider row response cursor is non-canonical"
        )
    return rows


def _query_material(observation: ProviderOriginObservation) -> dict[str, str]:
    query = observation.qualified_observation.query_binding.query_binding.query
    material: dict[str, str] = {}
    for key, value in query.items():
        if type(key) is not str or type(value) is not str:
            raise ProviderAccountSurfaceFactsError(
                "provider row query contains non-canonical material"
            )
        material[key] = value
    return material


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountSurfaceFactScan:
    """Sealed row-scan result for one exact searched provider surface."""

    provider_scope_digest: str
    account_id: str
    qualification_id: str
    surface: str
    endpoint: str
    coverage_digest: str
    page_chain_digest: str
    historical_submission_digest: str
    target_client_order_id: str
    scan_state: str
    scanned_page_count: int
    scanned_row_count: int
    matching_row_count: int
    matching_row_refs_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountSurfaceFactsError(
            "provider surface fact scan must come from canonical issuer"
        )

    @property
    def matching_row_refs(self) -> tuple[str, ...]:
        require_provider_account_surface_fact_scan_authority(self)
        value = json.loads(self.matching_row_refs_json)
        if type(value) is not list or any(type(item) is not str for item in value):
            raise ProviderAccountSurfaceFactsError(
                "provider surface matching-row state is non-canonical"
            )
        return tuple(value)

    def payload(self) -> dict[str, object]:
        require_provider_account_surface_fact_scan_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "qualification_id": self.qualification_id,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "coverage_digest": self.coverage_digest,
            "page_chain_digest": self.page_chain_digest,
            "historical_submission_digest": self.historical_submission_digest,
            "target_client_order_id": self.target_client_order_id,
            "scan_state": self.scan_state,
            "scanned_page_count": self.scanned_page_count,
            "scanned_row_count": self.scanned_row_count,
            "matching_row_count": self.matching_row_count,
            "matching_row_refs": list(self.matching_row_refs),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-surface-fact-scan:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_fact_scan_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            weakref.ReferenceType,
            weakref.ReferenceType,
            weakref.ReferenceType,
            tuple[weakref.ReferenceType, ...],
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "qualification_id",
        "surface",
        "endpoint",
        "coverage_digest",
        "page_chain_digest",
        "historical_submission_digest",
        "target_client_order_id",
        "scan_state",
        "scanned_page_count",
        "scanned_row_count",
        "matching_row_count",
        "matching_row_refs_json",
    )

    def material(value: ProviderAccountSurfaceFactScan) -> tuple[object, ...]:
        if type(value) is not ProviderAccountSurfaceFactScan:
            raise ProviderAccountSurfaceFactsError(
                "exact ProviderAccountSurfaceFactScan is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountSurfaceFactScan,
        coverage: ProviderAccountSurfaceCoverage,
        page_chain: ProviderAccountPageChain,
        historical: HistoricalUnknownSubmissionBinding,
        observations: tuple[ProviderOriginObservation, ...],
    ) -> None:
        prune()
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            weakref.ref(coverage),
            weakref.ref(page_chain),
            weakref.ref(historical),
            tuple(weakref.ref(item) for item in observations),
        )

    def require(value: ProviderAccountSurfaceFactScan) -> ProviderAccountSurfaceFactScan:
        current = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountSurfaceFactsError(
                "provider surface fact-scan construction authority is unavailable"
            )
        if state[1] != current:
            raise ProviderAccountSurfaceFactsError(
                "provider surface fact scan changed after issuance"
            )
        coverage = state[2]()
        page_chain = state[3]()
        historical = state[4]()
        observations = tuple(item() for item in state[5])
        if coverage is None or page_chain is None or historical is None or any(
            item is None for item in observations
        ):
            raise ProviderAccountSurfaceFactsError(
                "provider surface fact-scan source authority is unavailable"
            )
        try:
            require_provider_account_surface_coverage_authority(coverage)
            require_provider_account_page_chain_authority(page_chain)
            require_historical_unknown_submission_authority(historical)
            for item in observations:
                _source_authority(item)
        except (
            ProviderAccountAbsenceCoverageError,
            ProviderAccountPageChainError,
            ProviderAccountSurfaceFactsError,
        ) as error:
            raise ProviderAccountSurfaceFactsError(
                "provider surface fact-scan source authority changed"
            ) from error
        return value

    return register, require


(
    _register_provider_account_surface_fact_scan_authority,
    require_provider_account_surface_fact_scan_authority,
) = _install_fact_scan_authority()
del _install_fact_scan_authority


def issue_provider_account_surface_fact_scan(
    *,
    coverage: ProviderAccountSurfaceCoverage,
    page_chain: ProviderAccountPageChain,
    historical_submission: HistoricalUnknownSubmissionBinding,
    observations: tuple[ProviderOriginObservation, ...],
) -> ProviderAccountSurfaceFactScan:
    """Inspect exact page bytes for the durable UNKNOWN's exact client order id.

    No ``absent``/``present``/``complete`` boolean is accepted. Caller order of
    observations is ignored; exact page order and membership come from the
    issuer-protected page chain.
    """
    if type(coverage) is not ProviderAccountSurfaceCoverage:
        raise TypeError("coverage must be exact ProviderAccountSurfaceCoverage")
    if type(page_chain) is not ProviderAccountPageChain:
        raise TypeError("page_chain must be exact ProviderAccountPageChain")
    if type(historical_submission) is not HistoricalUnknownSubmissionBinding:
        raise TypeError(
            "historical_submission must be exact HistoricalUnknownSubmissionBinding"
        )
    if type(observations) is not tuple or not observations:
        raise ProviderAccountSurfaceFactsError(
            "observations must be a non-empty exact tuple"
        )
    try:
        require_provider_account_surface_coverage_authority(coverage)
        require_provider_account_page_chain_authority(page_chain)
        require_historical_unknown_submission_authority(historical_submission)
    except (
        ProviderAccountAbsenceCoverageError,
        ProviderAccountPageChainError,
    ) as error:
        raise ProviderAccountSurfaceFactsError(
            "provider surface fact-scan source authority is unavailable"
        ) from error

    if coverage.surface not in _SUPPORTED_SURFACES:
        raise ProviderAccountSurfaceFactsError(
            "provider surface has no executable row-fact implementation"
        )
    if (
        coverage.page_chain_digest != page_chain.content_digest
        or coverage.historical_submission_digest
        != historical_submission.content_digest
    ):
        raise ProviderAccountSurfaceFactsError(
            "provider row scan inputs do not share exact issued coverage"
        )
    if (
        coverage.surface != page_chain.surface
        or coverage.endpoint != page_chain.endpoint
        or coverage.account_id != page_chain.account_id
        or coverage.qualification_id != page_chain.qualification_id
        or coverage.provider_scope_digest != page_chain.provider_scope_digest
        or historical_submission.account_id != page_chain.account_id
    ):
        raise ProviderAccountSurfaceFactsError(
            "provider row scan inputs do not share exact account/Q/surface scope"
        )

    pages = page_chain.pages
    expected_by_ref = {page["origin_ref"]: page for page in pages}
    if len(expected_by_ref) != len(pages):
        raise ProviderAccountSurfaceFactsError(
            "provider page chain contains duplicate origin identity"
        )
    observed_by_ref: dict[str, ProviderOriginObservation] = {}
    for observation in observations:
        binding = _source_authority(observation)
        if binding.origin_ref in observed_by_ref:
            raise ProviderAccountSurfaceFactsError(
                "provider row scan contains duplicate origin observation"
            )
        observed_by_ref[binding.origin_ref] = observation
    if set(observed_by_ref) != set(expected_by_ref):
        raise ProviderAccountSurfaceFactsError(
            "provider row observations do not exactly cover page chain"
        )

    matching_refs: list[str] = []
    scanned_rows = 0
    target = historical_submission.client_order_id
    for page in pages:
        origin_ref = page["origin_ref"]
        observation = observed_by_ref[origin_ref]
        binding = _source_authority(observation)
        query = _query_material(observation)
        cursor = query.get("cursor")
        if (
            binding.account_id != page_chain.account_id
            or binding.qualification_id != page_chain.qualification_id
            or binding.endpoint != page_chain.endpoint
            or binding.qualified_query_digest != page["qualified_query_digest"]
            or binding.response_sha256 != page["response_sha256"]
            or binding.observed_at != page["observed_at"]
            or binding.journal_sequence != page["observed_journal_sequence"]
            or cursor != page["request_cursor"]
        ):
            raise ProviderAccountSurfaceFactsError(
                "provider row observation differs from exact page-chain identity"
            )
        rows = _result_rows(observation)
        result = observation.payload.get("result")
        if (
            result.get("nextPageCursor") != page["response_next_cursor"]
            or len(rows) != page["item_count"]
        ):
            raise ProviderAccountSurfaceFactsError(
                "provider row payload differs from exact page-chain result"
            )
        for row_index, row in enumerate(rows):
            scanned_rows += 1
            if not hasattr(row, "get") or "orderLinkId" not in row:
                raise ProviderAccountSurfaceFactsError(
                    "provider row lacks exact orderLinkId field"
                )
            order_link_id = row.get("orderLinkId")
            if type(order_link_id) is not str:
                raise ProviderAccountSurfaceFactsError(
                    "provider row orderLinkId is non-canonical"
                )
            if coverage.surface in _DIRECT_TARGET_SURFACES:
                if order_link_id != target:
                    raise ProviderAccountSurfaceFactsError(
                        "exact-target provider query returned a contradictory orderLinkId"
                    )
                matching_refs.append(f"{origin_ref}#row:{row_index}")
            elif order_link_id == target:
                matching_refs.append(f"{origin_ref}#row:{row_index}")

    scan_state = _MATCH if matching_refs else _NO_MATCH
    value = object.__new__(ProviderAccountSurfaceFactScan)
    material = {
        "provider_scope_digest": coverage.provider_scope_digest,
        "account_id": coverage.account_id,
        "qualification_id": coverage.qualification_id,
        "surface": coverage.surface,
        "endpoint": coverage.endpoint,
        "coverage_digest": coverage.content_digest,
        "page_chain_digest": page_chain.content_digest,
        "historical_submission_digest": historical_submission.content_digest,
        "target_client_order_id": target,
        "scan_state": scan_state,
        "scanned_page_count": len(pages),
        "scanned_row_count": scanned_rows,
        "matching_row_count": len(matching_refs),
        "matching_row_refs_json": canonical_json(matching_refs),
    }
    for name, item in material.items():
        object.__setattr__(value, name, item)
    _register_provider_account_surface_fact_scan_authority(
        value,
        coverage,
        page_chain,
        historical_submission,
        observations,
    )
    return value
