"""Issuer-protected exact provider-origin binding set for one account acquisition.

This module deliberately stops before snapshot-consistency, coverage, absence,
balance/position interpretation, or accepted account-cut issuance.  It seals the
exact direct-wire provider origins that a later WP-20 cut authority may evaluate.
Caller-created booleans or content digests cannot enter this authority.
"""
from __future__ import annotations

from dataclasses import dataclass, InitVar
from datetime import datetime, timezone
from hashlib import sha256
import json
import re
import weakref

from .durable_provider_qualification import (
    DurableProviderQualificationRegistry,
    ProviderQualificationError,
)
from .persistence import JournalStore, canonical_json
from .provider_account_acquisition import (
    DurableProviderAccountAcquisitionAuthority,
    ProviderAccountAcquisitionError,
    SerializedProviderAccountAcquisition,
)
from .provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    require_current_provider_origin_account_acquisition,
    require_provider_origin_response_binding_authority,
)
from .provider_qualification_current_scope import ProviderQualificationCurrentScope


_SCHEMA_VERSION = "1.0.0"
_ISSUANCE_TOKEN = object()
_ORIGIN_REF_RE = re.compile(r"^provider-origin:sha256:[0-9a-f]{64}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_QID_RE = re.compile(r"^provider-qualification:sha256:[0-9a-f]{64}$")
_ACQUISITION_RE = re.compile(
    r"^provider-account-acquisition:sha256:[0-9a-f]{64}$"
)


class ProviderAccountOriginSetError(ValueError):
    """Exact account origin-set authority is absent, stale, or inconsistent."""


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderAccountOriginSetError(
            f"{name} must be exact timezone-aware datetime"
        )
    return value


def _entry(binding: AuthenticatedReadResponseBinding) -> dict[str, object]:
    require_provider_origin_response_binding_authority(binding)
    if type(binding) is not AuthenticatedReadResponseBinding:
        raise ProviderAccountOriginSetError(
            "origin set requires exact AuthenticatedReadResponseBinding"
        )
    if binding.execution_class != "DIRECT_PROVIDER_WIRE":
        raise ProviderAccountOriginSetError(
            "account origin set requires DIRECT_PROVIDER_WIRE evidence"
        )
    if _ORIGIN_REF_RE.fullmatch(binding.origin_ref) is None:
        raise ProviderAccountOriginSetError("provider origin ref is non-canonical")
    for name in (
        "qualified_query_digest",
        "endpoint_rule_digest",
        "qualified_route_rule_digest",
        "response_sha256",
        "wire_request_sha256",
        "wire_request_semantics_sha256",
    ):
        value = getattr(binding, name)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            raise ProviderAccountOriginSetError(
                f"provider origin {name} is non-canonical"
            )
    if _QID_RE.fullmatch(binding.qualification_id) is None:
        raise ProviderAccountOriginSetError(
            "provider origin qualification_id is non-canonical"
        )
    return {
        "origin_ref": binding.origin_ref,
        "attempt_id": binding.attempt_id,
        "endpoint": binding.endpoint,
        "qualified_query_digest": binding.qualified_query_digest,
        "endpoint_rule_digest": binding.endpoint_rule_digest,
        "qualified_route_rule_digest": binding.qualified_route_rule_digest,
        "data_entitlement": binding.data_entitlement,
        "parser_identity": binding.parser_identity,
        "response_sha256": binding.response_sha256,
        "observed_at": binding.observed_at,
        "journal_sequence": binding.journal_sequence,
        "wire_request_sha256": binding.wire_request_sha256,
        "wire_request_semantics_sha256": binding.wire_request_semantics_sha256,
    }


def _entry_digest(entry: dict[str, object]) -> str:
    return "sha256:" + sha256(canonical_json(entry).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountOriginBindingSet:
    """Sealed set of exact direct provider origins for one current acquisition."""

    account_id: str
    provider_scope_digest: str
    acquisition_id: str
    acquisition_generation: int
    acquisition_journal_sequence_cut: int
    qualification_id: str
    qualification_route_semantics_digest: str
    entries_json: str
    _issuance_token: InitVar[object | None] = None

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountOriginSetError(
            "ProviderAccountOriginBindingSet must come from canonical issuer"
        )

    @property
    def entries(self) -> tuple[dict[str, object], ...]:
        require_provider_account_origin_set_authority(self)
        decoded = json.loads(self.entries_json)
        if type(decoded) is not list:
            raise ProviderAccountOriginSetError("origin entries are not canonical")
        return tuple(dict(item) for item in decoded)

    def payload(self) -> dict[str, object]:
        require_provider_account_origin_set_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "account_id": self.account_id,
            "provider_scope_digest": self.provider_scope_digest,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "acquisition_journal_sequence_cut": self.acquisition_journal_sequence_cut,
            "qualification_id": self.qualification_id,
            "qualification_route_semantics_digest":
                self.qualification_route_semantics_digest,
            "entries": json.loads(self.entries_json),
        }

    @property
    def content_digest(self) -> str:
        require_provider_account_origin_set_authority(self)
        material = {
            "schema_version": _SCHEMA_VERSION,
            "account_id": self.account_id,
            "provider_scope_digest": self.provider_scope_digest,
            "acquisition_id": self.acquisition_id,
            "acquisition_generation": self.acquisition_generation,
            "acquisition_journal_sequence_cut": self.acquisition_journal_sequence_cut,
            "qualification_id": self.qualification_id,
            "qualification_route_semantics_digest":
                self.qualification_route_semantics_digest,
            "entries": json.loads(self.entries_json),
        }
        return "sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()


def _install_origin_set_authority(
    *,
    _qualification_registry_type=DurableProviderQualificationRegistry,
    _account_acquisition_authority_type=DurableProviderAccountAcquisitionAuthority,
    _journal_store_type=JournalStore,
):
    def descriptor_state(
        owner: type,
        names: tuple[str, ...],
    ) -> tuple[tuple[str, object], ...]:
        return tuple((name, getattr(owner, name)) for name in names)

    descriptor_authority = (
        (
            _qualification_registry_type,
            descriptor_state(
                _qualification_registry_type,
                (
                    "_authenticate_record",
                    "_history",
                    "_resolved_cut",
                    "current",
                    "qualification",
                    "require_exact_current",
                ),
            ),
        ),
        (
            _account_acquisition_authority_type,
            descriptor_state(
                _account_acquisition_authority_type,
                (
                    "_journal_authority",
                    "_replay",
                    "require_current",
                    "resolve_current",
                ),
            ),
        ),
        (
            _journal_store_type,
            descriptor_state(
                _journal_store_type,
                (
                    "load_events_by_aggregate_type",
                    "store_identity",
                    "whole_store_state_cut",
                ),
            ),
        ),
    )

    def require_descriptor_authority() -> None:
        for owner, expected in descriptor_authority:
            for name, descriptor in expected:
                if getattr(owner, name, None) is not descriptor:
                    raise ProviderAccountOriginSetError(
                        "origin-set currentness class authority changed"
                    )

    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            DurableProviderQualificationRegistry,
            DurableProviderAccountAcquisitionAuthority,
            SerializedProviderAccountAcquisition,
            JournalStore,
            object,
            object,
            object,
            tuple[str, ...],
        ],
    ] = {}

    def snapshot(value: ProviderAccountOriginBindingSet) -> tuple[object, ...]:
        if type(value) is not ProviderAccountOriginBindingSet:
            raise ProviderAccountOriginSetError(
                "exact ProviderAccountOriginBindingSet is required"
            )
        return (
            value.account_id,
            value.provider_scope_digest,
            value.acquisition_id,
            value.acquisition_generation,
            value.acquisition_journal_sequence_cut,
            value.qualification_id,
            value.qualification_route_semantics_digest,
            value.entries_json,
        )

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountOriginBindingSet,
        *,
        qualification_registry: DurableProviderQualificationRegistry,
        account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
        account_acquisition: SerializedProviderAccountAcquisition,
    ) -> None:
        require_descriptor_authority()
        if type(qualification_registry) is not _qualification_registry_type:
            raise TypeError(
                "qualification_registry must be exact DurableProviderQualificationRegistry"
            )
        if type(account_acquisition_authority) is not _account_acquisition_authority_type:
            raise TypeError(
                "account_acquisition_authority must be exact "
                "DurableProviderAccountAcquisitionAuthority"
            )
        if type(account_acquisition) is not SerializedProviderAccountAcquisition:
            raise TypeError(
                "account_acquisition must be exact SerializedProviderAccountAcquisition"
            )
        store = qualification_registry.store
        if (
            type(store) is not _journal_store_type
            or store is not account_acquisition_authority.store
        ):
            raise ProviderAccountOriginSetError(
                "origin-set currentness authorities must share one exact JournalStore"
            )
        store_identity = store.store_identity
        evidence_store = qualification_registry.evidence_store
        evidence_root = qualification_registry.evidence_root
        registry_state_names = tuple(sorted(vars(qualification_registry)))
        prune()
        object_id = id(value)
        state = snapshot(value)
        existing = states.get(object_id)
        if existing is not None and existing[0]() is not None:
            raise ProviderAccountOriginSetError(
                "origin-set authority identity collision"
            )
        value_ref = weakref.ref(
            value,
            lambda _ref, object_id=object_id: states.pop(object_id, None),
        )
        states[object_id] = (
            value_ref,
            state,
            qualification_registry,
            account_acquisition_authority,
            account_acquisition,
            store,
            store_identity,
            evidence_store,
            evidence_root,
            registry_state_names,
        )

    def require(
        value: ProviderAccountOriginBindingSet,
    ) -> ProviderAccountOriginBindingSet:
        prune()
        state = snapshot(value)
        expected = states.get(id(value))
        if expected is None or expected[0]() is not value:
            raise ProviderAccountOriginSetError(
                "origin-set construction authority is unavailable"
            )
        if expected[1] != state:
            raise ProviderAccountOriginSetError(
                "origin-set changed after canonical issuance"
            )
        return value

    def require_current(
        value: ProviderAccountOriginBindingSet,
        *,
        at: datetime,
    ) -> ProviderAccountOriginBindingSet:
        accepted = require(value)
        point = _utc(at, name="at")
        state = states.get(id(accepted))
        assert state is not None and state[0]() is accepted
        qualification_registry = state[2]
        account_acquisition_authority = state[3]
        account_acquisition = state[4]
        store = state[5]
        store_identity = state[6]
        evidence_store = state[7]
        evidence_root = state[8]
        registry_state_names = state[9]
        require_descriptor_authority()
        if (
            type(qualification_registry) is not _qualification_registry_type
            or type(account_acquisition_authority)
            is not _account_acquisition_authority_type
            or type(store) is not _journal_store_type
            or qualification_registry.store is not store
            or account_acquisition_authority.store is not store
            or store.store_identity != store_identity
        ):
            raise ProviderAccountOriginSetError(
                "origin-set currentness JournalStore generation changed"
            )
        if (
            tuple(sorted(vars(qualification_registry))) != registry_state_names
            or qualification_registry.evidence_store is not evidence_store
            or qualification_registry.evidence_root is not evidence_root
        ):
            raise ProviderAccountOriginSetError(
                "origin-set provider qualification registry authority changed"
            )

        try:
            current_acquisition = account_acquisition_authority.require_current(
                account_acquisition
            )
        except ProviderAccountAcquisitionError as error:
            raise ProviderAccountOriginSetError(
                "origin set account acquisition is no longer exact current authority"
            ) from error
        if (
            current_acquisition.account_id != accepted.account_id
            or current_acquisition.provider_scope.content_digest
            != accepted.provider_scope_digest
            or current_acquisition.acquisition_id != accepted.acquisition_id
            or current_acquisition.acquisition_generation
            != accepted.acquisition_generation
            or current_acquisition.acquisition_journal_sequence_cut
            != accepted.acquisition_journal_sequence_cut
        ):
            raise ProviderAccountOriginSetError(
                "origin set no longer matches exact current account acquisition"
            )

        try:
            accepted_q = qualification_registry.qualification(
                accepted.qualification_id
            )
        except ProviderQualificationError as error:
            raise ProviderAccountOriginSetError(
                "origin set provider qualification is unavailable"
            ) from error
        if (
            accepted_q.scope.provider_scope.content_digest
            != accepted.provider_scope_digest
            or accepted_q.identity.route_semantics_digest
            != accepted.qualification_route_semantics_digest
        ):
            raise ProviderAccountOriginSetError(
                "origin set provider qualification identity changed"
            )
        current_scope = ProviderQualificationCurrentScope(
            provider_scope=accepted_q.scope.provider_scope,
            product_family=accepted_q.scope.product_family,
            adapter_source_git_sha=accepted_q.scope.adapter_source_git_sha,
            packaged_artifact_digest=accepted_q.scope.packaged_artifact_digest,
            protocol_id=accepted_q.scope.protocol_id,
            protocol_version=accepted_q.scope.protocol_version,
        )
        try:
            qualification_registry.require_exact_current(
                scope=current_scope,
                at=point,
                expected_qualification_id=accepted.qualification_id,
            )
        except ProviderQualificationError as error:
            raise ProviderAccountOriginSetError(
                "origin set provider qualification is no longer exact current Q"
            ) from error
        return accepted

    return register, require, require_current


(
    _register_provider_account_origin_set_authority,
    require_provider_account_origin_set_authority,
    require_current_provider_account_origin_set_authority,
) = _install_origin_set_authority()
del _install_origin_set_authority


def issue_provider_account_origin_set(
    *,
    qualification_registry: DurableProviderQualificationRegistry,
    account_acquisition_authority: DurableProviderAccountAcquisitionAuthority,
    account_acquisition: SerializedProviderAccountAcquisition,
    response_bindings: tuple[AuthenticatedReadResponseBinding, ...],
    at: datetime,
) -> ProviderAccountOriginBindingSet:
    """Seal exact current Q + acquisition + direct origin bindings, and nothing more."""

    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if type(account_acquisition_authority) is not DurableProviderAccountAcquisitionAuthority:
        raise TypeError(
            "account_acquisition_authority must be exact DurableProviderAccountAcquisitionAuthority"
        )
    if type(account_acquisition) is not SerializedProviderAccountAcquisition:
        raise TypeError(
            "account_acquisition must be exact SerializedProviderAccountAcquisition"
        )
    if qualification_registry.store is not account_acquisition_authority.store:
        raise ProviderAccountOriginSetError(
            "qualification and acquisition authorities must share one JournalStore"
        )
    point = _utc(at, name="at")
    try:
        current_acquisition = account_acquisition_authority.require_current(
            account_acquisition
        )
    except ProviderAccountAcquisitionError as error:
        raise ProviderAccountOriginSetError(
            "account acquisition is not exact current authority"
        ) from error
    if type(response_bindings) is not tuple or not response_bindings:
        raise ProviderAccountOriginSetError(
            "response_bindings must be a non-empty exact tuple"
        )

    entries: list[dict[str, object]] = []
    qualification_id: str | None = None
    seen_origin_refs: set[str] = set()
    seen_query_digests: set[str] = set()
    for binding in response_bindings:
        if type(binding) is not AuthenticatedReadResponseBinding:
            raise ProviderAccountOriginSetError(
                "response_bindings must contain exact provider-origin bindings"
            )
        try:
            require_current_provider_origin_account_acquisition(
                response_binding=binding,
                account_acquisition_authority=account_acquisition_authority,
                account_acquisition=current_acquisition,
            )
        except ProviderOriginError as error:
            raise ProviderAccountOriginSetError(
                "provider origin is not bound to exact current account acquisition"
            ) from error
        if binding.origin_ref in seen_origin_refs:
            raise ProviderAccountOriginSetError(
                "provider origin set contains duplicate origin_ref"
            )
        if binding.qualified_query_digest in seen_query_digests:
            raise ProviderAccountOriginSetError(
                "provider origin set contains duplicate qualified query"
            )
        seen_origin_refs.add(binding.origin_ref)
        seen_query_digests.add(binding.qualified_query_digest)
        if qualification_id is None:
            qualification_id = binding.qualification_id
        elif binding.qualification_id != qualification_id:
            raise ProviderAccountOriginSetError(
                "provider origin set mixes qualification identities"
            )
        entries.append(_entry(binding))

    assert qualification_id is not None
    try:
        accepted_q = qualification_registry.qualification(qualification_id)
    except ProviderQualificationError as error:
        raise ProviderAccountOriginSetError(
            "provider origin qualification is not durable accepted Q"
        ) from error
    if (
        accepted_q.scope.provider_scope.content_digest
        != current_acquisition.provider_scope.content_digest
    ):
        raise ProviderAccountOriginSetError(
            "accepted Q provider scope differs from account acquisition"
        )
    current_scope = ProviderQualificationCurrentScope(
        provider_scope=accepted_q.scope.provider_scope,
        product_family=accepted_q.scope.product_family,
        adapter_source_git_sha=accepted_q.scope.adapter_source_git_sha,
        packaged_artifact_digest=accepted_q.scope.packaged_artifact_digest,
        protocol_id=accepted_q.scope.protocol_id,
        protocol_version=accepted_q.scope.protocol_version,
    )
    try:
        qualification_registry.require_exact_current(
            scope=current_scope,
            at=point,
            expected_qualification_id=qualification_id,
        )
    except ProviderQualificationError as error:
        raise ProviderAccountOriginSetError(
            "provider origin qualification is not exact current Q"
        ) from error

    entries.sort(key=lambda item: item["origin_ref"])
    normalized = []
    for entry in entries:
        normalized.append({**entry, "entry_digest": _entry_digest(entry)})
    entries_json = canonical_json(normalized)
    value = object.__new__(ProviderAccountOriginBindingSet)
    object.__setattr__(value, "account_id", current_acquisition.account_id)
    object.__setattr__(
        value,
        "provider_scope_digest",
        current_acquisition.provider_scope.content_digest,
    )
    object.__setattr__(value, "acquisition_id", current_acquisition.acquisition_id)
    object.__setattr__(
        value,
        "acquisition_generation",
        current_acquisition.acquisition_generation,
    )
    object.__setattr__(
        value,
        "acquisition_journal_sequence_cut",
        current_acquisition.acquisition_journal_sequence_cut,
    )
    object.__setattr__(value, "qualification_id", qualification_id)
    object.__setattr__(
        value,
        "qualification_route_semantics_digest",
        accepted_q.identity.route_semantics_digest,
    )
    object.__setattr__(value, "entries_json", entries_json)
    _register_provider_account_origin_set_authority(
        value,
        qualification_registry=qualification_registry,
        account_acquisition_authority=account_acquisition_authority,
        account_acquisition=current_acquisition,
    )
    return value
