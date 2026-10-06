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
from decimal import Decimal
from types import MappingProxyType
from typing import Callable, Mapping
from uuid import NAMESPACE_URL, uuid5
import re
import threading
import weakref

from .corporate_actions import CorporateEvent
from .instruments import InstrumentRegistry, InstrumentVersion
from .persistence import (
    JournalStore,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_core import (
    ProviderResponseObservation,
    Surface,
    provider_response_observation_projection,
    provider_response_observation_require_scope,
)


_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ENVIRONMENTS = frozenset({"REPLAY", "SIMULATION", "PAPER", "LIVE"})
_KINDS = frozenset(
    {"SPLIT", "CASH_DIVIDEND", "MERGER_CASH", "DELIST", "SYMBOL_CHANGE"}
)
_CORPORATE_ACTION_PARSER_ID = "autotrade.corporate-action.sealed-json"
_CORPORATE_ACTION_PARSER_VERSION = "1.0.0"
_CORPORATE_ACTION_RESERVED_FIELDS = frozenset(
    {
        "external_event_id",
        "provider_revision",
        "instrument_id",
        "instrument_version",
        "effective_at",
        "kind",
        "complete",
        "source_sequence",
        "announcement_at",
        "record_at",
        "ex_at",
        "pay_at",
        "corrects_external_event_id",
    }
)
_CORPORATE_ACTION_PARSER_CONTRACT_DIGEST = payload_digest(
    {
        "parser_id": _CORPORATE_ACTION_PARSER_ID,
        "parser_version": _CORPORATE_ACTION_PARSER_VERSION,
        "source_type": "ProviderResponseObservation",
        "source_surface": "ACTIVITIES",
        "required_fields": [
            "external_event_id",
            "provider_revision",
            "instrument_id",
            "instrument_version",
            "effective_at",
            "kind",
            "complete",
        ],
        "optional_fields": [
            "source_sequence",
            "announcement_at",
            "record_at",
            "ex_at",
            "pay_at",
            "corrects_external_event_id",
        ],
        "event_payload": "ALL_NON_RESERVED_EXACT_FIELDS",
        "financial_binding": "EXACT_SEALED_PAYLOAD",
    }
)


class CorporateActionEvidenceError(ValueError):
    """Provider evidence cannot authorize a corporate-action fact."""


def _text(value: object, name: str) -> str:
    if type(value) is not str:
        raise CorporateActionEvidenceError(
            f"{name} must be canonical exact text"
        )
    stripped = str.strip(value)
    if not stripped or value != stripped:
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
        # Corporate actions are commonly announced before their economic effective
        # time.  Preserve causal observation time independently from effective time;
        # the durable financial writer must gate mutation on effective/pay semantics.
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


def _provider_instant(value: object, name: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CorporateActionEvidenceError(
            f"{name} must be canonical provider UTC text"
        )
    try:
        return datetime.fromisoformat(
            value[:-1] + "+00:00"
        ).astimezone(timezone.utc)
    except ValueError as error:
        raise CorporateActionEvidenceError(
            f"{name} must be canonical provider UTC text"
        ) from error


def _canonical_observation_from_sealed_response(
    projection: Mapping[str, object],
) -> CorporateActionObservation:
    """Parse financially authoritative fields only from one verified snapshot."""

    payload = projection["payload"]
    if not isinstance(payload, Mapping):
        raise CorporateActionEvidenceError(
            "corporate-action provider payload must be an object"
        )
    required = {
        "external_event_id",
        "provider_revision",
        "instrument_id",
        "instrument_version",
        "effective_at",
        "kind",
        "complete",
    }
    missing = required - set(payload)
    if missing:
        raise CorporateActionEvidenceError(
            "corporate-action provider payload is missing required fields: "
            + ", ".join(sorted(missing))
        )

    instrument_version = payload["instrument_version"]
    if (
        isinstance(instrument_version, bool)
        or not isinstance(instrument_version, int)
        or instrument_version < 1
    ):
        raise CorporateActionEvidenceError(
            "provider instrument_version must be a positive integer"
        )
    complete = payload["complete"]
    if type(complete) is not bool:
        raise CorporateActionEvidenceError(
            "provider complete flag must be boolean"
        )
    source_sequence = payload.get("source_sequence")
    if source_sequence is not None and (
        isinstance(source_sequence, bool)
        or not isinstance(source_sequence, int)
        or source_sequence < 0
    ):
        raise CorporateActionEvidenceError(
            "provider source_sequence must be non-negative"
        )

    optional_times: dict[str, datetime | None] = {}
    for name in ("announcement_at", "record_at", "ex_at", "pay_at"):
        raw = payload.get(name)
        optional_times[name] = (
            None if raw is None else _provider_instant(raw, name)
        )

    event_payload = {
        key: value
        for key, value in payload.items()
        if key not in _CORPORATE_ACTION_RESERVED_FIELDS
    }
    source_observed = _provider_instant(
        projection["observed_at"], "observed_at"
    )
    return CorporateActionObservation(
        provider_id=projection["provider_id"],
        account_id=projection["account_id"],
        environment=projection["environment"],
        provider_instrument_version=projection["instrument_version"],
        instrument_id=payload["instrument_id"],
        instrument_version=instrument_version,
        external_event_id=payload["external_event_id"],
        provider_revision=payload["provider_revision"],
        kind=payload["kind"],
        effective_at=_provider_instant(payload["effective_at"], "effective_at"),
        observed_at=source_observed,
        raw_evidence_digest=projection["response_sha256"],
        payload=event_payload,
        complete=complete,
        source_sequence=source_sequence,
        announcement_at=optional_times["announcement_at"],
        record_at=optional_times["record_at"],
        ex_at=optional_times["ex_at"],
        pay_at=optional_times["pay_at"],
        corrects_external_event_id=payload.get(
            "corrects_external_event_id"
        ),
    )


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


def _authoritative_corporate_action_operations():
    """Retain resolver issuance authority outside caller-writable dataclass state."""

    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}
    state_lock = threading.RLock()
    canonical_action_type = AuthoritativeCorporateAction
    canonical_event_type = CorporateEvent
    canonical_event_create = CorporateEvent.create

    def event_snapshot(event: CorporateEvent) -> tuple[object, ...]:
        if type(event) is not canonical_event_type:
            raise CorporateActionEvidenceError(
                "authoritative corporate action requires exact CorporateEvent"
            )
        for name in ("event_id", "instrument_id", "kind", "source_revision"):
            if type(getattr(event, name)) is not str:
                raise CorporateActionEvidenceError(
                    f"authoritative corporate-action event {name} must remain exact text"
                )
        if type(event.instrument_version) is not int:
            raise CorporateActionEvidenceError(
                "authoritative corporate-action instrument_version must remain exact int"
            )
        if event.source_sequence is not None and type(event.source_sequence) is not int:
            raise CorporateActionEvidenceError(
                "authoritative corporate-action source_sequence must remain exact int"
            )
        if event.effective_at is not None and type(event.effective_at) is not datetime:
            raise CorporateActionEvidenceError(
                "authoritative corporate-action effective_at must remain exact datetime"
            )
        if type(event.payload) is not dict:
            raise CorporateActionEvidenceError(
                "authoritative corporate-action payload must remain canonical"
            )
        if any(
            type(key) is not str or type(item) is not str
            for key, item in event.payload.items()
        ):
            raise CorporateActionEvidenceError(
                "authoritative corporate-action payload scalars must remain exact text"
            )
        return (
            event.event_id,
            event.instrument_id,
            event.instrument_version,
            event.kind,
            event.effective_date,
            event.source_revision,
            tuple(sorted(event.payload.items())),
            event.source_sequence,
            event.effective_at,
        )

    def snapshot(value: AuthoritativeCorporateAction) -> tuple[object, ...]:
        for name in (
            "evidence_ref",
            "provider_id",
            "account_id",
            "environment",
            "external_event_id",
            "provider_revision",
            "raw_evidence_digest",
            "query_digest",
            "capability_snapshot_id",
            "provider_instrument_version",
            "observed_at",
            "provenance_digest",
        ):
            if type(getattr(value, name)) is not str:
                raise CorporateActionEvidenceError(
                    f"authoritative corporate-action {name} must remain exact text"
                )
        if (
            value.corrects_external_event_id is not None
            and type(value.corrects_external_event_id) is not str
        ):
            raise CorporateActionEvidenceError(
                "authoritative corporate-action correction identity must remain exact text"
            )
        return (
            event_snapshot(value.event),
            value.evidence_ref,
            value.provider_id,
            value.account_id,
            value.environment,
            value.external_event_id,
            value.provider_revision,
            value.raw_evidence_digest,
            value.query_digest,
            value.capability_snapshot_id,
            value.provider_instrument_version,
            value.observed_at,
            value.provenance_digest,
            value.corrects_external_event_id,
        )

    def detached(
        state: tuple[object, ...],
    ) -> AuthoritativeCorporateAction:
        event_state = state[0]
        event = canonical_event_create(
            event_id=event_state[0],
            instrument_id=event_state[1],
            instrument_version=event_state[2],
            kind=event_state[3],
            effective_date=event_state[4],
            source_revision=event_state[5],
            payload=dict(event_state[6]),
            source_sequence=event_state[7],
            effective_at=event_state[8],
        )
        return canonical_action_type(
            event=event,
            evidence_ref=state[1],
            provider_id=state[2],
            account_id=state[3],
            environment=state[4],
            external_event_id=state[5],
            provider_revision=state[6],
            raw_evidence_digest=state[7],
            query_digest=state[8],
            capability_snapshot_id=state[9],
            provider_instrument_version=state[10],
            observed_at=state[11],
            provenance_digest=state[12],
            corrects_external_event_id=state[13],
        )

    def prune_dead() -> None:
        dead = [
            object_id
            for object_id, (value_ref, _state) in states.items()
            if value_ref() is None
        ]
        for object_id in dead:
            states.pop(object_id, None)

    def register(value: AuthoritativeCorporateAction) -> None:
        if type(value) is not canonical_action_type:
            raise TypeError(
                "issued corporate action must be exact AuthoritativeCorporateAction"
            )
        current = snapshot(value)
        with state_lock:
            prune_dead()
            object_id = id(value)
            previous = states.get(object_id)
            if previous is not None and previous[0]() is not None:
                raise CorporateActionEvidenceError(
                    "authoritative corporate-action issuance identity collision"
                )
            states[object_id] = (weakref.ref(value), current)

    def require(
        value: AuthoritativeCorporateAction,
    ) -> AuthoritativeCorporateAction:
        if type(value) is not canonical_action_type:
            raise TypeError(
                "accepted must be canonical AuthoritativeCorporateAction"
            )
        with state_lock:
            prune_dead()
            state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise CorporateActionEvidenceError(
                "corporate action lacks canonical resolver issuance authority"
            )
        if snapshot(value) != state[1]:
            raise CorporateActionEvidenceError(
                "canonical corporate action changed after resolver issuance"
            )
        trusted = detached(state[1])
        register(trusted)
        return trusted

    return register, require


(
    _register_authoritative_corporate_action,
    _require_authoritative_corporate_action,
) = _authoritative_corporate_action_operations()
del _authoritative_corporate_action_operations


def _resolve_authoritative_corporate_action_impl(
    evidence_ref: str,
    *,
    evidence_resolver: EvidenceResolver,
    instrument_registry: InstrumentRegistry,
    expected_provider_id: str,
    expected_account_id: str,
    expected_environment: str,
    allowed_endpoints: frozenset[str],
    permission_scope: str,
    normalizer: object | None = None,
    instrument_resolver: object | None = None,
    _register_authority,
    _provider_projection,
    _provider_require_scope,
    _instrument_registry_type,
    _corporate_event_create,
    _authoritative_action_type,
) -> AuthoritativeCorporateAction:
    """Resolve one accepted event exclusively from sealed provider evidence.

    Caller-supplied financial normalizers/instrument objects are explicitly
    rejected.  Provider bytes are parsed by this authority and instrument truth
    comes from the canonical immutable registry at the economic effective cut.
    """

    expected_environment_value = str.upper(
        _text(expected_environment, "expected_environment")
    )
    if expected_environment_value not in _ENVIRONMENTS:
        raise CorporateActionEvidenceError(
            "expected_environment must be canonical"
        )
    if expected_environment_value in {"PAPER", "LIVE"}:
        raise CorporateActionEvidenceError(
            "PAPER/LIVE corporate actions require durable provider-origin authority"
        )

    reference = _text(evidence_ref, "evidence_ref")
    if not callable(evidence_resolver):
        raise TypeError("evidence_resolver must be callable")
    if type(instrument_registry) is not _instrument_registry_type:
        raise TypeError("instrument_registry must be exact InstrumentRegistry")
    if any(name in vars(instrument_registry) for name in ("exact", "at")):
        raise TypeError("instrument_registry lookup methods must not be shadowed")
    if normalizer is not None:
        raise TypeError(
            "caller-supplied corporate-action normalizer is not financial authority"
        )
    if instrument_resolver is not None:
        raise TypeError(
            "caller-supplied instrument_resolver is not financial authority"
        )
    expected_provider = str.upper(
        _text(expected_provider_id, "expected_provider_id")
    )
    expected_account = _text(expected_account_id, "expected_account_id")
    if type(allowed_endpoints) is not frozenset or not allowed_endpoints:
        raise TypeError("allowed_endpoints must be an exact non-empty frozenset")
    endpoints = frozenset(
        _text(value, "allowed endpoint") for value in allowed_endpoints
    )
    if any(
        not value.startswith("/") or "://" in value
        for value in endpoints
    ):
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
    if type(source) is not ProviderResponseObservation:
        raise CorporateActionEvidenceError(
            "corporate-action evidence must be an exact sealed ProviderResponseObservation"
        )
    if "require_scope" in vars(source):
        raise CorporateActionEvidenceError(
            "corporate-action provider observation callback must not be shadowed"
        )

    try:
        projection = _provider_projection(source)
    except Exception as error:
        raise CorporateActionEvidenceError(
            "corporate-action provider observation authority mismatch"
        ) from error

    if projection["evidence_ref"] != reference:
        raise CorporateActionEvidenceError(
            "resolved corporate-action evidence identity mismatch"
        )
    binding = projection["query_binding"]
    if "require_scope" in vars(binding):
        raise CorporateActionEvidenceError(
            "corporate-action provider binding callback must not be shadowed"
        )
    endpoint = projection["endpoint"]
    if endpoint not in endpoints:
        raise CorporateActionEvidenceError(
            "corporate-action evidence endpoint is not allowed"
        )
    try:
        projection = _provider_require_scope(
            source,
            provider_id=expected_provider,
            surface=Surface.ACTIVITIES,
            endpoint=endpoint,
            account_id=expected_account,
            environment=expected_environment_value,
        )
    except Exception as error:
        raise CorporateActionEvidenceError(
            "corporate-action provider evidence scope mismatch"
        ) from error
    if (
        projection["evidence_ref"] != reference
        or projection["endpoint"] != endpoint
    ):
        raise CorporateActionEvidenceError(
            "corporate-action provider observation changed during scope verification"
        )
    if projection["permission_scope"] != required_permission:
        raise CorporateActionEvidenceError(
            "corporate-action evidence permission scope mismatch"
        )

    observation = _canonical_observation_from_sealed_response(projection)
    if observation.complete is not True:
        raise CorporateActionEvidenceError(
            "incomplete corporate-action evidence cannot authorize mutation"
        )

    version_ref = (
        f"{observation.instrument_id}@{observation.instrument_version}"
    )
    try:
        instrument = _instrument_registry_type.exact(
            instrument_registry, version_ref
        )
        effective_instrument = _instrument_registry_type.at(
            instrument_registry,
            observation.instrument_id,
            observation.effective_at,
        )
    except Exception as error:
        raise CorporateActionEvidenceError(
            "canonical corporate-action instrument could not be resolved"
        ) from error
    if instrument != effective_instrument:
        raise CorporateActionEvidenceError(
            "corporate-action instrument version is not effective at action cut"
        )
    if (
        instrument.provider_id.upper() != observation.provider_id
        or f"{instrument.provider_symbol}@{instrument.version}"
        != projection["instrument_version"]
    ):
        raise CorporateActionEvidenceError(
            "corporate action does not match canonical instrument/provider binding"
        )

    provenance = {
        "schema_version": "1.0.0",
        "evidence_ref": projection["evidence_ref"],
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
        "observed_at": projection["observed_at"],
        "raw_evidence_digest": projection["response_sha256"],
        "query_digest": projection["query_digest"],
        "capability_snapshot_id": projection["capability_snapshot_id"],
        "endpoint": endpoint,
        "permission_scope": projection["permission_scope"],
        "parser_id": _CORPORATE_ACTION_PARSER_ID,
        "parser_version": _CORPORATE_ACTION_PARSER_VERSION,
        "parser_contract_digest": _CORPORATE_ACTION_PARSER_CONTRACT_DIGEST,
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
            None
            if observation.ex_at is None
            else _utc_text(observation.ex_at)
        ),
        "pay_at": (
            None
            if observation.pay_at is None
            else _utc_text(observation.pay_at)
        ),
        "corrects_external_event_id": (
            observation.corrects_external_event_id
        ),
        "complete": True,
        "payload": dict(observation.payload),
    }
    provenance_digest = payload_digest(provenance)
    event = _corporate_event_create(
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
    accepted = _authoritative_action_type(
        event=event,
        evidence_ref=projection["evidence_ref"],
        provider_id=observation.provider_id,
        account_id=observation.account_id,
        environment=observation.environment,
        external_event_id=observation.external_event_id,
        provider_revision=observation.provider_revision,
        raw_evidence_digest=projection["response_sha256"],
        query_digest=projection["query_digest"],
        capability_snapshot_id=projection["capability_snapshot_id"],
        provider_instrument_version=projection["instrument_version"],
        observed_at=projection["observed_at"],
        provenance_digest=provenance_digest,
        corrects_external_event_id=observation.corrects_external_event_id,
    )
    _register_authority(accepted)
    return accepted



def _bind_authoritative_corporate_action_resolver(
    resolve_impl,
    register_authority,
    provider_projection,
    provider_require_scope,
    instrument_registry_type,
    corporate_event_create,
    authoritative_action_type,
):
    def resolve_authoritative_corporate_action(
        evidence_ref: str,
        *,
        evidence_resolver: EvidenceResolver,
        instrument_registry: InstrumentRegistry,
        expected_provider_id: str,
        expected_account_id: str,
        expected_environment: str,
        allowed_endpoints: frozenset[str],
        permission_scope: str,
        normalizer: object | None = None,
        instrument_resolver: object | None = None,
    ) -> AuthoritativeCorporateAction:
        return resolve_impl(
            evidence_ref,
            evidence_resolver=evidence_resolver,
            instrument_registry=instrument_registry,
            expected_provider_id=expected_provider_id,
            expected_account_id=expected_account_id,
            expected_environment=expected_environment,
            allowed_endpoints=allowed_endpoints,
            permission_scope=permission_scope,
            normalizer=normalizer,
            instrument_resolver=instrument_resolver,
            _register_authority=register_authority,
            _provider_projection=provider_projection,
            _provider_require_scope=provider_require_scope,
            _instrument_registry_type=instrument_registry_type,
            _corporate_event_create=corporate_event_create,
            _authoritative_action_type=authoritative_action_type,
        )

    return resolve_authoritative_corporate_action


resolve_authoritative_corporate_action = _bind_authoritative_corporate_action_resolver(
    _resolve_authoritative_corporate_action_impl,
    _register_authoritative_corporate_action,
    provider_response_observation_projection,
    provider_response_observation_require_scope,
    InstrumentRegistry,
    CorporateEvent.create,
    AuthoritativeCorporateAction,
)
del _bind_authoritative_corporate_action_resolver
del _resolve_authoritative_corporate_action_impl
del _register_authoritative_corporate_action


class CorporateActionEvidenceConflict(CorporateActionEvidenceError):
    """Durable provider action identity conflicts with retained evidence."""


@dataclass(frozen=True)
class DurableCorporateActionEvidenceResult:
    event_id: str
    external_event_id: str
    aggregate_version: int
    inserted: bool
    provenance_digest: str
    corrects_external_event_id: str | None


@dataclass(frozen=True)
class PreparedCorporateActionEvidenceMutation:
    """One evidence mutation prepared from one immutable source-journal cut."""

    accepted: AuthoritativeCorporateAction
    event_id: str
    aggregate_version: int
    envelope: dict[str, object] | None
    request: dict[str, object]
    result: dict[str, object]
    command_id: str
    idempotency_key: str
    already_committed: bool = False


def _durable_corporate_action_store_operations():
    """Seal one durable scope without discoverable weakref callbacks."""

    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            weakref.ReferenceType,
            object,
            str,
            str,
            str,
            str,
        ],
    ] = {}
    state_lock = threading.RLock()

    def prune_dead() -> None:
        dead = [key for key, state in states.items() if state[0]() is None]
        for key in dead:
            states.pop(key, None)

    def require_unbound(value: object) -> None:
        object_id = id(value)
        with state_lock:
            prune_dead()
            current = states.get(object_id)
            if current is None:
                return
            current_value = current[0]()
            if current_value is value:
                raise CorporateActionEvidenceConflict(
                    "corporate-action evidence composition is already initialized"
                )
            if current_value is not None:
                raise CorporateActionEvidenceConflict(
                    "corporate-action evidence binding identity collision"
                )
            states.pop(object_id, None)

    def register(
        value: object,
        *,
        store: JournalStore,
        store_identity: object,
        provider_id: str,
        account_id: str,
        environment: str,
        aggregate_id: str,
    ) -> None:
        object_id = id(value)
        with state_lock:
            prune_dead()
            current = states.get(object_id)
            if current is not None:
                current_value = current[0]()
                if current_value is value:
                    raise CorporateActionEvidenceConflict(
                        "corporate-action evidence composition is already initialized"
                    )
                if current_value is not None:
                    raise CorporateActionEvidenceConflict(
                        "corporate-action evidence binding identity collision"
                    )
                states.pop(object_id, None)
            states[object_id] = (
                weakref.ref(value),
                weakref.ref(store),
                store_identity,
                provider_id,
                account_id,
                environment,
                aggregate_id,
            )

    def binding(
        value: object,
    ) -> tuple[JournalStore, object, str, str, str, str]:
        with state_lock:
            state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise CorporateActionEvidenceConflict(
                "corporate-action evidence process binding is unavailable"
            )
        store = state[1]()
        if store is None:
            raise CorporateActionEvidenceConflict(
                "corporate-action evidence selected JournalStore was lost"
            )
        return store, state[2], state[3], state[4], state[5], state[6]

    return require_unbound, register, binding


(
    _require_unbound_durable_corporate_action_store,
    _register_durable_corporate_action_store,
    _durable_corporate_action_store_binding,
) = _durable_corporate_action_store_operations()
del _durable_corporate_action_store_operations

class DurableCorporateActionEvidenceStore:
    """Exactly-once durable history for admitted corporate-action evidence.

    This store persists evidence authority only.  It does not mutate positions,
    cash, basis or settlement.  A later WP-31 financial writer must atomically
    combine one retained evidence event with canonical economic-book mutation.
    """

    _AGGREGATE_TYPE = "corporate_action_evidence"
    _EVENT_TYPE = "CorporateActionEvidenceAccepted"
    _ACTOR = "corporate-action-evidence"

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ) -> None:
        if type(self) is not DurableCorporateActionEvidenceStore:
            raise TypeError(
                "durable corporate-action evidence store must be exact canonical type"
            )
        _require_unbound_durable_corporate_action_store(self)
        store_identity = require_exact_journal_store_authority(
            store,
            subject="corporate-action evidence JournalStore",
        )
        provider = _text(provider_id, "provider_id").upper()
        account = _text(account_id, "account_id")
        environment_value = _text(environment, "environment").upper()
        if environment_value not in _ENVIRONMENTS:
            raise CorporateActionEvidenceError("environment must be canonical")
        aggregate_id = (
            "corporate-action-evidence:"
            + payload_digest(
                {
                    "provider_id": provider,
                    "account_id": account,
                    "environment": environment_value,
                }
            )[7:]
        )
        _register_durable_corporate_action_store(
            self,
            store=store,
            store_identity=store_identity,
            provider_id=provider,
            account_id=account,
            environment=environment_value,
            aggregate_id=aggregate_id,
        )
        # Compatibility/diagnostic views only; financial authority resolves the
        # closure-owned composition and fails closed if these are retargeted.
        self._store_identity = store_identity
        self.store = store
        self.provider_id = provider
        self.account_id = account
        self.environment = environment_value
        self.aggregate_id = aggregate_id

    def _composition(self):
        (
            store,
            expected_identity,
            provider_id,
            account_id,
            environment,
            aggregate_id,
        ) = _durable_corporate_action_store_binding(self)
        visible = vars(self)
        if (
            visible.get("store") is not store
            or visible.get("_store_identity") != expected_identity
            or visible.get("provider_id") != provider_id
            or visible.get("account_id") != account_id
            or visible.get("environment") != environment
            or visible.get("aggregate_id") != aggregate_id
        ):
            raise CorporateActionEvidenceConflict(
                "corporate-action evidence composition was modified"
            )
        current_identity = require_exact_journal_store_authority(
            store,
            subject="corporate-action evidence JournalStore",
        )
        if current_identity != expected_identity:
            raise CorporateActionEvidenceConflict(
                "corporate-action evidence JournalStore generation changed"
            )
        return (
            store,
            expected_identity,
            provider_id,
            account_id,
            environment,
            aggregate_id,
        )

    def _validated_store(self):
        store, identity, _, _, _, _ = (
            DurableCorporateActionEvidenceStore._composition(self)
        )
        return store, identity

    def _events(self) -> list[dict[str, object]]:
        (
            store,
            identity,
            provider_id,
            account_id,
            environment,
            aggregate_id,
        ) = DurableCorporateActionEvidenceStore._composition(self)
        with journal_store_authority_scope(store, identity):
            events = JournalStore.load_events(
                store,
                self._AGGREGATE_TYPE,
                aggregate_id,
            )
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise CorporateActionEvidenceConflict(
                    "corporate-action evidence versions are not contiguous"
                )
            expected_version += 1
            if event.get("event_type") != self._EVENT_TYPE:
                raise CorporateActionEvidenceConflict(
                    "corporate-action evidence journal contains unsupported event"
                )
            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise CorporateActionEvidenceConflict(
                    "corporate-action durable payload is invalid"
                )
            if (
                payload.get("provider_id") != provider_id
                or payload.get("account_id") != account_id
                or payload.get("environment") != environment
            ):
                raise CorporateActionEvidenceConflict(
                    "corporate-action durable scope is invalid"
                )
        return events

    @staticmethod
    def _payload(event: Mapping[str, object]) -> Mapping[str, object]:
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise CorporateActionEvidenceConflict(
                "corporate-action durable payload is invalid"
            )
        return payload

    def _command_id(self, external_event_id: str) -> str:
        _, _, provider_id, account_id, environment, _ = (
            DurableCorporateActionEvidenceStore._composition(self)
        )
        return str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/corporate-action-evidence/"
                + provider_id
                + "/"
                + account_id
                + "/"
                + environment
                + "/"
                + external_event_id,
            )
        )

    def _idempotency_key(self, external_event_id: str) -> str:
        _, _, provider_id, account_id, environment, _ = (
            DurableCorporateActionEvidenceStore._composition(self)
        )
        return (
            "corporate-action-evidence:"
            + payload_digest(
                {
                    "provider_id": provider_id,
                    "account_id": account_id,
                    "environment": environment,
                    "external_event_id": external_event_id,
                }
            )[7:]
        )

    def prepare_record_mutation(
        self,
        accepted: AuthoritativeCorporateAction,
    ) -> PreparedCorporateActionEvidenceMutation:
        """Prepare source evidence for a shared JournalStore transaction."""

        _, _, provider_id, account_id, environment, aggregate_id = (
            DurableCorporateActionEvidenceStore._composition(self)
        )
        if (
            accepted.provider_id != provider_id
            or accepted.account_id != account_id
            or accepted.environment != environment
        ):
            raise CorporateActionEvidenceConflict(
                "accepted corporate action does not match durable scope"
            )

        events = DurableCorporateActionEvidenceStore._events(self)
        same_identity = [
            event
            for event in events
            if DurableCorporateActionEvidenceStore._payload(event).get("external_event_id")
            == accepted.external_event_id
        ]
        if same_identity:
            if len(same_identity) != 1:
                raise CorporateActionEvidenceConflict(
                    "external corporate-action identity appears more than once"
                )
            saved = DurableCorporateActionEvidenceStore._payload(same_identity[0])
            if (
                saved.get("provenance_digest") != accepted.provenance_digest
                or saved.get("evidence_ref") != accepted.evidence_ref
                or saved.get("raw_evidence_digest") != accepted.raw_evidence_digest
                or saved.get("provider_revision") != accepted.provider_revision
                or saved.get("corrects_external_event_id")
                != accepted.corrects_external_event_id
            ):
                raise CorporateActionEvidenceConflict(
                    "external corporate-action identity was reused with changed evidence"
                )
            event_id = str(same_identity[0]["event_id"])
            version = int(same_identity[0]["aggregate_version"])
            request = {
                "schema_version": "1.0.0",
                "external_event_id": accepted.external_event_id,
                "provenance_digest": accepted.provenance_digest,
                "evidence_ref": accepted.evidence_ref,
            }
            result = {
                "event_id": event_id,
                "external_event_id": accepted.external_event_id,
                "aggregate_version": version,
                "provenance_digest": accepted.provenance_digest,
                "corrects_external_event_id": accepted.corrects_external_event_id,
            }
            return PreparedCorporateActionEvidenceMutation(
                accepted=accepted,
                event_id=event_id,
                aggregate_version=version,
                envelope=None,
                request=request,
                result=result,
                command_id=DurableCorporateActionEvidenceStore._command_id(self, accepted.external_event_id),
                idempotency_key=DurableCorporateActionEvidenceStore._idempotency_key(self, accepted.external_event_id),
                already_committed=True,
            )

        corrected = None
        if accepted.corrects_external_event_id is not None:
            if accepted.corrects_external_event_id == accepted.external_event_id:
                raise CorporateActionEvidenceConflict(
                    "corporate action cannot correct itself"
                )
            prior = [
                event
                for event in events
                if DurableCorporateActionEvidenceStore._payload(event).get("external_event_id")
                == accepted.corrects_external_event_id
            ]
            if len(prior) != 1:
                raise CorporateActionEvidenceConflict(
                    "corporate-action correction target must identify one retained event"
                )
            prior_payload = DurableCorporateActionEvidenceStore._payload(prior[0])
            already_corrected = [
                event
                for event in events
                if DurableCorporateActionEvidenceStore._payload(event).get("corrects_external_event_id")
                == accepted.corrects_external_event_id
            ]
            if already_corrected:
                raise CorporateActionEvidenceConflict(
                    "corporate-action evidence already has a correction"
                )
            if (
                prior_payload.get("instrument_id") != accepted.event.instrument_id
                or prior_payload.get("instrument_version")
                != accepted.event.instrument_version
                or prior_payload.get("kind") != accepted.event.kind
            ):
                raise CorporateActionEvidenceConflict(
                    "corporate-action correction cannot change immutable action identity"
                )
            if prior_payload.get("provider_revision") == accepted.provider_revision:
                raise CorporateActionEvidenceConflict(
                    "corporate-action correction requires a new provider revision"
                )
            if (
                prior_payload.get("evidence_ref") == accepted.evidence_ref
                or prior_payload.get("raw_evidence_digest")
                == accepted.raw_evidence_digest
            ):
                raise CorporateActionEvidenceConflict(
                    "corporate-action correction requires fresh provider evidence"
                )
            corrected = accepted.corrects_external_event_id

        next_version = 1 if not events else int(events[-1]["aggregate_version"]) + 1
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/corporate-action-evidence/"
                + provider_id
                + "/"
                + account_id
                + "/"
                + environment
                + "/"
                + accepted.external_event_id,
            )
        )
        durable_payload = {
            "schema_version": "1.0.0",
            "provider_id": accepted.provider_id,
            "account_id": accepted.account_id,
            "environment": accepted.environment,
            "external_event_id": accepted.external_event_id,
            "corrects_external_event_id": corrected,
            "provider_revision": accepted.provider_revision,
            "instrument_id": accepted.event.instrument_id,
            "instrument_version": accepted.event.instrument_version,
            "kind": accepted.event.kind,
            "effective_at": _utc_text(accepted.event.effective_at),
            "observed_at": accepted.observed_at,
            "source_sequence": accepted.event.source_sequence,
            "evidence_ref": accepted.evidence_ref,
            "raw_evidence_digest": accepted.raw_evidence_digest,
            "query_digest": accepted.query_digest,
            "capability_snapshot_id": accepted.capability_snapshot_id,
            "provider_instrument_version": accepted.provider_instrument_version,
            "provenance_digest": accepted.provenance_digest,
            "source_revision": accepted.event.source_revision,
            "payload": dict(accepted.event.payload),
        }
        envelope = {
            "event_id": event_id,
            "event_type": self._EVENT_TYPE,
            "aggregate_type": self._AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(next_version),
            "committed_at": accepted.observed_at,
            "payload": durable_payload,
            "payload_hash": payload_digest(durable_payload),
        }
        request = {
            "schema_version": "1.0.0",
            "external_event_id": accepted.external_event_id,
            "provenance_digest": accepted.provenance_digest,
            "evidence_ref": accepted.evidence_ref,
        }
        result = {
            "event_id": event_id,
            "external_event_id": accepted.external_event_id,
            "aggregate_version": next_version,
            "provenance_digest": accepted.provenance_digest,
            "corrects_external_event_id": corrected,
        }
        return PreparedCorporateActionEvidenceMutation(
            accepted=accepted,
            event_id=event_id,
            aggregate_version=next_version,
            envelope=envelope,
            request=request,
            result=result,
            command_id=DurableCorporateActionEvidenceStore._command_id(self, accepted.external_event_id),
            idempotency_key=DurableCorporateActionEvidenceStore._idempotency_key(self, accepted.external_event_id),
        )

    def record(
        self,
        accepted: AuthoritativeCorporateAction,
    ) -> DurableCorporateActionEvidenceResult:
        plan = DurableCorporateActionEvidenceStore.prepare_record_mutation(
            self,
            accepted,
        )
        if plan.already_committed:
            return DurableCorporateActionEvidenceResult(
                event_id=plan.event_id,
                external_event_id=accepted.external_event_id,
                aggregate_version=plan.aggregate_version,
                inserted=False,
                provenance_digest=accepted.provenance_digest,
                corrects_external_event_id=accepted.corrects_external_event_id,
            )
        if plan.envelope is None:
            raise CorporateActionEvidenceConflict(
                "fresh corporate-action evidence plan lacks durable envelope"
            )
        store, identity, _, _, environment, _ = (
            DurableCorporateActionEvidenceStore._composition(self)
        )
        with journal_store_authority_scope(store, identity):
            _, inserted, _ = JournalStore.commit_command(
                store,
                command_id=plan.command_id,
                actor=self._ACTOR,
                environment=environment,
                idempotency_key=plan.idempotency_key,
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, None)],
            )
        return DurableCorporateActionEvidenceResult(
            event_id=plan.event_id,
            external_event_id=accepted.external_event_id,
            aggregate_version=plan.aggregate_version,
            inserted=inserted,
            provenance_digest=accepted.provenance_digest,
            corrects_external_event_id=accepted.corrects_external_event_id,
        )

def _bind_durable_corporate_action_verifier(
    prepare_record_mutation,
    require_authority,
):
    def verified_prepare_record_mutation(
        self,
        accepted: AuthoritativeCorporateAction,
    ) -> PreparedCorporateActionEvidenceMutation:
        trusted = require_authority(accepted)
        return prepare_record_mutation(self, trusted)

    return verified_prepare_record_mutation


DurableCorporateActionEvidenceStore.prepare_record_mutation = (
    _bind_durable_corporate_action_verifier(
        DurableCorporateActionEvidenceStore.prepare_record_mutation,
        _require_authoritative_corporate_action,
    )
)
del _bind_durable_corporate_action_verifier
del _require_authoritative_corporate_action


