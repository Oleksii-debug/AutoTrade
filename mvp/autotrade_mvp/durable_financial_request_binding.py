"""Durably bind one admitted financial decision to one exact provider request.

``FinancialRequestBindingMaterial`` already defines the immutable provider/wire
content identity required by financial composition.  This module adds only the
missing restart-safe ownership link from that content identity to the canonical
ADMITTED ``AdmissionRecord``.  It does not prepare a request, select a provider,
open a transport, call ``GuardedDispatcher``, or grant send authority.

The binding is one immutable journal event keyed by ``admission_id``.  Binding
reconstructs ``AuthorityService`` from the same exact JournalStore, revalidates
the historical financial writer evidence, and requires every authority axis
shared by the admission and prepared request to agree.  Provider-only execution
semantics (order type, TIF, wire hashes, price semantics, endpoint, and similar)
remain sealed content identity rather than values guessed from ``RiskIntent``.
"""

from __future__ import annotations

from typing import Mapping

from .authority import AdmissionRecord, AuthorityConflict, AuthorityService
from .financial_request_binding import (
    FinancialRequestBindingError,
    FinancialRequestBindingMaterial,
)
from .persistence import JournalStore, payload_digest
from .risk_policy_authority import (
    journal_store_identity_digest,
)


class DurableFinancialRequestBindingError(RuntimeError):
    """Raised when admitted provider-request identity is missing or inconsistent."""


_AGGREGATE_TYPE = "admitted_financial_request_binding"
_EVENT_TYPE = "AdmittedFinancialRequestBound.v1"
_SCHEMA_VERSION = "1.0.0"

_CANONICAL_APPEND_EVENT = JournalStore.append_event
_CANONICAL_LOAD_EVENTS = JournalStore.load_events
_CANONICAL_STORE_IDENTITY = JournalStore.store_identity
_CANONICAL_PAYLOAD_DIGEST = payload_digest
_AUTHORITY_TYPE = AuthorityService
_ADMISSION_TYPE = AdmissionRecord
_MATERIAL_TYPE = FinancialRequestBindingMaterial


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DurableFinancialRequestBindingError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _store_identity_digest(store: JournalStore) -> str:
    if type(store) is not JournalStore:
        raise DurableFinancialRequestBindingError(
            "financial request binding requires exact JournalStore authority"
        )
    try:
        identity = _CANONICAL_STORE_IDENTITY.__get__(store, JournalStore)
        return journal_store_identity_digest(identity)
    except (TypeError, ValueError, RuntimeError) as error:
        raise DurableFinancialRequestBindingError(
            "financial request binding JournalStore authority is unavailable"
        ) from error


def _material_from_payload(value: object) -> FinancialRequestBindingMaterial:
    if type(value) is not dict:
        raise DurableFinancialRequestBindingError(
            "durable financial request material must be an exact object"
        )
    material_payload = dict(value)
    if material_payload.pop("schema_version", None) != _SCHEMA_VERSION:
        raise DurableFinancialRequestBindingError(
            "durable financial request material schema is invalid"
        )
    try:
        material = _MATERIAL_TYPE(**material_payload)
    except (FinancialRequestBindingError, TypeError, ValueError) as error:
        raise DurableFinancialRequestBindingError(
            "durable financial request material is invalid"
        ) from error
    if material.payload() != value:
        raise DurableFinancialRequestBindingError(
            "durable financial request material is non-canonical"
        )
    return material


def _binding_payload(
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
    store_identity_digest: str,
) -> dict[str, object]:
    return {
        "schema_version": _SCHEMA_VERSION,
        "admission_id": admission_id,
        "binding_id": material.binding_id,
        "journal_store_identity_digest": store_identity_digest,
        "material": material.payload(),
    }


def _risk_intent_axis(
    risk_payload: Mapping[str, object],
    *,
    name: str,
):
    intent = risk_payload.get("risk_intent")
    if type(intent) is not dict:
        raise DurableFinancialRequestBindingError(
            "admitted financial risk intent is unavailable"
        )
    return intent.get(name)


def _validate_material_against_admission(
    authority: AuthorityService,
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
) -> AdmissionRecord:
    if type(authority) is not _AUTHORITY_TYPE:
        raise DurableFinancialRequestBindingError(
            "financial request binding requires exact AuthorityService reconstruction"
        )
    record = authority._admissions.get(admission_id)
    if type(record) is not _ADMISSION_TYPE:
        raise DurableFinancialRequestBindingError(
            "financial request binding admission is missing or invalid"
        )
    if record.outcome != "ADMITTED":
        raise DurableFinancialRequestBindingError(
            "only an ADMITTED financial record may own provider request content"
        )
    if (
        record.risk_decision_id is None
        or record.reservation_id is None
        or record.capability_snapshot_id is None
        or record.policy_version is None
    ):
        raise DurableFinancialRequestBindingError(
            "admitted financial record lacks complete provider-request authority evidence"
        )
    policy = authority._policies.get(record.policy_id)
    if policy is None:
        raise DurableFinancialRequestBindingError(
            "admitted financial record AuthorityPolicy is unavailable"
        )
    try:
        risk_payload = authority._validate_historical_financial_retry_evidence(
            record,
            policy,
        )
    except (AuthorityConflict, KeyError, TypeError, ValueError) as error:
        raise DurableFinancialRequestBindingError(
            "admitted financial writer evidence cannot be revalidated"
        ) from error

    snapshot = risk_payload.get("authoritative_risk_snapshot")
    if not isinstance(snapshot, Mapping):
        raise DurableFinancialRequestBindingError(
            "admitted authoritative risk snapshot is unavailable"
        )
    provider_id = snapshot.get("provider_id")
    provider_environment = snapshot.get("provider_environment")
    entity_policy_id = snapshot.get("entity_policy_id")
    if type(provider_id) is not str:
        raise DurableFinancialRequestBindingError(
            "admitted provider identity is unavailable"
        )
    if provider_environment is not None and type(provider_environment) is not str:
        raise DurableFinancialRequestBindingError(
            "admitted provider environment is malformed"
        )
    if entity_policy_id is not None and type(entity_policy_id) is not str:
        raise DurableFinancialRequestBindingError(
            "admitted provider entity policy identity is malformed"
        )

    durable_side = _risk_intent_axis(risk_payload, name="side")
    durable_quantity = _risk_intent_axis(risk_payload, name="quantity")
    durable_reduce_only = _risk_intent_axis(risk_payload, name="reduce_only")
    risk_event_cut = risk_payload.get("journal_sequence_cut")
    if type(risk_event_cut) is not int or risk_event_cut < 0:
        raise DurableFinancialRequestBindingError(
            "admitted financial journal cut is unavailable"
        )

    mismatches: list[str] = []
    checks = (
        ("risk_snapshot_id", material.risk_snapshot_id, snapshot.get("snapshot_id")),
        ("risk_decision_id", material.risk_decision_id, record.risk_decision_id),
        ("reservation_id", material.reservation_id, record.reservation_id),
        (
            "capability_snapshot_id",
            material.capability_snapshot_id,
            record.capability_snapshot_id,
        ),
        ("account_id", material.account_id, record.account_id),
        ("runtime_environment", material.runtime_environment, record.environment),
        (
            "instrument_id",
            material.instrument_id,
            record.instrument_version.instrument_id,
        ),
        (
            "instrument_version",
            material.instrument_version,
            record.instrument_version.version,
        ),
        ("provider_id", material.provider_id, provider_id),
        ("side", material.side, durable_side),
        ("quantity", material.quantity, durable_quantity),
        ("reduce_only", material.reduce_only, durable_reduce_only),
    )
    for name, actual, expected in checks:
        if actual != expected:
            mismatches.append(name)
    if (
        provider_environment is not None
        and material.provider_environment != provider_environment
    ):
        mismatches.append("provider_environment")
    if entity_policy_id is not None and material.entity_policy_id != entity_policy_id:
        mismatches.append("entity_policy_id")
    if material.admitted_journal_sequence_cut < risk_event_cut:
        mismatches.append("admitted_journal_sequence_cut")
    if mismatches:
        raise DurableFinancialRequestBindingError(
            "provider request material differs from durable admitted authority: "
            + ", ".join(sorted(mismatches))
        )
    return record


class DurableFinancialRequestBindingRegistry:
    """One immutable prepared-request content identity per ADMITTED admission."""

    def __init__(self, store: JournalStore) -> None:
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        self._store = store
        self._store_identity_digest = _store_identity_digest(store)

    def _require_store(self) -> JournalStore:
        if type(self._store) is not JournalStore:
            raise DurableFinancialRequestBindingError(
                "financial request binding JournalStore authority changed"
            )
        if _store_identity_digest(self._store) != self._store_identity_digest:
            raise DurableFinancialRequestBindingError(
                "financial request binding JournalStore generation changed"
            )
        return self._store

    def _authority(self) -> AuthorityService:
        try:
            return _AUTHORITY_TYPE(self._require_store())
        except (AuthorityConflict, TypeError, ValueError, RuntimeError) as error:
            raise DurableFinancialRequestBindingError(
                "canonical financial authority cannot be reconstructed"
            ) from error

    def _load_payload(self, admission_id: str) -> dict[str, object]:
        aid = _text(admission_id, name="admission_id")
        events = _CANONICAL_LOAD_EVENTS(
            self._require_store(),
            _AGGREGATE_TYPE,
            aid,
        )
        if len(events) != 1:
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding is missing or ambiguous"
            )
        event = events[0]
        if (
            type(event) is not dict
            or event.get("event_id") != f"admitted-financial-request:{aid}"
            or event.get("event_type") != _EVENT_TYPE
            or event.get("aggregate_type") != _AGGREGATE_TYPE
            or event.get("aggregate_id") != aid
            or event.get("aggregate_version") != 1
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding chronology is invalid"
            )
        payload = event.get("payload")
        if (
            type(payload) is not dict
            or event.get("payload_hash") != _CANONICAL_PAYLOAD_DIGEST(payload)
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding payload hash mismatch"
            )
        expected_fields = {
            "schema_version",
            "admission_id",
            "binding_id",
            "journal_store_identity_digest",
            "material",
        }
        if (
            set(payload) != expected_fields
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("admission_id") != aid
            or payload.get("journal_store_identity_digest")
            != self._store_identity_digest
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding payload is invalid"
            )
        return payload

    def resolve(self, admission_id: str) -> FinancialRequestBindingMaterial:
        aid = _text(admission_id, name="admission_id")
        payload = self._load_payload(aid)
        material = _material_from_payload(payload.get("material"))
        if payload.get("binding_id") != material.binding_id:
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding id does not match material"
            )
        _validate_material_against_admission(
            self._authority(),
            admission_id=aid,
            material=material,
        )
        if _binding_payload(
            admission_id=aid,
            material=material,
            store_identity_digest=self._store_identity_digest,
        ) != payload:
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding is non-canonical"
            )
        return material

    def bind(
        self,
        *,
        admission_id: str,
        material: FinancialRequestBindingMaterial,
    ) -> FinancialRequestBindingMaterial:
        aid = _text(admission_id, name="admission_id")
        if type(material) is not _MATERIAL_TYPE:
            raise TypeError("material must be exact FinancialRequestBindingMaterial")
        record = _validate_material_against_admission(
            self._authority(),
            admission_id=aid,
            material=material,
        )
        payload = _binding_payload(
            admission_id=aid,
            material=material,
            store_identity_digest=self._store_identity_digest,
        )
        envelope = {
            "event_id": f"admitted-financial-request:{aid}",
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aid,
            "aggregate_version": "1",
            "payload": payload,
            "payload_hash": _CANONICAL_PAYLOAD_DIGEST(payload),
            "committed_at": record.admitted_at,
        }
        try:
            _CANONICAL_APPEND_EVENT(self._require_store(), envelope)
        except ValueError as error:
            try:
                existing = self.resolve(aid)
            except DurableFinancialRequestBindingError:
                raise DurableFinancialRequestBindingError(
                    "admitted financial request binding conflicts with durable state"
                ) from error
            if existing != material:
                raise DurableFinancialRequestBindingError(
                    "admitted financial request binding conflicts with durable state"
                ) from error
            return existing
        return self.resolve(aid)
