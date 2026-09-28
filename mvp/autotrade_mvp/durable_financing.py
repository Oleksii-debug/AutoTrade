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
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Any, Mapping, Protocol
from uuid import NAMESPACE_URL, uuid5

from .accounting import JournalTransaction
from .financing import (
    FinancingConflict,
    FinancingError,
    FinancingEvent,
    FinancingRevisionBook,
    FinancingUpdate,
    book_financing_delta,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook
from .provider_core import ProviderResponseObservation, Surface


_FINANCING_AGGREGATE_TYPE = "provider_financing_charge"
_FINANCING_EVENT_TYPE = "ProviderFinancingRevisionAccepted"
_FINANCING_TOPIC = "autotrade.financing.events"
_ACTOR = "provider-financing-accounting"
_EVIDENCE_SCHEMA_VERSION = "1.0.0"


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
    if not isinstance(value, str) or not value.strip():
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
    internal chart-of-accounts bucket.  The mapping is deliberately small and
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
    if not isinstance(data, bytes):
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
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                FinancingError(
                    f"financing evidence contains non-finite JSON constant: {value}"
                )
            ),
        )
    except (json.JSONDecodeError, TypeError) as error:
        raise FinancingError("financing evidence must be valid JSON") from error
    if not isinstance(value, dict):
        raise FinancingError("financing evidence must be a JSON object")
    return value


def _normalize_artifact_digest(manifest: Mapping[str, Any]) -> str:
    digest = manifest.get("sha256")
    if (
        not isinstance(digest, str)
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
    if not isinstance(data, bytes):
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
    if not isinstance(manifest, Mapping):
        raise FinancingError("authenticated artifact manifest is invalid")
    if manifest.get("artifact_id") != aid:
        raise FinancingError("authenticated artifact identity does not match request")
    if manifest.get("media_type") != "application/json":
        raise FinancingError("financing evidence artifact must be application/json")
    rights = manifest.get("rights")
    if not isinstance(rights, Mapping) or rights.get("storage") is not True:
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
    if isinstance(value, bool):
        raise FinancingError(f"{name} must be epoch milliseconds")
    if isinstance(value, int):
        milliseconds = value
    elif isinstance(value, str) and value.isdigit():
        milliseconds = int(value)
    else:
        raise FinancingError(f"{name} must be epoch milliseconds")
    if milliseconds < 0:
        raise FinancingError(f"{name} must be non-negative")
    seconds, remainder = divmod(milliseconds, 1000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).replace(
        microsecond=remainder * 1000
    )


def _bybit_funding_event_from_exact_response(
    raw: bytes,
    *,
    observation: ProviderResponseObservation,
    artifact_id: str,
    artifact_digest: str,
    row_id: str,
    instrument_versions: Mapping[str, str],
) -> tuple[FinancingEvent, str, str]:
    response = _strict_json_object(raw)
    if response.get("retCode") != 0:
        raise FinancingError("Bybit transaction-log response was not successful")
    result = response.get("result")
    if not isinstance(result, Mapping):
        raise FinancingError("Bybit transaction-log result must be an object")
    rows = result.get("list")
    if not isinstance(rows, list):
        raise FinancingError("Bybit transaction-log result.list must be an array")
    target_id = _text(row_id, name="row_id")
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and row.get("id") == target_id
    ]
    if len(matches) != 1:
        raise FinancingError(
            "Bybit funding row_id must resolve to exactly one transaction-log row"
        )
    row = matches[0]
    if _text(row.get("type"), name="type").upper() != "SETTLEMENT":
        raise FinancingError("Bybit financing row must be a SETTLEMENT record")
    raw_funding = row.get("funding")
    if not isinstance(raw_funding, str) or raw_funding != raw_funding.strip():
        raise FinancingError("Bybit funding must be an exact decimal string")
    try:
        funding = Decimal(raw_funding)
    except InvalidOperation as error:
        raise FinancingError("Bybit funding must be an exact decimal string") from error
    if not funding.is_finite() or funding >= 0:
        raise FinancingError(
            "Bybit funding authority currently supports paid funding charges only"
        )
    currency = _text(row.get("currency"), name="currency").upper()
    symbol = _text(row.get("symbol"), name="symbol")
    if not isinstance(instrument_versions, Mapping):
        raise FinancingError("instrument_versions must be a mapping")
    try:
        instrument_version = _text(
            instrument_versions[symbol],
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
        amount=-funding,
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


class DurableFinancingBook:
    """Journal-backed provider/account financing revision authority."""

    def __init__(
        self,
        store: JournalStore,
        economic_book: DurableProviderEconomicBook,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        if not isinstance(economic_book, DurableProviderEconomicBook):
            raise TypeError("economic_book must be DurableProviderEconomicBook")
        self.store = store
        self.economic_book = economic_book
        self.provider_id = _text(provider_id, name="provider_id").upper()
        self.account_id = _text(account_id, name="account_id")
        self.environment = _environment(environment)
        if economic_book.store is not store:
            raise ValueError("financing and economic authorities must share JournalStore")
        if (
            economic_book.provider_id != self.provider_id
            or economic_book.account_id != self.account_id
            or economic_book.environment != self.environment
        ):
            raise ValueError(
                "financing and economic authorities must share provider/account/environment"
            )

    def _aggregate_id(self, charge_id: str) -> str:
        return _scoped_identity(
            "provider-financing",
            self.provider_id,
            self.account_id,
            self.environment,
            _text(charge_id, name="charge_id"),
        )

    def _events(self, charge_id: str) -> list[dict[str, Any]]:
        return self.store.load_events(
            _FINANCING_AGGREGATE_TYPE,
            self._aggregate_id(charge_id),
        )

    def _book_from_durable_events(
        self,
        charge_id: str,
        events: list[dict[str, Any]],
    ) -> FinancingRevisionBook:
        history: list[FinancingEvent] = []
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
            _charge_scope(
                payload.get("charge_scope_type"),
                payload.get("charge_scope_id"),
                account_id=self.account_id,
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
            candidate_update = candidate_book.record(candidate_event)
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
            history = resulting_history
        return FinancingRevisionBook(history)

    def _replay(self, charge_id: str) -> FinancingRevisionBook:
        return self._book_from_durable_events(charge_id, self._events(charge_id))

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

    def latest(self, charge_id: str) -> FinancingEvent | None:
        normalized = _text(charge_id, name="charge_id")
        return self._replay(normalized).latest(normalized)

    def record_authenticated_artifact(
        self,
        artifact_store: AuthenticatedArtifactStore,
        *,
        artifact_id: str,
        committed_at: str | None = None,
    ) -> DurableFinancingResult:
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
        instrument_versions: Mapping[str, str],
        committed_at: str | None = None,
    ) -> DurableFinancingResult:
        if not isinstance(observation, ProviderResponseObservation):
            raise TypeError("observation must be ProviderResponseObservation")
        observation.require_scope(
            provider_id="BYBIT",
            surface=Surface.ACTIVITIES,
            endpoint="/v5/account/transaction-log",
            account_id=self.account_id,
            environment=self.environment,
        )
        if self.provider_id != "BYBIT":
            raise FinancingError("Bybit financing observation requires BYBIT authority")
        aid = _text(artifact_id, name="artifact_id")
        try:
            manifest, raw = artifact_store.read_authenticated_snapshot(aid)
        except Exception as error:
            raise FinancingError(
                "Bybit financing artifact could not be authenticated"
            ) from error
        if not isinstance(manifest, Mapping) or manifest.get("artifact_id") != aid:
            raise FinancingError("Bybit financing artifact identity mismatch")
        if manifest.get("media_type") != "application/json":
            raise FinancingError("Bybit financing artifact must be application/json")
        rights = manifest.get("rights")
        if not isinstance(rights, Mapping) or rights.get("storage") is not True:
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
    ) -> DurableFinancingResult:
        aggregate_id = self._aggregate_id(event.charge_id)
        durable_events = self._events(event.charge_id)
        book = self._replay(event.charge_id)
        previous_revision_digest = _revision_book_digest(list(book.events))
        update = book.record(event)
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
                )
                previous = prior.latest(event.charge_id)
                previous_final = (
                    previous.amount
                    if previous is not None and previous.kind == "FINAL"
                    else Decimal("0")
                )
                expected_delta = event.amount - previous_final
                if expected_delta != 0:
                    economic_transaction = self._economic_transaction(
                        aggregate_id=aggregate_id,
                        event_id=durable_event_id,
                        event=event,
                        economic_delta=expected_delta,
                    )
                    existing_economics = self.economic_book.prepare_batch_mutation(
                        (economic_transaction,)
                    )
                    if not existing_economics.already_committed:
                        raise FinancingConflict(
                            "durable financing revision is missing its economic posting"
                        )
            else:
                stray = tuple(
                    transaction
                    for transaction in self.economic_book.transactions
                    if transaction.cause_event_id == durable_event_id
                )
                if stray:
                    raise FinancingConflict(
                        "non-economic financing revision has an economic posting"
                    )
            if event.kind == "FINAL" and expected_delta == 0:
                stray = tuple(
                    transaction
                    for transaction in self.economic_book.transactions
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
            )
        finally:
            self.economic_book.refresh()

        return DurableFinancingResult(
            inserted=inserted,
            event=event,
            update=update,
            economic_transaction=economic_transaction,
        )
