"""Source-bound corporate actions and canonical accounting integration.

``CorporateActionBook`` remains the pure deterministic transition engine.  This
module adds the missing financial admission seam: a normalized corporate event
must be proven field-for-field against one authenticated provider response before
it may create a canonical accounting transaction.

Provider-specific adapters supply only immutable JSON paths into the already
authenticated response.  They do not supply values or an authority callback.
The response bytes, query scope, resolved source fields, normalized event,
instrument version, lifecycle dates, and resulting accounting identity remain
bound by content digests.

This slice intentionally supports durable accounting only for SPLIT and
CASH_DIVIDEND.  Merger, delist, symbol-change, tax/withholding, cash-in-lieu,
spinoff, rights/tender, and settlement release remain fail-closed until their
canonical accounting/reconciliation semantics exist.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping
import re

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    ScopedEconomicBook,
    book_equity_split_adjustment,
    posting,
    validate_transaction,
)
from .corporate_actions import CorporateActionBook, CorporateEvent, EquityState, Transition
from .exact_decimal import (
    ExactDecimalError,
    exact_multiply,
    exact_subtract,
    parse_bounded_exact_decimal,
)
from .instruments import InstrumentRegistry, InstrumentVersion
from .persistence import payload_digest
from .provider_core import (
    AuthenticatedReadQueryBinding,
    ProviderResponseObservation,
    Surface,
)


class CorporateActionAdmissionError(ValueError):
    """Raised before accounting mutation when provenance or lifecycle is unsafe."""


_ACCEPTED_TOKEN = object()
_SUPPORTED_DURABLE_KINDS = frozenset({"SPLIT", "CASH_DIVIDEND"})
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_REF = re.compile(r"^provider-read:sha256:[0-9a-f]{64}$")
_PATH_SEGMENT_TYPES = frozenset({str, int})


def _canonical_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise CorporateActionAdmissionError(f"{name} must be canonical non-empty text")
    return value


def _utc_datetime(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise CorporateActionAdmissionError(
            f"{name} must be an exact timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime, *, name: str) -> str:
    return _utc_datetime(value, name=name).isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, *, name: str) -> datetime:
    text = _canonical_text(value, name=name)
    if not text.endswith("Z"):
        raise CorporateActionAdmissionError(f"{name} must be canonical UTC text")
    try:
        point = datetime.fromisoformat(text[:-1] + "+00:00")
    except ValueError as error:
        raise CorporateActionAdmissionError(f"{name} must be canonical UTC text") from error
    canonical = point.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise CorporateActionAdmissionError(f"{name} must be canonical UTC text")
    return point.astimezone(timezone.utc)


def _parse_date_text(value: object, *, name: str) -> date:
    text = _canonical_text(value, name=name)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as error:
        raise CorporateActionAdmissionError(
            f"{name} must be canonical ISO date text"
        ) from error
    if parsed.isoformat() != text:
        raise CorporateActionAdmissionError(f"{name} must be canonical ISO date text")
    return parsed


def _instrument_ref(instrument: InstrumentVersion) -> str:
    if type(instrument) is not InstrumentVersion:
        raise TypeError("instrument_version must be an exact InstrumentVersion")
    return f"{instrument.instrument_id}@{instrument.version}"


def _require_registered_instrument(
    instrument: InstrumentVersion,
    registry: InstrumentRegistry,
) -> None:
    if type(registry) is not InstrumentRegistry:
        raise TypeError("registry must be an exact InstrumentRegistry")
    matches = tuple(
        item
        for item in registry.versions(instrument.instrument_id)
        if item.version == instrument.version
    )
    if len(matches) != 1 or matches[0] != instrument:
        raise CorporateActionAdmissionError(
            "instrument_version is not the exact registered instrument version"
        )


def _path(value: object, *, name: str) -> tuple[str | int, ...]:
    if type(value) is not tuple or not value:
        raise CorporateActionAdmissionError(f"{name} must be a non-empty exact tuple")
    normalized: list[str | int] = []
    for segment in value:
        if type(segment) not in _PATH_SEGMENT_TYPES:
            raise CorporateActionAdmissionError(
                f"{name} path segments must be exact text or non-negative integers"
            )
        if type(segment) is int:
            if segment < 0:
                raise CorporateActionAdmissionError(
                    f"{name} integer path segments must be non-negative"
                )
            normalized.append(segment)
        else:
            normalized.append(_canonical_text(segment, name=f"{name} segment"))
    return tuple(normalized)


def _optional_path(value: object, *, name: str) -> tuple[str | int, ...] | None:
    return None if value is None else _path(value, name=name)


@dataclass(frozen=True)
class CorporateActionSourceLocator:
    """Immutable JSON paths proving where normalized event fields came from."""

    event_id: tuple[str | int, ...]
    source_revision: tuple[str | int, ...]
    kind: tuple[str | int, ...]
    effective_at: tuple[str | int, ...]
    source_sequence: tuple[str | int, ...]
    payload: Mapping[str, tuple[str | int, ...]]
    announcement_at: tuple[str | int, ...] | None = None
    record_date: tuple[str | int, ...] | None = None
    ex_date: tuple[str | int, ...] | None = None
    pay_date: tuple[str | int, ...] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _path(self.event_id, name="event_id"))
        object.__setattr__(
            self,
            "source_revision",
            _path(self.source_revision, name="source_revision"),
        )
        object.__setattr__(self, "kind", _path(self.kind, name="kind"))
        object.__setattr__(
            self, "effective_at", _path(self.effective_at, name="effective_at")
        )
        object.__setattr__(
            self,
            "source_sequence",
            _path(self.source_sequence, name="source_sequence"),
        )
        if type(self.payload) is not dict or not self.payload:
            raise CorporateActionAdmissionError(
                "payload locator must be a non-empty exact object"
            )
        normalized_payload: dict[str, tuple[str | int, ...]] = {}
        for key, path in self.payload.items():
            normalized_key = _canonical_text(key, name="payload locator key")
            if normalized_key in normalized_payload:
                raise CorporateActionAdmissionError(
                    "payload locator keys must be unique"
                )
            normalized_payload[normalized_key] = _path(
                path, name=f"payload locator {normalized_key}"
            )
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(dict(sorted(normalized_payload.items()))),
        )
        for field_name in ("announcement_at", "record_date", "ex_date", "pay_date"):
            object.__setattr__(
                self,
                field_name,
                _optional_path(getattr(self, field_name), name=field_name),
            )

    def material(self) -> dict[str, object]:
        return {
            "event_id": list(self.event_id),
            "source_revision": list(self.source_revision),
            "kind": list(self.kind),
            "effective_at": list(self.effective_at),
            "source_sequence": list(self.source_sequence),
            "payload": {
                key: list(path) for key, path in sorted(self.payload.items())
            },
            "announcement_at": (
                None if self.announcement_at is None else list(self.announcement_at)
            ),
            "record_date": (
                None if self.record_date is None else list(self.record_date)
            ),
            "ex_date": None if self.ex_date is None else list(self.ex_date),
            "pay_date": None if self.pay_date is None else list(self.pay_date),
        }


def _resolve_path(root: object, path: tuple[str | int, ...], *, name: str) -> object:
    current = root
    for segment in path:
        if type(segment) is str:
            if not isinstance(current, Mapping) or segment not in current:
                raise CorporateActionAdmissionError(
                    f"{name} source path is absent from authenticated provider payload"
                )
            current = current[segment]
        else:
            if type(current) is not tuple or segment >= len(current):
                raise CorporateActionAdmissionError(
                    f"{name} source path is absent from authenticated provider payload"
                )
            current = current[segment]
    return current


def _source_scalar_text(value: object, *, name: str) -> str:
    if type(value) is str:
        return _canonical_text(value, name=name)
    if type(value) is int:
        return str(value)
    if type(value) is Decimal:
        if not value.is_finite():
            raise CorporateActionAdmissionError(f"{name} must be finite")
        return str(value)
    raise CorporateActionAdmissionError(
        f"{name} must resolve to exact text, integer, or Decimal provider data"
    )


def _source_kind(value: object) -> str:
    text = _source_scalar_text(value, name="kind")
    return text.upper().replace("-", "_").replace(" ", "_")


def _event_material(event: CorporateEvent) -> dict[str, object]:
    if type(event) is not CorporateEvent:
        raise TypeError("event must be an exact CorporateEvent")
    if type(event.effective_date) is not date:
        raise CorporateActionAdmissionError(
            "corporate event effective_date must be an exact date"
        )
    if type(event.source_sequence) is not int or event.source_sequence < 0:
        raise CorporateActionAdmissionError(
            "corporate event source_sequence must be a non-negative exact integer"
        )
    if event.effective_at is None:
        raise CorporateActionAdmissionError(
            "corporate event requires exact effective_at"
        )
    if type(event.payload) is not dict:
        raise CorporateActionAdmissionError(
            "corporate event payload must remain an exact normalized object"
        )
    payload: dict[str, str] = {}
    for key, value in event.payload.items():
        if type(key) is not str or type(value) is not str:
            raise CorporateActionAdmissionError(
                "corporate event payload must remain normalized exact text"
            )
        normalized_key = _canonical_text(key, name="payload key")
        if normalized_key in payload:
            raise CorporateActionAdmissionError(
                "corporate event payload keys must remain unique"
            )
        payload[normalized_key] = value
    return {
        "event_id": _canonical_text(event.event_id, name="event_id"),
        "instrument_id": _canonical_text(event.instrument_id, name="instrument_id"),
        "instrument_version": event.instrument_version,
        "kind": _canonical_text(event.kind, name="kind"),
        "effective_date": event.effective_date.isoformat(),
        "effective_at": _utc_text(event.effective_at, name="effective_at"),
        "source_revision": _canonical_text(
            event.source_revision, name="source_revision"
        ),
        "source_sequence": event.source_sequence,
        "payload": dict(sorted(payload.items())),
    }


def _scope_event_identity(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    instrument_version_ref: str,
    event_id: str,
) -> str:
    return payload_digest(
        {
            "schema_version": 1,
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
            "instrument_version": instrument_version_ref,
            "external_event_id": event_id,
        }
    )


def _semantic_event_digest(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    instrument_version_ref: str,
    event_material: Mapping[str, object],
) -> str:
    return payload_digest(
        {
            "schema_version": 1,
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
            "instrument_version": instrument_version_ref,
            "event": dict(event_material),
        }
    )


def _provenance_digest(
    *,
    semantic_digest: str,
    locator_digest: str,
    query_digest: str,
    response_sha256: str,
    evidence_ref: str,
    observed_at: str,
    announcement_at: str | None,
    record_date: date | None,
    ex_date: date | None,
    pay_date: date | None,
) -> str:
    return payload_digest(
        {
            "schema_version": 1,
            "semantic_digest": semantic_digest,
            "source_locator_digest": locator_digest,
            "query_digest": query_digest,
            "response_sha256": response_sha256,
            "evidence_ref": evidence_ref,
            "observed_at": observed_at,
            "announcement_at": announcement_at,
            "record_date": None if record_date is None else record_date.isoformat(),
            "ex_date": None if ex_date is None else ex_date.isoformat(),
            "pay_date": None if pay_date is None else pay_date.isoformat(),
        }
    )


@dataclass(frozen=True)
class AcceptedCorporateAction:
    """One provider-payload-backed corporate event admitted for financial use."""

    event: CorporateEvent
    provider_id: str
    account_id: str
    environment: str
    instrument_version_ref: str
    query_digest: str
    response_sha256: str
    evidence_ref: str
    observed_at: str
    source_locator_digest: str
    event_digest: str
    semantic_digest: str
    provenance_digest: str
    announcement_at: str | None
    record_date: date | None
    ex_date: date | None
    pay_date: date | None
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _ACCEPTED_TOKEN:
            raise CorporateActionAdmissionError(
                "accepted corporate actions must come from authenticated provider evidence"
            )


@dataclass(frozen=True)
class CorporateActionAccountingResult:
    accepted: AcceptedCorporateAction
    transition: Transition | None
    transaction: JournalTransaction
    appended: bool


def _match_event_to_source(
    *,
    observation: ProviderResponseObservation,
    event: CorporateEvent,
    locator: CorporateActionSourceLocator,
) -> tuple[str | None, date | None, date | None, date | None]:
    payload = observation.payload
    event_material = _event_material(event)

    source_event_id = _source_scalar_text(
        _resolve_path(payload, locator.event_id, name="event_id"),
        name="event_id",
    )
    if source_event_id != event.event_id:
        raise CorporateActionAdmissionError(
            "normalized corporate event_id does not match authenticated provider payload"
        )

    source_revision = _source_scalar_text(
        _resolve_path(payload, locator.source_revision, name="source_revision"),
        name="source_revision",
    )
    if source_revision != event.source_revision:
        raise CorporateActionAdmissionError(
            "normalized source_revision does not match authenticated provider payload"
        )

    if _source_kind(_resolve_path(payload, locator.kind, name="kind")) != event.kind:
        raise CorporateActionAdmissionError(
            "normalized corporate-action kind does not match authenticated provider payload"
        )

    source_effective_at = _source_scalar_text(
        _resolve_path(payload, locator.effective_at, name="effective_at"),
        name="effective_at",
    )
    source_effective_point = _parse_utc_text(
        source_effective_at, name="effective_at"
    )
    if _utc_text(event.effective_at, name="effective_at") != source_effective_at:
        raise CorporateActionAdmissionError(
            "normalized effective_at does not match authenticated provider payload"
        )
    if event.effective_date != source_effective_point.date():
        raise CorporateActionAdmissionError(
            "corporate event effective_date conflicts with provider effective_at"
        )

    source_sequence = _resolve_path(
        payload, locator.source_sequence, name="source_sequence"
    )
    if type(source_sequence) is not int or source_sequence != event.source_sequence:
        raise CorporateActionAdmissionError(
            "normalized source_sequence does not match authenticated provider payload"
        )

    if set(locator.payload) != set(event.payload):
        raise CorporateActionAdmissionError(
            "source locator must cover every normalized corporate-event payload field exactly"
        )
    for key, path in locator.payload.items():
        source_value = _source_scalar_text(
            _resolve_path(payload, path, name=f"payload.{key}"),
            name=f"payload.{key}",
        )
        if source_value != event.payload[key]:
            raise CorporateActionAdmissionError(
                f"normalized payload.{key} does not match authenticated provider payload"
            )

    def source_datetime(
        path: tuple[str | int, ...] | None, name: str
    ) -> str | None:
        if path is None:
            return None
        value = _source_scalar_text(
            _resolve_path(payload, path, name=name),
            name=name,
        )
        return _parse_utc_text(value, name=name).isoformat().replace("+00:00", "Z")

    def source_date(
        path: tuple[str | int, ...] | None, name: str
    ) -> date | None:
        if path is None:
            return None
        value = _source_scalar_text(
            _resolve_path(payload, path, name=name),
            name=name,
        )
        return _parse_date_text(value, name=name)

    announcement_at = source_datetime(locator.announcement_at, "announcement_at")
    record_date = source_date(locator.record_date, "record_date")
    ex_date = source_date(locator.ex_date, "ex_date")
    pay_date = source_date(locator.pay_date, "pay_date")

    if event.kind == "CASH_DIVIDEND":
        if ex_date is None or pay_date is None:
            raise CorporateActionAdmissionError(
                "cash dividend requires source-bound ex_date and pay_date"
            )
        if ex_date != event.effective_date:
            raise CorporateActionAdmissionError(
                "cash-dividend ex_date must match event effective_date"
            )
        if pay_date < ex_date:
            raise CorporateActionAdmissionError(
                "cash-dividend pay_date cannot precede ex_date"
            )

    if payload_digest(event_material) != payload_digest(_event_material(event)):
        raise CorporateActionAdmissionError(
            "corporate event changed during provenance admission"
        )
    return announcement_at, record_date, ex_date, pay_date


def accept_corporate_action_observation(
    *,
    observation: ProviderResponseObservation,
    event: CorporateEvent,
    source_locator: CorporateActionSourceLocator,
    instrument_version: InstrumentVersion,
    registry: InstrumentRegistry,
) -> AcceptedCorporateAction:
    """Admit one normalized event only when exact provider bytes prove its fields."""

    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be an exact ProviderResponseObservation")
    if type(observation.query_binding) is not AuthenticatedReadQueryBinding:
        raise TypeError(
            "observation query_binding must be an exact AuthenticatedReadQueryBinding"
        )
    if type(event) is not CorporateEvent:
        raise TypeError("event must be an exact CorporateEvent")
    if type(source_locator) is not CorporateActionSourceLocator:
        raise TypeError("source_locator must be an exact CorporateActionSourceLocator")
    if type(instrument_version) is not InstrumentVersion:
        raise TypeError("instrument_version must be an exact InstrumentVersion")

    _require_registered_instrument(instrument_version, registry)
    if observation.query_binding.surface is not Surface.ACTIVITIES:
        raise CorporateActionAdmissionError(
            "corporate-action financial provenance requires an authenticated ACTIVITIES observation"
        )

    provider = _canonical_text(observation.provider_id, name="provider_id").upper()
    expected_provider = _canonical_text(
        instrument_version.provider_id, name="instrument provider_id"
    ).upper()
    if provider != expected_provider:
        raise CorporateActionAdmissionError(
            "provider observation does not match instrument provider identity"
        )

    instrument_ref = _instrument_ref(instrument_version)
    if observation.query_binding.instrument_version != instrument_ref:
        raise CorporateActionAdmissionError(
            "provider observation does not match exact instrument version"
        )
    if (
        event.instrument_id != instrument_version.instrument_id
        or event.instrument_version != instrument_version.version
    ):
        raise CorporateActionAdmissionError(
            "corporate event does not match exact instrument version"
        )

    announcement_at, record_date, ex_date, pay_date = _match_event_to_source(
        observation=observation,
        event=event,
        locator=source_locator,
    )

    observed_at = _canonical_text(observation.observed_at, name="observed_at")
    observed_point = _parse_utc_text(observed_at, name="observed_at")
    if announcement_at is not None:
        announcement_point = _parse_utc_text(
            announcement_at, name="announcement_at"
        )
        if announcement_point > observed_point:
            raise CorporateActionAdmissionError(
                "announcement_at cannot be after the authenticated observation"
            )

    event_material = _event_material(event)
    event_digest = payload_digest(event_material)
    account = _canonical_text(observation.account_id, name="account_id")
    environment = _canonical_text(
        observation.environment, name="environment"
    ).upper()
    semantic_digest = _semantic_event_digest(
        provider_id=provider,
        account_id=account,
        environment=environment,
        instrument_version_ref=instrument_ref,
        event_material=event_material,
    )
    locator_digest = payload_digest(source_locator.material())
    provenance_digest = _provenance_digest(
        semantic_digest=semantic_digest,
        locator_digest=locator_digest,
        query_digest=observation.query_binding.query_digest,
        response_sha256=observation.response_sha256,
        evidence_ref=observation.evidence_ref,
        observed_at=observed_at,
        announcement_at=announcement_at,
        record_date=record_date,
        ex_date=ex_date,
        pay_date=pay_date,
    )

    return AcceptedCorporateAction(
        event=event,
        provider_id=provider,
        account_id=account,
        environment=environment,
        instrument_version_ref=instrument_ref,
        query_digest=observation.query_binding.query_digest,
        response_sha256=observation.response_sha256,
        evidence_ref=observation.evidence_ref,
        observed_at=observed_at,
        source_locator_digest=locator_digest,
        event_digest=event_digest,
        semantic_digest=semantic_digest,
        provenance_digest=provenance_digest,
        announcement_at=announcement_at,
        record_date=record_date,
        ex_date=ex_date,
        pay_date=pay_date,
        _token=_ACCEPTED_TOKEN,
    )


def _require_accepted_integrity(
    accepted: AcceptedCorporateAction,
    *,
    instrument_version: InstrumentVersion,
    registry: InstrumentRegistry,
) -> None:
    if type(accepted) is not AcceptedCorporateAction:
        raise TypeError("accepted must be an exact AcceptedCorporateAction")
    if type(instrument_version) is not InstrumentVersion:
        raise TypeError("instrument_version must be an exact InstrumentVersion")
    _require_registered_instrument(instrument_version, registry)

    provider = _canonical_text(accepted.provider_id, name="accepted provider_id")
    account = _canonical_text(accepted.account_id, name="accepted account_id")
    environment = _canonical_text(
        accepted.environment, name="accepted environment"
    ).upper()
    instrument_ref = _canonical_text(
        accepted.instrument_version_ref, name="accepted instrument_version_ref"
    )
    if instrument_ref != _instrument_ref(instrument_version):
        raise CorporateActionAdmissionError(
            "accepted corporate action instrument binding changed"
        )
    if instrument_version.provider_id.upper() != provider:
        raise CorporateActionAdmissionError(
            "accepted corporate action provider binding changed"
        )
    for name, value, pattern in (
        ("query_digest", accepted.query_digest, _SHA256),
        ("response_sha256", accepted.response_sha256, _SHA256),
        ("evidence_ref", accepted.evidence_ref, _EVIDENCE_REF),
        ("source_locator_digest", accepted.source_locator_digest, _SHA256),
        ("event_digest", accepted.event_digest, _SHA256),
        ("semantic_digest", accepted.semantic_digest, _SHA256),
        ("provenance_digest", accepted.provenance_digest, _SHA256),
    ):
        text = _canonical_text(value, name=name)
        if pattern.fullmatch(text) is None:
            raise CorporateActionAdmissionError(f"{name} changed or is not canonical")
    _parse_utc_text(accepted.observed_at, name="accepted observed_at")

    event_material = _event_material(accepted.event)
    if payload_digest(event_material) != accepted.event_digest:
        raise CorporateActionAdmissionError(
            "accepted corporate event changed after provenance admission"
        )
    semantic_digest = _semantic_event_digest(
        provider_id=provider,
        account_id=account,
        environment=environment,
        instrument_version_ref=instrument_ref,
        event_material=event_material,
    )
    if semantic_digest != accepted.semantic_digest:
        raise CorporateActionAdmissionError(
            "accepted corporate event semantic identity changed"
        )
    expected_provenance = _provenance_digest(
        semantic_digest=semantic_digest,
        locator_digest=accepted.source_locator_digest,
        query_digest=accepted.query_digest,
        response_sha256=accepted.response_sha256,
        evidence_ref=accepted.evidence_ref,
        observed_at=accepted.observed_at,
        announcement_at=accepted.announcement_at,
        record_date=accepted.record_date,
        ex_date=accepted.ex_date,
        pay_date=accepted.pay_date,
    )
    if expected_provenance != accepted.provenance_digest:
        raise CorporateActionAdmissionError(
            "accepted corporate-action provenance binding changed"
        )


def _cash_dividend_transaction(
    *,
    accepted: AcceptedCorporateAction,
    state: EquityState,
    transition: Transition,
    transaction_id: str,
    cause_event_id: str,
    economic_effective_at: str,
    economic_order_key: str,
) -> JournalTransaction:
    event = accepted.event
    if set(event.payload) != {"per_share", "currency"}:
        raise CorporateActionAdmissionError(
            "cash dividend requires exactly per_share and currency"
        )
    currency = _canonical_text(event.payload["currency"], name="currency").upper()
    if currency != state.currency:
        raise CorporateActionAdmissionError(
            "cash-dividend currency does not match current equity state"
        )
    try:
        per_share = parse_bounded_exact_decimal(event.payload["per_share"])
        entitlement = exact_multiply(state.quantity, per_share)
        income_offset = exact_subtract(Decimal("0"), entitlement)
    except ExactDecimalError as error:
        raise CorporateActionAdmissionError(
            "cash-dividend economics exceed exact arithmetic resource envelope"
        ) from error
    if per_share < 0:
        raise CorporateActionAdmissionError("cash-dividend per_share cannot be negative")

    if transition.economic_pnl != entitlement:
        raise CorporateActionAdmissionError(
            "pure corporate-action transition disagrees with exact dividend economics"
        )

    transaction = JournalTransaction(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        postings=(
            posting(f"UNSETTLED_CASH:{currency}", currency, entitlement),
            posting(
                f"CORPORATE_ACTION_INCOME:{currency}",
                currency,
                income_offset,
            ),
        ),
        economic_effective_at=economic_effective_at,
        economic_order_key=economic_order_key,
        observed_at=accepted.observed_at,
    )
    validate_transaction(transaction)
    return transaction


def book_accepted_corporate_action(
    *,
    accepted: AcceptedCorporateAction,
    state: EquityState,
    instrument_version: InstrumentVersion,
    registry: InstrumentRegistry,
    book: ScopedEconomicBook,
    as_of: datetime,
) -> CorporateActionAccountingResult:
    """Apply one source-bound event to the existing canonical accounting book.

    Re-observing the same semantic provider event is exactly-once.  Reuse of the
    same external provider event identity with changed revision/economics
    conflicts through a stable cause identity and requires explicit
    reversal/replacement instead of silent overwrite.
    """

    if type(state) is not EquityState:
        raise TypeError("state must be an exact EquityState")
    if type(book) is not ScopedEconomicBook:
        raise TypeError("book must be an exact ScopedEconomicBook")
    _require_accepted_integrity(
        accepted,
        instrument_version=instrument_version,
        registry=registry,
    )
    if book.environment != accepted.environment or book.account_id != accepted.account_id:
        raise CorporateActionAdmissionError(
            "scoped economic book does not match corporate-action account/environment"
        )

    event = accepted.event
    if event.kind not in _SUPPORTED_DURABLE_KINDS:
        raise CorporateActionAdmissionError(
            f"{event.kind} has no canonical durable accounting semantics yet"
        )

    cutoff = _utc_datetime(as_of, name="as_of")
    effective = _utc_datetime(event.effective_at, name="effective_at")
    observed = _parse_utc_text(accepted.observed_at, name="observed_at")
    if effective > cutoff:
        raise CorporateActionAdmissionError(
            "future corporate action cannot mutate accounting before effective_at"
        )
    if observed > cutoff:
        raise CorporateActionAdmissionError(
            "corporate action cannot mutate accounting before provider observation"
        )

    cause_digest = _scope_event_identity(
        provider_id=accepted.provider_id,
        account_id=accepted.account_id,
        environment=accepted.environment,
        instrument_version_ref=accepted.instrument_version_ref,
        event_id=event.event_id,
    )
    transaction_id = (
        "corporate-action:"
        + accepted.semantic_digest.removeprefix("sha256:")
    )
    cause_event_id = (
        "corporate-action-event:"
        + cause_digest.removeprefix("sha256:")
    )

    for existing in book.transactions:
        if existing.transaction_id == transaction_id:
            if existing.cause_event_id != cause_event_id:
                raise AccountingConflict(
                    "corporate-action transaction identity collides with different cause"
                )
            return CorporateActionAccountingResult(
                accepted=accepted,
                transition=None,
                transaction=existing,
                appended=False,
            )

    transition_book = CorporateActionBook(
        state,
        instrument_version=instrument_version,
        registry=registry,
    )
    transition = transition_book.apply(event)

    effective_text = _utc_text(effective, name="effective_at")
    order_key = (
        f"{event.source_sequence:020d}:"
        f"{accepted.provenance_digest.removeprefix('sha256:')}"
    )

    if event.kind == "SPLIT":
        transaction = book_equity_split_adjustment(
            transaction_id=transaction_id,
            cause_event_id=cause_event_id,
            instrument=instrument_version.provider_symbol,
            pre_split_quantity=state.quantity,
            numerator=event.payload["numerator"],
            denominator=event.payload["denominator"],
            economic_effective_at=effective_text,
            economic_order_key=order_key,
            observed_at=accepted.observed_at,
        )
    elif event.kind == "CASH_DIVIDEND":
        transaction = _cash_dividend_transaction(
            accepted=accepted,
            state=state,
            transition=transition,
            transaction_id=transaction_id,
            cause_event_id=cause_event_id,
            economic_effective_at=effective_text,
            economic_order_key=order_key,
        )
    else:
        raise AssertionError("unsupported durable corporate action escaped admission")

    try:
        appended = book.append(transaction)
    except AccountingConflict as error:
        raise AccountingConflict(
            "corporate-action external event changed; explicit reversal/replacement is required"
        ) from error

    return CorporateActionAccountingResult(
        accepted=accepted,
        transition=transition,
        transaction=transaction,
        appended=appended,
    )
