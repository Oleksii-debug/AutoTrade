"""Durably bind one admitted financial decision to one exact provider request.

``FinancialRequestBindingMaterial`` defines the immutable provider/wire content
identity required by financial composition.  This module adds only the missing
restart-safe ownership link from that content identity to the canonical
ADMITTED ``AdmissionRecord``.  It does not prepare a request, select a provider,
open a transport, call ``GuardedDispatcher``, or grant send authority.

Only axes that already have a canonical owner are promoted to durable authority
here.  In particular, the exact admission journal cut, provider financial scope
and reservation cut are reconstructed from their owning JournalStore evidence;
they are never accepted as caller labels.  SIMULATION has an explicit diagnostic
account/qualification projection so tests can retain complete content identity.
PAPER/LIVE binding remains fail-closed until issuer-protected account-cut,
provider-qualification and instrument/exposure identities are present in the
accepted risk snapshot.  This prevents a content-only placeholder from becoming
future broker/exchange authority by persistence alone.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
from typing import Mapping

from .authority import AdmissionRecord, AuthorityConflict, AuthorityService
from .bybit_v5 import (
    BybitPreparedSubmission,
    guarded_order_projection,
    require_canonical_bybit_prepared_submission,
)
from .durable_reservations import DurableReservationBook
from .financial_request_binding import (
    FinancialRequestBindingError,
    FinancialRequestBindingMaterial,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_core import ProviderCoreError
from .provider_domain import ProviderDomainError, ProviderFinancialScope
from .risk_policy_authority import journal_store_identity_digest


class DurableFinancialRequestBindingError(RuntimeError):
    """Raised when admitted provider-request identity is missing or inconsistent."""


_AGGREGATE_TYPE = "admitted_financial_request_binding"
_EVENT_TYPE = "AdmittedFinancialRequestBound.v1"
_SCHEMA_VERSION = "1.0.0"

_CANONICAL_APPEND_EVENT = JournalStore.append_event
_CANONICAL_LOAD_EVENTS = JournalStore.load_events
_CANONICAL_CURRENT_SEQUENCE = JournalStore.current_journal_sequence
_CANONICAL_STORE_IDENTITY = JournalStore.store_identity
_CANONICAL_PAYLOAD_DIGEST = payload_digest
_AUTHORITY_TYPE = AuthorityService
_ADMISSION_TYPE = AdmissionRecord
_MATERIAL_TYPE = FinancialRequestBindingMaterial
_BYBIT_PREPARED_TYPE = BybitPreparedSubmission
_CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION = guarded_order_projection
_CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION_CODE = guarded_order_projection.__code__
_CANONICAL_BYBIT_REQUIRE_PREPARED = require_canonical_bybit_prepared_submission
_CANONICAL_BYBIT_REQUIRE_PREPARED_CODE = require_canonical_bybit_prepared_submission.__code__
_BYBIT_PREPARED_ORIGIN_SCHEMA = "bybit-prepared-origin.v1"
_BINDING_PAYLOAD_BASE_FIELDS = frozenset(
    {
        "schema_version",
        "admission_id",
        "binding_id",
        "journal_store_identity_digest",
        "material",
    }
)
_BINDING_PAYLOAD_PRODUCTION_FIELDS = (
    _BINDING_PAYLOAD_BASE_FIELDS | {"provider_request_origin"}
)
_BYBIT_TRIGGER_PROTECTION_KEYS = (
    "triggerDirection",
    "triggerPrice",
    "triggerBy",
    "orderFilter",
    "takeProfit",
    "stopLoss",
    "tpTriggerBy",
    "slTriggerBy",
    "tpslMode",
    "tpLimitPrice",
    "slLimitPrice",
    "tpOrderType",
    "slOrderType",
    "closeOnTrigger",
)


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise DurableFinancialRequestBindingError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise DurableFinancialRequestBindingError(
            f"{name} must be canonical UTC text"
        ) from error
    if parsed.tzinfo is None:
        raise DurableFinancialRequestBindingError(
            f"{name} must include a timezone"
        )
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != text:
        raise DurableFinancialRequestBindingError(
            f"{name} must be canonical UTC text"
        )
    return canonical


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


_CANONICAL_MATERIAL_FROM_PAYLOAD = _material_from_payload
_CANONICAL_MATERIAL_FROM_PAYLOAD_CODE = _material_from_payload.__code__


def _production_request_origin_receipt(
    material: FinancialRequestBindingMaterial,
) -> dict[str, object] | None:
    """Return the durable proof schema required for production request origin.

    The receipt is deliberately deterministic from the already-bound material.
    Its authority comes from the writer gate below: legacy PAPER/LIVE rows that
    predate canonical provider preparation do not contain this receipt and
    therefore fail canonical replay rather than being silently upgraded.
    """

    if type(material) is not _MATERIAL_TYPE:
        raise TypeError("material must be exact FinancialRequestBindingMaterial")
    if material.runtime_environment not in {"PAPER", "LIVE"}:
        return None
    if material.provider_id != "BYBIT":
        raise DurableFinancialRequestBindingError(
            "PAPER/LIVE financial request binding has no canonical prepared-request origin verifier"
        )
    return {
        "schema_version": _BYBIT_PREPARED_ORIGIN_SCHEMA,
        "provider_id": "BYBIT",
        "request_sha256": material.request_sha256,
        "body_sha256": material.body_sha256,
        "query_sha256": material.query_sha256,
        "trigger_protection_digest": material.trigger_protection_digest,
    }


def _require_bybit_prepared_request_origin(
    material: FinancialRequestBindingMaterial,
    prepared_request: object,
) -> dict[str, object]:
    """Prove production Bybit binding content came from canonical adapter prep."""

    if type(material) is not _MATERIAL_TYPE:
        raise TypeError("material must be exact FinancialRequestBindingMaterial")
    if type(prepared_request) is not _BYBIT_PREPARED_TYPE:
        raise DurableFinancialRequestBindingError(
            "BYBIT PAPER/LIVE binding requires exact canonical BybitPreparedSubmission"
        )
    if BybitPreparedSubmission is not _BYBIT_PREPARED_TYPE:
        raise DurableFinancialRequestBindingError(
            "Bybit prepared-request type authority changed"
        )
    if (
        require_canonical_bybit_prepared_submission
        is not _CANONICAL_BYBIT_REQUIRE_PREPARED
        or getattr(
            _CANONICAL_BYBIT_REQUIRE_PREPARED,
            "__code__",
            None,
        )
        is not _CANONICAL_BYBIT_REQUIRE_PREPARED_CODE
    ):
        raise DurableFinancialRequestBindingError(
            "Bybit prepared-request provenance authority changed"
        )
    try:
        _CANONICAL_BYBIT_REQUIRE_PREPARED(prepared_request)
    except (ProviderCoreError, TypeError, ValueError) as error:
        raise DurableFinancialRequestBindingError(
            "Bybit prepared request lacks canonical issuance provenance"
        ) from error
    if (
        guarded_order_projection is not _CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION
        or getattr(
            _CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION,
            "__code__",
            None,
        )
        is not _CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION_CODE
    ):
        raise DurableFinancialRequestBindingError(
            "Bybit prepared-request projection authority changed"
        )

    projected = _CANONICAL_BYBIT_GUARDED_ORDER_PROJECTION(prepared_request)
    request = dict(projected)
    body = request.get("body")
    if type(body) is not dict:
        raise DurableFinancialRequestBindingError(
            "canonical Bybit prepared request body is unavailable"
        )

    actual_body_digest = _CANONICAL_PAYLOAD_DIGEST(body)
    if request.get("body_sha256") != actual_body_digest:
        raise DurableFinancialRequestBindingError(
            "canonical Bybit prepared request body digest is inconsistent"
        )
    if actual_body_digest != material.body_sha256:
        raise DurableFinancialRequestBindingError(
            "Bybit prepared body differs from financial binding"
        )
    if _CANONICAL_PAYLOAD_DIGEST(request) != material.request_sha256:
        raise DurableFinancialRequestBindingError(
            "Bybit prepared request differs from financial binding"
        )
    if material.query_sha256 != _CANONICAL_PAYLOAD_DIGEST({}):
        raise DurableFinancialRequestBindingError(
            "Bybit place-order financial binding must carry canonical empty query"
        )

    protection = {
        key: body[key]
        for key in _BYBIT_TRIGGER_PROTECTION_KEYS
        if key in body
    }
    if (
        material.trigger_protection_digest
        != _CANONICAL_PAYLOAD_DIGEST(protection)
    ):
        raise DurableFinancialRequestBindingError(
            "Bybit trigger/protection/close semantics differ from financial binding"
        )

    projection_checks = (
        ("endpoint", request.get("endpoint"), material.endpoint),
        ("account_id", request.get("account_id"), material.account_id),
        (
            "runtime_environment",
            request.get("environment"),
            material.runtime_environment,
        ),
        (
            "provider_environment",
            request.get("provider_environment"),
            material.provider_environment,
        ),
        (
            "capability_snapshot_id",
            request.get("capability_snapshot_id"),
            material.capability_snapshot_id,
        ),
    )
    for name, actual, expected in projection_checks:
        if actual != expected:
            raise DurableFinancialRequestBindingError(
                f"Bybit prepared {name} differs from financial binding"
            )

    side = {"Buy": "BUY", "Sell": "SELL"}.get(body.get("side"))
    order_type = {"Market": "MARKET", "Limit": "LIMIT"}.get(
        body.get("orderType")
    )
    time_in_force = {
        "GTC": "GTC",
        "IOC": "IOC",
        "FOK": "FOK",
        "PostOnly": "POST_ONLY",
    }.get(body.get("timeInForce"))
    if side is None or order_type is None or time_in_force is None:
        raise DurableFinancialRequestBindingError(
            "canonical Bybit prepared order semantics are unsupported"
        )
    reduce_only = body.get("reduceOnly", False)
    if type(reduce_only) is not bool:
        raise DurableFinancialRequestBindingError(
            "canonical Bybit prepared reduce-only semantics are invalid"
        )
    price = body.get("price")
    if price is not None and type(price) is not str:
        raise DurableFinancialRequestBindingError(
            "canonical Bybit prepared price semantics are invalid"
        )
    semantic_checks = (
        ("client_order_id", body.get("orderLinkId"), material.client_order_id),
        ("side", side, material.side),
        ("quantity", body.get("qty"), material.quantity),
        ("price", price, material.price),
        ("order_type", order_type, material.order_type),
        ("time_in_force", time_in_force, material.time_in_force),
        ("reduce_only", reduce_only, material.reduce_only),
    )
    for name, actual, expected in semantic_checks:
        if actual != expected:
            raise DurableFinancialRequestBindingError(
                f"Bybit prepared {name} differs from financial binding"
            )

    receipt = _production_request_origin_receipt(material)
    if receipt is None:
        raise DurableFinancialRequestBindingError(
            "Bybit prepared request origin receipt is unavailable"
        )
    return receipt


_CANONICAL_PRODUCTION_REQUEST_ORIGIN_RECEIPT = _production_request_origin_receipt
_CANONICAL_PRODUCTION_REQUEST_ORIGIN_RECEIPT_CODE = (
    _production_request_origin_receipt.__code__
)


def _require_prepared_request_origin(
    material: FinancialRequestBindingMaterial,
    prepared_request: object | None,
) -> dict[str, object] | None:
    if material.runtime_environment not in {"PAPER", "LIVE"}:
        if prepared_request is not None:
            raise DurableFinancialRequestBindingError(
                "prepared_request is only accepted for PAPER/LIVE financial binding"
            )
        return None
    if material.provider_id != "BYBIT":
        raise DurableFinancialRequestBindingError(
            "PAPER/LIVE financial request binding has no canonical prepared-request origin verifier"
        )
    return _require_bybit_prepared_request_origin(material, prepared_request)


_CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN = _require_prepared_request_origin
_CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN_CODE = (
    _require_prepared_request_origin.__code__
)


def _binding_payload(
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
    store_identity_digest: str,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "admission_id": admission_id,
        "binding_id": material.binding_id,
        "journal_store_identity_digest": store_identity_digest,
        "material": material.payload(),
    }
    origin = _production_request_origin_receipt(material)
    if origin is not None:
        payload["provider_request_origin"] = origin
    return payload


_CANONICAL_BINDING_PAYLOAD = _binding_payload
_CANONICAL_BINDING_PAYLOAD_CODE = _binding_payload.__code__


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


def _reservation_scope_digest(
    *,
    environment: str,
    account_id: str,
    scope_id: str,
) -> str:
    return _CANONICAL_PAYLOAD_DIGEST(
        {
            "schema_version": "reservation-scope.v1",
            "environment": environment,
            "account_id": account_id,
            "scope_id": scope_id,
        }
    )


def _reservation_cut_digest(
    *,
    environment: str,
    account_id: str,
    events: list[dict[str, object]],
) -> str:
    version = 0 if not events else int(events[-1]["aggregate_version"])
    return _CANONICAL_PAYLOAD_DIGEST(
        {
            "environment": environment,
            "account_id": account_id,
            "version": version,
            "events": [
                {
                    "event_id": event["event_id"],
                    "aggregate_version": str(event["aggregate_version"]),
                    "payload_hash": event["payload_hash"],
                }
                for event in events
            ],
        }
    )


def _reservation_authority(
    store: JournalStore,
    record: AdmissionRecord,
    snapshot: Mapping[str, object],
) -> tuple[str, int, str]:
    """Reconstruct the exact reservation cut created with this admission."""

    try:
        book = DurableReservationBook(
            store,
            environment=record.environment,
            account_id=record.account_id,
        )
    except (TypeError, ValueError, RuntimeError) as error:
        raise DurableFinancialRequestBindingError(
            "admitted reservation authority cannot be reconstructed"
        ) from error
    events = _CANONICAL_LOAD_EVENTS(store, "reservation_book", book.scope_id)
    matches: list[tuple[int, dict[str, object]]] = []
    for index, event in enumerate(events):
        payload = event.get("payload")
        if type(payload) is not dict or payload.get("operation") != "RESERVE":
            continue
        request = payload.get("request")
        if (
            type(request) is dict
            and request.get("reservation_id") == record.reservation_id
            and request.get("intent_id") == record.intent_id
        ):
            matches.append((index, event))
    if len(matches) != 1:
        raise DurableFinancialRequestBindingError(
            "admitted reservation creation event is missing or ambiguous"
        )
    index, event = matches[0]
    aggregate_version = event.get("aggregate_version")
    if type(aggregate_version) is not int or aggregate_version < 1:
        raise DurableFinancialRequestBindingError(
            "admitted reservation aggregate version is invalid"
        )
    prefix_before = events[:index]
    prefix_after = events[: index + 1]
    pre_version = 0 if not prefix_before else int(prefix_before[-1]["aggregate_version"])
    pre_digest = _reservation_cut_digest(
        environment=record.environment,
        account_id=record.account_id,
        events=prefix_before,
    )
    snapshot_version = snapshot.get("reservation_version")
    snapshot_digest = snapshot.get("reservation_state_digest")
    if (
        snapshot_version != pre_version
        or type(snapshot_digest) is not str
        or snapshot_digest.removeprefix("sha256:")
        != pre_digest.removeprefix("sha256:")
        or aggregate_version != pre_version + 1
    ):
        raise DurableFinancialRequestBindingError(
            "reservation creation does not succeed the admitted risk cut"
        )
    return (
        _reservation_scope_digest(
            environment=record.environment,
            account_id=record.account_id,
            scope_id=book.scope_id,
        ),
        aggregate_version,
        _reservation_cut_digest(
            environment=record.environment,
            account_id=record.account_id,
            events=prefix_after,
        ),
    )


def _admission_journal_sequence(
    store: JournalStore,
    admission_id: str,
) -> int:
    matches = []
    for event in _CANONICAL_LOAD_EVENTS(store, "authority_state", "canonical"):
        payload = event.get("payload")
        if (
            event.get("event_type") == "AuthorityAdmissionRecorded"
            and type(payload) is dict
            and payload.get("admission_id") == admission_id
        ):
            matches.append(event)
    if len(matches) != 1:
        raise DurableFinancialRequestBindingError(
            "durable admission event is missing or ambiguous"
        )
    sequence = matches[0].get("journal_sequence")
    if type(sequence) is not int or sequence <= 0:
        raise DurableFinancialRequestBindingError(
            "durable admission event lacks journal sequence"
        )
    return sequence


def _simulation_account_cut(
    availability: Mapping[str, object],
) -> tuple[str, str, int]:
    checkpoint_event_id = _text(
        availability.get("checkpoint_event_id"),
        name="checkpoint_event_id",
    )
    checkpoint_payload_hash = _text(
        availability.get("checkpoint_payload_hash"),
        name="checkpoint_payload_hash",
    )
    head_sequence = availability.get("scope_latest_checkpoint_journal_sequence")
    if type(head_sequence) is not int or head_sequence <= 0:
        raise DurableFinancialRequestBindingError(
            "simulation account cut lacks durable scope head"
        )
    digest = sha256(
        canonical_json(
            {
                "schema_version": "simulation-account-cut.v1",
                "checkpoint_event_id": checkpoint_event_id,
                "checkpoint_payload_hash": checkpoint_payload_hash,
                "scope_latest_checkpoint_journal_sequence": head_sequence,
            }
        ).encode("utf-8")
    ).hexdigest()
    return (
        "provider-account-cut:sha256:" + digest,
        checkpoint_payload_hash,
        head_sequence,
    )


def _simulation_qualification_identity(
    scope: ProviderFinancialScope,
    capability_snapshot_id: str,
) -> str:
    digest = sha256(
        canonical_json(
            {
                "schema_version": "simulation-unqualified-provider.v1",
                "provider_scope_digest": scope.content_digest,
                "capability_snapshot_id": capability_snapshot_id,
            }
        ).encode("utf-8")
    ).hexdigest()
    return "provider-qualification:sha256:" + digest


def _require_admitted_price_semantics(
    snapshot: Mapping[str, object],
    material: FinancialRequestBindingMaterial,
) -> str:
    """Require production price semantics to come from admitted durable authority.

    PAPER/LIVE material may carry the digest only as an equality target.  The
    authoritative value must already exist in the accepted risk snapshot,
    where the product-owned risk/instrument composition can cross-bind the
    canonical InstrumentVersion/provider price-rule owner.  This module does
    not mint a fallback digest from provider wire fields.

    SIMULATION keeps the material-carried value as diagnostic identity only.
    """

    if type(material) is not _MATERIAL_TYPE:
        raise TypeError("material must be exact FinancialRequestBindingMaterial")
    if material.runtime_environment not in {"PAPER", "LIVE"}:
        return material.price_semantics_digest

    admitted = snapshot.get("price_semantics_digest")
    if type(admitted) is not str:
        raise DurableFinancialRequestBindingError(
            "PAPER/LIVE request binding requires authoritative price-semantics identity"
        )
    if admitted != material.price_semantics_digest:
        raise DurableFinancialRequestBindingError(
            "financial request price semantics differ from admitted authority"
        )
    return admitted


_CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS = _require_admitted_price_semantics
_CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS_CODE = (
    _require_admitted_price_semantics.__code__
)


def _validate_material_against_admission(
    store: JournalStore,
    authority: AuthorityService,
    *,
    admission_id: str,
    material: FinancialRequestBindingMaterial,
    current_journal_sequence: int,
) -> AdmissionRecord:
    if type(store) is not JournalStore:
        raise DurableFinancialRequestBindingError(
            "financial request binding requires exact JournalStore authority"
        )
    if type(authority) is not _AUTHORITY_TYPE:
        raise DurableFinancialRequestBindingError(
            "financial request binding requires exact AuthorityService reconstruction"
        )
    if type(current_journal_sequence) is not int or current_journal_sequence < 0:
        raise DurableFinancialRequestBindingError(
            "current financial request binding journal cut is invalid"
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
    if (
        _require_admitted_price_semantics
        is not _CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS
        or getattr(
            _CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS,
            "__code__",
            None,
        )
        is not _CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS_CODE
    ):
        raise DurableFinancialRequestBindingError(
            "price-semantics authority executable changed"
        )
    _CANONICAL_REQUIRE_ADMITTED_PRICE_SEMANTICS(snapshot, material)
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
    admission_sequence = _admission_journal_sequence(store, admission_id)
    if admission_sequence > current_journal_sequence:
        raise DurableFinancialRequestBindingError(
            "durable admission sequence is beyond current journal cut"
        )

    try:
        provider_scope = ProviderFinancialScope(
            provider_id=material.provider_id,
            runtime_environment=material.runtime_environment,
            provider_environment=material.provider_environment,
            entity_policy_id=material.entity_policy_id,
        )
    except ProviderDomainError as error:
        raise DurableFinancialRequestBindingError(
            "provider financial scope is invalid"
        ) from error

    reservation_scope_digest, reservation_version, reservation_state_digest = (
        _reservation_authority(store, record, snapshot)
    )

    availability = risk_payload.get("reservation_availability_evidence")
    if not isinstance(availability, Mapping):
        raise DurableFinancialRequestBindingError(
            "admitted reservation availability evidence is unavailable"
        )

    if record.environment in {"PAPER", "LIVE"}:
        production_fields = (
            snapshot.get("account_cut_id"),
            snapshot.get("account_cut_digest"),
            snapshot.get("account_head_journal_sequence"),
            snapshot.get("qualification_identity_digest"),
            snapshot.get("quantity_unit"),
            snapshot.get("equivalent_exposure_digest"),
        )
        if any(value is None for value in production_fields):
            raise DurableFinancialRequestBindingError(
                "PAPER/LIVE request binding requires accepted account/Q/instrument authority"
            )
        expected_account_cut_id = production_fields[0]
        expected_account_cut_digest = production_fields[1]
        expected_account_head_sequence = production_fields[2]
        expected_qualification = production_fields[3]
        expected_quantity_unit = production_fields[4]
        expected_equivalent_exposure = production_fields[5]
        if provider_environment is None or entity_policy_id is None:
            raise DurableFinancialRequestBindingError(
                "PAPER/LIVE request binding requires exact provider domain authority"
            )
    else:
        (
            expected_account_cut_id,
            expected_account_cut_digest,
            expected_account_head_sequence,
        ) = _simulation_account_cut(availability)
        expected_qualification = _simulation_qualification_identity(
            provider_scope,
            record.capability_snapshot_id,
        )
        expected_quantity_unit = material.quantity_unit
        expected_equivalent_exposure = material.equivalent_exposure_digest

    mismatches: list[str] = []
    checks = (
        ("risk_snapshot_id", material.risk_snapshot_id, snapshot.get("snapshot_id")),
        ("risk_decision_id", material.risk_decision_id, record.risk_decision_id),
        ("admitted_journal_sequence_cut", material.admitted_journal_sequence_cut, admission_sequence),
        ("account_cut_id", material.account_cut_id, expected_account_cut_id),
        ("account_cut_digest", material.account_cut_digest, expected_account_cut_digest),
        (
            "account_head_journal_sequence",
            material.account_head_journal_sequence,
            expected_account_head_sequence,
        ),
        ("reservation_id", material.reservation_id, record.reservation_id),
        (
            "reservation_scope_digest",
            material.reservation_scope_digest,
            reservation_scope_digest,
        ),
        ("reservation_version", material.reservation_version, reservation_version),
        (
            "reservation_state_digest",
            material.reservation_state_digest,
            reservation_state_digest,
        ),
        (
            "provider_scope_digest",
            material.provider_scope_digest,
            provider_scope.content_digest,
        ),
        (
            "capability_snapshot_id",
            material.capability_snapshot_id,
            record.capability_snapshot_id,
        ),
        (
            "qualification_identity_digest",
            material.qualification_identity_digest,
            expected_qualification,
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
        ("quantity_unit", material.quantity_unit, expected_quantity_unit),
        (
            "equivalent_exposure_digest",
            material.equivalent_exposure_digest,
            expected_equivalent_exposure,
        ),
    )
    for name, actual, expected in checks:
        if actual != expected:
            mismatches.append(name)
    if provider_environment is not None and material.provider_environment != provider_environment:
        mismatches.append("provider_environment")
    if entity_policy_id is not None and material.entity_policy_id != entity_policy_id:
        mismatches.append("entity_policy_id")
    if mismatches:
        raise DurableFinancialRequestBindingError(
            "provider request material differs from durable admitted authority: "
            + ", ".join(sorted(mismatches))
        )
    return record


_CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION = _validate_material_against_admission
_CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION_CODE = (
    _validate_material_against_admission.__code__
)


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

    def _current_sequence(self) -> int:
        try:
            value = _CANONICAL_CURRENT_SEQUENCE(self._require_store())
        except (TypeError, ValueError, RuntimeError) as error:
            raise DurableFinancialRequestBindingError(
                "financial request binding current journal cut is unavailable"
            ) from error
        if type(value) is not int or value < 0:
            raise DurableFinancialRequestBindingError(
                "financial request binding current journal cut is invalid"
            )
        return value

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
        material = _CANONICAL_MATERIAL_FROM_PAYLOAD(payload.get("material"))
        expected_origin = _CANONICAL_PRODUCTION_REQUEST_ORIGIN_RECEIPT(material)
        expected_fields = (
            _BINDING_PAYLOAD_PRODUCTION_FIELDS
            if expected_origin is not None
            else _BINDING_PAYLOAD_BASE_FIELDS
        )
        if (
            frozenset(payload) != expected_fields
            or payload.get("schema_version") != _SCHEMA_VERSION
            or payload.get("admission_id") != aid
            or payload.get("journal_store_identity_digest")
            != self._store_identity_digest
            or (
                expected_origin is not None
                and payload.get("provider_request_origin") != expected_origin
            )
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding payload is invalid"
            )
        return payload

    def resolve(self, admission_id: str) -> FinancialRequestBindingMaterial:
        aid = _text(admission_id, name="admission_id")
        payload = self._load_payload(aid)
        material = _CANONICAL_MATERIAL_FROM_PAYLOAD(payload.get("material"))
        if payload.get("binding_id") != material.binding_id:
            raise DurableFinancialRequestBindingError(
                "admitted financial request binding id does not match material"
            )
        current_validator = _validate_material_against_admission
        if (
            current_validator is not _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION
            or getattr(
                _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION,
                "__code__",
                None,
            )
            is not _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION_CODE
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial validator executable changed"
            )
        _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION(
            self._require_store(),
            self._authority(),
            admission_id=aid,
            material=material,
            current_journal_sequence=self._current_sequence(),
        )
        if _CANONICAL_BINDING_PAYLOAD(
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
        bound_at: str,
        prepared_request: object | None = None,
    ) -> FinancialRequestBindingMaterial:
        aid = _text(admission_id, name="admission_id")
        if type(material) is not _MATERIAL_TYPE:
            raise TypeError("material must be exact FinancialRequestBindingMaterial")
        committed_at = _utc_text(bound_at, name="bound_at")
        current_origin_gate = _require_prepared_request_origin
        if (
            current_origin_gate is not _CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN
            or getattr(
                _CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN,
                "__code__",
                None,
            )
            is not _CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN_CODE
        ):
            raise DurableFinancialRequestBindingError(
                "prepared-request origin executable authority changed"
            )
        _CANONICAL_REQUIRE_PREPARED_REQUEST_ORIGIN(
            material,
            prepared_request,
        )
        store = self._require_store()
        current_validator = _validate_material_against_admission
        if (
            current_validator is not _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION
            or getattr(
                _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION,
                "__code__",
                None,
            )
            is not _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION_CODE
        ):
            raise DurableFinancialRequestBindingError(
                "admitted financial validator executable changed"
            )
        record = _CANONICAL_VALIDATE_MATERIAL_AGAINST_ADMISSION(
            store,
            self._authority(),
            admission_id=aid,
            material=material,
            current_journal_sequence=self._current_sequence(),
        )
        payload = _CANONICAL_BINDING_PAYLOAD(
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
            "committed_at": committed_at,
        }
        try:
            _CANONICAL_APPEND_EVENT(store, envelope)
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
