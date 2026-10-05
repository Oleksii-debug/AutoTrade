"""Issuer-protected provider page/cursor graph for WP-20 account truth.

This module proves only that one exact qualified provider endpoint was traversed
from its root query through its source-owned pagination cursor to a terminal
page. It does not prove retention/window coverage, consistency-horizon expiry,
searched submission absence, account-cut consistency, or PROVEN_ABSENT.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
import weakref

from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import JournalStore, canonical_json
from .provider_account_absence_semantics import (
    ProviderAccountAbsenceSemanticsError,
    QualifiedProviderAccountAbsenceSemantics,
    require_provider_account_absence_rule,
)
from .provider_account_origin_set import (
    ProviderAccountOriginBindingSet,
    ProviderAccountOriginSetError,
    require_provider_account_origin_set_authority,
)
from .provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginObservation,
    require_provider_origin_response_binding_authority,
    require_provider_origin_response_binding_store,
)
from .provider_route_reads import (
    ProviderRouteReadError,
    _require_qualified_provider_response_authority,
)

_SCHEMA_VERSION = "1.0.0"
_PROVIDER_ORIGIN_AGGREGATE = "qualified_authenticated_provider_read"
_SUPPORTED_PROVIDER = "BYBIT"
_SUPPORTED_PAGINATION_RULE = "BYBIT_V5_CURSOR_V1"
_SUPPORTED_QUERY_SCOPE_RULE = "BYBIT_SPOT_ACCOUNT_QUERY_V1"


class ProviderAccountPageChainError(ValueError):
    """Exact source-owned provider pagination authority is absent or inconsistent."""


def _canonical_query(observation: ProviderOriginObservation) -> dict[str, str]:
    query = observation.qualified_observation.query_binding.query_binding.query
    material: dict[str, str] = {}
    for key, value in query.items():
        if type(key) is not str or type(value) is not str:
            raise ProviderAccountPageChainError(
                "qualified provider query contains non-canonical material"
            )
        material[key] = value
    return material


def _validated_observation(
    observation: ProviderOriginObservation,
    *,
    store: JournalStore,
) -> tuple[AuthenticatedReadResponseBinding, dict[str, str], int]:
    if type(observation) is not ProviderOriginObservation:
        raise TypeError("observations must contain exact ProviderOriginObservation")
    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    binding = observation.response_binding
    if type(binding) is not AuthenticatedReadResponseBinding:
        raise ProviderAccountPageChainError(
            "provider-origin observation lacks exact durable response binding"
        )
    try:
        require_provider_origin_response_binding_authority(binding)
        require_provider_origin_response_binding_store(binding, store)
        _require_qualified_provider_response_authority(
            observation.qualified_observation
        )
    except (ProviderOriginError, ProviderRouteReadError) as error:
        raise ProviderAccountPageChainError(
            "provider-origin observation construction authority is unavailable"
        ) from error

    qualified = observation.qualified_observation
    if (
        binding.execution_class != "DIRECT_PROVIDER_WIRE"
        or binding.provider_id != _SUPPORTED_PROVIDER
        or binding.provider_id != qualified.provider_id
        or binding.account_id != qualified.account_id
        or binding.environment != qualified.environment
        or binding.qualification_id != qualified.qualification_id
        or binding.qualified_query_digest != qualified.query_binding.query_digest
        or binding.response_sha256 != qualified.observation.response_sha256
    ):
        raise ProviderAccountPageChainError(
            "provider-origin observation does not match exact supported durable response"
        )

    events = JournalStore.load_events(store, _PROVIDER_ORIGIN_AGGREGATE, binding.attempt_id)
    if (
        len(events) != 3
        or events[0].get("event_type") != "AuthenticatedReadPrepared"
        or events[2].get("event_type") != "AuthenticatedReadObserved"
        or events[2].get("journal_sequence") != binding.journal_sequence
    ):
        raise ProviderAccountPageChainError(
            "provider-origin durable request/response chronology is unavailable"
        )
    prepared_sequence = events[0].get("journal_sequence")
    if (
        type(prepared_sequence) is not int
        or prepared_sequence < 1
        or type(binding.journal_sequence) is not int
        or binding.journal_sequence <= prepared_sequence
    ):
        raise ProviderAccountPageChainError(
            "provider-origin durable request/response chronology is invalid"
        )
    return binding, _canonical_query(observation), prepared_sequence


def _parse_bybit_page(observation: ProviderOriginObservation) -> tuple[str, int]:
    payload = observation.qualified_observation.observation.payload
    if not hasattr(payload, "get"):
        raise ProviderAccountPageChainError(
            "Bybit page response lacks canonical success envelope"
        )
    ret_code = payload.get("retCode")
    if type(ret_code) is not int or ret_code != 0:
        raise ProviderAccountPageChainError(
            "Bybit page response is not a successful provider result"
        )
    result = payload.get("result")
    if not hasattr(result, "get"):
        raise ProviderAccountPageChainError("Bybit page result must be an object")
    rows = result.get("list")
    if type(rows) is not tuple:
        raise ProviderAccountPageChainError("Bybit page result list is unavailable")
    if "nextPageCursor" not in result:
        raise ProviderAccountPageChainError(
            "Bybit page result lacks explicit nextPageCursor"
        )
    cursor = result.get("nextPageCursor")
    if type(cursor) is not str:
        raise ProviderAccountPageChainError("Bybit nextPageCursor must be exact text")
    if cursor != cursor.strip() or len(cursor) > 1024:
        raise ProviderAccountPageChainError("Bybit nextPageCursor is non-canonical")
    return cursor, len(rows)


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountPageChain:
    """Sealed exact ordered pagination graph for one qualified absence surface."""

    provider_scope_digest: str
    account_id: str
    acquisition_id: str
    acquisition_generation: int
    qualification_id: str
    absence_semantics_digest: str
    origin_set_digest: str
    surface: str
    endpoint: str
    data_entitlement: str
    query_scope_rule_id: str
    pagination_rule_id: str
    root_query_json: str
    pages_json: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountPageChainError(
            "provider account page chain must come from canonical issuer"
        )

    @property
    def pages(self) -> tuple[dict[str, object], ...]:
        require_provider_account_page_chain_authority(self)
        value = json.loads(self.pages_json)
        if type(value) is not list:
            raise ProviderAccountPageChainError("page chain state is non-canonical")
        return tuple(dict(item) for item in value)

    def payload(self) -> dict[str, object]:
        require_provider_account_page_chain_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "qualification_id": self.qualification_id,
            "absence_semantics_digest": self.absence_semantics_digest,
            "origin_set_digest": self.origin_set_digest,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "data_entitlement": self.data_entitlement,
            "query_scope_rule_id": self.query_scope_rule_id,
            "pagination_rule_id": self.pagination_rule_id,
            "root_query": json.loads(self.root_query_json),
            "pages": json.loads(self.pages_json),
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-page-chain:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_page_chain_authority():
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "acquisition_id",
        "acquisition_generation",
        "qualification_id",
        "absence_semantics_digest",
        "origin_set_digest",
        "surface",
        "endpoint",
        "data_entitlement",
        "query_scope_rule_id",
        "pagination_rule_id",
        "root_query_json",
        "pages_json",
    )

    def material(value: ProviderAccountPageChain) -> tuple[object, ...]:
        if type(value) is not ProviderAccountPageChain:
            raise ProviderAccountPageChainError("exact ProviderAccountPageChain is required")
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(value: ProviderAccountPageChain) -> None:
        prune()
        if id(value) in states and states[id(value)][0]() is not None:
            raise ProviderAccountPageChainError("page-chain authority identity collision")
        states[id(value)] = (weakref.ref(value), material(value))

    def require(value: ProviderAccountPageChain) -> ProviderAccountPageChain:
        current = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountPageChainError(
                "page-chain construction authority is unavailable"
            )
        if state[1] != current:
            raise ProviderAccountPageChainError(
                "page-chain authority changed after issuance"
            )
        return value

    return register, require


_register_provider_account_page_chain_authority, require_provider_account_page_chain_authority = (
    _install_page_chain_authority()
)
del _install_page_chain_authority


def issue_provider_account_page_chain(
    *,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    origin_set: ProviderAccountOriginBindingSet,
    observations: tuple[ProviderOriginObservation, ...],
    qualification_registry: DurableProviderQualificationRegistry,
    surface: str,
    at: datetime,
) -> ProviderAccountPageChain:
    """Derive a terminal page/cursor chain from exact provider-origin bytes.

    No pagination-complete boolean or caller page ordering is accepted. The
    graph starts only from the cursor-free qualified root query, follows each
    response-owned ``nextPageCursor`` to the exact next qualified request, and
    terminates only when the provider response returns an explicit empty cursor.
    """
    if type(absence_semantics) is not QualifiedProviderAccountAbsenceSemantics:
        raise TypeError(
            "absence_semantics must be exact QualifiedProviderAccountAbsenceSemantics"
        )
    if type(origin_set) is not ProviderAccountOriginBindingSet:
        raise TypeError("origin_set must be exact ProviderAccountOriginBindingSet")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if type(observations) is not tuple or not observations:
        raise ProviderAccountPageChainError(
            "observations must be a non-empty exact tuple"
        )
    try:
        require_provider_account_origin_set_authority(origin_set)
    except ProviderAccountOriginSetError as error:
        raise ProviderAccountPageChainError(
            "provider origin set authority is unavailable"
        ) from error

    if (
        origin_set.provider_scope_digest != absence_semantics.provider_scope_digest
        or origin_set.qualification_id != absence_semantics.qualification_id
    ):
        raise ProviderAccountPageChainError(
            "origin set and absence semantics do not share exact provider Q scope"
        )

    matching_rules = [
        rule for rule in absence_semantics.rules if rule.get("surface") == surface
    ]
    if len(matching_rules) != 1:
        raise ProviderAccountPageChainError(
            "requested surface lacks one exact provider-Q absence rule"
        )
    semantic_rule = matching_rules[0]
    try:
        rule = require_provider_account_absence_rule(
            absence_semantics,
            surface=surface,
            endpoint=semantic_rule["endpoint"],
            data_entitlement=semantic_rule["data_entitlement"],
            qualification_registry=qualification_registry,
            at=at,
        )
    except ProviderAccountAbsenceSemanticsError as error:
        raise ProviderAccountPageChainError(
            "provider-Q absence rule is not exact current authority"
        ) from error

    if rule["pagination_rule_id"] != _SUPPORTED_PAGINATION_RULE:
        raise ProviderAccountPageChainError(
            "pagination rule has no executable source-owned page-chain implementation"
        )
    if rule["query_scope_rule_id"] != _SUPPORTED_QUERY_SCOPE_RULE:
        raise ProviderAccountPageChainError(
            "query-scope rule has no executable source-owned page-chain implementation"
        )

    endpoint = rule["endpoint"]
    entitlement = rule["data_entitlement"]
    endpoint_entries = tuple(
        entry for entry in origin_set.entries if entry.get("endpoint") == endpoint
    )
    if not endpoint_entries:
        raise ProviderAccountPageChainError(
            "origin set contains no provider pages for qualified surface endpoint"
        )
    expected_refs = {entry["origin_ref"] for entry in endpoint_entries}
    if len(expected_refs) != len(endpoint_entries):
        raise ProviderAccountPageChainError(
            "origin set contains duplicate endpoint origin identities"
        )

    by_request_cursor: dict[str | None, tuple[dict[str, object], str]] = {}
    observed_refs: set[str] = set()
    root_query: dict[str, str] | None = None

    for observation in observations:
        binding, query, prepared_sequence = _validated_observation(
            observation,
            store=qualification_registry.store,
        )
        qualified = observation.qualified_observation
        base_query = qualified.query_binding.query_binding
        if (
            binding.origin_ref not in expected_refs
            or binding.qualification_id != origin_set.qualification_id
            or qualified.route_semantics_digest
            != origin_set.qualification_route_semantics_digest
            or base_query.endpoint != endpoint
            or qualified.data_entitlement != entitlement
            or binding.endpoint != endpoint
            or binding.data_entitlement != entitlement
        ):
            raise ProviderAccountPageChainError(
                "provider page is outside exact qualified origin-set surface"
            )
        if binding.origin_ref in observed_refs:
            raise ProviderAccountPageChainError(
                "provider page chain contains duplicate origin identity"
            )
        observed_refs.add(binding.origin_ref)

        cursor = query.get("cursor")
        if cursor == "":
            raise ProviderAccountPageChainError(
                "Bybit cursor query must omit the root cursor instead of using empty text"
            )
        if cursor is not None and (
            type(cursor) is not str
            or cursor != cursor.strip()
            or not cursor
            or len(cursor) > 1024
        ):
            raise ProviderAccountPageChainError("Bybit request cursor is non-canonical")

        scoped_query = {key: value for key, value in query.items() if key != "cursor"}
        if root_query is None:
            root_query = scoped_query
        elif scoped_query != root_query:
            raise ProviderAccountPageChainError(
                "provider page chain changes qualified query scope across cursors"
            )

        next_cursor, item_count = _parse_bybit_page(observation)
        if cursor in by_request_cursor:
            raise ProviderAccountPageChainError(
                "provider page chain contains duplicate request cursor"
            )
        by_request_cursor[cursor] = (
            {
                "origin_ref": binding.origin_ref,
                "qualified_query_digest": binding.qualified_query_digest,
                "request_cursor": cursor,
                "response_next_cursor": next_cursor,
                "response_sha256": binding.response_sha256,
                "prepared_journal_sequence": prepared_sequence,
                "observed_journal_sequence": binding.journal_sequence,
                "item_count": item_count,
            },
            next_cursor,
        )

    if observed_refs != expected_refs:
        raise ProviderAccountPageChainError(
            "provider page observations do not exactly cover origin-set endpoint pages"
        )
    if None not in by_request_cursor:
        raise ProviderAccountPageChainError(
            "provider page chain lacks a cursor-free root query"
        )

    ordered: list[dict[str, object]] = []
    visited: set[str | None] = set()
    request_cursor: str | None = None
    while True:
        if request_cursor in visited:
            raise ProviderAccountPageChainError("provider page cursor graph contains a cycle")
        visited.add(request_cursor)
        node = by_request_cursor.get(request_cursor)
        if node is None:
            raise ProviderAccountPageChainError(
                "provider response cursor has no exact next qualified page"
            )
        page, next_cursor = node
        ordered.append(page)
        if next_cursor == "":
            break
        request_cursor = next_cursor

    if len(visited) != len(by_request_cursor):
        raise ProviderAccountPageChainError(
            "provider page chain contains disconnected or caller-added pages"
        )

    previous_observed_sequence: int | None = None
    for page in ordered:
        prepared_sequence = page["prepared_journal_sequence"]
        observed_sequence = page["observed_journal_sequence"]
        if (
            type(prepared_sequence) is not int
            or type(observed_sequence) is not int
            or prepared_sequence >= observed_sequence
            or (
                previous_observed_sequence is not None
                and prepared_sequence <= previous_observed_sequence
            )
        ):
            raise ProviderAccountPageChainError(
                "provider cursor chain violates durable causal order"
            )
        previous_observed_sequence = observed_sequence

    assert root_query is not None
    value = object.__new__(ProviderAccountPageChain)
    material = {
        "provider_scope_digest": origin_set.provider_scope_digest,
        "account_id": origin_set.account_id,
        "acquisition_id": origin_set.acquisition_id,
        "acquisition_generation": origin_set.acquisition_generation,
        "qualification_id": origin_set.qualification_id,
        "absence_semantics_digest": absence_semantics.content_digest,
        "origin_set_digest": origin_set.content_digest,
        "surface": rule["surface"],
        "endpoint": endpoint,
        "data_entitlement": entitlement,
        "query_scope_rule_id": rule["query_scope_rule_id"],
        "pagination_rule_id": rule["pagination_rule_id"],
        "root_query_json": canonical_json(root_query),
        "pages_json": canonical_json(ordered),
    }
    for name, item in material.items():
        object.__setattr__(value, name, item)
    _register_provider_account_page_chain_authority(value)
    return value
