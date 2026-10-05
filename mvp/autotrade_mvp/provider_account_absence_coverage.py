"""Issuer-protected searched-submission and provider-surface coverage evidence.

This WP-20/#697 layer removes two caller-authored inputs from the future
PROVEN_ABSENT path:

* the historical ambiguous send is reconstructed from the canonical durable
  SubmissionPrepared -> SubmissionSending -> SubmissionUnknown chronology;
* one provider surface coverage window is derived from a sealed terminal
  ProviderAccountPageChain plus exact current provider-Q absence semantics.

It deliberately does not issue PROVEN_ABSENT or claim that the qualified
consistency horizon has elapsed.  A later account-cut/negative-proof issuer
must compose all required surface coverage, provider facts, independent
horizon authority, and current acquisition/origin-set authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
import weakref

from .dispatch import (
    _canonical_journal_authority_snapshot,
    submission_attempt_aggregate_id,
)
from .durable_provider_qualification import DurableProviderQualificationRegistry
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_account_absence_semantics import (
    ProviderAccountAbsenceSemanticsError,
    QualifiedProviderAccountAbsenceSemantics,
    require_provider_account_absence_rule,
)
from .provider_account_page_chain import (
    ProviderAccountPageChain,
    ProviderAccountPageChainError,
    require_current_provider_account_page_chain_authority,
    require_provider_account_page_chain_authority,
)

_SCHEMA_VERSION = "1.0.0"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SUPPORTED_PROVIDER = "BYBIT"
_SUPPORTED_QUERY_SCOPE_RULE = "BYBIT_SPOT_ACCOUNT_QUERY_V1"
_SUPPORTED_PAGINATION_RULE = "BYBIT_V5_CURSOR_V1"
_SUPPORTED_HORIZON_RULE = "BYBIT_ACCOUNT_CONSISTENCY_HORIZON_V1"
_SUPPORTED_RETENTION_RULE_BY_SURFACE = {
    "OPEN_ORDERS": "BYBIT_OPEN_ORDER_RETENTION_V1",
    "ORDER_HISTORY": "BYBIT_ORDER_HISTORY_RETENTION_V1",
    "EXECUTIONS": "BYBIT_EXECUTION_RETENTION_V1",
    "ACTIVITIES": "BYBIT_ACTIVITY_RETENTION_V1",
}
_SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
_ORDER_HISTORY_FULL_STATUS_LIMIT = timedelta(hours=24)


class ProviderAccountAbsenceCoverageError(ValueError):
    """Durable searched-send or exact provider coverage authority is invalid."""


def _exact_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderAccountAbsenceCoverageError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ProviderAccountAbsenceCoverageError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ProviderAccountAbsenceCoverageError(
            f"{name} must be canonical UTC text"
        ) from error
    if point.tzinfo is None or point.utcoffset() is None:
        raise ProviderAccountAbsenceCoverageError(f"{name} must include timezone")
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ProviderAccountAbsenceCoverageError(f"{name} must be canonical UTC text")
    return canonical


def _point(value: str, *, name: str) -> datetime:
    return datetime.fromisoformat(_utc_text(value, name=name).replace("Z", "+00:00"))


def _at_point(value: object) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise ProviderAccountAbsenceCoverageError(
            "at must be exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _event_version(value: object, *, name: str) -> int:
    if type(value) is int and value >= 1:
        return value
    if type(value) is str and value.isdigit() and value == str(int(value)):
        parsed = int(value)
        if parsed >= 1:
            return parsed
    raise ProviderAccountAbsenceCoverageError(f"{name} is non-canonical")


def _require_dispatch_event(
    event: object,
    *,
    aggregate_id: str,
    event_type: str,
    version: int,
) -> dict[str, object]:
    if type(event) is not dict:
        raise ProviderAccountAbsenceCoverageError(
            "durable submission event must be an exact object"
        )
    if (
        event.get("aggregate_type") != "submission_attempt"
        or event.get("aggregate_id") != aggregate_id
        or event.get("event_type") != event_type
        or _event_version(event.get("aggregate_version"), name="aggregate_version")
        != version
    ):
        raise ProviderAccountAbsenceCoverageError(
            "durable submission chronology is not exact"
        )
    payload = event.get("payload")
    if type(payload) is not dict:
        raise ProviderAccountAbsenceCoverageError(
            "durable submission payload must be an exact object"
        )
    if event.get("payload_hash") != payload_digest(payload):
        raise ProviderAccountAbsenceCoverageError(
            "durable submission payload hash mismatch"
        )
    return payload


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class HistoricalUnknownSubmissionBinding:
    """Sealed restart-reconstructable identity of one possible provider send."""

    attempt_id: str
    intent_id: str
    client_order_id: str
    provider_id: str
    account_id: str
    environment: str
    request_hash: str
    submission_scope_hash: str
    prepared_at: str
    sending_at: str
    unknown_at: str
    owner_token: str
    owner_epoch: int
    unknown_reason: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountAbsenceCoverageError(
            "historical UNKNOWN binding must come from canonical submission journal"
        )

    def payload(self) -> dict[str, object]:
        require_historical_unknown_submission_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "attempt_id": self.attempt_id,
            "intent_id": self.intent_id,
            "client_order_id": self.client_order_id,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "request_hash": self.request_hash,
            "submission_scope_hash": self.submission_scope_hash,
            "prepared_at": self.prepared_at,
            "sending_at": self.sending_at,
            "unknown_at": self.unknown_at,
            "owner_token": self.owner_token,
            "owner_epoch": self.owner_epoch,
            "unknown_reason": self.unknown_reason,
        }

    @property
    def content_digest(self) -> str:
        return "historical-unknown-submission:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_historical_unknown_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            weakref.ReferenceType,
            object,
            object,
        ],
    ] = {}
    fields = (
        "attempt_id",
        "intent_id",
        "client_order_id",
        "provider_id",
        "account_id",
        "environment",
        "request_hash",
        "submission_scope_hash",
        "prepared_at",
        "sending_at",
        "unknown_at",
        "owner_token",
        "owner_epoch",
        "unknown_reason",
    )

    def material(value: HistoricalUnknownSubmissionBinding) -> tuple[object, ...]:
        if type(value) is not HistoricalUnknownSubmissionBinding:
            raise ProviderAccountAbsenceCoverageError(
                "exact HistoricalUnknownSubmissionBinding is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: HistoricalUnknownSubmissionBinding,
        store: JournalStore,
        path: object,
        store_identity: object,
    ) -> None:
        prune()
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            weakref.ref(store),
            path,
            store_identity,
        )

    def require(
        value: HistoricalUnknownSubmissionBinding,
    ) -> HistoricalUnknownSubmissionBinding:
        current = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountAbsenceCoverageError(
                "historical UNKNOWN construction authority is unavailable"
            )
        if state[1] != current:
            raise ProviderAccountAbsenceCoverageError(
                "historical UNKNOWN binding changed after journal resolution"
            )
        store = state[2]()
        if store is None:
            raise ProviderAccountAbsenceCoverageError(
                "historical UNKNOWN JournalStore authority is unavailable"
            )
        path, identity = _canonical_journal_authority_snapshot(store)
        if path != state[3] or identity != state[4]:
            raise ProviderAccountAbsenceCoverageError(
                "historical UNKNOWN JournalStore generation changed"
            )
        return value

    return register, require


(
    _register_historical_unknown_submission_authority,
    require_historical_unknown_submission_authority,
) = _install_historical_unknown_authority()
del _install_historical_unknown_authority


def resolve_historical_unknown_submission(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
) -> HistoricalUnknownSubmissionBinding:
    """Resolve only a durable possible-send UNKNOWN, never caller submission fields."""

    path, store_identity = _canonical_journal_authority_snapshot(store)
    normalized_environment = _exact_text(environment, name="environment").upper()
    if normalized_environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ProviderAccountAbsenceCoverageError("environment is unsupported")
    account = _exact_text(account_id, name="account_id")
    attempt = _exact_text(attempt_id, name="attempt_id")
    aggregate_id = submission_attempt_aggregate_id(
        environment=normalized_environment,
        account_id=account,
        attempt_id=attempt,
    )
    events = JournalStore.load_events(store, "submission_attempt", aggregate_id)
    if len(events) != 3:
        raise ProviderAccountAbsenceCoverageError(
            "historical UNKNOWN requires exact Prepared -> Sending -> Unknown chronology"
        )
    prepared, sending, unknown = events
    prepared_payload = _require_dispatch_event(
        prepared,
        aggregate_id=aggregate_id,
        event_type="SubmissionPrepared",
        version=1,
    )
    sending_payload = _require_dispatch_event(
        sending,
        aggregate_id=aggregate_id,
        event_type="SubmissionSending",
        version=2,
    )
    unknown_payload = _require_dispatch_event(
        unknown,
        aggregate_id=aggregate_id,
        event_type="SubmissionUnknown",
        version=3,
    )
    for event in events:
        if event.get("environment") != normalized_environment:
            raise ProviderAccountAbsenceCoverageError(
                "historical UNKNOWN event environment mismatch"
            )

    expected_text = {
        "attempt_id": attempt,
        "environment": normalized_environment,
        "account_id": account,
    }
    for name, expected in expected_text.items():
        if prepared_payload.get(name) != expected:
            raise ProviderAccountAbsenceCoverageError(
                f"historical UNKNOWN Prepared {name} mismatch"
            )

    intent_id = _exact_text(prepared_payload.get("intent_id"), name="intent_id")
    client_order_id = _exact_text(
        prepared_payload.get("client_order_id"),
        name="client_order_id",
    )
    provider_id = _exact_text(
        prepared_payload.get("provider"),
        name="provider",
    ).upper()
    request_hash = _exact_text(
        prepared_payload.get("request_hash"),
        name="request_hash",
    )
    submission_scope_hash = _exact_text(
        prepared_payload.get("submission_scope_hash"),
        name="submission_scope_hash",
    )
    if _SHA256_RE.fullmatch(request_hash) is None:
        raise ProviderAccountAbsenceCoverageError("request_hash is non-canonical")
    if _SHA256_RE.fullmatch(submission_scope_hash) is None:
        raise ProviderAccountAbsenceCoverageError(
            "submission_scope_hash is non-canonical"
        )
    submission_scope = prepared_payload.get("submission_scope")
    if type(submission_scope) is not dict:
        raise ProviderAccountAbsenceCoverageError(
            "durable submission scope must be an exact object"
        )
    expected_scope_hash = "sha256:" + sha256(
        canonical_json(submission_scope).encode("utf-8")
    ).hexdigest()
    if submission_scope_hash != expected_scope_hash:
        raise ProviderAccountAbsenceCoverageError(
            "durable submission scope digest mismatch"
        )

    owner_token = _exact_text(
        prepared_payload.get("owner_token"),
        name="owner_token",
    )
    owner_epoch = prepared_payload.get("owner_epoch")
    if type(owner_epoch) is not int or owner_epoch < 1:
        raise ProviderAccountAbsenceCoverageError("owner_epoch is non-canonical")
    if (
        sending_payload.get("client_order_id") != client_order_id
        or sending_payload.get("owner_token") != owner_token
        or sending_payload.get("owner_epoch") != owner_epoch
        or unknown_payload.get("client_order_id") != client_order_id
    ):
        raise ProviderAccountAbsenceCoverageError(
            "historical UNKNOWN send identity changes across chronology"
        )

    prepared_at = _utc_text(
        prepared_payload.get("prepared_at"),
        name="prepared_at",
    )
    sending_at = _utc_text(sending.get("observed_at"), name="sending_at")
    unknown_at = _utc_text(unknown.get("observed_at"), name="unknown_at")
    if not (
        _point(prepared_at, name="prepared_at")
        <= _point(sending_at, name="sending_at")
        <= _point(unknown_at, name="unknown_at")
    ):
        raise ProviderAccountAbsenceCoverageError(
            "historical UNKNOWN chronology moved backwards"
        )
    unknown_reason = _exact_text(
        unknown_payload.get("reason"),
        name="unknown_reason",
    )

    value = object.__new__(HistoricalUnknownSubmissionBinding)
    material = {
        "attempt_id": attempt,
        "intent_id": intent_id,
        "client_order_id": client_order_id,
        "provider_id": provider_id,
        "account_id": account,
        "environment": normalized_environment,
        "request_hash": request_hash,
        "submission_scope_hash": submission_scope_hash,
        "prepared_at": prepared_at,
        "sending_at": sending_at,
        "unknown_at": unknown_at,
        "owner_token": owner_token,
        "owner_epoch": owner_epoch,
        "unknown_reason": unknown_reason,
    }
    for name, item in material.items():
        object.__setattr__(value, name, item)
    _register_historical_unknown_submission_authority(
        value,
        store,
        path,
        store_identity,
    )
    return value


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderAccountSurfaceCoverage:
    """Sealed exact query-window coverage for one required absence surface."""

    provider_scope_digest: str
    account_id: str
    qualification_id: str
    absence_semantics_digest: str
    page_chain_digest: str
    historical_submission_digest: str
    surface: str
    endpoint: str
    data_entitlement: str
    query_scope_rule_id: str
    pagination_rule_id: str
    retention_rule_id: str
    consistency_horizon_rule_id: str
    search_binding: str
    coverage_start: str
    coverage_end: str
    root_query_json: str
    page_count: int
    latest_provider_observed_at: str

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderAccountAbsenceCoverageError(
            "provider surface coverage must come from canonical issuer"
        )

    @property
    def root_query(self) -> dict[str, str]:
        require_provider_account_surface_coverage_authority(self)
        value = json.loads(self.root_query_json)
        if type(value) is not dict or any(
            type(key) is not str or type(item) is not str
            for key, item in value.items()
        ):
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage query is non-canonical"
            )
        return dict(value)

    def payload(self) -> dict[str, object]:
        require_provider_account_surface_coverage_authority(self)
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_scope_digest": self.provider_scope_digest,
            "account_id": self.account_id,
            "qualification_id": self.qualification_id,
            "absence_semantics_digest": self.absence_semantics_digest,
            "page_chain_digest": self.page_chain_digest,
            "historical_submission_digest": self.historical_submission_digest,
            "surface": self.surface,
            "endpoint": self.endpoint,
            "data_entitlement": self.data_entitlement,
            "query_scope_rule_id": self.query_scope_rule_id,
            "pagination_rule_id": self.pagination_rule_id,
            "retention_rule_id": self.retention_rule_id,
            "consistency_horizon_rule_id": self.consistency_horizon_rule_id,
            "search_binding": self.search_binding,
            "coverage_start": self.coverage_start,
            "coverage_end": self.coverage_end,
            "root_query": json.loads(self.root_query_json),
            "page_count": self.page_count,
            "latest_provider_observed_at": self.latest_provider_observed_at,
        }

    @property
    def content_digest(self) -> str:
        return "provider-account-surface-coverage:sha256:" + sha256(
            canonical_json(self.payload()).encode("utf-8")
        ).hexdigest()


def _install_surface_coverage_authority():
    states: dict[
        int,
        tuple[
            weakref.ReferenceType,
            tuple[object, ...],
            weakref.ReferenceType,
            weakref.ReferenceType,
            DurableProviderQualificationRegistry,
        ],
    ] = {}
    fields = (
        "provider_scope_digest",
        "account_id",
        "qualification_id",
        "absence_semantics_digest",
        "page_chain_digest",
        "historical_submission_digest",
        "surface",
        "endpoint",
        "data_entitlement",
        "query_scope_rule_id",
        "pagination_rule_id",
        "retention_rule_id",
        "consistency_horizon_rule_id",
        "search_binding",
        "coverage_start",
        "coverage_end",
        "root_query_json",
        "page_count",
        "latest_provider_observed_at",
    )

    def material(value: ProviderAccountSurfaceCoverage) -> tuple[object, ...]:
        if type(value) is not ProviderAccountSurfaceCoverage:
            raise ProviderAccountAbsenceCoverageError(
                "exact ProviderAccountSurfaceCoverage is required"
            )
        return tuple(getattr(value, field) for field in fields)

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def register(
        value: ProviderAccountSurfaceCoverage,
        page_chain: ProviderAccountPageChain,
        historical_submission: HistoricalUnknownSubmissionBinding,
        qualification_registry: DurableProviderQualificationRegistry,
    ) -> None:
        if type(qualification_registry) is not DurableProviderQualificationRegistry:
            raise TypeError(
                "qualification_registry must be exact DurableProviderQualificationRegistry"
            )
        prune()
        states[id(value)] = (
            weakref.ref(value),
            material(value),
            weakref.ref(page_chain),
            weakref.ref(historical_submission),
            qualification_registry,
        )

    def require(value: ProviderAccountSurfaceCoverage) -> ProviderAccountSurfaceCoverage:
        current = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage construction authority is unavailable"
            )
        if state[1] != current:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage changed after issuance"
            )
        page_chain = state[2]()
        historical = state[3]()
        if page_chain is None or historical is None:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage source authority is unavailable"
            )
        try:
            require_provider_account_page_chain_authority(page_chain)
            require_historical_unknown_submission_authority(historical)
        except (
            ProviderAccountPageChainError,
            ProviderAccountAbsenceCoverageError,
        ) as error:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage source authority changed"
            ) from error
        return value

    def require_current(
        value: ProviderAccountSurfaceCoverage,
        *,
        at: datetime,
    ) -> ProviderAccountSurfaceCoverage:
        require(value)
        state = states.get(id(value))
        if state is None:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage construction authority is unavailable"
            )
        page_chain = state[2]()
        historical = state[3]()
        qualification_registry = state[4]
        if page_chain is None or historical is None:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage source authority is unavailable"
            )
        try:
            require_current_provider_account_page_chain_authority(
                page_chain,
                at=at,
            )
            require_historical_unknown_submission_authority(historical)
        except (
            ProviderAccountPageChainError,
            ProviderAccountAbsenceCoverageError,
        ) as error:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage is not exact current authority"
            ) from error
        if (
            value.page_chain_digest != page_chain.content_digest
            or value.historical_submission_digest != historical.content_digest
            or value.provider_scope_digest != page_chain.provider_scope_digest
            or value.account_id != page_chain.account_id
            or value.qualification_id != page_chain.qualification_id
        ):
            raise ProviderAccountAbsenceCoverageError(
                "provider surface coverage no longer matches current source authority"
            )
        return value

    return register, require, require_current


(
    _register_provider_account_surface_coverage_authority,
    require_provider_account_surface_coverage_authority,
    require_current_provider_account_surface_coverage_authority,
) = _install_surface_coverage_authority()
del _install_surface_coverage_authority


def _root_query(page_chain: ProviderAccountPageChain) -> dict[str, str]:
    raw = json.loads(page_chain.root_query_json)
    if type(raw) is not dict or any(
        type(key) is not str or type(value) is not str
        for key, value in raw.items()
    ):
        raise ProviderAccountAbsenceCoverageError(
            "provider page-chain root query is non-canonical"
        )
    return dict(raw)


def _milliseconds(value: object, *, name: str) -> int:
    if type(value) is not str or not value or not value.isdigit():
        raise ProviderAccountAbsenceCoverageError(
            f"{name} must be an exact non-negative millisecond string"
        )
    if len(value) > 1 and value.startswith("0"):
        raise ProviderAccountAbsenceCoverageError(f"{name} is non-canonical")
    return int(value)


def _datetime_from_milliseconds(value: int) -> datetime:
    return datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
        milliseconds=value
    )


def _milliseconds_from_datetime(value: datetime) -> int:
    point = value.astimezone(timezone.utc)
    delta = point - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86_400_000
        + delta.seconds * 1000
        + delta.microseconds // 1000
    )


def _utc_from_milliseconds(value: int) -> str:
    point = _datetime_from_milliseconds(value)
    return point.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_query_shape(
    *,
    surface: str,
    query: dict[str, str],
    historical_submission: HistoricalUnknownSubmissionBinding,
) -> tuple[str, int | None, int | None]:
    if query.get("category") != "spot":
        raise ProviderAccountAbsenceCoverageError(
            "qualified Bybit SPOT coverage requires exact category=spot"
        )
    limit = query.get("limit")
    if limit is not None:
        if not limit.isdigit() or int(limit) < 1:
            raise ProviderAccountAbsenceCoverageError(
                "provider coverage limit is non-canonical"
            )
        maximum = 100 if surface == "EXECUTIONS" else 50
        if int(limit) > maximum:
            raise ProviderAccountAbsenceCoverageError(
                "provider coverage limit exceeds qualified endpoint maximum"
            )

    if surface == "OPEN_ORDERS":
        allowed = {"category", "orderLinkId", "limit"}
        if set(query) - allowed:
            raise ProviderAccountAbsenceCoverageError(
                "open-order coverage query contains an unsafe narrowing filter"
            )
        if query.get("orderLinkId") != historical_submission.client_order_id:
            raise ProviderAccountAbsenceCoverageError(
                "open-order coverage did not search exact historical client order id"
            )
        return "EXACT_CLIENT_ORDER_ID_POINT_LOOKUP", None, None

    if surface in {"ORDER_HISTORY", "EXECUTIONS"}:
        allowed = {"category", "orderLinkId", "startTime", "endTime", "limit"}
        if set(query) - allowed:
            raise ProviderAccountAbsenceCoverageError(
                "historical provider coverage query contains an unsafe narrowing filter"
            )
        if query.get("orderLinkId") != historical_submission.client_order_id:
            raise ProviderAccountAbsenceCoverageError(
                "historical provider coverage did not search exact client order id"
            )
        search_binding = "EXACT_CLIENT_ORDER_ID_WINDOW"
    elif surface == "ACTIVITIES":
        allowed = {"accountType", "category", "startTime", "endTime", "limit"}
        if set(query) - allowed:
            raise ProviderAccountAbsenceCoverageError(
                "activity coverage query contains an unsafe narrowing filter"
            )
        if "accountType" in query and query["accountType"] != "UNIFIED":
            raise ProviderAccountAbsenceCoverageError(
                "activity coverage accountType is not qualified UNIFIED scope"
            )
        search_binding = "ACCOUNT_WINDOW_COMPANION"
    else:
        raise ProviderAccountAbsenceCoverageError(
            "provider surface has no executable coverage implementation"
        )

    if "startTime" not in query or "endTime" not in query:
        raise ProviderAccountAbsenceCoverageError(
            "historical provider coverage requires explicit startTime and endTime"
        )
    start_ms = _milliseconds(query["startTime"], name="startTime")
    end_ms = _milliseconds(query["endTime"], name="endTime")
    if end_ms < start_ms or end_ms - start_ms > _SEVEN_DAYS_MS:
        raise ProviderAccountAbsenceCoverageError(
            "historical provider query window is invalid or exceeds seven days"
        )
    return search_binding, start_ms, end_ms


def issue_provider_account_surface_coverage(
    *,
    absence_semantics: QualifiedProviderAccountAbsenceSemantics,
    page_chain: ProviderAccountPageChain,
    historical_submission: HistoricalUnknownSubmissionBinding,
    qualification_registry: DurableProviderQualificationRegistry,
    at: datetime,
) -> ProviderAccountSurfaceCoverage:
    """Issue exact searched-window coverage without caller completeness booleans."""

    if type(absence_semantics) is not QualifiedProviderAccountAbsenceSemantics:
        raise TypeError(
            "absence_semantics must be exact QualifiedProviderAccountAbsenceSemantics"
        )
    if type(page_chain) is not ProviderAccountPageChain:
        raise TypeError("page_chain must be exact ProviderAccountPageChain")
    if type(historical_submission) is not HistoricalUnknownSubmissionBinding:
        raise TypeError(
            "historical_submission must be exact HistoricalUnknownSubmissionBinding"
        )
    if type(qualification_registry) is not DurableProviderQualificationRegistry:
        raise TypeError(
            "qualification_registry must be exact DurableProviderQualificationRegistry"
        )
    point = _at_point(at)
    try:
        require_current_provider_account_page_chain_authority(
            page_chain,
            at=point,
        )
        require_historical_unknown_submission_authority(historical_submission)
    except (
        ProviderAccountPageChainError,
        ProviderAccountAbsenceCoverageError,
    ) as error:
        raise ProviderAccountAbsenceCoverageError(
            "provider coverage source authority is unavailable"
        ) from error

    if historical_submission.provider_id != _SUPPORTED_PROVIDER:
        raise ProviderAccountAbsenceCoverageError(
            "provider coverage historical submission is not BYBIT"
        )
    if historical_submission.account_id != page_chain.account_id:
        raise ProviderAccountAbsenceCoverageError(
            "provider coverage account differs from historical UNKNOWN"
        )
    if (
        page_chain.provider_scope_digest != absence_semantics.provider_scope_digest
        or page_chain.qualification_id != absence_semantics.qualification_id
        or page_chain.absence_semantics_digest != absence_semantics.content_digest
    ):
        raise ProviderAccountAbsenceCoverageError(
            "provider page chain and absence semantics do not share exact Q scope"
        )

    semantic_rules = [
        rule
        for rule in absence_semantics.rules
        if rule.get("surface") == page_chain.surface
    ]
    if len(semantic_rules) != 1:
        raise ProviderAccountAbsenceCoverageError(
            "provider surface lacks one exact current absence rule"
        )
    raw_rule = semantic_rules[0]
    try:
        rule = require_provider_account_absence_rule(
            absence_semantics,
            surface=page_chain.surface,
            endpoint=page_chain.endpoint,
            data_entitlement=page_chain.data_entitlement,
            qualification_registry=qualification_registry,
            at=point,
        )
    except ProviderAccountAbsenceSemanticsError as error:
        raise ProviderAccountAbsenceCoverageError(
            "provider absence semantics are not exact current authority"
        ) from error
    if rule != raw_rule:
        raise ProviderAccountAbsenceCoverageError(
            "provider absence rule changed during coverage issuance"
        )

    qualification = qualification_registry.qualification(
        page_chain.qualification_id
    )
    provider_scope = qualification.scope.provider_scope
    if (
        provider_scope.content_digest != page_chain.provider_scope_digest
        or provider_scope.provider_id != _SUPPORTED_PROVIDER
        or provider_scope.runtime_environment != historical_submission.environment
    ):
        raise ProviderAccountAbsenceCoverageError(
            "historical UNKNOWN does not share exact qualified provider runtime scope"
        )

    surface = page_chain.surface
    expected_retention = _SUPPORTED_RETENTION_RULE_BY_SURFACE.get(surface)
    if (
        page_chain.query_scope_rule_id != _SUPPORTED_QUERY_SCOPE_RULE
        or page_chain.pagination_rule_id != _SUPPORTED_PAGINATION_RULE
        or rule["query_scope_rule_id"] != _SUPPORTED_QUERY_SCOPE_RULE
        or rule["pagination_rule_id"] != _SUPPORTED_PAGINATION_RULE
        or rule["consistency_horizon_rule_id"] != _SUPPORTED_HORIZON_RULE
        or expected_retention is None
        or rule["retention_rule_id"] != expected_retention
    ):
        raise ProviderAccountAbsenceCoverageError(
            "provider surface rule has no executable coverage implementation"
        )

    query = _root_query(page_chain)
    search_binding, start_ms, end_ms = _validate_query_shape(
        surface=surface,
        query=query,
        historical_submission=historical_submission,
    )
    send_point = _point(
        historical_submission.sending_at,
        name="historical_submission.sending_at",
    )
    send_ms = _milliseconds_from_datetime(send_point)

    pages = page_chain.pages
    if not pages:
        raise ProviderAccountAbsenceCoverageError(
            "provider surface coverage requires at least one terminal page"
        )
    page_points: list[datetime] = []
    for page in pages:
        observed_at = page.get("observed_at")
        observed = _point(observed_at, name="page.observed_at")
        if observed > point:
            raise ProviderAccountAbsenceCoverageError(
                "provider surface page observation is in the future"
            )
        page_points.append(observed)
    latest_observed = max(page_points)

    if surface == "OPEN_ORDERS":
        if latest_observed < send_point:
            raise ProviderAccountAbsenceCoverageError(
                "open-order lookup predates historical possible send"
            )
        coverage_start = historical_submission.sending_at
        coverage_end = latest_observed.isoformat().replace("+00:00", "Z")
    else:
        assert start_ms is not None and end_ms is not None
        if not (start_ms <= send_ms <= end_ms):
            raise ProviderAccountAbsenceCoverageError(
                "historical possible send is outside provider query window"
            )
        query_end = _datetime_from_milliseconds(end_ms)
        if any(observed < query_end for observed in page_points):
            raise ProviderAccountAbsenceCoverageError(
                "provider page observation precedes declared query end"
            )
        if query_end > point:
            raise ProviderAccountAbsenceCoverageError(
                "provider query end is in the future"
            )
        if surface == "ORDER_HISTORY" and any(
            observed - send_point > _ORDER_HISTORY_FULL_STATUS_LIMIT
            for observed in page_points
        ):
            raise ProviderAccountAbsenceCoverageError(
                "order-history full-status retention window exceeded 24 hours"
            )
        coverage_start = _utc_from_milliseconds(start_ms)
        coverage_end = _utc_from_milliseconds(end_ms)

    value = object.__new__(ProviderAccountSurfaceCoverage)
    material = {
        "provider_scope_digest": page_chain.provider_scope_digest,
        "account_id": page_chain.account_id,
        "qualification_id": page_chain.qualification_id,
        "absence_semantics_digest": absence_semantics.content_digest,
        "page_chain_digest": page_chain.content_digest,
        "historical_submission_digest": historical_submission.content_digest,
        "surface": surface,
        "endpoint": page_chain.endpoint,
        "data_entitlement": page_chain.data_entitlement,
        "query_scope_rule_id": rule["query_scope_rule_id"],
        "pagination_rule_id": rule["pagination_rule_id"],
        "retention_rule_id": rule["retention_rule_id"],
        "consistency_horizon_rule_id": rule["consistency_horizon_rule_id"],
        "search_binding": search_binding,
        "coverage_start": coverage_start,
        "coverage_end": coverage_end,
        "root_query_json": canonical_json(query),
        "page_count": len(pages),
        "latest_provider_observed_at": latest_observed.isoformat().replace(
            "+00:00", "Z"
        ),
    }
    for name, item in material.items():
        object.__setattr__(value, name, item)
    _register_provider_account_surface_coverage_authority(
        value,
        page_chain,
        historical_submission,
        qualification_registry,
    )
    return value
