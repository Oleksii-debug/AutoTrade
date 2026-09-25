"""Authoritative provider-evidence boundary for corporate actions.

CorporateActionBook remains a pure deterministic transition engine.  This module
is the admission boundary for provider/account financial truth: a CorporateEvent
may be promoted toward durable financial mutation only after it is derived from
one sealed ProviderResponseObservation with an exact provider/account/environment,
instrument, endpoint, permission, raw-response digest, revision and observation
time binding.

This increment deliberately does not create a second ledger, provider adapter or
reconciliation authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Callable, Mapping
import re

from .corporate_actions import CorporateEvent
from .persistence import payload_digest
from .provider_core import ProviderResponseObservation, Surface


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_KINDS = frozenset(
    {"SPLIT", "CASH_DIVIDEND", "MERGER_CASH", "DELIST", "SYMBOL_CHANGE"}
)


class CorporateActionEvidenceError(ValueError):
    """Provider evidence cannot authorize a corporate-action fact."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CorporateActionEvidenceError(
            f"{name} must be canonical non-empty text"
        )
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CorporateActionEvidenceError(
            f"{name} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _exact_payload(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise CorporateActionEvidenceError("payload must be a mapping")
    normalized: dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = _text(raw_key, "payload key")
        if key in normalized:
            raise CorporateActionEvidenceError(
                "payload keys must be unique after normalization"
            )
        if isinstance(raw_value, (bool, float)):
            raise CorporateActionEvidenceError(
                "corporate-action numeric payload must use exact values"
            )
        if isinstance(raw_value, Decimal):
            if not raw_value.is_finite():
                raise CorporateActionEvidenceError(
                    "corporate-action decimal payload must be finite"
                )
            normalized[key] = format(raw_value, "f")
        elif isinstance(raw_value, int):
            normalized[key] = str(raw_value)
        elif isinstance(raw_value, str):
            normalized[key] = raw_value
        else:
            raise CorporateActionEvidenceError(
                "corporate-action payload values must be text or exact numeric values"
            )
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class CorporateActionObservation:
    """Normalized provider fact before promotion to the pure transition engine."""

    provider_id: str
    account_id: str
    environment: str
    provider_instrument_version: str
    instrument_id: str
    instrument_version: int
    external_event_id: str
    provider_revision: str
    kind: str
    effective_at: datetime
    observed_at: datetime
    raw_evidence_digest: str
    payload: Mapping[str, object]
    complete: bool
    source_sequence: int | None = None
    announcement_at: datetime | None = None
    record_at: datetime | None = None
    ex_at: datetime | None = None
    pay_at: datetime | None = None
    corrects_external_event_id: str | None = None

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, "provider_id").upper()
        account = _text(self.account_id, "account_id")
        environment = _text(self.environment, "environment").upper()
        if environment not in _ENVIRONMENTS:
            raise CorporateActionEvidenceError(
                "environment must be canonical"
            )
        provider_version = _text(
            self.provider_instrument_version, "provider_instrument_version"
        )
        instrument_id = _text(self.instrument_id, "instrument_id")
        if (
            isinstance(self.instrument_version, bool)
            or not isinstance(self.instrument_version, int)
            or self.instrument_version < 1
        ):
            raise CorporateActionEvidenceError(
                "instrument_version must be a positive integer"
            )
        external_id = _text(self.external_event_id, "external_event_id")
        revision = _text(self.provider_revision, "provider_revision")
        kind = _text(self.kind, "kind").upper()
        if kind not in _KINDS:
            raise CorporateActionEvidenceError(
                "unsupported corporate action kind"
            )
        effective = _utc(self.effective_at, "effective_at")
        observed = _utc(self.observed_at, "observed_at")
        if observed < effective:
            raise CorporateActionEvidenceError(
                "provider evidence observed before economic effective time cannot authorize mutation"
            )
        digest = _text(self.raw_evidence_digest, "raw_evidence_digest")
        if _DIGEST.fullmatch(digest) is None:
            raise CorporateActionEvidenceError(
                "raw_evidence_digest must be canonical SHA-256"
            )
        if type(self.complete) is not bool:
            raise CorporateActionEvidenceError("complete must be boolean")
        if self.source_sequence is not None and (
            isinstance(self.source_sequence, bool)
            or not isinstance(self.source_sequence, int)
            or self.source_sequence < 0
        ):
            raise CorporateActionEvidenceError(
                "source_sequence must be non-negative when provided"
            )
        for name in ("announcement_at", "record_at", "ex_at", "pay_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value, name))
        if self.corrects_external_event_id is not None:
            object.__setattr__(
                self,
                "corrects_external_event_id",
                _text(
                    self.corrects_external_event_id,
                    "corrects_external_event_id",
                ),
            )

        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", account)
        object.__setattr__(self, "environment", environment)
        object.__setattr__(
            self, "provider_instrument_version", provider_version
        )
        object.__setattr__(self, "instrument_id", instrument_id)
        object.__setattr__(self, "external_event_id", external_id)
        object.__setattr__(self, "provider_revision", revision)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "effective_at", effective)
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "raw_evidence_digest", digest)
        object.__setattr__(self, "payload", _exact_payload(self.payload))


@dataclass(frozen=True)
class AuthoritativeCorporateAction:
    """CorporateEvent plus immutable evidence identities needed downstream."""

    event: CorporateEvent
    evidence_ref: str
    provider_id: str
    account_id: str
    environment: str
    external_event_id: str
    provider_revision: str
    raw_evidence_digest: str
    query_digest: str
    capability_snapshot_id: str
    provider_instrument_version: str
    observed_at: str
    provenance_digest: str
    corrects_external_event_id: str | None


EvidenceResolver = Callable[[str], ProviderResponseObservation]
CorporateActionNormalizer = Callable[
    [ProviderResponseObservation], CorporateActionObservation
]


def resolve_authoritative_corporate_action(
    evidence_ref: str,
    *,
    evidence_resolver: EvidenceResolver,
    normalizer: CorporateActionNormalizer,
    allowed_endpoints: frozenset[str],
    permission_scope: str,
) -> AuthoritativeCorporateAction:
    """Resolve one accepted event exclusively from sealed provider evidence."""

    reference = _text(evidence_ref, "evidence_ref")
    if not callable(evidence_resolver):
        raise TypeError("evidence_resolver must be callable")
    if not callable(normalizer):
        raise TypeError("normalizer must be callable")
    if not isinstance(allowed_endpoints, frozenset) or not allowed_endpoints:
        raise TypeError("allowed_endpoints must be a non-empty frozenset")
    endpoints = frozenset(
        _text(value, "allowed endpoint") for value in allowed_endpoints
    )
    if any(not value.startswith("/") or "://" in value for value in endpoints):
        raise CorporateActionEvidenceError(
            "allowed endpoints must be provider-relative paths"
        )
    required_permission = _text(permission_scope, "permission_scope")

    try:
        source = evidence_resolver(reference)
    except Exception as error:
        raise CorporateActionEvidenceError(
            "corporate-action evidence could not be resolved"
        ) from error
    if not isinstance(source, ProviderResponseObservation):
        raise CorporateActionEvidenceError(
            "corporate-action evidence must be a sealed ProviderResponseObservation"
        )
    if source.evidence_ref != reference:
        raise CorporateActionEvidenceError(
            "resolved corporate-action evidence identity mismatch"
        )

    binding = source.query_binding
    endpoint = binding.endpoint
    if endpoint not in endpoints:
        raise CorporateActionEvidenceError(
            "corporate-action evidence endpoint is not allowed"
        )
    try:
        source.require_scope(
            provider_id=source.provider_id,
            surface=Surface.ACTIVITIES,
            endpoint=endpoint,
            account_id=source.account_id,
            environment=source.environment,
        )
    except Exception as error:
        raise CorporateActionEvidenceError(
            "corporate-action provider evidence scope mismatch"
        ) from error
    if binding.permission_scope != required_permission:
        raise CorporateActionEvidenceError(
            "corporate-action evidence permission scope mismatch"
        )

    try:
        observation = normalizer(source)
    except Exception as error:
        raise CorporateActionEvidenceError(
            "corporate-action evidence normalization failed"
        ) from error
    if not isinstance(observation, CorporateActionObservation):
        raise CorporateActionEvidenceError(
            "corporate-action normalizer returned an invalid observation"
        )

    source_observed = datetime.fromisoformat(
        source.observed_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    if (
        observation.provider_id != source.provider_id
        or observation.account_id != source.account_id
        or observation.environment != source.environment
        or observation.provider_instrument_version
        != binding.instrument_version
        or observation.raw_evidence_digest != source.response_sha256
        or observation.observed_at != source_observed
    ):
        raise CorporateActionEvidenceError(
            "normalized corporate action does not match sealed provider evidence"
        )
    if observation.complete is not True:
        raise CorporateActionEvidenceError(
            "incomplete corporate-action evidence cannot authorize mutation"
        )

    provenance = {
        "schema_version": "1.0.0",
        "evidence_ref": source.evidence_ref,
        "provider_id": observation.provider_id,
        "account_id": observation.account_id,
        "environment": observation.environment,
        "external_event_id": observation.external_event_id,
        "provider_revision": observation.provider_revision,
        "provider_instrument_version": observation.provider_instrument_version,
        "instrument_id": observation.instrument_id,
        "instrument_version": observation.instrument_version,
        "kind": observation.kind,
        "effective_at": _utc_text(observation.effective_at),
        "observed_at": source.observed_at,
        "raw_evidence_digest": source.response_sha256,
        "query_digest": binding.query_digest,
        "capability_snapshot_id": binding.capability_snapshot_id,
        "endpoint": endpoint,
        "permission_scope": binding.permission_scope,
        "source_sequence": observation.source_sequence,
        "announcement_at": (
            None
            if observation.announcement_at is None
            else _utc_text(observation.announcement_at)
        ),
        "record_at": (
            None
            if observation.record_at is None
            else _utc_text(observation.record_at)
        ),
        "ex_at": (
            None if observation.ex_at is None else _utc_text(observation.ex_at)
        ),
        "pay_at": (
            None if observation.pay_at is None else _utc_text(observation.pay_at)
        ),
        "corrects_external_event_id": observation.corrects_external_event_id,
        "complete": True,
        "payload": dict(observation.payload),
    }
    provenance_digest = payload_digest(provenance)
    event = CorporateEvent.create(
        event_id=observation.external_event_id,
        instrument_id=observation.instrument_id,
        instrument_version=observation.instrument_version,
        kind=observation.kind,
        effective_date=observation.effective_at.date(),
        effective_at=observation.effective_at,
        source_revision=(
            f"{observation.provider_id}:{observation.provider_revision}:"
            f"{provenance_digest}"
        ),
        source_sequence=observation.source_sequence,
        payload=dict(observation.payload),
    )
    return AuthoritativeCorporateAction(
        event=event,
        evidence_ref=source.evidence_ref,
        provider_id=observation.provider_id,
        account_id=observation.account_id,
        environment=observation.environment,
        external_event_id=observation.external_event_id,
        provider_revision=observation.provider_revision,
        raw_evidence_digest=source.response_sha256,
        query_digest=binding.query_digest,
        capability_snapshot_id=binding.capability_snapshot_id,
        provider_instrument_version=binding.instrument_version,
        observed_at=source.observed_at,
        provenance_digest=provenance_digest,
        corrects_external_event_id=observation.corrects_external_event_id,
    )
