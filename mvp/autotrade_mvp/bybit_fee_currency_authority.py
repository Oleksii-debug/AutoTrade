"""Qualified Bybit execution fee-currency authority.

The execution payload is primary evidence when ``feeCurrency`` is present.
When Bybit omits that field, this module allows a fallback only when one exact
accepted provider Q has qualified a versioned fee rule for one exact canonical
InstrumentVersion and one fresh capability identity.

This authority does not qualify a provider, grant trading permission, infer a
currency from symbol spelling/quote/settlement assets, or perform network I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from types import MappingProxyType
import weakref

from .capabilities import CapabilitySnapshot
from .instruments import InstrumentRegistry, InstrumentRegistryError, InstrumentVersion
from .persistence import canonical_json
from .provider_qualification_authority import (
    AcceptedProviderQualification,
    ProviderQualificationScope,
)
from .provider_qualification_identity import ProviderQualificationIdentity
from .provider_domain import ProviderFinancialScope
from .qualification_attestation import EvidenceArtifactRef


_SHA_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}$")
_BYBIT_CATEGORY_BY_PRODUCT_FAMILY = MappingProxyType(
    {
        "SPOT": "spot",
        "MARGIN": "spot",
        "LINEAR_DERIVATIVES": "linear",
        "INVERSE_DERIVATIVES": "inverse",
        "OPTIONS": "option",
    }
)


class BybitFeeCurrencyAuthorityError(ValueError):
    """Bybit fee-currency authority could not be established or consumed safely."""


def _text(value: object, *, name: str, upper: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BybitFeeCurrencyAuthorityError(f"{name} must be canonical non-empty text")
    result = value.upper() if upper else value
    if len(result) > 512:
        raise BybitFeeCurrencyAuthorityError(f"{name} exceeds canonical text bound")
    return result


def _token(value: object, *, name: str, upper: bool = False) -> str:
    result = _text(value, name=name, upper=upper)
    if _TOKEN_RE.fullmatch(result) is None:
        raise BybitFeeCurrencyAuthorityError(f"{name} is not canonical")
    return result


def _point(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BybitFeeCurrencyAuthorityError(
            f"{name} must be exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, *, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip() or not value.endswith("Z"):
        raise BybitFeeCurrencyAuthorityError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BybitFeeCurrencyAuthorityError(f"{name} must be canonical UTC text") from error
    canonical = _utc_text(point)
    if canonical != value:
        raise BybitFeeCurrencyAuthorityError(f"{name} must be canonical UTC text")
    return point.astimezone(timezone.utc)


def _instrument_ref(version: InstrumentVersion) -> str:
    if type(version) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    return f"{version.instrument_id}@{version.version}"


def _instrument_evidence_digest(version: InstrumentVersion) -> str:
    if type(version.metadata_evidence) is not tuple or not version.metadata_evidence:
        raise BybitFeeCurrencyAuthorityError(
            "instrument version lacks immutable metadata evidence"
        )
    evidence = []
    for item in version.metadata_evidence:
        if type(item) is not MappingProxyType:
            raise BybitFeeCurrencyAuthorityError(
                "instrument metadata evidence is not canonical immutable evidence"
            )
        evidence.append(dict(item))
    return "sha256:" + sha256(canonical_json(evidence).encode("utf-8")).hexdigest()


def _canonical_digest(value: object) -> str:
    return "sha256:" + sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


def _fee_rule_material(
    *,
    provider_environment: object,
    product_family: object,
    category: object,
    instrument: InstrumentVersion,
    fee_currency: object,
    rule_id: object,
    rule_version: object,
    rule_valid_from: object,
    rule_valid_until: object,
) -> tuple[str, str, dict[str, object]]:
    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument must be exact InstrumentVersion")
    if instrument.provider_id != "BYBIT":
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency rule instrument must belong to BYBIT"
        )
    provider_environment = _token(
        provider_environment,
        name="provider_environment",
        upper=True,
    )
    product_family = _token(product_family, name="product_family", upper=True)
    expected_category = _BYBIT_CATEGORY_BY_PRODUCT_FAMILY.get(product_family)
    if expected_category is None:
        raise BybitFeeCurrencyAuthorityError(
            "product_family has no canonical Bybit execution category"
        )
    category = _text(category, name="category")
    if category != expected_category:
        raise BybitFeeCurrencyAuthorityError(
            "category does not match canonical Bybit product family"
        )
    raw_fee_currency = _text(fee_currency, name="fee_currency")
    fee_currency = _token(raw_fee_currency, name="fee_currency", upper=True)
    if fee_currency != raw_fee_currency:
        raise BybitFeeCurrencyAuthorityError(
            "fee_currency must already be canonical uppercase text"
        )
    rule_id = _token(rule_id, name="rule_id")
    rule_version = _token(rule_version, name="rule_version")
    valid_from = _point(rule_valid_from, name="rule_valid_from")
    valid_until = _point(rule_valid_until, name="rule_valid_until")
    if valid_until <= valid_from:
        raise BybitFeeCurrencyAuthorityError(
            "rule_valid_until must be after rule_valid_from"
        )
    instrument_ref = _instrument_ref(instrument)
    instrument_binding = instrument.metadata_evidence_binding()
    instrument_evidence_digest = _instrument_evidence_digest(instrument)
    locator = {
        "provider_id": "BYBIT",
        "provider_environment": provider_environment,
        "product_family": product_family,
        "category": category,
        "instrument_version": instrument_ref,
        "provider_symbol": instrument.provider_symbol,
        "rule_id": rule_id,
        "rule_version": rule_version,
    }
    claim_key = "EXECUTION_FEE_CURRENCY_RULE:" + sha256(
        canonical_json(locator).encode("utf-8")
    ).hexdigest()
    policy = {
        **locator,
        "fee_currency": fee_currency,
        "rule_valid_from": _utc_text(valid_from),
        "rule_valid_until": _utc_text(valid_until),
        "instrument_metadata_binding": instrument_binding,
        "instrument_evidence_digest": instrument_evidence_digest,
    }
    claim_digest = "sha256:" + sha256(
        canonical_json(policy).encode("utf-8")
    ).hexdigest()
    return claim_key, claim_digest, policy


def bybit_execution_fee_currency_semantic_claim(
    *,
    provider_environment: str,
    product_family: str,
    category: str,
    instrument: InstrumentVersion,
    fee_currency: str,
    rule_id: str,
    rule_version: str,
    rule_valid_from: datetime,
    rule_valid_until: datetime,
) -> tuple[str, str]:
    """Return the exact provider-Q semantic claim for one Bybit fee rule."""

    claim_key, claim_digest, _policy = _fee_rule_material(
        provider_environment=provider_environment,
        product_family=product_family,
        category=category,
        instrument=instrument,
        fee_currency=fee_currency,
        rule_id=rule_id,
        rule_version=rule_version,
        rule_valid_from=rule_valid_from,
        rule_valid_until=rule_valid_until,
    )
    return claim_key, claim_digest


def _qualification_semantics(
    qualification: AcceptedProviderQualification,
) -> dict[str, str]:
    if type(qualification) is not AcceptedProviderQualification:
        raise TypeError(
            "qualification must be exact AcceptedProviderQualification"
        )
    raw = object.__getattribute__(qualification, "route_semantics_json")
    if type(raw) is not str or not raw:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification route semantics are unavailable"
        )
    try:
        semantics = json.loads(raw)
    except (json.JSONDecodeError, RecursionError) as error:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification route semantics are malformed"
        ) from error
    if (
        type(semantics) is not dict
        or not semantics
        or any(
            type(key) is not str or type(value) is not str
            for key, value in semantics.items()
        )
        or canonical_json(semantics) != raw
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification route semantics are non-canonical"
        )
    identity = object.__getattribute__(qualification, "identity")
    scope = object.__getattribute__(qualification, "scope")
    if type(identity) is not ProviderQualificationIdentity:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification identity is non-canonical"
        )
    if type(scope) is not ProviderQualificationScope:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope is non-canonical"
        )
    provider_scope = object.__getattribute__(scope, "provider_scope")
    if type(provider_scope) is not ProviderFinancialScope:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification financial scope is non-canonical"
        )

    for field_name in (
        "provider_id",
        "runtime_environment",
        "provider_environment",
        "entity_policy_id",
    ):
        if type(object.__getattribute__(provider_scope, field_name)) is not str:
            raise BybitFeeCurrencyAuthorityError(
                "provider qualification financial scope fields are non-canonical"
            )

    for field_name in (
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
        "content_digest",
    ):
        # content_digest is a computed property; all source fields it consumes
        # must be exact scalar types before it can be read safely.
        if field_name == "content_digest":
            continue
        if type(getattr(identity, field_name)) is not str:
            raise BybitFeeCurrencyAuthorityError(
                "provider qualification identity fields are non-canonical"
            )

    for field_name in ("campaign_id", "protocol_id", "protocol_version", "product_family"):
        if type(getattr(scope, field_name)) is not str:
            raise BybitFeeCurrencyAuthorityError(
                "provider qualification scope fields are non-canonical"
            )
    if type(getattr(scope, "campaign_version", None)) is not int:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope fields are non-canonical"
        )
    if type(getattr(scope, "packaged_artifact_digest", None)) is not str:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope fields are non-canonical"
        )
    if type(getattr(scope, "adapter_source_git_sha", None)) is not str:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope fields are non-canonical"
        )

    qualification_id = object.__getattribute__(qualification, "qualification_id")
    if type(qualification_id) is not str:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification id is non-canonical"
        )
    semantics_digest = "sha256:" + sha256(raw.encode("utf-8")).hexdigest()
    if (
        getattr(identity, "route_semantics_digest", None) != semantics_digest
        or getattr(identity, "content_digest", None) != qualification_id
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification semantics do not match Q identity"
        )
    if (
        getattr(identity, "provider_scope", None) != provider_scope
        or getattr(identity, "product_family", None) != getattr(scope, "product_family", None)
        or getattr(identity, "adapter_source_git_sha", None)
        != getattr(scope, "adapter_source_git_sha", None)
        or getattr(identity, "packaged_artifact_digest", None)
        != getattr(scope, "packaged_artifact_digest", None)
        or getattr(identity, "campaign_id", None) != getattr(scope, "campaign_id", None)
        or getattr(identity, "campaign_version", None)
        != getattr(scope, "campaign_version", None)
        or getattr(identity, "protocol_id", None) != getattr(scope, "protocol_id", None)
        or getattr(identity, "protocol_version", None)
        != getattr(scope, "protocol_version", None)
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope does not match Q identity"
        )

    campaign_ref = object.__getattribute__(
        qualification,
        "campaign_artifact_ref",
    )
    raw_refs = object.__getattribute__(qualification, "raw_evidence_refs")
    if (
        type(campaign_ref) is not EvidenceArtifactRef
        or type(raw_refs) is not tuple
        or any(type(item) is not EvidenceArtifactRef for item in raw_refs)
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification evidence set is non-canonical"
        )
    all_refs = tuple(
        sorted(
            (campaign_ref, *raw_refs),
            key=lambda item: item.artifact_id,
        )
    )
    if getattr(identity, "evidence_set_digest", None) != _canonical_digest(
        [item.canonical() for item in all_refs]
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification evidence set does not match Q identity"
        )

    completed_at = object.__getattribute__(qualification, "completed_at")
    valid_until = object.__getattribute__(qualification, "valid_until")
    if getattr(identity, "chronology_digest", None) != _canonical_digest(
        {
            "completed_at": completed_at,
            "valid_until": valid_until,
        }
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification chronology does not match Q identity"
        )

    attestation_id = object.__getattribute__(qualification, "attestation_id")
    attestation_digest = object.__getattribute__(
        qualification,
        "attestation_digest",
    )
    policy_id = object.__getattribute__(qualification, "policy_id")
    policy_version = object.__getattribute__(qualification, "policy_version")
    trust_root_id = object.__getattribute__(qualification, "trust_root_id")
    producer_id = object.__getattribute__(qualification, "producer_id")
    verifier_id = object.__getattribute__(qualification, "verifier_id")
    signed_at = object.__getattribute__(qualification, "signed_at")
    release_artifact_id = object.__getattribute__(
        qualification,
        "release_artifact_id",
    )
    acceptance_metadata = {
        "attestation_id": attestation_id,
        "attestation_digest": attestation_digest,
        "policy_id": policy_id,
        "policy_version": policy_version,
        "trust_root_id": trust_root_id,
        "producer_id": producer_id,
        "verifier_id": verifier_id,
        "signed_at": signed_at,
        "release_artifact_id": release_artifact_id,
    }
    if (
        getattr(identity, "acceptance_metadata_digest", None)
        != _canonical_digest(acceptance_metadata)
        or getattr(identity, "attestation_digest", None) != attestation_digest
        or getattr(identity, "trust_policy_digest", None) != policy_id
        or getattr(identity, "issuer_identity_digest", None)
        != _canonical_digest(
            {
                "producer_id": producer_id,
                "trust_root_id": trust_root_id,
            }
        )
        or getattr(identity, "verifier_identity_digest", None)
        != _canonical_digest({"verifier_id": verifier_id})
        or getattr(identity, "packaged_artifact_id", None)
        != release_artifact_id
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification acceptance metadata does not match Q identity"
        )

    required_cases = object.__getattribute__(qualification, "required_cases")
    unsupported_features = object.__getattribute__(
        qualification,
        "unsupported_features",
    )
    documentation_revisions = object.__getattribute__(
        qualification,
        "documentation_revisions",
    )
    supersedes_qualification_id = object.__getattribute__(
        qualification,
        "supersedes_qualification_id",
    )
    for field_name, value in (
        ("required_cases", required_cases),
        ("unsupported_features", unsupported_features),
        ("documentation_revisions", documentation_revisions),
    ):
        if type(value) is not tuple or any(type(item) is not str for item in value):
            raise BybitFeeCurrencyAuthorityError(
                f"provider qualification {field_name} are non-canonical"
            )
    if supersedes_qualification_id is not None and type(
        supersedes_qualification_id
    ) is not str:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification lineage is non-canonical"
        )
    if (
        getattr(identity, "required_case_policy_digest", None)
        != _canonical_digest(list(required_cases))
        or getattr(identity, "result_set_digest", None)
        != _canonical_digest(
            {
                "passed_cases": list(required_cases),
                "failed_cases": [],
                "unsupported_features": list(unsupported_features),
            }
        )
        or getattr(identity, "documentation_revision_digest", None)
        != _canonical_digest(list(documentation_revisions))
        or getattr(identity, "lineage_digest", None)
        != _canonical_digest(
            {"supersedes_qualification_id": supersedes_qualification_id}
        )
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification campaign content does not match Q identity"
        )
    if (
        getattr(identity, "content_digest", None)
        != object.__getattribute__(qualification, "qualification_id")
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification content identity does not match Q record"
        )
    return semantics


@dataclass(frozen=True, slots=True)
class BybitExecutionFeeCurrencyProjection:
    provider_id: str
    runtime_environment: str
    provider_environment: str
    account_id: str
    entity_id: str
    entity_policy_id: str
    capability_snapshot_id: str
    qualification_id: str
    adapter_source_git_sha: str
    packaged_artifact_digest: str
    product_family: str
    category: str
    instrument_version: str
    provider_symbol: str
    fee_currency: str
    rule_id: str
    rule_version: str
    rule_valid_from: datetime
    rule_valid_until: datetime
    instrument_metadata_binding: str
    instrument_evidence_digest: str
    campaign_artifact_id: str
    campaign_artifact_sha256: str
    semantic_claim_key: str
    semantic_claim_digest: str
    evidence_ref: str

    def require_execution_scope(
        self,
        *,
        provider_id: object,
        runtime_environment: object,
        account_id: object,
        entity_id: object,
        capability_snapshot_id: object,
        category: object,
        instrument_version: object,
        provider_symbol: object,
        trade_time: datetime,
    ) -> None:
        if provider_id != self.provider_id:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority provider mismatch"
            )
        if runtime_environment != self.runtime_environment:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority runtime environment mismatch"
            )
        if account_id != self.account_id:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority account mismatch"
            )
        if entity_id != self.entity_id:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority entity mismatch"
            )
        if capability_snapshot_id != self.capability_snapshot_id:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority capability mismatch"
            )
        if category != self.category:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority category mismatch"
            )
        if instrument_version != self.instrument_version:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority instrument version mismatch"
            )
        if provider_symbol != self.provider_symbol:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority provider symbol mismatch"
            )
        point = _point(trade_time, name="trade_time")
        if not self.rule_valid_from <= point < self.rule_valid_until:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority is not valid at execution time"
            )


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class BybitExecutionFeeCurrencyAuthority:
    provider_id: str
    runtime_environment: str
    provider_environment: str
    account_id: str
    entity_id: str
    entity_policy_id: str
    capability_snapshot_id: str
    qualification_id: str
    adapter_source_git_sha: str
    packaged_artifact_digest: str
    product_family: str
    category: str
    instrument_version: str
    provider_symbol: str
    fee_currency: str
    rule_id: str
    rule_version: str
    rule_valid_from: datetime
    rule_valid_until: datetime
    instrument_metadata_binding: str
    instrument_evidence_digest: str
    campaign_artifact_id: str
    campaign_artifact_sha256: str
    semantic_claim_key: str
    semantic_claim_digest: str
    evidence_ref: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise BybitFeeCurrencyAuthorityError(
            "Bybit fee-currency authority must come from canonical Q/instrument issuance"
        )


def _install_authority_registry():
    authority_fields = tuple(
        BybitExecutionFeeCurrencyProjection.__dataclass_fields__
    )
    states: dict[
        int,
        tuple[
            weakref.ReferenceType[BybitExecutionFeeCurrencyAuthority],
            tuple[tuple[str, object], ...],
        ],
    ] = {}

    def prune() -> None:
        for object_id, (value_ref, _projection) in tuple(states.items()):
            if value_ref() is None:
                states.pop(object_id, None)

    def register(
        value: BybitExecutionFeeCurrencyAuthority,
        projection: BybitExecutionFeeCurrencyProjection,
    ) -> None:
        if type(value) is not BybitExecutionFeeCurrencyAuthority:
            raise TypeError("authority must be exact BybitExecutionFeeCurrencyAuthority")
        if type(projection) is not BybitExecutionFeeCurrencyProjection:
            raise TypeError("projection must be exact BybitExecutionFeeCurrencyProjection")
        prune()
        object_id = id(value)
        current = states.get(object_id)
        if current is not None and current[0]() is not None:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority identity collision"
            )
        canonical_values = tuple(
            (field_name, getattr(projection, field_name))
            for field_name in authority_fields
        )
        for field_name, field_value in canonical_values:
            object.__setattr__(
                value,
                field_name,
                field_value,
            )
        states[object_id] = (weakref.ref(value), canonical_values)

    def project(
        value: BybitExecutionFeeCurrencyAuthority,
    ) -> BybitExecutionFeeCurrencyProjection:
        if type(value) is not BybitExecutionFeeCurrencyAuthority:
            raise TypeError("authority must be exact BybitExecutionFeeCurrencyAuthority")
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise BybitFeeCurrencyAuthorityError(
                "fee-currency authority lacks canonical issuance"
            )
        canonical_values = state[1]
        projection_values = {}
        for field_name, expected in canonical_values:
            try:
                current = object.__getattribute__(value, field_name)
            except AttributeError as error:
                raise BybitFeeCurrencyAuthorityError(
                    "fee-currency authority state changed"
                ) from error
            if current != expected:
                raise BybitFeeCurrencyAuthorityError(
                    "fee-currency authority state changed"
                )
            projection_values[field_name] = expected
        return BybitExecutionFeeCurrencyProjection(**projection_values)

    return register, project


_register_authority, project_bybit_execution_fee_currency_authority = (
    _install_authority_registry()
)
del _install_authority_registry


def _issue_bybit_execution_fee_currency_authority_impl(
    *,
    qualification: AcceptedProviderQualification,
    capability: CapabilitySnapshot,
    instrument_registry: InstrumentRegistry,
    venue_id: str,
    provider_symbol: str,
    fee_currency: str,
    rule_id: str,
    rule_version: str,
    rule_valid_from: datetime,
    rule_valid_until: datetime,
    at: datetime,
    _register,
) -> BybitExecutionFeeCurrencyAuthority:
    if type(qualification) is not AcceptedProviderQualification:
        raise TypeError(
            "qualification must be exact AcceptedProviderQualification"
        )
    if type(capability) is not CapabilitySnapshot:
        raise TypeError("capability must be exact CapabilitySnapshot")
    if type(instrument_registry) is not InstrumentRegistry:
        raise TypeError("instrument_registry must be exact InstrumentRegistry")

    point = _point(at, name="at")
    if (
        capability.status != "VERIFIED"
        or not getattr(capability, "_can_admit", False)
        or not capability.observed_at <= point < capability.expires_at
    ):
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency issuance requires fresh verified capability authority"
        )
    if capability.provider_id != "BYBIT":
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency issuance requires BYBIT capability"
        )
    if "ORDER.READ" not in capability.permission_scopes:
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency issuance requires ORDER.READ capability"
        )
    if "EXECUTIONS" not in capability.data_entitlements:
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency issuance requires EXECUTIONS entitlement"
        )

    q_completed = _parse_utc_text(
        qualification.completed_at,
        name="qualification.completed_at",
    )
    q_valid_until = _parse_utc_text(
        qualification.valid_until,
        name="qualification.valid_until",
    )
    if not q_completed <= point < q_valid_until:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification is not current at fee-currency issuance"
        )
    semantics = _qualification_semantics(qualification)
    scope = qualification.scope
    provider_scope = scope.provider_scope
    if (
        provider_scope.provider_id != "BYBIT"
        or provider_scope.provider_id != capability.provider_id
        or provider_scope.runtime_environment != capability.environment
        or provider_scope.provider_environment != capability.provider_environment
    ):
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification scope does not match capability domain"
        )

    product_family = scope.product_family
    category = _BYBIT_CATEGORY_BY_PRODUCT_FAMILY.get(product_family)
    if category is None:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification product family has no Bybit execution category"
        )

    venue_id = _text(venue_id, name="venue_id")
    provider_symbol = _text(provider_symbol, name="provider_symbol")
    valid_from = _point(rule_valid_from, name="rule_valid_from")
    valid_until = _point(rule_valid_until, name="rule_valid_until")
    if valid_until <= valid_from:
        raise BybitFeeCurrencyAuthorityError(
            "rule_valid_until must be after rule_valid_from"
        )
    try:
        instrument = instrument_registry.resolve(
            "BYBIT",
            venue_id,
            provider_symbol,
            valid_from,
        )
        end_probe = valid_until - timedelta(microseconds=1)
        instrument_at_end = instrument_registry.resolve(
            "BYBIT",
            venue_id,
            provider_symbol,
            end_probe,
        )
    except InstrumentRegistryError as error:
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency rule does not resolve to one canonical instrument interval"
        ) from error
    instrument_ref = _instrument_ref(instrument)
    if _instrument_ref(instrument_at_end) != instrument_ref:
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency rule crosses an InstrumentVersion boundary"
        )
    if capability.instrument_version != instrument_ref:
        raise BybitFeeCurrencyAuthorityError(
            "fee-currency rule instrument does not match capability InstrumentVersion"
        )

    claim_key, claim_digest, policy = _fee_rule_material(
        provider_environment=provider_scope.provider_environment,
        product_family=product_family,
        category=category,
        instrument=instrument,
        fee_currency=fee_currency,
        rule_id=rule_id,
        rule_version=rule_version,
        rule_valid_from=valid_from,
        rule_valid_until=valid_until,
    )
    if semantics.get(claim_key) != claim_digest:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification does not cover exact fee-currency rule"
        )

    campaign_ref = qualification.campaign_artifact_ref
    if type(campaign_ref) is not EvidenceArtifactRef:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification campaign evidence is non-canonical"
        )
    if _SHA_RE.fullmatch(campaign_ref.sha256) is None:
        raise BybitFeeCurrencyAuthorityError(
            "provider qualification campaign digest is non-canonical"
        )

    material = {
        "provider_id": "BYBIT",
        "runtime_environment": capability.environment,
        "provider_environment": capability.provider_environment,
        "account_id": capability.account_id,
        "entity_id": capability.entity_id,
        "entity_policy_id": provider_scope.entity_policy_id,
        "capability_snapshot_id": capability.snapshot_id,
        "qualification_id": qualification.qualification_id,
        "adapter_source_git_sha": scope.adapter_source_git_sha,
        "packaged_artifact_digest": scope.packaged_artifact_digest,
        "product_family": product_family,
        "category": category,
        "instrument_version": instrument_ref,
        "provider_symbol": instrument.provider_symbol,
        "fee_currency": policy["fee_currency"],
        "rule_id": policy["rule_id"],
        "rule_version": policy["rule_version"],
        "rule_valid_from": valid_from,
        "rule_valid_until": valid_until,
        "instrument_metadata_binding": policy["instrument_metadata_binding"],
        "instrument_evidence_digest": policy["instrument_evidence_digest"],
        "campaign_artifact_id": campaign_ref.artifact_id,
        "campaign_artifact_sha256": campaign_ref.sha256,
        "semantic_claim_key": claim_key,
        "semantic_claim_digest": claim_digest,
    }
    identity_payload = {
        key: (_utc_text(value) if type(value) is datetime else value)
        for key, value in material.items()
    }
    evidence_ref = "bybit-fee-currency:sha256:" + sha256(
        canonical_json(identity_payload).encode("utf-8")
    ).hexdigest()
    projection = BybitExecutionFeeCurrencyProjection(
        **material,
        evidence_ref=evidence_ref,
    )
    authority = object.__new__(BybitExecutionFeeCurrencyAuthority)
    _register(authority, projection)
    return authority


def _bind_issuer(register, implementation):
    def issue_bybit_execution_fee_currency_authority(
        *,
        qualification: AcceptedProviderQualification,
        capability: CapabilitySnapshot,
        instrument_registry: InstrumentRegistry,
        venue_id: str,
        provider_symbol: str,
        fee_currency: str,
        rule_id: str,
        rule_version: str,
        rule_valid_from: datetime,
        rule_valid_until: datetime,
        at: datetime,
    ) -> BybitExecutionFeeCurrencyAuthority:
        """Issue a sealed fallback only from exact current Q/C + instrument state."""

        return implementation(
            qualification=qualification,
            capability=capability,
            instrument_registry=instrument_registry,
            venue_id=venue_id,
            provider_symbol=provider_symbol,
            fee_currency=fee_currency,
            rule_id=rule_id,
            rule_version=rule_version,
            rule_valid_from=rule_valid_from,
            rule_valid_until=rule_valid_until,
            at=at,
            _register=register,
        )

    return issue_bybit_execution_fee_currency_authority


issue_bybit_execution_fee_currency_authority = _bind_issuer(
    _register_authority,
    _issue_bybit_execution_fee_currency_authority_impl,
)
del _register_authority
del _issue_bybit_execution_fee_currency_authority_impl
del _bind_issuer
