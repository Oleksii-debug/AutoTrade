"""Durable historical valuation-observation authority.

This module owns immutable normalized MARK/FX observation history and historical
selection.  It deliberately does not own provider transport, provider
qualification, market normalization, risk policy, or FX conversion arithmetic.

Public fixture construction can create TEST_DIAGNOSTIC observations only.
PROVIDER_ORIGIN issuance is reserved for the future product-owned composition of
accepted provider origin + qualified parser/capability authority.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import threading
from typing import Iterable, Mapping
import weakref

from .exact_decimal import canonical_decimal_text, parse_canonical_decimal_text
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_domain import ProviderDomainError, provider_financial_scope
from .risk_policy_authority import (
    ResolvedRiskPolicy,
    RiskPolicyAuthorityError,
    journal_store_identity_digest,
    require_registry_issued_resolved_policy,
)
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)


class ValuationError(ValueError):
    pass


class ValuationConflict(ValuationError):
    pass


_KINDS = frozenset({"MARK", "FX_QUOTE"})
_EVIDENCE_CLASSES = frozenset({"TEST_DIAGNOSTIC", "PROVIDER_ORIGIN"})
_PRODUCTION_ISSUER_TOKEN = object()
_AGGREGATE_TYPE = "valuation_observation"
_EVENT_TYPE = "ValuationObservation.v1"
_MAX_STABLE_READ_ATTEMPTS = 4


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValuationError(f"{name} must be exact non-empty text")
    return value


def _digest(value: object, name: str) -> str:
    text = _text(value, name)
    if not text.startswith("sha256:") or len(text) != 71:
        raise ValuationError(f"{name} must be a canonical SHA-256 digest")
    try:
        int(text[7:], 16)
    except ValueError as error:
        raise ValuationError(f"{name} must be a canonical SHA-256 digest") from error
    if text[7:] != text[7:].lower():
        raise ValuationError(f"{name} must be a canonical SHA-256 digest")
    return text


def _instant(value: datetime, name: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ValuationError(f"{name} must be an exact timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _instant_text(value: datetime) -> str:
    return _instant(value, "timestamp").isoformat().replace("+00:00", "Z")


def _instant_from_text(value: object, name: str) -> datetime:
    text = _text(value, name)
    if not text.endswith("Z"):
        raise ValuationError(f"{name} must be canonical UTC text")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValuationError(f"{name} must be canonical UTC text") from error
    if _instant_text(parsed) != text:
        raise ValuationError(f"{name} must be canonical UTC text")
    return parsed.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal:
        raise ValuationError(f"{name} must be exact Decimal")
    try:
        canonical = canonical_decimal_text(value)
        return parse_canonical_decimal_text(canonical)
    except Exception as error:
        raise ValuationError(f"{name} is outside the exact decimal authority") from error


def _decimal_from_text(value: object, name: str) -> Decimal:
    if type(value) is not str:
        raise ValuationError(f"{name} must be canonical decimal text")
    try:
        parsed = parse_canonical_decimal_text(value)
    except Exception as error:
        raise ValuationError(f"{name} must be canonical decimal text") from error
    if canonical_decimal_text(parsed) != value:
        raise ValuationError(f"{name} must be canonical decimal text")
    return parsed


@dataclass(frozen=True, slots=True)
class ValuationObservation:
    kind: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    route_policy_id: str
    capability_snapshot_id: str
    provider_qualification_id: str
    adapter_build_id: str
    source_revision: str
    source_event_at: datetime
    available_at: datetime
    observed_at: datetime
    freshness_rule_id: str
    origin_binding_id: str
    query_digest: str
    response_sha256: str
    evidence_class: str
    instrument_version: str | None = None
    mark: Decimal | None = None
    base_currency: str | None = None
    quote_currency: str | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    supersedes_observation_id: str | None = None
    observation_id: str = field(init=False)
    scope_id: str = field(init=False)
    _authority_token: InitVar[object | None] = None

    def __post_init__(self, _authority_token: object | None) -> None:
        kind = _text(self.kind, "kind").upper()
        if kind not in _KINDS:
            raise ValuationError("kind must be MARK or FX_QUOTE")
        object.__setattr__(self, "kind", kind)

        provider = _text(self.provider_id, "provider_id").upper()
        environment = _text(self.environment, "environment").upper()
        try:
            scope = provider_financial_scope(
                provider_id=provider,
                environment=environment,
                provider_environment=self.provider_environment,
                route_policy_id=self.route_policy_id,
            )
        except ProviderDomainError as error:
            raise ValuationError(str(error)) from error
        object.__setattr__(self, "provider_id", scope.provider_id)
        object.__setattr__(self, "environment", scope.environment)
        object.__setattr__(
            self,
            "provider_environment",
            scope.provider_environment,
        )
        object.__setattr__(self, "route_policy_id", scope.route_policy_id)

        for name in (
            "account_id",
            "capability_snapshot_id",
            "provider_qualification_id",
            "adapter_build_id",
            "source_revision",
            "freshness_rule_id",
            "origin_binding_id",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(self, "query_digest", _digest(self.query_digest, "query_digest"))
        object.__setattr__(
            self,
            "response_sha256",
            _digest(self.response_sha256, "response_sha256"),
        )

        source_event_at = _instant(self.source_event_at, "source_event_at")
        available_at = _instant(self.available_at, "available_at")
        observed_at = _instant(self.observed_at, "observed_at")
        if not source_event_at <= available_at <= observed_at:
            raise ValuationError(
                "valuation chronology must satisfy source_event_at <= available_at <= observed_at"
            )
        object.__setattr__(self, "source_event_at", source_event_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "observed_at", observed_at)

        evidence_class = _text(self.evidence_class, "evidence_class").upper()
        if evidence_class not in _EVIDENCE_CLASSES:
            raise ValuationError("unsupported valuation evidence_class")
        if (
            evidence_class == "PROVIDER_ORIGIN"
            and _authority_token is not _PRODUCTION_ISSUER_TOKEN
        ):
            raise ValuationError(
                "PROVIDER_ORIGIN valuation requires the product-owned issuer"
            )
        object.__setattr__(self, "evidence_class", evidence_class)

        if kind == "MARK":
            instrument = _text(self.instrument_version, "instrument_version")
            mark = _decimal(self.mark, "mark")
            if mark <= 0:
                raise ValuationError("mark must be positive")
            if any(
                item is not None
                for item in (self.base_currency, self.quote_currency, self.bid, self.ask)
            ):
                raise ValuationError("MARK cannot carry FX quote fields")
            object.__setattr__(self, "instrument_version", instrument)
            object.__setattr__(self, "mark", mark)
        else:
            base = _text(self.base_currency, "base_currency").upper()
            quote = _text(self.quote_currency, "quote_currency").upper()
            if base == quote:
                raise ValuationError("FX base and quote currencies must differ")
            bid = _decimal(self.bid, "bid")
            ask = _decimal(self.ask, "ask")
            if bid <= 0 or ask <= 0 or bid > ask:
                raise ValuationError("FX quote must satisfy 0 < bid <= ask")
            if self.instrument_version is not None or self.mark is not None:
                raise ValuationError("FX_QUOTE cannot carry MARK instrument fields")
            object.__setattr__(self, "base_currency", base)
            object.__setattr__(self, "quote_currency", quote)
            object.__setattr__(self, "bid", bid)
            object.__setattr__(self, "ask", ask)

        if self.supersedes_observation_id is not None:
            object.__setattr__(
                self,
                "supersedes_observation_id",
                _text(
                    self.supersedes_observation_id,
                    "supersedes_observation_id",
                ),
            )

        scope_payload = self._scope_payload()
        scope_id = "valuation-scope:" + payload_digest(scope_payload)
        object.__setattr__(self, "scope_id", scope_id)
        body = self._body_payload()
        object.__setattr__(
            self,
            "observation_id",
            "valuation:" + payload_digest(body),
        )
        if self.supersedes_observation_id == self.observation_id:
            raise ValuationError("valuation observation cannot supersede itself")

    def _scope_payload(self) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": "1.0.0",
            "kind": self.kind,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "route_policy_id": self.route_policy_id,
        }
        if self.kind == "MARK":
            body["instrument_version"] = self.instrument_version
        else:
            body["base_currency"] = self.base_currency
            body["quote_currency"] = self.quote_currency
        return body

    def _body_payload(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            **self._scope_payload(),
            # Persist the complete closed v1 shape for both variants.  The
            # non-applicable identity/scalar fields are explicit nulls so
            # replay never infers record kind from a changing key set.
            "instrument_version": self.instrument_version,
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "capability_snapshot_id": self.capability_snapshot_id,
            "provider_qualification_id": self.provider_qualification_id,
            "adapter_build_id": self.adapter_build_id,
            "source_revision": self.source_revision,
            "source_event_at": _instant_text(self.source_event_at),
            "available_at": _instant_text(self.available_at),
            "observed_at": _instant_text(self.observed_at),
            "freshness_rule_id": self.freshness_rule_id,
            "origin_binding_id": self.origin_binding_id,
            "query_digest": self.query_digest,
            "response_sha256": self.response_sha256,
            "evidence_class": self.evidence_class,
            "mark": (
                None
                if self.mark is None
                else canonical_decimal_text(self.mark)
            ),
            "bid": (
                None
                if self.bid is None
                else canonical_decimal_text(self.bid)
            ),
            "ask": (
                None
                if self.ask is None
                else canonical_decimal_text(self.ask)
            ),
            "supersedes_observation_id": self.supersedes_observation_id,
        }

    def to_contract_dict(self) -> dict[str, object]:
        return {
            **self._body_payload(),
            "observation_id": self.observation_id,
            "scope_id": self.scope_id,
        }


def diagnostic_mark_observation(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str,
    route_policy_id: str,
    capability_snapshot_id: str,
    provider_qualification_id: str,
    adapter_build_id: str,
    source_revision: str,
    source_event_at: datetime,
    available_at: datetime,
    observed_at: datetime,
    freshness_rule_id: str,
    origin_binding_id: str,
    query_digest: str,
    response_sha256: str,
    instrument_version: str,
    mark: Decimal,
    supersedes_observation_id: str | None = None,
) -> ValuationObservation:
    return ValuationObservation(
        kind="MARK",
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
        route_policy_id=route_policy_id,
        capability_snapshot_id=capability_snapshot_id,
        provider_qualification_id=provider_qualification_id,
        adapter_build_id=adapter_build_id,
        source_revision=source_revision,
        source_event_at=source_event_at,
        available_at=available_at,
        observed_at=observed_at,
        freshness_rule_id=freshness_rule_id,
        origin_binding_id=origin_binding_id,
        query_digest=query_digest,
        response_sha256=response_sha256,
        evidence_class="TEST_DIAGNOSTIC",
        instrument_version=instrument_version,
        mark=mark,
        supersedes_observation_id=supersedes_observation_id,
    )


def diagnostic_fx_observation(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str,
    route_policy_id: str,
    capability_snapshot_id: str,
    provider_qualification_id: str,
    adapter_build_id: str,
    source_revision: str,
    source_event_at: datetime,
    available_at: datetime,
    observed_at: datetime,
    freshness_rule_id: str,
    origin_binding_id: str,
    query_digest: str,
    response_sha256: str,
    base_currency: str,
    quote_currency: str,
    bid: Decimal,
    ask: Decimal,
    supersedes_observation_id: str | None = None,
) -> ValuationObservation:
    return ValuationObservation(
        kind="FX_QUOTE",
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
        route_policy_id=route_policy_id,
        capability_snapshot_id=capability_snapshot_id,
        provider_qualification_id=provider_qualification_id,
        adapter_build_id=adapter_build_id,
        source_revision=source_revision,
        source_event_at=source_event_at,
        available_at=available_at,
        observed_at=observed_at,
        freshness_rule_id=freshness_rule_id,
        origin_binding_id=origin_binding_id,
        query_digest=query_digest,
        response_sha256=response_sha256,
        evidence_class="TEST_DIAGNOSTIC",
        base_currency=base_currency,
        quote_currency=quote_currency,
        bid=bid,
        ask=ask,
        supersedes_observation_id=supersedes_observation_id,
    )


@dataclass(frozen=True, slots=True)
class ValuationFreshnessEvidence:
    """Derived freshness proof bound to one durable observation and policy cut."""

    observation_id: str
    origin_binding_id: str
    query_digest: str
    response_sha256: str
    observation_freshness_rule_id: str
    policy_id: str
    policy_version: int
    policy_content_digest: str
    policy_registration_event_id: str
    policy_activation_event_id: str
    policy_journal_sequence_cut: int
    freshness_field: str
    max_age_seconds: Decimal
    source_age_microseconds: int
    as_of: datetime
    evidence_digest: str = field(init=False)

    def __post_init__(self) -> None:
        observation_id = _text(self.observation_id, "observation_id")
        origin_binding_id = _text(self.origin_binding_id, "origin_binding_id")
        query_digest = _digest(self.query_digest, "query_digest")
        response_sha256 = _digest(self.response_sha256, "response_sha256")
        observation_freshness_rule_id = _text(
            self.observation_freshness_rule_id,
            "observation_freshness_rule_id",
        )
        policy_id = _text(self.policy_id, "policy_id")
        if type(self.policy_version) is not int or self.policy_version <= 0:
            raise ValuationError("policy_version must be a positive integer")
        policy_digest = _digest(self.policy_content_digest, "policy_content_digest")
        registration_event_id = _text(
            self.policy_registration_event_id,
            "policy_registration_event_id",
        )
        activation_event_id = _text(
            self.policy_activation_event_id,
            "policy_activation_event_id",
        )
        if (
            type(self.policy_journal_sequence_cut) is not int
            or self.policy_journal_sequence_cut < 0
        ):
            raise ValuationError(
                "policy_journal_sequence_cut must be a non-negative integer"
            )
        freshness_field = _text(self.freshness_field, "freshness_field")
        if freshness_field not in {"max_data_age_seconds", "max_fx_age_seconds"}:
            raise ValuationError("unsupported valuation freshness field")
        max_age = _decimal(self.max_age_seconds, "max_age_seconds")
        if max_age < 0:
            raise ValuationError("max_age_seconds must be non-negative")
        if type(self.source_age_microseconds) is not int or self.source_age_microseconds < 0:
            raise ValuationError(
                "source_age_microseconds must be a non-negative integer"
            )
        point = _instant(self.as_of, "as_of")
        object.__setattr__(self, "observation_id", observation_id)
        object.__setattr__(self, "origin_binding_id", origin_binding_id)
        object.__setattr__(self, "query_digest", query_digest)
        object.__setattr__(self, "response_sha256", response_sha256)
        object.__setattr__(
            self,
            "observation_freshness_rule_id",
            observation_freshness_rule_id,
        )
        object.__setattr__(self, "policy_id", policy_id)
        object.__setattr__(self, "policy_content_digest", policy_digest)
        object.__setattr__(
            self,
            "policy_registration_event_id",
            registration_event_id,
        )
        object.__setattr__(self, "policy_activation_event_id", activation_event_id)
        object.__setattr__(self, "freshness_field", freshness_field)
        object.__setattr__(self, "max_age_seconds", max_age)
        object.__setattr__(self, "as_of", point)
        digest = payload_digest(
            {
                "schema_version": "1.0.0",
                "observation_id": observation_id,
                "origin_binding_id": origin_binding_id,
                "query_digest": query_digest,
                "response_sha256": response_sha256,
                "observation_freshness_rule_id": observation_freshness_rule_id,
                "policy_id": policy_id,
                "policy_version": str(self.policy_version),
                "policy_content_digest": policy_digest,
                "policy_registration_event_id": registration_event_id,
                "policy_activation_event_id": activation_event_id,
                "policy_journal_sequence_cut": str(self.policy_journal_sequence_cut),
                "freshness_field": freshness_field,
                "max_age_seconds": canonical_decimal_text(max_age),
                "source_age_microseconds": str(self.source_age_microseconds),
                "as_of": _instant_text(point),
            }
        )
        object.__setattr__(self, "evidence_digest", digest)


def evaluate_valuation_freshness(
    observation: ValuationObservation,
    policy: ResolvedRiskPolicy,
    *,
    as_of: datetime,
    journal_sequence_cut: int,
) -> ValuationFreshnessEvidence:
    """Prove one selected observation is fresh under one registry-issued policy."""

    observation = _canonical_observation(observation)
    try:
        policy = require_registry_issued_resolved_policy(policy)
    except RiskPolicyAuthorityError as error:
        raise ValuationError(str(error)) from error
    if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
        raise ValuationError("journal_sequence_cut must be a non-negative integer")
    if policy.resolved_journal_sequence_cut != journal_sequence_cut:
        raise ValuationError(
            "valuation and risk policy must resolve at the same journal sequence cut"
        )
    point = _instant(as_of, "as_of")
    if point < observation.source_event_at:
        raise ValuationError("valuation source event is in the future")
    if point < observation.available_at or point < observation.observed_at:
        raise ValuationError("valuation observation is not causally available at as_of")

    scope = policy.identity.scope
    if (
        observation.provider_id != scope.provider_id
        or observation.account_id != scope.account_id
        or observation.environment != scope.environment
        or observation.provider_environment != scope.provider_environment
        or observation.route_policy_id != scope.entity_policy_id
    ):
        raise ValuationError(
            "valuation observation and quantitative RiskPolicy scope do not match"
        )

    if observation.kind == "MARK":
        freshness_field = "max_data_age_seconds"
        max_age = policy.policy.max_data_age_seconds
    elif observation.kind == "FX_QUOTE":
        freshness_field = "max_fx_age_seconds"
        max_age = policy.policy.max_fx_age_seconds
    else:
        raise ValuationError("unsupported valuation kind")

    age = point - observation.source_event_at
    age_microseconds = (
        age.days * 86_400_000_000
        + age.seconds * 1_000_000
        + age.microseconds
    )
    max_age_fraction = Fraction(max_age)
    if (
        age_microseconds * max_age_fraction.denominator
        > max_age_fraction.numerator * 1_000_000
    ):
        raise ValuationError(
            f"{observation.kind} valuation is stale under activated quantitative RiskPolicy"
        )

    return ValuationFreshnessEvidence(
        observation_id=observation.observation_id,
        origin_binding_id=observation.origin_binding_id,
        query_digest=observation.query_digest,
        response_sha256=observation.response_sha256,
        observation_freshness_rule_id=observation.freshness_rule_id,
        policy_id=policy.identity.policy_id,
        policy_version=policy.identity.version,
        policy_content_digest=policy.identity.content_digest,
        policy_registration_event_id=policy.registration_event_id,
        policy_activation_event_id=policy.activation_event_id,
        policy_journal_sequence_cut=policy.resolved_journal_sequence_cut,
        freshness_field=freshness_field,
        max_age_seconds=max_age,
        source_age_microseconds=age_microseconds,
        as_of=point,
    )


def _canonical_observation(
    value: object,
) -> ValuationObservation:
    """Detach and revalidate an observation before any authority-bearing use."""

    if type(value) is not ValuationObservation:
        raise TypeError("observation must be exact ValuationObservation")
    payload = ValuationObservation.to_contract_dict(value)
    if type(payload) is not dict:
        raise ValuationError("valuation observation contract must be an exact object")
    return _observation_from_payload(payload)


def observation_set_digest(
    observations: Iterable[ValuationObservation],
) -> str:
    raw_records = tuple(observations)
    if not raw_records:
        raise ValuationError("valuation observation set cannot be empty")
    records = tuple(_canonical_observation(item) for item in raw_records)
    ids = sorted(item.observation_id for item in records)
    if len(set(ids)) != len(ids):
        raise ValuationError("valuation observation set contains duplicate identities")
    return payload_digest(
        {
            "schema_version": "1.0.0",
            "observation_ids": ids,
        }
    )


def _observation_from_payload(payload: Mapping[str, object]) -> ValuationObservation:
    if type(payload) is not dict:
        raise ValuationConflict("durable valuation payload must be a mapping")
    expected = {
        "schema_version",
        "kind",
        "provider_id",
        "account_id",
        "environment",
        "provider_environment",
        "route_policy_id",
        "capability_snapshot_id",
        "provider_qualification_id",
        "adapter_build_id",
        "source_revision",
        "source_event_at",
        "available_at",
        "observed_at",
        "freshness_rule_id",
        "origin_binding_id",
        "query_digest",
        "response_sha256",
        "evidence_class",
        "mark",
        "bid",
        "ask",
        "supersedes_observation_id",
        "observation_id",
        "scope_id",
        "instrument_version",
        "base_currency",
        "quote_currency",
    }
    if set(payload) != expected or payload.get("schema_version") != "1.0.0":
        raise ValuationConflict(
            "durable valuation payload has unsupported or missing fields"
        )
    evidence_class = _text(payload.get("evidence_class"), "evidence_class").upper()
    if evidence_class == "PROVIDER_ORIGIN":
        raise ValuationConflict(
            "durable PROVIDER_ORIGIN replay requires integrated provider-origin issuer authority"
        )
    token = None
    kind = _text(payload.get("kind"), "kind").upper()
    observation = ValuationObservation(
        kind=kind,
        provider_id=payload.get("provider_id"),
        account_id=payload.get("account_id"),
        environment=payload.get("environment"),
        provider_environment=payload.get("provider_environment"),
        route_policy_id=payload.get("route_policy_id"),
        capability_snapshot_id=payload.get("capability_snapshot_id"),
        provider_qualification_id=payload.get("provider_qualification_id"),
        adapter_build_id=payload.get("adapter_build_id"),
        source_revision=payload.get("source_revision"),
        source_event_at=_instant_from_text(
            payload.get("source_event_at"),
            "source_event_at",
        ),
        available_at=_instant_from_text(
            payload.get("available_at"),
            "available_at",
        ),
        observed_at=_instant_from_text(
            payload.get("observed_at"),
            "observed_at",
        ),
        freshness_rule_id=payload.get("freshness_rule_id"),
        origin_binding_id=payload.get("origin_binding_id"),
        query_digest=payload.get("query_digest"),
        response_sha256=payload.get("response_sha256"),
        evidence_class=evidence_class,
        instrument_version=(
            None
            if payload.get("instrument_version") is None
            else _text(payload.get("instrument_version"), "instrument_version")
        ),
        mark=(
            None
            if payload.get("mark") is None
            else _decimal_from_text(payload.get("mark"), "mark")
        ),
        base_currency=(
            None
            if payload.get("base_currency") is None
            else _text(payload.get("base_currency"), "base_currency")
        ),
        quote_currency=(
            None
            if payload.get("quote_currency") is None
            else _text(payload.get("quote_currency"), "quote_currency")
        ),
        bid=(
            None
            if payload.get("bid") is None
            else _decimal_from_text(payload.get("bid"), "bid")
        ),
        ask=(
            None
            if payload.get("ask") is None
            else _decimal_from_text(payload.get("ask"), "ask")
        ),
        supersedes_observation_id=(
            None
            if payload.get("supersedes_observation_id") is None
            else _text(
                payload.get("supersedes_observation_id"),
                "supersedes_observation_id",
            )
        ),
        _authority_token=token,
    )
    if observation.to_contract_dict() != dict(payload):
        raise ValuationConflict(
            "durable valuation payload is not canonical or content identity changed"
        )
    return observation


def _store_identity_payload(identity: JournalStoreIdentity) -> dict[str, object]:
    exact = require_exact_journal_store_identity(
        identity,
        subject="valuation JournalStore identity",
    )
    if exact.identity_source == "windows_by_handle":
        return {
            "schema_version": "1.0.0",
            "identity_source": exact.identity_source,
            "windows_volume_serial": exact.windows_volume_serial,
            "windows_file_index_high": exact.windows_file_index_high,
            "windows_file_index_low": exact.windows_file_index_low,
        }
    return {
        "schema_version": "1.0.0",
        "identity_source": exact.identity_source,
        "canonical_path": exact.canonical_path,
        "filesystem_device": exact.filesystem_device,
        "filesystem_inode": exact.filesystem_inode,
    }


def _store_identity_digest(identity: JournalStoreIdentity) -> str:
    return journal_store_identity_digest(identity)


@dataclass(frozen=True, slots=True)
class _ValuationBookBinding:
    store: JournalStore
    store_identity: JournalStoreIdentity
    store_identity_digest: str


def _build_valuation_book_binding_methods():
    # This table and its lock are closure-owned. They are never exported as
    # module attributes, so re-creating historical module-global names cannot
    # retarget the store generation selected for an existing financial book.
    bindings: weakref.WeakKeyDictionary[object, _ValuationBookBinding] = (
        weakref.WeakKeyDictionary()
    )
    lock = threading.RLock()

    def initialize(value: object, store: JournalStore) -> None:
        if type(value) is not DurableValuationBook:
            raise TypeError("book must be exact DurableValuationBook")
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")

        with lock:
            if value in bindings:
                raise ValuationConflict(
                    "valuation journal composition is already initialized"
                )
            selected_identity = require_exact_journal_store_authority(
                store,
                subject="selected valuation JournalStore",
            )
            exact_identity = require_exact_journal_store_identity(
                selected_identity,
                subject="original valuation JournalStore identity",
            )
            identity_digest = _store_identity_digest(exact_identity)

            # Visible slots are diagnostics only. The closure-owned binding below
            # is the original-selection authority and is established only after
            # all inputs have been validated.
            object.__setattr__(value, "store", store)
            object.__setattr__(value, "store_identity", exact_identity)
            object.__setattr__(value, "store_identity_digest", identity_digest)
            bindings[value] = _ValuationBookBinding(
                store=store,
                store_identity=exact_identity,
                store_identity_digest=identity_digest,
            )

    def require(value: object) -> tuple[JournalStore, JournalStoreIdentity]:
        if type(value) is not DurableValuationBook:
            raise TypeError("book must be exact DurableValuationBook")
        with lock:
            binding = bindings.get(value)
        if binding is None:
            raise ValuationConflict(
                "valuation book lacks original journal composition"
            )

        if object.__getattribute__(value, "store") is not binding.store:
            raise ValuationConflict("valuation journal composition changed")
        visible_identity = require_exact_journal_store_identity(
            object.__getattribute__(value, "store_identity"),
            subject="visible valuation JournalStore identity",
        )
        if (
            visible_identity != binding.store_identity
            or object.__getattribute__(value, "store_identity_digest")
            != binding.store_identity_digest
        ):
            raise ValuationConflict("valuation journal composition changed")

        current = require_exact_journal_store_authority(
            binding.store,
            subject="current valuation JournalStore",
        )
        if current != binding.store_identity:
            raise ValuationConflict("valuation journal authority changed")
        return binding.store, binding.store_identity

    return initialize, require


class DurableValuationBook:
    """Append-only valuation history over one exact JournalStore authority."""

    __slots__ = (
        "store",
        "store_identity",
        "store_identity_digest",
        "__weakref__",
    )

    def __init_subclass__(cls, **_kwargs) -> None:
        raise TypeError("DurableValuationBook cannot be subclassed")

    __init__, _journal_store_authority = _build_valuation_book_binding_methods()

    def _stable_scope_events(
        self,
        scope_id: str,
    ) -> tuple[int, tuple[dict[str, object], ...]]:
        for _attempt in range(_MAX_STABLE_READ_ATTEMPTS):
            store, expected_identity = self._journal_store_authority()
            expected_digest = _store_identity_digest(expected_identity)
            with journal_store_authority_scope(store, expected_identity):
                cut_before = JournalStore.current_journal_sequence(store)
                events = tuple(
                    JournalStore.load_events(store, _AGGREGATE_TYPE, scope_id)
                )
                cut_after = JournalStore.current_journal_sequence(store)
            if cut_before == cut_after:
                decoded = self._validated_observations(
                    events,
                    scope_id=scope_id,
                    store_identity_digest=expected_digest,
                )
                self._active(item for item, _sequence in decoded)
                return cut_after, events
        raise ValuationConflict(
            "journal changed continuously while reading valuation authority"
        )

    @staticmethod
    def _validated_observations(
        events: Iterable[Mapping[str, object]],
        *,
        scope_id: str,
        store_identity_digest: str,
    ) -> tuple[tuple[ValuationObservation, int], ...]:
        result: list[tuple[ValuationObservation, int]] = []
        expected_version = 1
        for event in events:
            if type(event) is not dict:
                raise ValuationConflict("durable valuation event is not canonical")
            if (
                event.get("event_type") != _EVENT_TYPE
                or event.get("aggregate_type") != _AGGREGATE_TYPE
                or event.get("aggregate_id") != scope_id
                or event.get("aggregate_version") != expected_version
            ):
                raise ValuationConflict(
                    "durable valuation aggregate structure is invalid"
                )
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence < 1:
                raise ValuationConflict(
                    "durable valuation event lacks canonical journal sequence"
                )
            payload = event.get("payload")
            if type(payload) is not dict:
                raise ValuationConflict(
                    "durable valuation event payload must be a mapping"
                )
            if set(payload) != {
                "schema_version",
                "store_identity",
                "store_identity_digest",
                "observation",
            } or payload.get("schema_version") != "1.0.0":
                raise ValuationConflict(
                    "durable valuation event payload has unsupported or missing fields"
                )
            stored_identity = payload.get("store_identity")
            if type(stored_identity) is not dict:
                raise ValuationConflict(
                    "durable valuation store identity is missing"
                )
            if (
                payload.get("store_identity_digest")
                != payload_digest(dict(stored_identity))
                or payload.get("store_identity_digest")
                != store_identity_digest
            ):
                raise ValuationConflict(
                    "durable valuation event belongs to a different JournalStore generation"
                )
            observation_payload = payload.get("observation")
            if type(observation_payload) is not dict:
                raise ValuationConflict(
                    "durable valuation observation payload is missing"
                )
            observation = _observation_from_payload(observation_payload)
            if observation.scope_id != scope_id:
                raise ValuationConflict(
                    "durable valuation observation escaped aggregate scope"
                )
            if event.get("event_id") != observation.observation_id:
                raise ValuationConflict(
                    "durable valuation event identity does not match content"
                )
            result.append((observation, sequence))
            expected_version += 1
        return tuple(result)

    @staticmethod
    def _active(
        observations: Iterable[ValuationObservation],
    ) -> tuple[ValuationObservation, ...]:
        # Replay the exact writer state machine in durable order.  Correction
        # edges are causal transitions, not an unordered graph: a future row
        # cannot retroactively become the predecessor of an earlier row, and
        # restart must enforce the same chronology that record() enforced.
        current: ValuationObservation | None = None
        seen_ids: set[str] = set()
        for item in observations:
            if type(item) is not ValuationObservation:
                raise TypeError(
                    "valuation replay requires exact ValuationObservation values"
                )
            if item.observation_id in seen_ids:
                raise ValuationConflict(
                    "durable valuation identity appears more than once"
                )
            seen_ids.add(item.observation_id)
            predecessor = item.supersedes_observation_id
            if current is None:
                if predecessor is not None:
                    raise ValuationConflict(
                        "first valuation observation cannot supersede missing history"
                    )
                current = item
                continue
            if predecessor is None:
                raise ValuationConflict(
                    "new valuation fact requires explicit supersession of current authority"
                )
            if predecessor != current.observation_id:
                raise ValuationConflict(
                    "valuation correction must supersede the one current observation"
                )
            if item.available_at <= current.available_at:
                raise ValuationConflict(
                    "valuation correction availability must advance"
                )
            if item.observed_at < current.observed_at:
                raise ValuationConflict(
                    "valuation correction observation cannot move backward"
                )
            current = item
        return () if current is None else (current,)

    def record(self, observation: ValuationObservation) -> bool:
        observation = _canonical_observation(observation)
        if observation.evidence_class == "PROVIDER_ORIGIN":
            raise ValuationError(
                "PROVIDER_ORIGIN persistence requires integrated provider-origin issuer authority"
            )
        _cut, events = self._stable_scope_events(observation.scope_id)
        decoded = self._validated_observations(
            events,
            scope_id=observation.scope_id,
            store_identity_digest=self.store_identity_digest,
        )
        history = tuple(item for item, _sequence in decoded)

        for item in history:
            if item.observation_id == observation.observation_id:
                if item != observation:
                    raise ValuationConflict(
                        "content-derived valuation identity changed content"
                    )
                return False

        active = self._active(history)
        predecessor = observation.supersedes_observation_id
        if not history:
            if predecessor is not None:
                raise ValuationConflict(
                    "first valuation observation cannot supersede missing history"
                )
        else:
            if predecessor is None:
                raise ValuationConflict(
                    "new valuation fact requires explicit supersession of current authority"
                )
            if len(active) != 1 or active[0].observation_id != predecessor:
                raise ValuationConflict(
                    "valuation correction must supersede the one current observation"
                )
            prior = active[0]
            if observation.available_at <= prior.available_at:
                raise ValuationConflict(
                    "valuation correction availability must advance"
                )
            if observation.observed_at < prior.observed_at:
                raise ValuationConflict(
                    "valuation correction observation cannot move backward"
                )

        store, expected_identity = self._journal_store_authority()
        expected_digest = _store_identity_digest(expected_identity)
        durable_payload = {
            "schema_version": "1.0.0",
            "store_identity": _store_identity_payload(expected_identity),
            "store_identity_digest": expected_digest,
            "observation": observation.to_contract_dict(),
        }
        envelope = {
            "event_id": observation.observation_id,
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": observation.scope_id,
            "aggregate_version": str(len(events) + 1),
            "payload": durable_payload,
            "payload_hash": payload_digest(durable_payload),
            "committed_at": _instant_text(observation.observed_at),
        }
        try:
            with journal_store_authority_scope(store, expected_identity):
                result = JournalStore.append_event(store, envelope)
        except ValueError as error:
            # Aggregate-version CAS lost a race. Collapse only an exact duplicate;
            # any other winner changes the semantic cut and requires caller refresh.
            _new_cut, latest_events = self._stable_scope_events(observation.scope_id)
            latest = self._validated_observations(
                latest_events,
                scope_id=observation.scope_id,
                store_identity_digest=self.store_identity_digest,
            )
            for item, _sequence in latest:
                if item.observation_id == observation.observation_id and item == observation:
                    return False
            raise ValuationConflict(
                "valuation history changed concurrently; refresh required"
            ) from error
        return bool(result.inserted)

    def resolve_at(
        self,
        *,
        journal_sequence_cut: int,
        as_of: datetime,
        kind: str,
        provider_id: str,
        account_id: str,
        environment: str,
        provider_environment: str,
        route_policy_id: str,
        instrument_version: str | None = None,
        base_currency: str | None = None,
        quote_currency: str | None = None,
        require_production: bool = True,
    ) -> ValuationObservation:
        if type(journal_sequence_cut) is not int or journal_sequence_cut < 0:
            raise ValuationError("journal_sequence_cut must be a non-negative integer")
        store, expected_identity = self._journal_store_authority()
        expected_digest = _store_identity_digest(expected_identity)
        with journal_store_authority_scope(store, expected_identity):
            current = JournalStore.current_journal_sequence(store)
        if journal_sequence_cut > current:
            raise ValuationError("valuation cut cannot be in the future")

        normalized_kind = _text(kind, "kind").upper()
        try:
            scope = provider_financial_scope(
                provider_id=provider_id,
                environment=environment,
                provider_environment=provider_environment,
                route_policy_id=route_policy_id,
            )
        except ProviderDomainError as error:
            raise ValuationError(str(error)) from error
        scope_payload: dict[str, object] = {
            "schema_version": "1.0.0",
            "kind": normalized_kind,
            "provider_id": scope.provider_id,
            "account_id": _text(account_id, "account_id"),
            "environment": scope.environment,
            "provider_environment": scope.provider_environment,
            "route_policy_id": scope.route_policy_id,
        }
        if normalized_kind == "MARK":
            scope_payload["instrument_version"] = _text(
                instrument_version,
                "instrument_version",
            )
        elif normalized_kind == "FX_QUOTE":
            base = _text(base_currency, "base_currency").upper()
            quote = _text(quote_currency, "quote_currency").upper()
            if base == quote:
                raise ValuationError("FX base and quote currencies must differ")
            scope_payload["base_currency"] = base
            scope_payload["quote_currency"] = quote
        else:
            raise ValuationError("kind must be MARK or FX_QUOTE")
        scope_id = "valuation-scope:" + payload_digest(scope_payload)

        with journal_store_authority_scope(store, expected_identity):
            events = tuple(
                JournalStore.load_events(store, _AGGREGATE_TYPE, scope_id)
            )
        decoded = self._validated_observations(
            events,
            scope_id=scope_id,
            store_identity_digest=expected_digest,
        )
        point = _instant(as_of, "as_of")
        visible = tuple(
            item
            for item, sequence in decoded
            if sequence <= journal_sequence_cut
            and item.available_at <= point
            and item.observed_at <= point
        )
        active = self._active(visible)
        if len(active) != 1:
            if not active:
                raise ValuationError(
                    "no valuation observation is authoritative at the requested cut"
                )
            raise ValuationConflict(
                "valuation history is ambiguous at the requested cut"
            )
        selected = active[0]
        if require_production and selected.evidence_class != "PROVIDER_ORIGIN":
            raise ValuationError(
                "production valuation requires PROVIDER_ORIGIN issuer authority"
            )
        return selected

    def resolve_fresh_at(
        self,
        *,
        journal_sequence_cut: int,
        as_of: datetime,
        policy: ResolvedRiskPolicy,
        kind: str,
        provider_id: str,
        account_id: str,
        environment: str,
        provider_environment: str,
        route_policy_id: str,
        instrument_version: str | None = None,
        base_currency: str | None = None,
        quote_currency: str | None = None,
    ) -> tuple[ValuationObservation, ValuationFreshnessEvidence]:
        """Resolve one production observation and prove freshness at the same cut."""

        try:
            policy = require_registry_issued_resolved_policy(policy)
        except RiskPolicyAuthorityError as error:
            raise ValuationError(str(error)) from error
        if (
            type(journal_sequence_cut) is not int
            or journal_sequence_cut < 0
        ):
            raise ValuationError(
                "journal_sequence_cut must be a non-negative integer"
            )
        if policy.resolved_journal_sequence_cut != journal_sequence_cut:
            raise ValuationError(
                "valuation and risk policy must resolve at the same journal sequence cut"
            )
        _store, expected_identity = self._journal_store_authority()
        if (
            policy.journal_store_identity_digest
            != _store_identity_digest(expected_identity)
        ):
            raise ValuationError(
                "valuation and risk policy must resolve from the same JournalStore generation"
            )
        selected = self.resolve_at(
            journal_sequence_cut=journal_sequence_cut,
            as_of=as_of,
            kind=kind,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
            route_policy_id=route_policy_id,
            instrument_version=instrument_version,
            base_currency=base_currency,
            quote_currency=quote_currency,
            require_production=True,
        )
        freshness = evaluate_valuation_freshness(
            selected,
            policy,
            as_of=as_of,
            journal_sequence_cut=journal_sequence_cut,
        )
        return selected, freshness
