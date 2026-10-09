"""Durable authenticated provider financing authority.

This module composes three existing authorities without replacing any of them:

* :mod:`financing` owns revision and delta semantics.
* :class:`JournalStore` owns durable atomic journal commits.
* :class:`DurableProviderEconomicBook` owns provider/account economic state.

A provider financing observation can become durable PAPER/LIVE economics only when
it is reconstructed from one authenticated immutable ArtifactStore snapshot.
The financing revision fact and any non-zero economic delta are then committed by
one JournalStore command transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from threading import RLock
from typing import Any, Mapping, Protocol
import weakref
from uuid import NAMESPACE_URL, uuid5

from .accounting import AccountingConflict, JournalTransaction
from .financing import (
    FinancingConflict,
    FinancingError,
    FinancingEvent,
    FinancingRevisionBook,
    FinancingUpdate,
    book_financing_delta,
)
from .exact_decimal import (
    ExactDecimalError,
    exact_subtract,
    parse_bounded_exact_decimal,
    parse_bounded_json_integer_token,
    parse_bounded_json_number_token,
)
from .instruments import (
    InstrumentRegistry,
    InstrumentRegistryError,
)
from .persistence import (
    JournalStore,
    canonical_json,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_activity_accounting import DurableProviderEconomicBook
from .store_identity import same_journal_backing_object
from .provider_core import ProviderResponseObservation, Surface


_FINANCING_AGGREGATE_TYPE = "provider_financing_charge"
_FINANCING_EVENT_TYPE = "ProviderFinancingRevisionAccepted"
_FINANCING_TOPIC = "autotrade.financing.events"
_ACTOR = "provider-financing-accounting"
_EVIDENCE_SCHEMA_VERSION = "1.0.0"
_UTC_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


class AuthenticatedArtifactStore(Protocol):
    def read_authenticated_snapshot(
        self,
        artifact_id: str,
    ) -> tuple[dict[str, Any], bytes]: ...


@dataclass(frozen=True)
class DurableFinancingResult:
    inserted: bool
    event: FinancingEvent
    update: FinancingUpdate
    economic_transaction: JournalTransaction | None


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value.strip():
        raise FinancingError(f"{name} is required")
    return value.strip()


def _environment(value: object) -> str:
    normalized = _text(value, name="environment").upper()
    if normalized not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise FinancingError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    return normalized


def _charge_scope(
    scope_type: object,
    scope_id: object,
    *,
    account_id: str,
) -> tuple[str, str]:
    normalized_type = _text(scope_type, name="charge_scope_type").upper()
    if normalized_type not in {"ACCOUNT", "INSTRUMENT", "POSITION"}:
        raise FinancingError(
            "charge_scope_type must be ACCOUNT, INSTRUMENT, or POSITION"
        )
    normalized_id = _text(scope_id, name="charge_scope_id")
    if normalized_type == "ACCOUNT" and normalized_id != account_id:
        raise FinancingError(
            "account-level financing scope must match the authority account"
        )
    return normalized_type, normalized_id


def _canonical_financing_source_account(
    *,
    provider_id: object,
    charge_scope_type: object,
    unit: object,
) -> str:
    """Return the AutoTrade-owned ledger account for a financing charge.

    Provider evidence may describe the economic charge, but it cannot choose an
    internal chart-of-accounts bucket. The mapping is deliberately small and
    fail-closed until a qualified provider-specific normalizer owns additional
    financing classifications.
    """

    provider = _text(provider_id, name="provider_id").upper()
    scope_type = _text(charge_scope_type, name="charge_scope_type").upper()
    charge_unit = _text(unit, name="unit").upper()
    if provider == "BYBIT" and scope_type == "INSTRUMENT":
        return f"CASH:{charge_unit}"
    if scope_type == "ACCOUNT":
        return f"BORROW_LIABILITY:{charge_unit}"
    raise FinancingError(
        "financing source account requires a qualified provider/scope mapping"
    )


def _validate_source_account_binding(
    source_account: object,
    *,
    provider_id: object,
    charge_scope_type: object,
    unit: object,
) -> str:
    source = _text(source_account, name="source_account")
    charge_unit = _text(unit, name="unit").upper()
    if ":" not in source or source.rsplit(":", 1)[1].upper() != charge_unit:
        raise FinancingError(
            "financing source account must be explicitly denominated in event unit"
        )
    expected = _canonical_financing_source_account(
        provider_id=provider_id,
        charge_scope_type=charge_scope_type,
        unit=charge_unit,
    )
    if source != expected:
        raise FinancingError(
            "provider financing evidence cannot select an internal ledger account"
        )
    return expected


def _instant(value: object, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise FinancingError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise FinancingError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _instant_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _scoped_identity(kind: str, *parts: str) -> str:
    material = canonical_json([kind, *parts]).encode("utf-8")
    return f"{kind}:{sha256(material).hexdigest()}"


def _strict_json_object(data: bytes) -> dict[str, Any]:
    if type(data) is not bytes:
        raise FinancingError("authenticated financing evidence bytes are required")
    try:
        text = data.decode("utf-8")
    except UnicodeError as error:
        raise FinancingError("financing evidence must be UTF-8 JSON") from error

    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise FinancingError(
                    f"financing evidence contains duplicate key: {key}"
                )
            result[key] = value
        return result

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_float=parse_bounded_json_number_token,
            parse_int=parse_bounded_json_integer_token,
            parse_constant=lambda value: (_ for _ in ()).throw(
                FinancingError(
                    f"financing evidence contains non-finite JSON constant: {value}"
                )
            ),
        )
    except (json.JSONDecodeError, ExactDecimalError, TypeError, ValueError) as error:
        raise FinancingError(
            "financing evidence must be valid bounded JSON"
        ) from error
    if type(value) is not dict:
        raise FinancingError("financing evidence must be a JSON object")
    return value


def _normalize_artifact_digest(manifest: Mapping[str, Any]) -> str:
    if type(manifest) is not dict:
        raise FinancingError(
            "authenticated artifact manifest must be an exact dict"
        )
    digest = manifest.get("sha256")
    if (
        type(digest) is not str
        or len(digest) != 71
        or not digest.startswith("sha256:")
        or any(ch not in "0123456789abcdef" for ch in digest[7:])
    ):
        raise FinancingError("authenticated artifact manifest digest is invalid")
    return digest


def _verify_authenticated_snapshot_bytes(
    data: bytes,
    *,
    artifact_digest: str,
) -> None:
    if type(data) is not bytes:
        raise FinancingError("authenticated financing evidence bytes are required")
    actual = "sha256:" + sha256(data).hexdigest()
    if actual != artifact_digest:
        raise FinancingError(
            "authenticated financing artifact digest does not match returned bytes"
        )


def _event_payload(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    event: FinancingEvent,
    artifact_id: str,
    artifact_digest: str,
    charge_scope_type: str,
    charge_scope_id: str,
    previous_revision_digest: str,
    resulting_revision_digest: str,
    resulting_final_charge: Decimal,
    economic_delta: Decimal,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "charge_id": event.charge_id,
        "revision": event.revision,
        "kind": event.kind,
        "effective_at": _instant_text(event.effective_at),
        "available_at": _instant_text(event.available_at),
        "unit": event.unit,
        "amount": _decimal_text(event.amount),
        "source_account": event.source_account,
        "charge_scope_type": charge_scope_type,
        "charge_scope_id": charge_scope_id,
        "previous_revision_digest": previous_revision_digest,
        "resulting_revision_digest": resulting_revision_digest,
        "resulting_final_charge": _decimal_text(resulting_final_charge),
        "economic_delta": _decimal_text(economic_delta),
        "artifact_id": artifact_id,
        "artifact_digest": artifact_digest,
        "evidence_ref": event.evidence_ref,
    }


def _event_from_payload(payload: Mapping[str, Any]) -> FinancingEvent:
    required = {
        "schema_version",
        "provider_id",
        "account_id",
        "environment",
        "charge_id",
        "revision",
        "kind",
        "effective_at",
        "available_at",
        "unit",
        "amount",
        "source_account",
        "charge_scope_type",
        "charge_scope_id",
        "previous_revision_digest",
        "resulting_revision_digest",
        "resulting_final_charge",
        "economic_delta",
        "artifact_id",
        "artifact_digest",
        "evidence_ref",
    }
    if set(payload) != required or payload.get("schema_version") != "1.0.0":
        raise FinancingConflict("durable financing event payload shape is invalid")
    return FinancingEvent.create(
        charge_id=payload["charge_id"],
        revision=payload["revision"],
        kind=payload["kind"],
        effective_at=_instant(payload["effective_at"], name="effective_at"),
        available_at=_instant(payload["available_at"], name="available_at"),
        unit=payload["unit"],
        amount=payload["amount"],
        source_account=payload["source_account"],
        evidence_ref=payload["evidence_ref"],
    )


def authenticated_financing_event(
    artifact_store: AuthenticatedArtifactStore,
    *,
    artifact_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
) -> tuple[FinancingEvent, str, str, str]:
    """Reconstruct one financing event from one authenticated artifact snapshot."""

    aid = _text(artifact_id, name="artifact_id")
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    if scope in {"PAPER", "LIVE"}:
        raise FinancingError(
            "PAPER/LIVE financing requires a qualified provider-specific normalizer"
        )
    try:
        manifest, data = artifact_store.read_authenticated_snapshot(aid)
    except Exception as error:
        raise FinancingError(
            "financing evidence could not be authenticated"
        ) from error
    if type(manifest) is not dict:
        raise FinancingError("authenticated artifact manifest is invalid")
    if manifest.get("artifact_id") != aid:
        raise FinancingError("authenticated artifact identity does not match request")
    if manifest.get("media_type") != "application/json":
        raise FinancingError("financing evidence artifact must be application/json")
    rights = manifest.get("rights")
    if type(rights) is not dict or rights.get("storage") is not True:
        raise FinancingError(
            "financing evidence artifact lacks canonical storage rights"
        )
    artifact_digest = _normalize_artifact_digest(manifest)
    _verify_authenticated_snapshot_bytes(
        data,
        artifact_digest=artifact_digest,
    )

    evidence = _strict_json_object(data)
    expected_keys = {
        "schema_version",
        "provider_id",
        "account_id",
        "environment",
        "charge_id",
        "revision",
        "kind",
        "effective_at",
        "available_at",
        "unit",
        "amount",
        "source_account",
        "charge_scope_type",
        "charge_scope_id",
    }
    if set(evidence) != expected_keys:
        raise FinancingError("financing evidence must use the canonical shape")
    if evidence.get("schema_version") != _EVIDENCE_SCHEMA_VERSION:
        raise FinancingError("unsupported financing evidence schema version")
    if _text(evidence.get("provider_id"), name="provider_id").upper() != provider:
        raise FinancingError("financing evidence provider does not match authority")
    if _text(evidence.get("account_id"), name="account_id") != account:
        raise FinancingError("financing evidence account does not match authority")
    if _environment(evidence.get("environment")) != scope:
        raise FinancingError("financing evidence environment does not match authority")

    charge_scope_type, charge_scope_id = _charge_scope(
        evidence.get("charge_scope_type"),
        evidence.get("charge_scope_id"),
        account_id=account,
    )
    source_account = _validate_source_account_binding(
        evidence.get("source_account"),
        provider_id=provider,
        charge_scope_type=charge_scope_type,
        unit=evidence.get("unit"),
    )

    evidence_ref = f"artifact:{aid}:{artifact_digest}"
    event = FinancingEvent.create(
        charge_id=evidence.get("charge_id"),
        revision=evidence.get("revision"),
        kind=evidence.get("kind"),
        effective_at=_instant(evidence.get("effective_at"), name="effective_at"),
        available_at=_instant(evidence.get("available_at"), name="available_at"),
        unit=evidence.get("unit"),
        amount=evidence.get("amount"),
        source_account=source_account,
        evidence_ref=evidence_ref,
    )
    return event, artifact_digest, charge_scope_type, charge_scope_id


def _milliseconds_instant(value: object, *, name: str) -> datetime:
    if type(value) is int:
        token = str(value)
    elif type(value) is str:
        token = value
    else:
        raise FinancingError(f"{name} must be epoch milliseconds")
    try:
        milliseconds = parse_bounded_json_integer_token(token)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise FinancingError(f"{name} must be bounded epoch milliseconds") from error
    if milliseconds < 0:
        raise FinancingError(f"{name} must be non-negative")
    # Provider chronology is product authority, not host C-library authority.
    # Fixed-epoch timedelta arithmetic is deterministic across supported hosts
    # and preserves exact millisecond identity without time_t conversion.
    try:
        return _UTC_EPOCH + timedelta(milliseconds=milliseconds)
    except (OverflowError, ValueError) as error:
        raise FinancingError(f"{name} is outside supported UTC range") from error


def _bybit_funding_event_from_exact_response(
    raw: bytes,
    *,
    observation: ProviderResponseObservation,
    artifact_id: str,
    artifact_digest: str,
    row_id: str,
    instrument_registry: InstrumentRegistry,
    instrument_versions: Mapping[str, str],
) -> tuple[FinancingEvent, str, str]:
    response = _strict_json_object(raw)
    if response.get("retCode") != 0:
        raise FinancingError("Bybit transaction-log response was not successful")
    result = response.get("result")
    if type(result) is not dict:
        raise FinancingError("Bybit transaction-log result must be an object")
    rows = result.get("list")
    if type(rows) is not list:
        raise FinancingError("Bybit transaction-log result.list must be an array")
    target_id = _text(row_id, name="row_id")
    matches = [
        row
        for row in rows
        if type(row) is dict and row.get("id") == target_id
    ]
    if len(matches) != 1:
        raise FinancingError(
            "Bybit funding row_id must resolve to exactly one transaction-log row"
        )
    row = matches[0]
    query_account_type = observation.query_binding.query.get("accountType")
    query_category = observation.query_binding.query.get("category")
    row_category = _text(row.get("category"), name="category")
    if query_account_type != "UNIFIED":
        raise FinancingError(
            "Bybit funding requires the exact authenticated UNIFIED account query"
        )
    if query_category != "linear" or row_category != query_category:
        raise FinancingError(
            "Bybit funding row category must match the exact authenticated linear query"
        )
    if _text(row.get("type"), name="type").upper() != "SETTLEMENT":
        raise FinancingError("Bybit financing row must be a SETTLEMENT record")
    raw_funding = row.get("funding")
    if type(raw_funding) is not str or raw_funding != raw_funding.strip():
        raise FinancingError("Bybit funding must be an exact decimal string")
    try:
        funding = parse_bounded_exact_decimal(raw_funding, allow_exponent=False)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise FinancingError(
            "Bybit funding must be a bounded exact decimal string"
        ) from error
    if funding >= 0:
        raise FinancingError(
            "Bybit funding authority currently supports paid funding charges only"
        )
    currency = _text(row.get("currency"), name="currency").upper()
    query_currency = observation.query_binding.query.get("currency")
    if (
        query_currency is not None
        and _text(query_currency, name="query currency").upper() != currency
    ):
        raise FinancingError(
            "Bybit funding row currency does not match authenticated query"
        )
    symbol = _text(row.get("symbol"), name="symbol")
    if type(instrument_registry) is not InstrumentRegistry:
        raise FinancingError(
            "Bybit financing requires exact canonical InstrumentRegistry authority"
        )
    if type(instrument_versions) is not dict:
        raise FinancingError(
            "instrument_versions must be a mapping backed by an exact dict"
        )
    instrument_version_snapshot = dict.copy(instrument_versions)
    try:
        requested_instrument_version = _text(
            instrument_version_snapshot[symbol],
            name="instrument_version",
        )
    except KeyError as error:
        raise FinancingError(
            "Bybit funding symbol lacks a qualified instrument version"
        ) from error
    effective_at = _milliseconds_instant(
        row.get("transactionTime"),
        name="transactionTime",
    )
    start_at = (
        None
        if observation.query_binding.query.get("startTime") is None
        else _milliseconds_instant(
            observation.query_binding.query.get("startTime"),
            name="startTime",
        )
    )
    end_at = (
        None
        if observation.query_binding.query.get("endTime") is None
        else _milliseconds_instant(
            observation.query_binding.query.get("endTime"),
            name="endTime",
        )
    )
    if start_at is not None and end_at is not None and start_at > end_at:
        raise FinancingError("Bybit funding query time bounds are contradictory")
    if start_at is not None and effective_at < start_at:
        raise FinancingError("Bybit funding transactionTime precedes authenticated query")
    if end_at is not None and effective_at > end_at:
        raise FinancingError("Bybit funding transactionTime exceeds authenticated query")
    raw_cash_flow = row.get("cashFlow")
    if raw_cash_flow is not None:
        try:
            companion_cash_flow = parse_bounded_exact_decimal(
                raw_cash_flow,
                allow_exponent=False,
            )
        except (ExactDecimalError, TypeError, ValueError) as error:
            raise FinancingError(
                "Bybit settlement cashFlow must be a bounded exact decimal string"
            ) from error
        if companion_cash_flow != 0:
            raise FinancingError(
                "Bybit SETTLEMENT with companion cashFlow requires settlement authority"
            )
    try:
        version = instrument_registry.exact(requested_instrument_version)
    except InstrumentRegistryError as error:
        raise FinancingError(
            "Bybit funding instrument_version is not canonical registry authority"
        ) from error
    instrument_version = f"{version.instrument_id}@{version.version}"
    try:
        effective_version = instrument_registry.at(
            version.instrument_id,
            effective_at,
        )
    except InstrumentRegistryError as error:
        raise FinancingError(
            "Bybit funding instrument_version is not effective at event time"
        ) from error
    if (
        effective_version != version
        or version.provider_id.strip().upper() != "BYBIT"
        or version.provider_symbol != symbol
        or version.asset_class != "PERPETUAL"
    ):
        raise FinancingError(
            "Bybit funding instrument_version does not match provider product at event time"
        )
    if version.settlement_currency.strip().upper() != currency:
        raise FinancingError(
            "Bybit funding currency does not match canonical instrument settlement unit"
        )
    query_base_coin = observation.query_binding.query.get("baseCoin")
    if (
        query_base_coin is not None
        and _text(query_base_coin, name="query baseCoin").upper()
        != version.base_currency.strip().upper()
    ):
        raise FinancingError(
            "Bybit funding instrument base currency does not match authenticated query"
        )
    if observation.query_binding.instrument_version != instrument_version:
        raise FinancingError(
            "Bybit funding authenticated query is not bound to the canonical instrument version"
        )
    cash_flow_text = row.get("cashFlow")
    fee_text = row.get("fee")
    change_text = row.get("change")
    if (
        type(cash_flow_text) is not str
        or cash_flow_text != cash_flow_text.strip()
        or type(fee_text) is not str
        or fee_text != fee_text.strip()
        or type(change_text) is not str
        or change_text != change_text.strip()
    ):
        raise FinancingError(
            "Bybit settlement row contains missing or noncanonical companion economics"
        )
    try:
        cash_flow = parse_bounded_exact_decimal(cash_flow_text, allow_exponent=False)
        fee = parse_bounded_exact_decimal(fee_text, allow_exponent=False)
        change = parse_bounded_exact_decimal(change_text, allow_exponent=False)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise FinancingError(
            "Bybit settlement row contains noncanonical companion economics"
        ) from error
    expected_change = exact_subtract(
        exact_subtract(cash_flow, fee),
        exact_subtract(Decimal("0"), funding),
    )
    if change != expected_change:
        raise FinancingError(
            "Bybit SETTLEMENT change does not equal cashFlow + funding - fee"
        )
    if cash_flow != 0 or fee != 0:
        raise FinancingError(
            "Bybit SETTLEMENT with companion economics requires settlement authority"
        )

    available_at = _instant(observation.observed_at, name="observed_at")
    evidence_ref = (
        f"artifact:{artifact_id}:{artifact_digest}|{observation.evidence_ref}"
    )
    event = FinancingEvent.create(
        charge_id=f"BYBIT:TRANSACTION:{target_id}:FUNDING",
        revision=1,
        kind="FINAL",
        effective_at=effective_at,
        available_at=available_at,
        unit=currency,
        amount=exact_subtract(Decimal("0"), funding),
        source_account=f"CASH:{currency}",
        evidence_ref=evidence_ref,
    )
    return event, "INSTRUMENT", instrument_version


def _revision_book_digest(events: list[FinancingEvent]) -> str:
    return payload_digest(
        {
            "schema_version": "1.0.0",
            "events": [
                {
                    "charge_id": item.charge_id,
                    "revision": item.revision,
                    "kind": item.kind,
                    "effective_at": _instant_text(item.effective_at),
                    "available_at": _instant_text(item.available_at),
                    "unit": item.unit,
                    "amount": _decimal_text(item.amount),
                    "source_account": item.source_account,
                    "evidence_ref": item.evidence_ref,
                }
                for item in events
            ],
        }
    )


def _provider_fact_digest(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    event: FinancingEvent,
    charge_scope_type: str,
    charge_scope_id: str,
) -> str:
    """Derive stable financial fact identity without binding observation receipt identity."""
    payload = {
        "schema_version": "1.0.0",
        "provider_id": _text(provider_id, name="provider_id").upper(),
        "account_id": _text(account_id, name="account_id"),
        "environment": _environment(environment),
        "charge_id": event.charge_id,
        "revision": event.revision,
        "kind": event.kind,
        "effective_at": _instant_text(event.effective_at),
        "unit": event.unit,
        "amount": _decimal_text(event.amount),
        "source_account": event.source_account,
        "charge_scope_type": _text(
            charge_scope_type, name="charge_scope_type"
        ).upper(),
        "charge_scope_id": _text(charge_scope_id, name="charge_scope_id"),
    }
    return payload_digest(payload)

def _record_exact(
    book: FinancingRevisionBook,
    event: FinancingEvent,
) -> FinancingUpdate:
    """Apply one pure revision while removing ambient Decimal-context authority."""

    previous = book.latest(event.charge_id)
    previous_final = (
        previous.amount
        if previous is not None and previous.kind == "FINAL"
        else Decimal("0")
    )
    update = book.record(event)
    if not update.accepted:
        return update
    exact_delta = exact_subtract(update.current_final_charge, previous_final)
    return FinancingUpdate(
        accepted=True,
        economic_delta=exact_delta,
        current_revision=update.current_revision,
        current_final_charge=update.current_final_charge,
    )


@dataclass(frozen=True)
class _DurableFinancingAuthorityBinding:
    store_ref: weakref.ReferenceType
    economic_book_ref: weakref.ReferenceType
    store_identity: object
    provider_id: str
    account_id: str
    environment: str


def _build_durable_financing_authority_accessors():
    """Retain selected financing authority outside caller-mutable instance state."""

    bindings: dict[
        int,
        tuple[weakref.ReferenceType, _DurableFinancingAuthorityBinding],
    ] = {}
    lock = RLock()

    def prune_dead() -> None:
        with lock:
            dead = [
                object_id
                for object_id, (value_ref, _binding) in bindings.items()
                if value_ref() is None
            ]
            for object_id in dead:
                bindings.pop(object_id, None)

    def registered_binding(
        value: object,
    ) -> _DurableFinancingAuthorityBinding | None:
        object_id = id(value)
        with lock:
            entry = bindings.get(object_id)
            if entry is None:
                return None
            value_ref, binding = entry
            current = value_ref()
            if current is value:
                return binding
            if current is None:
                bindings.pop(object_id, None)
                return None
            raise FinancingConflict("durable financing binding identity collision")

    def is_registered(value: object) -> bool:
        try:
            prune_dead()
            return registered_binding(value) is not None
        except TypeError:
            return False

    def initialize(value: object) -> None:
        if type(value) is not DurableFinancingBook:
            raise TypeError("financing book must be exact DurableFinancingBook")
        prune_dead()
        if registered_binding(value) is not None:
            raise FinancingConflict("financing authority is already established")
        state = object.__getattribute__(value, "__dict__")
        store = state.get("store")
        economic_book = state.get("economic_book")
        if type(store) is not JournalStore:
            raise TypeError("store must be the exact canonical JournalStore")
        if type(economic_book) is not DurableProviderEconomicBook:
            raise TypeError(
                "economic_book must be the exact canonical DurableProviderEconomicBook"
            )
        try:
            identity = require_exact_journal_store_authority(
                store,
                subject="durable financing JournalStore",
            )
            economic_store = economic_book.store
            economic_identity = require_exact_journal_store_authority(
                economic_store,
                subject="durable financing economic JournalStore",
            )
        except (AccountingConflict, TypeError, RuntimeError) as error:
            raise FinancingConflict(
                "financing authority composition is invalid"
            ) from error
        if not same_journal_backing_object(identity, economic_identity):
            raise FinancingConflict(
                "financing and economic authorities must share JournalStore backing generation"
            )
        binding = _DurableFinancingAuthorityBinding(
            store_ref=weakref.ref(store),
            economic_book_ref=weakref.ref(economic_book),
            store_identity=identity,
            provider_id=state["provider_id"],
            account_id=state["account_id"],
            environment=state["environment"],
        )
        with lock:
            if registered_binding(value) is not None:
                raise FinancingConflict("financing authority is already established")
            bindings[id(value)] = (weakref.ref(value), binding)

    def require(value: object) -> _DurableFinancingAuthorityBinding:
        if type(value) is not DurableFinancingBook:
            raise TypeError("financing book must be exact DurableFinancingBook")
        prune_dead()
        binding = registered_binding(value)
        if binding is None:
            raise FinancingConflict("financing authority is not established")
        state = object.__getattribute__(value, "__dict__")
        store = binding.store_ref()
        economic_book = binding.economic_book_ref()
        if store is None or economic_book is None:
            raise FinancingConflict(
                "financing authority resource was released while book is live"
            )
        if (
            state.get("store") is not store
            or state.get("economic_book") is not economic_book
            or state.get("provider_id") != binding.provider_id
            or state.get("account_id") != binding.account_id
            or state.get("environment") != binding.environment
        ):
            raise FinancingConflict(
                "financing authority state changed after construction"
            )
        try:
            current_identity = require_exact_journal_store_authority(
                store,
                subject="durable financing JournalStore",
            )
            economic_store = economic_book.store
            economic_identity = require_exact_journal_store_authority(
                economic_store,
                subject="durable financing economic JournalStore",
            )
        except (AccountingConflict, TypeError, RuntimeError) as error:
            raise FinancingConflict("financing authority generation changed") from error
        if current_identity != binding.store_identity:
            raise FinancingConflict("financing JournalStore generation changed")
        if not same_journal_backing_object(current_identity, economic_identity):
            raise FinancingConflict(
                "financing and economic authorities no longer share one JournalStore generation"
            )
        if (
            economic_book.provider_id != binding.provider_id
            or economic_book.account_id != binding.account_id
            or economic_book.environment != binding.environment
        ):
            raise FinancingConflict(
                "financing and economic authority scope changed"
            )
        return binding

    return is_registered, initialize, require


class DurableFinancingBook:
    """Journal-backed provider/account financing revision authority."""

    _AUTHORITY_STATE_NAMES = frozenset(
        {"store", "economic_book", "provider_id", "account_id", "environment"}
    )

    def __getattribute__(self, name: str):
        is_registered = globals().get(
            "_durable_financing_authority_is_registered"
        )
        if (
            type(name) is str
            and name != "__dict__"
            and is_registered is not None
            and is_registered(self)
        ):
            class_owned = any(
                name in base.__dict__ for base in DurableFinancingBook.__mro__
            )
            if name in DurableFinancingBook._AUTHORITY_STATE_NAMES or class_owned:
                _require_durable_financing_authority(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        is_registered = globals().get(
            "_durable_financing_authority_is_registered"
        )
        if is_registered is not None and is_registered(self):
            class_owned = (
                type(name) is str
                and any(name in base.__dict__ for base in DurableFinancingBook.__mro__)
            )
            if name in DurableFinancingBook._AUTHORITY_STATE_NAMES or class_owned:
                raise FinancingConflict(
                    "durable financing authority state is immutable"
                )
        object.__setattr__(self, name, value)

    def __init__(
        self,
        store: JournalStore,
        economic_book: DurableProviderEconomicBook,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ):
        if _durable_financing_authority_is_registered(self):
            raise FinancingConflict("financing authority is already established")
        if type(store) is not JournalStore:
            raise TypeError("store must be the exact canonical JournalStore")
        if type(economic_book) is not DurableProviderEconomicBook:
            raise TypeError(
                "economic_book must be the exact canonical DurableProviderEconomicBook"
            )
        if type(economic_book.store) is not JournalStore:
            raise TypeError(
                "economic_book.store must be the exact canonical JournalStore"
            )
        self.store = store
        self.economic_book = economic_book
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        if not same_journal_backing_object(
            store.store_identity,
            economic_book.store.store_identity,
        ):
            raise ValueError(
                "financing and economic authorities must share JournalStore backing generation"
            )
        if (
            economic_book.provider_id != self.provider_id
            or economic_book.account_id != self.account_id
            or economic_book.environment != self.environment
        ):
            raise ValueError(
                "financing and economic authorities must share provider/account/environment"
            )
        _initialize_durable_financing_authority(self)

    def _aggregate_id(self, charge_id: str) -> str:
        _require_durable_financing_authority(self)
        return _scoped_identity(
            "provider-financing",
            self.provider_id,
            self.account_id,
            self.environment,
            _text(charge_id, name="charge_id"),
        )

    def _events(self, charge_id: str) -> list[dict[str, Any]]:
        _require_durable_financing_authority(self)
        return self.store.load_events(
            _FINANCING_AGGREGATE_TYPE,
            self._aggregate_id(charge_id),
        )

    def _book_from_durable_events(
        self,
        charge_id: str,
        events: list[dict[str, Any]],
        *,
        economic_transactions: tuple[JournalTransaction, ...],
    ) -> FinancingRevisionBook:
        history: list[FinancingEvent] = []
        aggregate_id = self._aggregate_id(charge_id)
        canonical_charge_scope: tuple[str, str] | None = None
        for expected_version, durable in enumerate(events, 1):
            if (
                durable.get("event_type") != _FINANCING_EVENT_TYPE
                or durable.get("aggregate_version") != expected_version
            ):
                raise FinancingConflict("durable financing revision chain is invalid")
            payload = durable.get("payload")
            if not isinstance(payload, Mapping):
                raise FinancingConflict("durable financing payload is invalid")
            if payload_digest(payload) != durable.get("payload_hash"):
                raise FinancingConflict("durable financing payload hash is invalid")
            if (
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
                or payload.get("charge_id") != charge_id
            ):
                raise FinancingConflict("durable financing scope is invalid")
            durable_charge_scope = _charge_scope(
                payload.get("charge_scope_type"),
                payload.get("charge_scope_id"),
                account_id=self.account_id,
            )
            if canonical_charge_scope is None:
                canonical_charge_scope = durable_charge_scope
            elif durable_charge_scope != canonical_charge_scope:
                raise FinancingConflict(
                    "durable financing charge scope changed across revisions"
                )
            _validate_source_account_binding(
                payload.get("source_account"),
                provider_id=payload.get("provider_id"),
                charge_scope_type=payload.get("charge_scope_type"),
                unit=payload.get("unit"),
            )
            candidate_event = _event_from_payload(payload)
            previous_digest = _revision_book_digest(history)
            candidate_book = FinancingRevisionBook(history)
            candidate_update = _record_exact(candidate_book, candidate_event)
            resulting_history = list(candidate_book.events)
            if (
                payload.get("previous_revision_digest") != previous_digest
                or payload.get("resulting_revision_digest")
                != _revision_book_digest(resulting_history)
                or payload.get("resulting_final_charge")
                != _decimal_text(candidate_update.current_final_charge)
                or payload.get("economic_delta")
                != _decimal_text(candidate_update.economic_delta)
            ):
                raise FinancingConflict(
                    "durable financing revision-state binding is invalid"
                )
            event_id = _text(
                durable.get("event_id"),
                name="durable financing event_id",
            )
            self._validate_economic_conservation(
                aggregate_id=aggregate_id,
                event_id=event_id,
                event=candidate_event,
                economic_delta=candidate_update.economic_delta,
                economic_transactions=economic_transactions,
            )
            history = resulting_history
        return FinancingRevisionBook(history)

    def _stable_replay_cut(
        self,
        charge_id: str,
        *,
        max_attempts: int = 4,
    ) -> tuple[
        int,
        list[dict[str, Any]],
        tuple[JournalTransaction, ...],
        FinancingRevisionBook,
    ]:
        """Read financing + economics only from a bounded stable journal cut."""

        _require_durable_financing_authority(self)
        for _attempt in range(max_attempts):
            cut_before = self.store.current_journal_sequence()
            durable_events = self._events(charge_id)
            economic_cut = self.economic_book.read_cut()
            book = self._book_from_durable_events(
                charge_id,
                durable_events,
                economic_transactions=economic_cut.transactions,
            )
            cut_after = self.store.current_journal_sequence()
            if cut_before == cut_after:
                return cut_before, durable_events, economic_cut.transactions, book
        raise FinancingConflict(
            "financing authority could not obtain a stable JournalStore cut"
        )

    def _replay(self, charge_id: str) -> FinancingRevisionBook:
        return self._stable_replay_cut(charge_id)[3]

    def _economic_transaction(
        self,
        *,
        aggregate_id: str,
        event_id: str,
        event: FinancingEvent,
        economic_delta: Decimal,
    ) -> JournalTransaction:
        base = book_financing_delta(
            transaction_id=str(
                uuid5(
                    NAMESPACE_URL,
                    "https://transactions.autotrade.local/provider-financing/"
                    + aggregate_id
                    + "/"
                    + str(event.revision),
                )
            ),
            cause_event_id=event_id,
            unit=event.unit,
            source_account=event.source_account,
            economic_delta=economic_delta,
        )
        return JournalTransaction(
            transaction_id=base.transaction_id,
            cause_event_id=base.cause_event_id,
            postings=base.postings,
            economic_effective_at=_instant_text(event.effective_at),
            economic_order_key=(
                "provider-financing:"
                + aggregate_id
                + ":"
                + str(event.revision)
            ),
            observed_at=_instant_text(event.available_at),
        )

    def _validate_economic_conservation(
        self,
        *,
        aggregate_id: str,
        event_id: str,
        event: FinancingEvent,
        economic_delta: Decimal,
        economic_transactions: tuple[JournalTransaction, ...],
    ) -> JournalTransaction | None:
        """Require exact one-to-one financing/economic durable conservation."""

        matches = tuple(
            transaction
            for transaction in economic_transactions
            if transaction.cause_event_id == event_id
        )
        if economic_delta == 0:
            if matches:
                if event.kind == "FINAL":
                    raise FinancingConflict(
                        "zero-delta financing revision has an economic posting"
                    )
                raise FinancingConflict(
                    "non-economic financing revision has an economic posting"
                )
            return None

        expected = self._economic_transaction(
            aggregate_id=aggregate_id,
            event_id=event_id,
            event=event,
            economic_delta=economic_delta,
        )
        if not matches:
            raise FinancingConflict(
                "durable financing revision is missing its economic posting"
            )
        if len(matches) != 1:
            raise FinancingConflict(
                "durable financing revision has duplicate economic postings"
            )
        if matches[0] != expected:
            raise FinancingConflict(
                "durable financing revision economic posting does not match canonical financing delta"
            )
        if expected not in economic_transactions:
            raise FinancingConflict(
                "durable financing revision is missing its canonical economic batch"
            )
        return expected

    def latest(self, charge_id: str) -> FinancingEvent | None:
        _require_durable_financing_authority(self)
        normalized = _text(charge_id, name="charge_id")
        return self._replay(normalized).latest(normalized)

    def record_authenticated_artifact(
        self,
        artifact_store: AuthenticatedArtifactStore,
        *,
        artifact_id: str,
        committed_at: str | None = None,
    ) -> DurableFinancingResult:
        _require_durable_financing_authority(self)
        (
            event,
            artifact_digest,
            charge_scope_type,
            charge_scope_id,
        ) = authenticated_financing_event(
            artifact_store,
            artifact_id=artifact_id,
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
        )
        return self._record_preverified(
            event=event,
            artifact_id=artifact_id,
            artifact_digest=artifact_digest,
            charge_scope_type=charge_scope_type,
            charge_scope_id=charge_scope_id,
            committed_at=committed_at,
        )

    def record_bybit_funding_observation(
        self,
        observation: ProviderResponseObservation,
        artifact_store: AuthenticatedArtifactStore,
        *,
        artifact_id: str,
        row_id: str,
        instrument_registry: InstrumentRegistry,
        instrument_versions: Mapping[str, str],
        committed_at: str | None = None,
    ) -> DurableFinancingResult:
        _require_durable_financing_authority(self)
        if type(observation) is not ProviderResponseObservation:
            raise TypeError(
                "observation must be the exact canonical ProviderResponseObservation"
            )
        observation.require_scope(
            provider_id="BYBIT",
            surface=Surface.ACTIVITIES,
            endpoint="/v5/account/transaction-log",
            account_id=self.account_id,
            environment=self.environment,
        )
        if self.provider_id != "BYBIT":
            raise FinancingError("Bybit financing observation requires BYBIT authority")
        if self.environment in {"PAPER", "LIVE"}:
            raise FinancingError(
                "PAPER/LIVE Bybit financing requires journal-derived provider-origin authority"
            )
        query = dict(observation.query_binding.query)
        allowed_query_keys = {
            "accountType",
            "category",
            "currency",
            "baseCoin",
            "type",
            "startTime",
            "endTime",
            "limit",
            "cursor",
        }
        if (
            query.get("accountType") != "UNIFIED"
            or query.get("category") != "linear"
            or set(query) - allowed_query_keys
            or ("type" in query and query["type"] != "SETTLEMENT")
        ):
            raise FinancingError(
                "Bybit funding requires a qualified linear transaction-log query"
            )
        aid = _text(artifact_id, name="artifact_id")
        try:
            manifest, raw = artifact_store.read_authenticated_snapshot(aid)
        except Exception as error:
            raise FinancingError(
                "Bybit financing artifact could not be authenticated"
            ) from error
        if type(manifest) is not dict or manifest.get("artifact_id") != aid:
            raise FinancingError("Bybit financing artifact identity mismatch")
        if manifest.get("media_type") != "application/json":
            raise FinancingError("Bybit financing artifact must be application/json")
        rights = manifest.get("rights")
        if type(rights) is not dict or rights.get("storage") is not True:
            raise FinancingError("Bybit financing artifact lacks storage rights")
        artifact_digest = _normalize_artifact_digest(manifest)
        _verify_authenticated_snapshot_bytes(
            raw,
            artifact_digest=artifact_digest,
        )
        if artifact_digest != observation.response_sha256:
            raise FinancingError(
                "Bybit financing artifact bytes do not match provider observation"
            )
        event, charge_scope_type, charge_scope_id = (
            _bybit_funding_event_from_exact_response(
                raw,
                observation=observation,
                artifact_id=aid,
                artifact_digest=artifact_digest,
                row_id=row_id,
                instrument_registry=instrument_registry,
                instrument_versions=instrument_versions,
            )
        )
        return self._record_preverified(
            event=event,
            artifact_id=aid,
            artifact_digest=artifact_digest,
            charge_scope_type=charge_scope_type,
            charge_scope_id=charge_scope_id,
            committed_at=committed_at,
            allow_provider_fact_reobservation=True,
        )

    def _record_preverified(
        self,
        *,
        event: FinancingEvent,
        artifact_id: str,
        artifact_digest: str,
        charge_scope_type: str,
        charge_scope_id: str,
        committed_at: str | None,
        allow_provider_fact_reobservation: bool = False,
    ) -> DurableFinancingResult:
        _require_durable_financing_authority(self)
        aggregate_id = self._aggregate_id(event.charge_id)
        accepted_cut, durable_events, economic_transactions, book = self._stable_replay_cut(
            event.charge_id
        )
        incoming_charge_scope = _charge_scope(
            charge_scope_type,
            charge_scope_id,
            account_id=self.account_id,
        )
        if durable_events:
            first_payload = durable_events[0].get("payload")
            if not isinstance(first_payload, Mapping):
                raise FinancingConflict("durable financing payload is invalid")
            durable_charge_scope = _charge_scope(
                first_payload.get("charge_scope_type"),
                first_payload.get("charge_scope_id"),
                account_id=self.account_id,
            )
            if incoming_charge_scope != durable_charge_scope:
                raise FinancingConflict(
                    "financing charge scope cannot change across revisions"
                )
        if allow_provider_fact_reobservation and durable_events:
            existing = book.latest(event.charge_id)
            if existing is not None and existing.revision == event.revision:
                incoming_fact = _provider_fact_digest(
                    provider_id=self.provider_id,
                    account_id=self.account_id,
                    environment=self.environment,
                    event=event,
                    charge_scope_type=charge_scope_type,
                    charge_scope_id=charge_scope_id,
                )
                existing_fact = _provider_fact_digest(
                    provider_id=self.provider_id,
                    account_id=self.account_id,
                    environment=self.environment,
                    event=existing,
                    charge_scope_type=charge_scope_type,
                    charge_scope_id=charge_scope_id,
                )
                if incoming_fact == existing_fact:
                    return DurableFinancingResult(
                        inserted=False,
                        event=existing,
                        update=FinancingUpdate(
                            accepted=False,
                            economic_delta=Decimal("0"),
                            current_revision=existing.revision,
                            current_final_charge=book.latest(
                                event.charge_id
                            ).amount,
                        ),
                        economic_transaction=None,
                    )

        previous_revision_digest = _revision_book_digest(list(book.events))
        update = _record_exact(book, event)
        resulting_revision_digest = _revision_book_digest(list(book.events))

        if not update.accepted:
            economic_transaction = None
            durable_event_id = _text(
                durable_events[-1].get("event_id"),
                name="durable financing event_id",
            )
            if event.kind == "FINAL":
                prior = self._book_from_durable_events(
                    event.charge_id,
                    durable_events[:-1],
                    economic_transactions=economic_transactions,
                )
                previous = prior.latest(event.charge_id)
                previous_final = (
                    previous.amount
                    if previous is not None and previous.kind == "FINAL"
                    else Decimal("0")
                )
                expected_delta = exact_subtract(event.amount, previous_final)
                if expected_delta != 0:
                    economic_transaction = self._economic_transaction(
                        aggregate_id=aggregate_id,
                        event_id=durable_event_id,
                        event=event,
                        economic_delta=expected_delta,
                    )
                    if economic_transaction not in economic_transactions:
                        raise FinancingConflict(
                            "durable financing revision is missing its economic posting"
                        )
            else:
                stray = tuple(
                    transaction
                    for transaction in economic_transactions
                    if transaction.cause_event_id == durable_event_id
                )
                if stray:
                    raise FinancingConflict(
                        "non-economic financing revision has an economic posting"
                    )
            if event.kind == "FINAL" and expected_delta == 0:
                stray = tuple(
                    transaction
                    for transaction in economic_transactions
                    if transaction.cause_event_id == durable_event_id
                )
                if stray:
                    raise FinancingConflict(
                        "zero-delta financing revision has an economic posting"
                    )
            return DurableFinancingResult(
                inserted=False,
                event=event,
                update=update,
                economic_transaction=economic_transaction,
            )

        next_version = len(durable_events) + 1
        commit_instant = (
            datetime.now(timezone.utc)
            if committed_at is None
            else _instant(committed_at, name="committed_at")
        )
        if commit_instant < event.available_at:
            raise FinancingError(
                "financing evidence cannot be committed before available_at"
            )
        when = _instant_text(commit_instant)
        payload = _event_payload(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            event=event,
            artifact_id=_text(artifact_id, name="artifact_id"),
            artifact_digest=artifact_digest,
            charge_scope_type=charge_scope_type,
            charge_scope_id=charge_scope_id,
            previous_revision_digest=previous_revision_digest,
            resulting_revision_digest=resulting_revision_digest,
            resulting_final_charge=update.current_final_charge,
            economic_delta=update.economic_delta,
        )
        event_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/provider-financing/"
                + aggregate_id
                + "/"
                + str(event.revision),
            )
        )
        envelope = {
            "event_id": event_id,
            "event_type": _FINANCING_EVENT_TYPE,
            "aggregate_type": _FINANCING_AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(next_version),
            "committed_at": when,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }

        economic_transaction: JournalTransaction | None = None
        economic_plan = None
        if update.economic_delta != 0:
            economic_transaction = self._economic_transaction(
                aggregate_id=aggregate_id,
                event_id=event_id,
                event=event,
                economic_delta=update.economic_delta,
            )
            economic_plan = self.economic_book.prepare_batch_mutation(
                (economic_transaction,),
                committed_at=when,
            )
            if economic_plan.already_committed:
                raise FinancingConflict(
                    "financing economics exist without the matching durable revision"
                )
            if economic_plan.envelope is None:
                raise FinancingConflict("fresh financing economics lack durable event")

        request = {
            "schema_version": "1.0.0",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "financing_revision": payload,
            "economic_batch": (
                None if economic_plan is None else economic_plan.request
            ),
        }
        result = {
            "financing_event_id": event_id,
            "revision": event.revision,
            "economic_delta": _decimal_text(update.economic_delta),
            "current_final_charge": _decimal_text(update.current_final_charge),
            "economic_batch": (
                None if economic_plan is None else economic_plan.result
            ),
        }
        command_id = str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/provider-financing/"
                + aggregate_id
                + "/"
                + str(event.revision),
            )
        )
        idempotency_key = (
            "provider-financing:"
            + aggregate_id
            + ":"
            + str(event.revision)
            + ":"
            + payload_digest(request)
        )
        events: list[tuple[dict[str, Any], str | None]] = [
            (envelope, _FINANCING_TOPIC)
        ]
        state_version = next_version
        if economic_plan is not None:
            events.append((economic_plan.envelope, "autotrade.economic.events"))
            state_version = max(state_version, economic_plan.aggregate_version)

        try:
            _, inserted, _ = self.store.commit_command(
                command_id=command_id,
                actor=_ACTOR,
                environment=self.environment,
                idempotency_key=idempotency_key,
                request=request,
                result=result,
                state_version=state_version,
                events=events,
                expected_journal_sequence=accepted_cut,
            )
        finally:
            self.economic_book.refresh()

        return DurableFinancingResult(
            inserted=inserted,
            event=event,
            update=update,
            economic_transaction=economic_transaction,
        )

(
    _durable_financing_authority_is_registered,
    _initialize_durable_financing_authority,
    _require_durable_financing_authority,
) = _build_durable_financing_authority_accessors()
del _build_durable_financing_authority_accessors