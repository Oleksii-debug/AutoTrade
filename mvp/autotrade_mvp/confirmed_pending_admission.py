"""Fresh canonical financial admission for one server-confirmed pending intent.

This is the continuation bridge from durable operator confirmation back into the
existing ``AuthorityService.admit`` financial writer.  It never asks the client
to replay quantity, price, notional, RiskPolicy, reservation requirements,
account, environment, policy identity, confirmation identity, or source
``risk_reducing`` semantics.

The caller supplies only product-owned runtime authority needed for a *fresh*
financial decision: an exact AuthorityService configured with the authoritative
risk issuer, the current capability snapshot identity, current reconciliation
checkpoint/provider identity, and the exact DurableReservationBook.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Mapping

from .authority import (
    AdmissionRecord,
    AuthorityConflict,
    AuthorityService,
    InstrumentVersionIdentity,
    RiskAuthorityRequest,
    _authority_service_store,
    _authority_store_call,
)
from .confirmed_pending_intent import (
    ConfirmedPendingIntentResolutionError,
    resolve_confirmed_pending_intent,
)
from .confirmed_pending_source import (
    ConfirmedPendingSourceError,
    resolve_confirmed_pending_source_admission,
)
from .durable_reservations import DurableReservationBook
from .exact_decimal import canonical_decimal_text, parse_bounded_exact_decimal
from .persistence import JournalStore
from .reconciliation_journal import load_account_resource_availability_evidence


class ConfirmedPendingAdmissionError(RuntimeError):
    """Raised when confirmed intent cannot enter a fresh canonical admission."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfirmedPendingAdmissionError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _utc(value: object, *, name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise ConfirmedPendingAdmissionError(
            f"{name} must use an exact datetime with built-in timezone"
        )
    if datetime.utcoffset(value) is None:
        raise ConfirmedPendingAdmissionError(f"{name} must be timezone-aware")
    return datetime.astimezone(value, timezone.utc)


def _utc_text(value: object, *, name: str) -> str:
    return _utc(value, name=name).isoformat().replace("+00:00", "Z")


def _attempt_identity(material: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            material,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _existing_consumption(authority: AuthorityService, confirmed) -> AdmissionRecord | None:
    matches = [
        record
        for record in authority._admissions.values()
        if record.confirmation_id == confirmed.confirmation_id
    ]
    if len(matches) > 1:
        raise ConfirmedPendingAdmissionError(
            "financial confirmation was consumed by multiple admissions"
        )
    if not matches:
        return None
    record = matches[0]
    pending = confirmed.pending_intent
    if (
        record.outcome != "ADMITTED"
        or record.policy_id != pending.policy_id
        or record.intent_hash != pending.intent_hash
        or record.account_id != pending.account_id
        or record.environment != pending.environment
        or record.instrument_version.instrument_id != pending.instrument_id
        or record.instrument_version.version != pending.instrument_version
        or record.action != pending.authority_action
        or record.notional != pending.notional
        or record.policy_version != pending.authority_policy_version
    ):
        raise ConfirmedPendingAdmissionError(
            "durable confirmation consumption differs from confirmed pending intent"
        )
    return record


def admit_confirmed_pending_intent(
    authority: AuthorityService,
    *,
    pending_intent_id: str,
    at: datetime,
    capability_snapshot_id: str,
    reservation_book: DurableReservationBook,
    reservation_checkpoint_event_id: str,
    reservation_provider_id: str,
    reservation_max_age_seconds,
) -> AdmissionRecord:
    """Run one fresh financial admission from server-owned confirmed authority.

    A risk rejection is returned as the canonical ``AdmissionRecord`` and does
    not consume the confirmation.  A later call on a newer authority cut may
    therefore obtain a distinct fresh admission attempt.  Once the confirmation
    is durably consumed by an ADMITTED record, exact product retries return that
    record instead of trying to consume it again.
    """

    if type(authority) is not AuthorityService:
        raise TypeError("authority must be exact AuthorityService")
    if type(reservation_book) is not DurableReservationBook:
        raise TypeError("reservation_book must be exact DurableReservationBook")
    pending_id = _text(pending_intent_id, name="pending_intent_id")
    point = _utc(at, name="at")
    now_text = point.isoformat().replace("+00:00", "Z")
    capability = _text(capability_snapshot_id, name="capability_snapshot_id")
    checkpoint_id = _text(
        reservation_checkpoint_event_id,
        name="reservation_checkpoint_event_id",
    )
    provider_id = _text(
        reservation_provider_id,
        name="reservation_provider_id",
    ).upper()
    try:
        max_age = parse_bounded_exact_decimal(reservation_max_age_seconds)
    except (TypeError, ValueError) as error:
        raise ConfirmedPendingAdmissionError(
            "reservation_max_age_seconds must be a bounded finite decimal"
        ) from error
    if max_age < 0:
        raise ConfirmedPendingAdmissionError(
            "reservation_max_age_seconds must be non-negative"
        )
    max_age_text = canonical_decimal_text(max_age)

    try:
        store = _authority_service_store(authority, required=True)
    except (AuthorityConflict, PermissionError, TypeError, ValueError) as error:
        raise ConfirmedPendingAdmissionError(
            "AuthorityService JournalStore authority is unavailable"
        ) from error
    if type(store) is not JournalStore:
        raise ConfirmedPendingAdmissionError(
            "confirmed pending admission requires canonical JournalStore authority"
        )
    if reservation_book.store is not store:
        raise ConfirmedPendingAdmissionError(
            "reservation book and financial authority must share one JournalStore"
        )

    try:
        confirmed = resolve_confirmed_pending_intent(
            store,
            pending_intent_id=pending_id,
            at=point,
        )
        source = resolve_confirmed_pending_source_admission(
            store,
            pending_intent_id=pending_id,
            at=point,
        )
    except (
        ConfirmedPendingIntentResolutionError,
        ConfirmedPendingSourceError,
    ) as error:
        raise ConfirmedPendingAdmissionError(
            "confirmed pending authority cannot be recovered"
        ) from error

    consumed = _existing_consumption(authority, confirmed)
    if consumed is not None:
        return consumed

    pending = confirmed.pending_intent
    requirements = dict(confirmed.financial_binding.reservation_requirements)
    if (
        reservation_book.environment != pending.environment
        or reservation_book.account_id != pending.account_id
    ):
        raise ConfirmedPendingAdmissionError(
            "reservation book scope differs from confirmed pending intent"
        )

    policy = authority._policies.get(pending.policy_id)
    if policy is None or policy.version != pending.authority_policy_version:
        raise ConfirmedPendingAdmissionError(
            "current AuthorityPolicy version differs from confirmed pending authority"
        )
    try:
        source_evidence = authority._validate_historical_financial_retry_evidence(
            source,
            policy,
        )
    except (AuthorityConflict, KeyError, TypeError, ValueError) as error:
        raise ConfirmedPendingAdmissionError(
            "source financial evidence cannot be revalidated on current authority"
        ) from error
    if source_evidence.get("allocation_evidence") is not None:
        raise ConfirmedPendingAdmissionError(
            "allocation-backed confirmed intent requires allocation-aware continuation"
        )

    try:
        journal_cut = _authority_store_call(authority, "current_journal_sequence")
    except (PermissionError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingAdmissionError(
            "current journal cut is unavailable"
        ) from error
    if type(journal_cut) is not int or journal_cut < 0:
        raise ConfirmedPendingAdmissionError("current journal cut is invalid")

    request = RiskAuthorityRequest(
        risk_intent=pending.risk_intent,
        account_id=pending.account_id,
        environment=pending.environment,
        provider_id=provider_id,
        instrument_version=InstrumentVersionIdentity(
            pending.instrument_id,
            pending.instrument_version,
        ),
        capability_snapshot_id=capability,
        reconciliation_checkpoint_event_id=checkpoint_id,
        journal_sequence_cut=journal_cut,
        reservation_version=reservation_book.version,
        reservation_state_digest=reservation_book.state_digest,
        authority_policy_id=pending.policy_id,
        authority_policy_version=pending.authority_policy_version,
        evaluated_at=now_text,
        **authority._risk_policy_cut(
            provider_id=provider_id,
            account_id=pending.account_id,
            environment=pending.environment,
            journal_sequence_cut=journal_cut,
        ),
    )
    try:
        snapshot = authority._resolve_authoritative_risk_snapshot(request)
    except (AuthorityConflict, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingAdmissionError(
            "fresh authoritative risk snapshot is unavailable"
        ) from error

    try:
        availability_evidence = load_account_resource_availability_evidence(
            store,
            checkpoint_event_id=checkpoint_id,
            provider_id=provider_id,
            account_id=pending.account_id,
            environment=pending.environment,
            resources=tuple(requirements),
            now=now_text,
            max_age_seconds=max_age_text,
            evidence_artifact_store=authority.evidence_artifact_store,
            require_latest_scope=True,
        )
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingAdmissionError(
            "fresh reservation availability authority is unavailable"
        ) from error
    availability = availability_evidence.get("availability")
    if not isinstance(availability, Mapping):
        raise ConfirmedPendingAdmissionError(
            "fresh reservation availability evidence is malformed"
        )

    identity_material = {
        "schema_version": 1,
        "pending_intent_id": pending_id,
        "confirmation_id": confirmed.confirmation_id,
        "source_admission_id": source.admission_id,
        "journal_sequence_cut": journal_cut,
        "capability_snapshot_id": capability,
        "reservation_checkpoint_event_id": checkpoint_id,
        "reservation_provider_id": provider_id,
        "reservation_version": reservation_book.version,
        "reservation_state_digest": reservation_book.state_digest,
    }
    digest = _attempt_identity(identity_material)
    command_id = "confirmed-pending-command:sha256:" + digest
    idempotency_key = "confirmed-pending-idempotency:sha256:" + digest
    admission_id = "confirmed-pending-admission:sha256:" + digest
    intent_id = "confirmed-pending-intent:sha256:" + digest
    reservation_id = "confirmed-pending-reservation:sha256:" + digest

    try:
        return authority.admit(
            command_id=command_id,
            idempotency_key=idempotency_key,
            admission_id=admission_id,
            policy_id=pending.policy_id,
            intent_id=intent_id,
            intent_hash=pending.intent_hash,
            account_id=pending.account_id,
            environment=pending.environment,
            instrument_id=pending.instrument_id,
            instrument_version=pending.instrument_version,
            action=pending.authority_action,
            notional=pending.notional,
            capability_snapshot_id=capability,
            risk_intent=pending.risk_intent,
            risk_context=snapshot.context,
            risk_policy=snapshot.risk_policy,
            risk_valid_until=snapshot.valid_until,
            reservation_book=reservation_book,
            reservation_id=reservation_id,
            reservation_requirements=requirements,
            reservation_available=dict(availability),
            reservation_checkpoint_event_id=checkpoint_id,
            reservation_provider_id=provider_id,
            reservation_max_age_seconds=max_age_text,
            now=now_text,
            confirmation_id=confirmed.confirmation_id,
            risk_reducing=source.risk_reducing,
            allocation_result=None,
        )
    except (AuthorityConflict, KeyError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingAdmissionError(
            "canonical financial writer rejected confirmed pending continuation"
        ) from error
