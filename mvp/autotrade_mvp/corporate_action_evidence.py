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

from .corporate_actions import CorporateEvent
from .instruments import InstrumentRegistry, InstrumentVersion
from .persistence import JournalStore, payload_digest
from .provider_core import ProviderResponseObservation, Surface


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
    source: ProviderResponseObservation,
) -> CorporateActionObservation:
    """Parse financially authoritative fields only from the sealed response."""

    payload = source.payload
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
    source_observed = _provider_instant(source.observed_at, "observed_at")
    return CorporateActionObservation(
        provider_id=source.provider_id,
        account_id=source.account_id,
        environment=source.environment,
        provider_instrument_version=source.query_binding.instrument_version,
        instrument_id=payload["instrument_id"],
        instrument_version=instrument_version,
        external_event_id=payload["external_event_id"],
        provider_revision=payload["provider_revision"],
        kind=payload["kind"],
        effective_at=_provider_instant(payload["effective_at"], "effective_at"),
        observed_at=source_observed,
        raw_evidence_digest=source.response_sha256,
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
    """Resolve one accepted event exclusively from sealed provider evidence.

    Caller-supplied financial normalizers/instrument objects are explicitly
    rejected.  Provider bytes are parsed by this authority and instrument truth
    comes from the canonical immutable registry at the economic effective cut.
    """

    reference = _text(evidence_ref, "evidence_ref")
    if not callable(evidence_resolver):
        raise TypeError("evidence_resolver must be callable")
    if not isinstance(instrument_registry, InstrumentRegistry):
        raise TypeError("instrument_registry must be InstrumentRegistry")
    if normalizer is not None:
        raise TypeError(
            "caller-supplied corporate-action normalizer is not financial authority"
        )
    if instrument_resolver is not None:
        raise TypeError(
            "caller-supplied instrument_resolver is not financial authority"
        )
    expected_provider = _text(
        expected_provider_id, "expected_provider_id"
    ).upper()
    expected_account = _text(expected_account_id, "expected_account_id")
    expected_environment_value = _text(
        expected_environment, "expected_environment"
    ).upper()
    if expected_environment_value not in _ENVIRONMENTS:
        raise CorporateActionEvidenceError(
            "expected_environment must be canonical"
        )
    if not isinstance(allowed_endpoints, frozenset) or not allowed_endpoints:
        raise TypeError("allowed_endpoints must be a non-empty frozenset")
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
    if binding.permission_scope != required_permission:
        raise CorporateActionEvidenceError(
            "corporate-action evidence permission scope mismatch"
        )

    observation = _canonical_observation_from_sealed_response(source)
    if observation.complete is not True:
        raise CorporateActionEvidenceError(
            "incomplete corporate-action evidence cannot authorize mutation"
        )

    version_ref = (
        f"{observation.instrument_id}@{observation.instrument_version}"
    )
    try:
        instrument = instrument_registry.exact(version_ref)
        effective_instrument = instrument_registry.at(
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
        != binding.instrument_version
    ):
        raise CorporateActionEvidenceError(
            "corporate action does not match canonical instrument/provider binding"
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
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, "provider_id").upper()
        self.account_id = _text(account_id, "account_id")
        self.environment = _text(environment, "environment").upper()
        if self.environment not in _ENVIRONMENTS:
            raise CorporateActionEvidenceError("environment must be canonical")
        self.aggregate_id = (
            "corporate-action-evidence:"
            + payload_digest(
                {
                    "provider_id": self.provider_id,
                    "account_id": self.account_id,
                    "environment": self.environment,
                }
            )[7:]
        )

    def _events(self) -> list[dict[str, object]]:
        events = self.store.load_events(
            self._AGGREGATE_TYPE,
            self.aggregate_id,
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
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
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
        return str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/corporate-action-evidence/"
                + self.provider_id
                + "/"
                + self.account_id
                + "/"
                + self.environment
                + "/"
                + external_event_id,
            )
        )

    def _idempotency_key(self, external_event_id: str) -> str:
        return (
            "corporate-action-evidence:"
            + payload_digest(
                {
                    "provider_id": self.provider_id,
                    "account_id": self.account_id,
                    "environment": self.environment,
                    "external_event_id": external_event_id,
                }
            )[7:]
        )

    def prepare_record_mutation(
        self,
        accepted: AuthoritativeCorporateAction,
    ) -> PreparedCorporateActionEvidenceMutation:
        """Prepare source evidence for a shared JournalStore transaction."""

        if not isinstance(accepted, AuthoritativeCorporateAction):
            raise TypeError("accepted must be AuthoritativeCorporateAction")
        if (
            accepted.provider_id != self.provider_id
            or accepted.account_id != self.account_id
            or accepted.environment != self.environment
        ):
            raise CorporateActionEvidenceConflict(
                "accepted corporate action does not match durable scope"
            )

        events = self._events()
        same_identity = [
            event
            for event in events
            if self._payload(event).get("external_event_id")
            == accepted.external_event_id
        ]
        if same_identity:
            if len(same_identity) != 1:
                raise CorporateActionEvidenceConflict(
                    "external corporate-action identity appears more than once"
                )
            saved = self._payload(same_identity[0])
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
                command_id=self._command_id(accepted.external_event_id),
                idempotency_key=self._idempotency_key(accepted.external_event_id),
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
                if self._payload(event).get("external_event_id")
                == accepted.corrects_external_event_id
            ]
            if len(prior) != 1:
                raise CorporateActionEvidenceConflict(
                    "corporate-action correction target must identify one retained event"
                )
            prior_payload = self._payload(prior[0])
            already_corrected = [
                event
                for event in events
                if self._payload(event).get("corrects_external_event_id")
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
                + self.provider_id
                + "/"
                + self.account_id
                + "/"
                + self.environment
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
            "aggregate_id": self.aggregate_id,
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
            command_id=self._command_id(accepted.external_event_id),
            idempotency_key=self._idempotency_key(accepted.external_event_id),
        )

    def record(
        self,
        accepted: AuthoritativeCorporateAction,
    ) -> DurableCorporateActionEvidenceResult:
        plan = self.prepare_record_mutation(accepted)
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
        _, inserted, _ = self.store.commit_command(
            command_id=plan.command_id,
            actor=self._ACTOR,
            environment=self.environment,
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

