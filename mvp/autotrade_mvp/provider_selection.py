"""Deterministic provider crosswalk without implicit fallback or live authority.

This module joins the architectural provider registry, exact-code qualification
evidence and account/instrument capability snapshots. It never sends orders and
never upgrades non-live qualification into live trading authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable

from .capabilities import CapabilityError, CapabilityRegistry, CapabilitySnapshot
from .provider_core import QualificationEvidence, provider_definition
from .provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationAuthorityError,
    ProviderQualificationCurrentReader,
)


ASSET_FAMILY_COMPATIBILITY = {
    "CRYPTO_SPOT": {
        ("BYBIT", "SPOT"),
        ("KRAKEN", "SPOT"),
        ("WHITEBIT", "SPOT"),
        ("BINANCE", "SPOT"),
        ("ALPACA", "CRYPTO"),
    },
    "CRYPTO_MARGIN": {
        ("BYBIT", "MARGIN"),
        ("KRAKEN", "MARGIN"),
        ("WHITEBIT", "COLLATERAL"),
        ("BINANCE", "MARGIN"),
    },
    "LINEAR_PERPETUAL": {
        ("BYBIT", "LINEAR_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("WHITEBIT", "FUTURES"),
        ("BINANCE", "USD_M"),
    },
    "INVERSE_PERPETUAL": {
        ("BYBIT", "INVERSE_DERIVATIVES"),
        ("KRAKEN", "DERIVATIVES"),
        ("BINANCE", "COIN_M"),
    },
    "LISTED_FUTURE": {
        ("IBKR", "FUTURES"),
    },
    "EQUITY": {
        ("IBKR", "EQUITIES"),
        ("ALPACA", "EQUITIES"),
    },
    "OPTION": {
        ("BYBIT", "OPTIONS"),
        ("BINANCE", "OPTIONS"),
        ("IBKR", "OPTIONS"),
        ("ALPACA", "OPTIONS"),
    },
    "FX": {
        ("IBKR", "FX"),
    },
}


class ProviderSelectionError(ValueError):
    pass


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProviderSelectionError(f"{name} is required")
    return value.strip()


def _code_sha(value: str) -> str:
    text = _text(value, "adapter_code_sha")
    if len(text) != 40:
        raise ProviderSelectionError(
            "adapter_code_sha must be a canonical 40-character Git SHA"
        )
    try:
        int(text, 16)
    except ValueError as error:
        raise ProviderSelectionError("adapter_code_sha must be hexadecimal") from error
    if text != text.lower():
        raise ProviderSelectionError("adapter_code_sha must use canonical lowercase hex")
    return text


def _instant(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderSelectionError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ProviderRouteRequest:
    asset_class: str
    environment: str
    instrument_version: str
    order_type: str
    time_in_force: str
    permission_scope: str
    preferred_provider_id: str | None = None

    def __post_init__(self) -> None:
        asset = _text(self.asset_class, "asset_class").upper()
        if asset not in ASSET_FAMILY_COMPATIBILITY:
            raise ProviderSelectionError("asset_class has no canonical provider crosswalk")
        object.__setattr__(self, "asset_class", asset)
        environment = _text(self.environment, "environment").upper()
        if environment not in {"PAPER", "LIVE"}:
            raise ProviderSelectionError(
                "external provider selection is restricted to PAPER or LIVE"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self, "instrument_version", _text(self.instrument_version, "instrument_version")
        )
        object.__setattr__(self, "order_type", _text(self.order_type, "order_type").upper())
        object.__setattr__(
            self, "time_in_force", _text(self.time_in_force, "time_in_force").upper()
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


@dataclass(frozen=True)
class ProviderCandidate:
    """Policy candidate plus non-authoritative expected C/Q identities.

    qualification is retained only for legacy diagnostic compatibility.  Route
    eligibility never reads its passed cases, timestamps, unsupported features,
    or status.  Production eligibility comes from current Q + current C.
    """

    provider_id: str
    product_family: str
    adapter_code_sha: str
    qualification: QualificationEvidence | None
    capability: CapabilitySnapshot
    qualification_id: str | None = None
    route_policy_id: str | None = None
    entity_policy_id: str | None = None
    network_policy_id: str | None = None
    account_class: str | None = None
    release_artifact_id: str | None = None
    release_artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        definition = provider_definition(provider)
        family = _text(self.product_family, "product_family").upper()
        if family not in definition.product_families:
            raise ProviderSelectionError("product_family is not declared for provider")
        if type(self.capability) is not CapabilitySnapshot:
            raise TypeError("capability must be exact CapabilitySnapshot")
        if self.capability.provider_id.upper() != provider:
            raise ProviderSelectionError("capability provider does not match candidate")
        diagnostic = self.qualification
        if diagnostic is not None:
            if type(diagnostic) is not QualificationEvidence:
                raise TypeError("qualification must be exact QualificationEvidence or None")
            if diagnostic.provider_id != provider:
                raise ProviderSelectionError(
                    "diagnostic qualification provider does not match candidate"
                )
            if diagnostic.product_family != family:
                raise ProviderSelectionError(
                    "diagnostic qualification product family does not match candidate"
                )
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "product_family", family)
        object.__setattr__(
            self, "adapter_code_sha", _code_sha(self.adapter_code_sha)
        )

        if self.qualification_id is not None:
            qid = _text(self.qualification_id, "qualification_id")
            if (
                not qid.startswith("sha256:")
                or len(qid) != 71
            ):
                raise ProviderSelectionError(
                    "qualification_id must be canonical sha256 identity"
                )
            try:
                int(qid[7:], 16)
            except ValueError as error:
                raise ProviderSelectionError(
                    "qualification_id must be canonical sha256 identity"
                ) from error
            object.__setattr__(self, "qualification_id", qid)

        for field in ("route_policy_id", "entity_policy_id", "network_policy_id"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, _text(value, field))
        if self.account_class is not None:
            object.__setattr__(
                self,
                "account_class",
                _text(self.account_class, "account_class").upper(),
            )
        if (self.release_artifact_id is None) != (
            self.release_artifact_sha256 is None
        ):
            raise ProviderSelectionError(
                "release artifact identity and digest must be supplied together"
            )


@dataclass(frozen=True)
class CandidateDecision:
    provider_id: str
    product_family: str
    eligible: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SelectedProviderAuthority:
    """One bounded route decision binding the exact current C + accepted Q."""

    provider_id: str
    product_family: str
    adapter_code_sha: str
    qualification_id: str
    capability_snapshot_id: str
    account_id: str
    entity_id: str
    environment: str
    provider_environment: str
    instrument_version: str
    route_policy_id: str
    entity_policy_id: str
    network_policy_id: str
    account_class: str
    release_artifact_id: str | None
    release_artifact_sha256: str | None
    reconciliation_semantics_id: str | None


@dataclass(frozen=True)
class ProviderSelection:
    status: str
    selected: SelectedProviderAuthority | None
    eligible: tuple[SelectedProviderAuthority, ...]
    decisions: tuple[CandidateDecision, ...]


def _append_reason(reasons: list[str], value: str) -> None:
    if value not in reasons:
        reasons.append(value)


def _qualification_scope_ready(candidate: ProviderCandidate) -> bool:
    return all(
        getattr(candidate, name) is not None
        for name in (
            "route_policy_id",
            "entity_policy_id",
            "network_policy_id",
            "account_class",
        )
    )


def _current_qualification(
    candidate: ProviderCandidate,
    reader: ProviderQualificationCurrentReader,
) -> AcceptedProviderQualification:
    return reader.require_current_for_route(
        provider_id=candidate.provider_id,
        product_family=candidate.product_family,
        environment=candidate.capability.environment,
        provider_environment=candidate.capability.provider_environment,
        route_policy_id=candidate.route_policy_id,
        entity_policy_id=candidate.entity_policy_id,
        network_policy_id=candidate.network_policy_id,
        account_class=candidate.account_class,
        adapter_source_sha=candidate.adapter_code_sha,
        release_artifact_id=candidate.release_artifact_id,
        release_artifact_sha256=candidate.release_artifact_sha256,
        expected_qualification_id=candidate.qualification_id,
    )


def _current_capability(
    candidate: ProviderCandidate,
    registry: CapabilityRegistry,
    *,
    at: datetime,
) -> CapabilitySnapshot:
    return registry.require_verified(
        provider_id=candidate.provider_id,
        account_id=candidate.capability.account_id,
        entity_id=candidate.capability.entity_id,
        environment=candidate.capability.environment,
        provider_environment=candidate.capability.provider_environment,
        instrument_version=candidate.capability.instrument_version,
        at=at,
    )


def _selected_authority(
    candidate: ProviderCandidate,
    capability: CapabilitySnapshot,
    qualification: AcceptedProviderQualification,
) -> SelectedProviderAuthority:
    campaign = qualification.campaign
    scope = qualification.provider_scope
    return SelectedProviderAuthority(
        provider_id=candidate.provider_id,
        product_family=candidate.product_family,
        adapter_code_sha=candidate.adapter_code_sha,
        qualification_id=qualification.qualification_id,
        capability_snapshot_id=capability.snapshot_id,
        account_id=capability.account_id,
        entity_id=capability.entity_id,
        environment=capability.environment,
        provider_environment=capability.provider_environment,
        instrument_version=capability.instrument_version,
        route_policy_id=scope.route_policy_id,
        entity_policy_id=campaign.entity_policy_id,
        network_policy_id=campaign.network_policy_id,
        account_class=campaign.account_class,
        release_artifact_id=campaign.release_artifact_id,
        release_artifact_sha256=campaign.release_artifact_sha256,
        reconciliation_semantics_id=qualification.reconciliation_semantics_id,
    )


def select_provider(
    request: ProviderRouteRequest,
    candidates: Iterable[ProviderCandidate],
    *,
    at: datetime,
    qualification_reader: ProviderQualificationCurrentReader | None = None,
    capability_registry: CapabilityRegistry | None = None,
) -> ProviderSelection:
    """Resolve one exact route from current accepted Q + current C or fail closed.

    Legacy caller-built QualificationEvidence remains diagnostic only.  A route
    is eligible only when an exact ProviderQualificationCurrentReader and exact
    CapabilityRegistry resolve the same provider/domain/build twice around the
    decision, and the returned selection freezes both authority identities.

    This bounded selector does not replace the final pre-wire barrier: a prepared
    request must later re-resolve the exact qualification_id and capability id.
    """

    if type(request) is not ProviderRouteRequest:
        raise TypeError("request must be exact ProviderRouteRequest")
    if (
        qualification_reader is not None
        and type(qualification_reader) is not ProviderQualificationCurrentReader
    ):
        raise TypeError(
            "qualification_reader must be exact ProviderQualificationCurrentReader"
        )
    if (
        capability_registry is not None
        and type(capability_registry) is not CapabilityRegistry
    ):
        raise TypeError("capability_registry must be exact CapabilityRegistry")

    point = _instant(at, "at")
    materialized = tuple(candidates)
    if any(type(candidate) is not ProviderCandidate for candidate in materialized):
        raise TypeError("candidates must contain exact ProviderCandidate values")
    identities = [
        (
            candidate.provider_id,
            candidate.product_family,
            candidate.capability.account_id,
            candidate.capability.entity_id,
            candidate.capability.environment,
            candidate.capability.provider_environment,
            candidate.capability.instrument_version,
        )
        for candidate in materialized
    ]
    if len(identities) != len(set(identities)):
        raise ProviderSelectionError(
            "provider route candidate identities must be unique"
        )

    allowed_pairs = ASSET_FAMILY_COMPATIBILITY[request.asset_class]
    eligible: list[SelectedProviderAuthority] = []
    decisions: list[CandidateDecision] = []

    for candidate in sorted(
        materialized,
        key=lambda item: (
            item.provider_id,
            item.product_family,
            item.capability.provider_environment,
            item.capability.account_id,
            item.capability.entity_id,
        ),
    ):
        reasons: list[str] = []
        pair = (candidate.provider_id, candidate.product_family)
        if pair not in allowed_pairs:
            _append_reason(reasons, "ASSET_PRODUCT_MISMATCH")

        expected_capability = candidate.capability
        if expected_capability.environment != request.environment:
            _append_reason(reasons, "CAPABILITY_ENVIRONMENT_MISMATCH")
        if expected_capability.instrument_version != request.instrument_version:
            _append_reason(reasons, "CAPABILITY_INSTRUMENT_MISMATCH")

        if qualification_reader is None:
            _append_reason(reasons, "QUALIFICATION_AUTHORITY_REQUIRED")
        if capability_registry is None:
            _append_reason(reasons, "CAPABILITY_AUTHORITY_REQUIRED")
        if not _qualification_scope_ready(candidate):
            _append_reason(reasons, "QUALIFICATION_ROUTE_SCOPE_REQUIRED")

        current_q: AcceptedProviderQualification | None = None
        current_c: CapabilitySnapshot | None = None
        if (
            qualification_reader is not None
            and capability_registry is not None
            and _qualification_scope_ready(candidate)
        ):
            try:
                current_q = _current_qualification(candidate, qualification_reader)
            except ProviderQualificationAuthorityError:
                _append_reason(
                    reasons,
                    "QUALIFICATION_NOT_CURRENT_OR_SCOPE_MISMATCH",
                )
            try:
                current_c = _current_capability(
                    candidate,
                    capability_registry,
                    at=point,
                )
            except CapabilityError:
                _append_reason(reasons, "CAPABILITY_NOT_CURRENT")

        if current_q is not None:
            campaign = current_q.campaign
            scope = current_q.provider_scope
            if scope.provider_id != candidate.provider_id:
                _append_reason(reasons, "QUALIFICATION_PROVIDER_MISMATCH")
            if campaign.product_family != candidate.product_family:
                _append_reason(reasons, "QUALIFICATION_PRODUCT_MISMATCH")
            if campaign.adapter_source_sha != candidate.adapter_code_sha:
                _append_reason(reasons, "QUALIFICATION_CODE_MISMATCH")
            if scope.environment != request.environment:
                _append_reason(reasons, "QUALIFICATION_ENVIRONMENT_MISMATCH")
            if candidate.qualification_id is not None and (
                current_q.qualification_id != candidate.qualification_id
            ):
                _append_reason(reasons, "QUALIFICATION_ID_MISMATCH")
            if request.environment == "LIVE" and not campaign.live_authorized:
                _append_reason(reasons, "LIVE_QUALIFICATION_NOT_ESTABLISHED")

            requested_features = {
                f"ASSET_CLASS:{request.asset_class}",
                f"ORDER_TYPE:{request.order_type}",
                f"TIF:{request.time_in_force}",
                f"SCOPE:{request.permission_scope}",
            }
            if set(campaign.unsupported_features) & requested_features:
                _append_reason(
                    reasons,
                    "QUALIFICATION_EXPLICITLY_UNSUPPORTED",
                )

        if current_c is not None:
            if type(current_c) is not CapabilitySnapshot:
                raise ProviderSelectionError(
                    "capability registry returned non-canonical snapshot"
                )
            if current_c.snapshot_id != expected_capability.snapshot_id:
                _append_reason(reasons, "CAPABILITY_SNAPSHOT_ID_MISMATCH")
            if current_c.environment != request.environment:
                _append_reason(reasons, "CAPABILITY_ENVIRONMENT_MISMATCH")
            if current_c.instrument_version != request.instrument_version:
                _append_reason(reasons, "CAPABILITY_INSTRUMENT_MISMATCH")
            if not current_c.admits(
                at=point,
                order_type=request.order_type,
                time_in_force=request.time_in_force,
                permission_scope=request.permission_scope,
            ):
                _append_reason(reasons, "CAPABILITY_DOES_NOT_ADMIT_ACTION")

        if current_q is not None and current_c is not None:
            if (
                current_q.provider_scope.provider_environment
                != current_c.provider_environment
            ):
                _append_reason(reasons, "QUALIFICATION_CAPABILITY_DOMAIN_MISMATCH")

            if not reasons:
                try:
                    final_q = _current_qualification(
                        candidate,
                        qualification_reader,
                    )
                except ProviderQualificationAuthorityError:
                    _append_reason(
                        reasons,
                        "QUALIFICATION_REGISTRY_CHANGED_DURING_SELECTION",
                    )
                else:
                    if final_q != current_q:
                        _append_reason(
                            reasons,
                            "QUALIFICATION_REGISTRY_CHANGED_DURING_SELECTION",
                        )
                try:
                    final_c = _current_capability(
                        candidate,
                        capability_registry,
                        at=point,
                    )
                except CapabilityError:
                    _append_reason(
                        reasons,
                        "CAPABILITY_REGISTRY_CHANGED_DURING_SELECTION",
                    )
                else:
                    if final_c != current_c:
                        _append_reason(
                            reasons,
                            "CAPABILITY_REGISTRY_CHANGED_DURING_SELECTION",
                        )

        decision = CandidateDecision(
            provider_id=candidate.provider_id,
            product_family=candidate.product_family,
            eligible=not reasons,
            reasons=tuple(reasons),
        )
        decisions.append(decision)
        if not reasons and current_q is not None and current_c is not None:
            eligible.append(
                _selected_authority(candidate, current_c, current_q)
            )

    preferred = request.preferred_provider_id
    if preferred is not None:
        matching = [
            authority
            for authority in eligible
            if authority.provider_id == preferred
        ]
        if len(matching) == 1:
            return ProviderSelection(
                status="SELECTED_BY_EXPLICIT_POLICY",
                selected=matching[0],
                eligible=tuple(eligible),
                decisions=tuple(decisions),
            )
        return ProviderSelection(
            status="NO_ELIGIBLE_PREFERRED_PROVIDER",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )

    if len(eligible) == 1:
        return ProviderSelection(
            status="SELECTED_UNAMBIGUOUS",
            selected=eligible[0],
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )
    if len(eligible) > 1:
        return ProviderSelection(
            status="AMBIGUOUS_REQUIRES_POLICY",
            selected=None,
            eligible=tuple(eligible),
            decisions=tuple(decisions),
        )
    return ProviderSelection(
        status="NO_ELIGIBLE_PROVIDER",
        selected=None,
        eligible=(),
        decisions=tuple(decisions),
    )
