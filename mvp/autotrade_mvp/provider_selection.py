"""Fail-closed provider routing from one durable capability/qualification cut.

Candidates describe static product/build/account composition only. They do not
carry caller-constructed capability or qualification authority. Selection reads
current C and current Q from their canonical durable registries at one exact
global JournalStore sequence and binds that pair into a sealed route value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from types import MappingProxyType
from typing import Callable
from weakref import ref as weakref_ref

from .capabilities import CapabilityError, CapabilitySnapshot
from .durable_capabilities import DurableCapabilityRegistry
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    require_exact_journal_store_authority,
)
from .provider_core import provider_definition
from .provider_domain import ProviderDomainError, ProviderFinancialScope
from .provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationError,
    ProviderQualificationScope,
)
from .provider_qualification_identity import ProviderQualificationIdentity
from .qualification_attestation import EvidenceArtifactRef
from .provider_qualification_current_scope import (
    ProviderQualificationCurrentScope,
    ProviderQualificationCurrentScopeError,
)


_ASSET_FAMILY_COMPATIBILITY = MappingProxyType({
    "CRYPTO_SPOT": frozenset({
        ("BYBIT", "SPOT"),
        ("KRAKEN", "SPOT"),
        ("WHITEBIT", "SPOT"),
        ("BINANCE", "SPOT"),
        ("ALPACA", "CRYPTO"),
    }),
    "CRYPTO_MARGIN": frozenset({
        ("BYBIT", "MARGIN"),
        ("KRAKEN", "MARGIN"),
        ("WHITEBIT", "COLLATERAL"),
        ("BINANCE", "MARGIN"),
    }),
    "LINEAR_PERPETUAL": frozenset({
        ("BYBIT", "LINEAR_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("WHITEBIT", "FUTURES"),
        ("BINANCE", "USD_M"),
    }),
    "INVERSE_PERPETUAL": frozenset({
        ("BYBIT", "INVERSE_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("BINANCE", "COIN_M"),
    }),
    "LISTED_FUTURE": frozenset({("IBKR", "FUTURES")}),
    "EQUITY": frozenset({("IBKR", "EQUITIES"), ("ALPACA", "EQUITIES")}),
    "OPTION": frozenset({
        ("BYBIT", "OPTIONS"),
        ("BINANCE", "OPTIONS"),
        ("IBKR", "OPTIONS"),
        ("ALPACA", "OPTIONS"),
    }),
    "FX": frozenset({("IBKR", "FX")}),
})
# Public diagnostic view only. Financial selection binds the original immutable
# object at function definition time and does not trust later module rebinding.
ASSET_FAMILY_COMPATIBILITY = _ASSET_FAMILY_COMPATIBILITY

_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class ProviderSelectionError(ValueError):
    pass


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderSelectionError(f"{name} is required as canonical text")
    return value


def _code_sha(value: object) -> str:
    text = _text(value, "adapter_code_sha")
    if _GIT_SHA_RE.fullmatch(text) is None:
        raise ProviderSelectionError(
            "adapter_code_sha must be a canonical lowercase 40-character Git SHA"
        )
    return text


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if _SHA256_RE.fullmatch(text) is None:
        raise ProviderSelectionError(f"{name} must be canonical sha256:<64-hex>")
    return text


def _instant(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ProviderSelectionError(
            f"{name} must be an exact stdlib timezone datetime"
        )
    return value.astimezone(timezone.utc)


def _install_journal_cut_reader():
    """Freeze provider selection to canonical JournalStore cut authority."""

    store_type = JournalStore
    canonical_require = require_exact_journal_store_authority
    canonical_scope = journal_store_authority_scope
    canonical_operation = store_type.whole_store_state_cut
    canonical_getattr = getattr

    def journal_cut(store: object) -> int:
        if (
            JournalStore is not store_type
            or require_exact_journal_store_authority is not canonical_require
            or journal_store_authority_scope is not canonical_scope
            or canonical_getattr(store_type, "whole_store_state_cut", None)
            is not canonical_operation
        ):
            raise ProviderSelectionError("journal cut authority changed")

        identity = canonical_require(
            store,
            subject="provider selection JournalStore",
        )
        with canonical_scope(store, identity):
            value = canonical_operation(store)
        if type(value) is not dict:
            raise ProviderSelectionError("whole-store decision cut is non-canonical")
        sequence = value.get("journal_sequence")
        if type(sequence) is not int or sequence < 0:
            raise ProviderSelectionError(
                "whole-store decision cut lacks canonical sequence"
            )
        return sequence

    return journal_cut


_journal_cut = _install_journal_cut_reader()
del _install_journal_cut_reader


@dataclass(frozen=True, slots=True)
class ProviderRouteRequest:
    asset_class: str
    environment: str
    instrument_version: str
    order_type: str
    time_in_force: str
    permission_scope: str
    preferred_provider_id: str | None = None

    def __post_init__(
        self,
        _compatibility=_ASSET_FAMILY_COMPATIBILITY,
    ) -> None:
        asset = _text(self.asset_class, "asset_class").upper()
        if asset not in _compatibility:
            raise ProviderSelectionError("asset_class has no canonical provider crosswalk")
        object.__setattr__(self, "asset_class", asset)
        environment = _text(self.environment, "environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise ProviderSelectionError(
                "external provider selection is restricted to PAPER or LIVE"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, "instrument_version"),
        )
        object.__setattr__(
            self,
            "order_type",
            _text(self.order_type, "order_type").upper(),
        )
        object.__setattr__(
            self,
            "time_in_force",
            _text(self.time_in_force, "time_in_force").upper(),
        )
        object.__setattr__(
            self,
            "permission_scope",
            _text(self.permission_scope, "permission_scope").upper(),
        )
        if self.preferred_provider_id is not None:
            object.__setattr__(
                self,
                "preferred_provider_id",
                _text(self.preferred_provider_id, "preferred_provider_id").upper(),
            )


@dataclass(frozen=True, slots=True)
class ProviderCandidate:
    """Static composition only; contains no financial authority values."""

    provider_id: str
    product_family: str
    provider_environment: str
    account_id: str
    entity_id: str
    entity_policy_id: str
    adapter_code_sha: str
    packaged_artifact_digest: str
    protocol_id: str
    protocol_version: str

    def __post_init__(
        self,
        _provider_definition=provider_definition,
    ) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        definition = _provider_definition(provider)
        family = _text(self.product_family, "product_family").upper()
        if family not in definition.product_families:
            raise ProviderSelectionError("product_family is not declared for provider")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "product_family", family)
        object.__setattr__(
            self,
            "provider_environment",
            _text(self.provider_environment, "provider_environment").upper(),
        )
        for field in ("account_id", "entity_id"):
            object.__setattr__(self, field, _text(getattr(self, field), field))
        object.__setattr__(
            self,
            "entity_policy_id",
            _text(self.entity_policy_id, "entity_policy_id").upper(),
        )
        object.__setattr__(self, "adapter_code_sha", _code_sha(self.adapter_code_sha))
        object.__setattr__(
            self,
            "packaged_artifact_digest",
            _digest(self.packaged_artifact_digest, "packaged_artifact_digest"),
        )
        for field in ("protocol_id", "protocol_version"):
            object.__setattr__(self, field, _text(getattr(self, field), field))

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.provider_id,
            self.product_family,
            self.provider_environment,
            self.account_id,
            self.entity_id,
        )


def _install_selected_route_authority() -> tuple[
    type[object],
    Callable[..., object],
]:
    """Create the route type and its non-exported canonical selection issuer.

    The binding table is closure-owned and keyed by exact object identity. Weak
    references have no callbacks: dead entries are pruned only during later
    canonical issuance, so external code cannot erase or mint authority by
    invoking a weakref callback.  Every authority-bearing field read revalidates
    the construction snapshot before returning caller-visible state.
    """

    route_ref = weakref_ref
    capability_type = CapabilitySnapshot
    qualification_type = AcceptedProviderQualification
    qualification_scope_type = ProviderQualificationScope
    qualification_identity_type = ProviderQualificationIdentity
    financial_scope_type = ProviderFinancialScope
    evidence_ref_type = EvidenceArtifactRef
    mapping_proxy_type = MappingProxyType
    datetime_type = datetime
    timezone_type = timezone
    bindings: dict[
        int,
        tuple[
            object,
            ProviderCandidate,
            tuple[str, ...],
            CapabilitySnapshot,
            tuple[object, ...],
            AcceptedProviderQualification,
            tuple[object, ...],
            int,
        ],
    ] = {}
    authority_fields = frozenset(
        {
            "candidate",
            "capability",
            "qualification",
            "decision_journal_sequence_cut",
            "qualification_id",
            "capability_snapshot_id",
        }
    )

    candidate_field_names = (
        "provider_id",
        "product_family",
        "provider_environment",
        "account_id",
        "entity_id",
        "entity_policy_id",
        "adapter_code_sha",
        "packaged_artifact_digest",
        "protocol_id",
        "protocol_version",
    )

    def authority_changed() -> None:
        message = "selected provider route authority changed"
        # The generic selector must not statically depend on the financial
        # product layer.  When that layer is already loaded, preserve its
        # established domain error at the product boundary; otherwise use the
        # selector's own fail-closed error.
        try:
            from .financial_send_authority import FinancialSendAuthorityError
        except (ImportError, AttributeError):
            raise ProviderSelectionError(message)
        raise FinancialSendAuthorityError(message)

    def candidate_snapshot(value: ProviderCandidate) -> tuple[str, ...]:
        current = tuple(
            object.__getattribute__(value, field_name)
            for field_name in candidate_field_names
        )
        if any(type(item) is not str for item in current):
            authority_changed()
        return current

    def _string_tuple_state(value: object) -> tuple[str, ...]:
        if type(value) is not tuple or any(type(item) is not str for item in value):
            authority_changed()
        return value

    def _string_frozenset_state(value: object) -> tuple[str, ...]:
        if type(value) is not frozenset or any(type(item) is not str for item in value):
            authority_changed()
        return tuple(sorted(value))

    def _financial_scope_state(value: object) -> tuple[str, str, str, str]:
        if type(value) is not financial_scope_type:
            authority_changed()
        state = tuple(
            object.__getattribute__(value, name)
            for name in (
                "provider_id",
                "runtime_environment",
                "provider_environment",
                "entity_policy_id",
            )
        )
        if any(type(item) is not str for item in state):
            authority_changed()
        return state

    def _capability_state(value: object) -> tuple[object, ...]:
        if type(value) is not capability_type:
            authority_changed()
        text_fields = tuple(
            object.__getattribute__(value, name)
            for name in (
                "snapshot_id",
                "provider_id",
                "account_id",
                "entity_id",
                "environment",
                "provider_environment",
                "instrument_version",
                "position_mode",
                "rate_limit_policy_id",
                "status",
            )
        )
        if any(type(item) is not str for item in text_fields):
            authority_changed()
        observed_at = object.__getattribute__(value, "observed_at")
        expires_at = object.__getattribute__(value, "expires_at")
        for instant in (observed_at, expires_at):
            if (
                type(instant) is not datetime_type
                or type(object.__getattribute__(instant, "tzinfo")) is not timezone_type
            ):
                authority_changed()
        set_state = tuple(
            _string_frozenset_state(object.__getattribute__(value, name))
            for name in (
                "supported_order_types",
                "time_in_force",
                "permission_scopes",
                "native_protection",
                "data_entitlements",
                "sources",
            )
        )
        evidence = object.__getattribute__(value, "evidence")
        if type(evidence) is not tuple:
            authority_changed()
        evidence_state = []
        for item in evidence:
            if type(item) is not mapping_proxy_type:
                authority_changed()
            pairs = tuple(item.items())
            if any(type(key) is not str or type(raw) is not str for key, raw in pairs):
                authority_changed()
            evidence_state.append(tuple(sorted(pairs)))
        can_admit = object.__getattribute__(value, "_can_admit")
        if type(can_admit) is not bool:
            authority_changed()
        return (
            text_fields,
            observed_at,
            expires_at,
            set_state,
            tuple(evidence_state),
            can_admit,
        )

    def _qualification_scope_state(value: object) -> tuple[object, ...]:
        if type(value) is not qualification_scope_type:
            authority_changed()
        campaign_version = object.__getattribute__(value, "campaign_version")
        text_fields = tuple(
            object.__getattribute__(value, name)
            for name in (
                "product_family",
                "adapter_source_git_sha",
                "packaged_artifact_digest",
                "campaign_id",
                "protocol_id",
                "protocol_version",
            )
        )
        if type(campaign_version) is not int or any(
            type(item) is not str for item in text_fields
        ):
            authority_changed()
        return (
            _financial_scope_state(object.__getattribute__(value, "provider_scope")),
            text_fields,
            campaign_version,
        )

    def _qualification_identity_state(value: object) -> tuple[object, ...]:
        if type(value) is not qualification_identity_type:
            authority_changed()
        campaign_version = object.__getattribute__(value, "campaign_version")
        packaged_artifact_id = object.__getattribute__(value, "packaged_artifact_id")
        if type(campaign_version) is not int or (
            packaged_artifact_id is not None
            and type(packaged_artifact_id) is not str
        ):
            authority_changed()
        text_fields = tuple(
            object.__getattribute__(value, name)
            for name in (
                "product_family",
                "adapter_source_git_sha",
                "packaged_artifact_digest",
                "campaign_id",
                "protocol_id",
                "protocol_version",
                "required_case_policy_digest",
                "result_set_digest",
                "route_semantics_digest",
                "documentation_revision_digest",
                "evidence_set_digest",
                "chronology_digest",
                "lineage_digest",
                "acceptance_metadata_digest",
                "attestation_digest",
                "trust_policy_digest",
                "issuer_identity_digest",
                "verifier_identity_digest",
            )
        )
        if any(type(item) is not str for item in text_fields):
            authority_changed()
        return (
            _financial_scope_state(object.__getattribute__(value, "provider_scope")),
            text_fields,
            packaged_artifact_id,
            campaign_version,
        )

    def _evidence_ref_state(value: object) -> tuple[str, str, str, str, str]:
        if type(value) is not evidence_ref_type:
            authority_changed()
        state = tuple(
            object.__getattribute__(value, name)
            for name in (
                "artifact_id",
                "sha256",
                "media_type",
                "evidence_kind",
                "source_sha",
            )
        )
        if any(type(item) is not str for item in state):
            authority_changed()
        return state

    def _qualification_state(value: object) -> tuple[object, ...]:
        if type(value) is not qualification_type:
            authority_changed()
        required_cases = _string_tuple_state(
            object.__getattribute__(value, "required_cases")
        )
        unsupported_features = _string_tuple_state(
            object.__getattribute__(value, "unsupported_features")
        )
        documentation_revisions = _string_tuple_state(
            object.__getattribute__(value, "documentation_revisions")
        )
        raw_refs = object.__getattribute__(value, "raw_evidence_refs")
        if type(raw_refs) is not tuple:
            authority_changed()
        raw_ref_state = tuple(_evidence_ref_state(item) for item in raw_refs)
        optional_fields = tuple(
            object.__getattribute__(value, name)
            for name in ("supersedes_qualification_id", "release_artifact_id")
        )
        if any(item is not None and type(item) is not str for item in optional_fields):
            authority_changed()
        text_fields = tuple(
            object.__getattribute__(value, name)
            for name in (
                "qualification_id",
                "route_semantics_json",
                "completed_at",
                "valid_until",
                "attestation_id",
                "attestation_digest",
                "policy_id",
                "policy_version",
                "trust_root_id",
                "producer_id",
                "verifier_id",
                "signed_at",
            )
        )
        if any(type(item) is not str for item in text_fields):
            authority_changed()
        return (
            _qualification_identity_state(
                object.__getattribute__(value, "identity")
            ),
            _qualification_scope_state(object.__getattribute__(value, "scope")),
            text_fields,
            required_cases,
            unsupported_features,
            documentation_revisions,
            _evidence_ref_state(
                object.__getattribute__(value, "campaign_artifact_ref")
            ),
            raw_ref_state,
            optional_fields,
        )

    @dataclass(frozen=True, slots=True, init=False, weakref_slot=True)
    class SelectedProviderRoute:
        """Selection-issued binding of static route composition to exact C/Q."""

        candidate: ProviderCandidate
        capability: CapabilitySnapshot
        qualification: AcceptedProviderQualification
        decision_journal_sequence_cut: int

        def __init__(
            self,
            *,
            candidate: ProviderCandidate,
            capability: CapabilitySnapshot,
            qualification: AcceptedProviderQualification,
            decision_journal_sequence_cut: int,
            _selection_token: object | None = None,
        ) -> None:
            del candidate, capability, qualification, decision_journal_sequence_cut
            del _selection_token
            raise ProviderSelectionError(
                "SelectedProviderRoute must come from canonical provider selection"
            )

        def __getattribute__(self, name: str) -> object:
            if name in authority_fields:
                binding = bindings.get(id(self))
                if binding is None:
                    authority_changed()
                (
                    bound_ref,
                    candidate,
                    candidate_state,
                    capability,
                    capability_state,
                    qualification,
                    qualification_state,
                    decision_cut,
                ) = binding
                if bound_ref() is not self:
                    authority_changed()
                try:
                    current_candidate = object.__getattribute__(self, "candidate")
                    current_capability = object.__getattribute__(self, "capability")
                    current_qualification = object.__getattribute__(self, "qualification")
                    current_cut = object.__getattribute__(
                        self,
                        "decision_journal_sequence_cut",
                    )
                except AttributeError:
                    authority_changed()
                if (
                    current_candidate is not candidate
                    or candidate_snapshot(current_candidate) != candidate_state
                    or current_capability is not capability
                    or _capability_state(current_capability) != capability_state
                    or current_qualification is not qualification
                    or _qualification_state(current_qualification) != qualification_state
                    or type(current_cut) is not int
                    or current_cut != decision_cut
                ):
                    authority_changed()
            return object.__getattribute__(self, name)

        @property
        def qualification_id(self) -> str:
            return self.qualification.qualification_id

        @property
        def capability_snapshot_id(self) -> str:
            return self.capability.snapshot_id

    def issue_selected_route(
        *,
        candidate: ProviderCandidate,
        capability: CapabilitySnapshot,
        qualification: AcceptedProviderQualification,
        decision_journal_sequence_cut: int,
    ) -> SelectedProviderRoute:
        if type(candidate) is not ProviderCandidate:
            raise TypeError("candidate must be exact ProviderCandidate")
        if type(capability) is not CapabilitySnapshot:
            raise TypeError("capability must be exact CapabilitySnapshot")
        if type(qualification) is not AcceptedProviderQualification:
            raise TypeError(
                "qualification must be exact AcceptedProviderQualification"
            )
        if (
            type(decision_journal_sequence_cut) is not int
            or decision_journal_sequence_cut < 0
        ):
            raise ProviderSelectionError(
                "decision cut must be a non-negative exact integer"
            )

        dead = [
            key
            for key, (existing_ref, *_rest) in tuple(bindings.items())
            if existing_ref() is None
        ]
        for key in dead:
            bindings.pop(key, None)

        candidate_state = candidate_snapshot(candidate)
        capability_state = _capability_state(capability)
        qualification_state = _qualification_state(qualification)
        route = object.__new__(SelectedProviderRoute)
        object.__setattr__(route, "candidate", candidate)
        object.__setattr__(route, "capability", capability)
        object.__setattr__(route, "qualification", qualification)
        object.__setattr__(
            route,
            "decision_journal_sequence_cut",
            decision_journal_sequence_cut,
        )
        bindings[id(route)] = (
            route_ref(route),
            candidate,
            candidate_state,
            capability,
            capability_state,
            qualification,
            qualification_state,
            decision_journal_sequence_cut,
        )
        return route

    return SelectedProviderRoute, issue_selected_route


SelectedProviderRoute, _selected_route_issuer = _install_selected_route_authority()
del _install_selected_route_authority


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    provider_id: str
    product_family: str
    provider_environment: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProviderSelection:
    status: str
    selected: SelectedProviderRoute | None
    eligible: tuple[SelectedProviderRoute, ...]
    decisions: tuple[CandidateDecision, ...]
    decision_journal_sequence_cut: int


def _qualification_scope(
    candidate: ProviderCandidate,
    request: ProviderRouteRequest,
) -> ProviderQualificationCurrentScope:
    provider_scope = ProviderFinancialScope(
        provider_id=candidate.provider_id,
        runtime_environment=request.environment,
        provider_environment=candidate.provider_environment,
        entity_policy_id=candidate.entity_policy_id,
    )
    return ProviderQualificationCurrentScope(
        provider_scope=provider_scope,
        product_family=candidate.product_family,
        adapter_source_git_sha=candidate.adapter_code_sha,
        packaged_artifact_digest=candidate.packaged_artifact_digest,
        protocol_id=candidate.protocol_id,
        protocol_version=candidate.protocol_version,
    )


def _unsupported_features(request: ProviderRouteRequest) -> frozenset[str]:
    return frozenset(
        {
            f"ASSET_CLASS:{request.asset_class}",
            f"ORDER_TYPE:{request.order_type}",
            f"TIF:{request.time_in_force}",
            f"SCOPE:{request.permission_scope}",
        }
    )


def _select_provider_impl(
    request: ProviderRouteRequest,
    candidates: list[ProviderCandidate] | tuple[ProviderCandidate, ...],
    *,
    at: datetime,
    capability_registry: DurableCapabilityRegistry,
    qualification_registry: DurableProviderQualificationRegistry,
    route_issuer: Callable[..., SelectedProviderRoute],
    _compatibility=_ASSET_FAMILY_COMPATIBILITY,
) -> ProviderSelection:
    if type(request) is not ProviderRouteRequest:
        raise TypeError("request must be exact ProviderRouteRequest")
    if type(capability_registry) is not DurableCapabilityRegistry:
        raise TypeError("capability_registry must be exact DurableCapabilityRegistry")
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    if capability_registry.store is not qualification_registry.store:
        raise ProviderSelectionError(
            "capability and qualification authorities must share one JournalStore instance"
        )

    point = _instant(at, "at")
    if type(candidates) is tuple:
        materialized = candidates
    elif type(candidates) is list:
        materialized = tuple(candidates)
    else:
        raise TypeError("candidates must be an exact list or tuple")
    if any(type(candidate) is not ProviderCandidate for candidate in materialized):
        raise TypeError("candidates must contain exact ProviderCandidate values")
    identities = [candidate.identity for candidate in materialized]
    if len(identities) != len(set(identities)):
        raise ProviderSelectionError("provider route candidate identities must be unique")

    decision_cut = _journal_cut(capability_registry.store)
    allowed_pairs = _compatibility[request.asset_class]
    requested_features = _unsupported_features(request)
    eligible: list[SelectedProviderRoute] = []
    decisions: list[CandidateDecision] = []

    for candidate in sorted(materialized, key=lambda item: item.identity):
        reasons: list[str] = []
        capability: CapabilitySnapshot | None = None
        qualification: AcceptedProviderQualification | None = None

        if (candidate.provider_id, candidate.product_family) not in allowed_pairs:
            reasons.append("ASSET_PRODUCT_MISMATCH")

        try:
            scope = _qualification_scope(candidate, request)
        except (ProviderDomainError, ProviderQualificationCurrentScopeError, ValueError):
            scope = None
            reasons.append("PROVIDER_DOMAIN_MISMATCH")

        if scope is not None:
            try:
                current_q = qualification_registry.current(
                    scope=scope,
                    at=point,
                    journal_sequence_cut=decision_cut,
                )
                if current_q.journal_sequence_cut != decision_cut:
                    raise ProviderSelectionError(
                        "qualification authority did not honor decision cut"
                    )
                qualification = current_q.qualification
            except ProviderQualificationError:
                reasons.append("QUALIFICATION_NOT_CURRENT")

        try:
            capability = capability_registry.require_verified(
                provider_id=candidate.provider_id,
                account_id=candidate.account_id,
                entity_id=candidate.entity_id,
                environment=request.environment,
                provider_environment=candidate.provider_environment,
                instrument_version=request.instrument_version,
                at=point,
                journal_sequence_cut=decision_cut,
            )
        except CapabilityError:
            reasons.append("CAPABILITY_NOT_CURRENT_VERIFIED")

        if capability is not None and not capability.admits(
            at=point,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            permission_scope=request.permission_scope,
        ):
            reasons.append("CAPABILITY_DOES_NOT_ADMIT_ACTION")

        if (
            qualification is not None
            and frozenset(qualification.unsupported_features) & requested_features
        ):
            reasons.append("QUALIFICATION_EXPLICITLY_UNSUPPORTED")

        decision = CandidateDecision(
            provider_id=candidate.provider_id,
            product_family=candidate.product_family,
            provider_environment=candidate.provider_environment,
            eligible=not reasons,
            reasons=tuple(reasons),
        )
        decisions.append(decision)
        if not reasons:
            if capability is None or qualification is None:
                raise ProviderSelectionError(
                    "eligible route lacks resolved canonical authority"
                )
            eligible.append(
                route_issuer(
                    candidate=candidate,
                    capability=capability,
                    qualification=qualification,
                    decision_journal_sequence_cut=decision_cut,
                )
            )

    if _journal_cut(capability_registry.store) != decision_cut:
        return ProviderSelection(
            status="DECISION_CUT_ADVANCED_RETRY_REQUIRED",
            selected=None,
            eligible=(),
            decisions=tuple(decisions),
            decision_journal_sequence_cut=decision_cut,
        )

    preferred = request.preferred_provider_id
    if preferred is not None:
        matching = [route for route in eligible if route.candidate.provider_id == preferred]
        if len(matching) == 1:
            return ProviderSelection(
                status="SELECTED_BY_EXPLICIT_POLICY",
                selected=matching[0],
                eligible=tuple(eligible),
                decisions=tuple(decisions),
                decision_journal_sequence_cut=decision_cut,
            )
        return ProviderSelection(
            status="NO_ELIGIBLE_PREFERRED_PROVIDER",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
            decision_journal_sequence_cut=decision_cut,
        )

    if len(eligible) == 1:
        return ProviderSelection(
            status="SELECTED_UNAMBIGUOUS",
            selected=eligible[0],
            eligible=tuple(eligible),
            decisions=tuple(decisions),
            decision_journal_sequence_cut=decision_cut,
        )
    if len(eligible) > 1:
        return ProviderSelection(
            status="AMBIGUOUS_REQUIRES_POLICY",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
            decision_journal_sequence_cut=decision_cut,
        )
    return ProviderSelection(
        status="NO_ELIGIBLE_PROVIDER",
        selected=None,
        eligible=(),
        decisions=tuple(decisions),
        decision_journal_sequence_cut=decision_cut,
    )


def _install_provider_selector(
    route_issuer: Callable[..., SelectedProviderRoute],
    implementation: Callable[..., ProviderSelection],
) -> Callable[..., ProviderSelection]:
    """Bind canonical route issuance and detach caller-owned route inputs."""

    request_type = ProviderRouteRequest
    candidate_type = ProviderCandidate
    request_initializer = request_type.__post_init__
    candidate_initializer = candidate_type.__post_init__
    object_new = object.__new__
    object_getattribute = object.__getattribute__
    object_setattr = object.__setattr__
    request_fields = (
        "asset_class",
        "environment",
        "instrument_version",
        "order_type",
        "time_in_force",
        "permission_scope",
        "preferred_provider_id",
    )
    candidate_fields = (
        "provider_id",
        "product_family",
        "provider_environment",
        "account_id",
        "entity_id",
        "entity_policy_id",
        "adapter_code_sha",
        "packaged_artifact_digest",
        "protocol_id",
        "protocol_version",
    )

    def detach_request(value: object) -> ProviderRouteRequest:
        if type(value) is not request_type:
            raise TypeError("request must be exact ProviderRouteRequest")
        state = tuple(object_getattribute(value, name) for name in request_fields)
        if any(type(item) is not str for item in state[:-1]) or (
            state[-1] is not None and type(state[-1]) is not str
        ):
            raise TypeError("request fields must be canonical inert values")
        detached = object_new(request_type)
        for name, item in zip(request_fields, state, strict=True):
            object_setattr(detached, name, item)
        request_initializer(detached)
        return detached

    def detach_candidate(value: object) -> ProviderCandidate:
        if type(value) is not candidate_type:
            raise TypeError("candidates must contain exact ProviderCandidate values")
        state = tuple(object_getattribute(value, name) for name in candidate_fields)
        if any(type(item) is not str for item in state):
            raise TypeError("candidate fields must be canonical inert values")
        detached = object_new(candidate_type)
        for name, item in zip(candidate_fields, state, strict=True):
            object_setattr(detached, name, item)
        candidate_initializer(detached)
        return detached

    def select_provider(
        request: ProviderRouteRequest,
        candidates: list[ProviderCandidate] | tuple[ProviderCandidate, ...],
        *,
        at: datetime,
        capability_registry: DurableCapabilityRegistry,
        qualification_registry: DurableProviderQualificationRegistry,
    ) -> ProviderSelection:
        """Resolve one exact route from durable C/Q authority or fail closed.

        Caller-owned request/candidate objects are detached before any registry
        callback. C and Q are replayed at one captured global journal sequence;
        if that JournalStore advances during selection, no route is returned.
        """

        detached_request = detach_request(request)
        if type(candidates) is tuple:
            detached_candidates = tuple(detach_candidate(item) for item in candidates)
        elif type(candidates) is list:
            detached_candidates = tuple(detach_candidate(item) for item in candidates)
        else:
            raise TypeError("candidates must be an exact list or tuple")

        return implementation(
            detached_request,
            detached_candidates,
            at=at,
            capability_registry=capability_registry,
            qualification_registry=qualification_registry,
            route_issuer=route_issuer,
        )

    return select_provider


select_provider = _install_provider_selector(
    _selected_route_issuer,
    _select_provider_impl,
)
del _selected_route_issuer
del _select_provider_impl
del _install_provider_selector
