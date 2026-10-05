"""Fresh canonical financial admission for one server-confirmed pending intent.

This is the continuation bridge from durable operator confirmation back into the
existing ``AuthorityService.admit`` financial writer. It never asks the client
to replay quantity, price, notional, RiskPolicy, reservation requirements,
account, environment, policy identity, confirmation identity, or source
``risk_reducing`` semantics.

For a new attempt the caller supplies only product-owned runtime authority: an
exact AuthorityService configured with the authoritative risk issuer, the
current capability/reconciliation identities, and the exact reservation book.
Once the confirmation has been durably consumed by an ADMITTED financial
record, exact product retry is historical replay and does not depend on the
pending proposal still being current or unexpired.
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
from .pending_intents import DurablePendingIntentRegistry, PendingIntentError
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


def _historical_consumption(
    authority: AuthorityService,
    store: JournalStore,
    pending_intent_id: str,
) -> AdmissionRecord | None:
    """Return an already-consumed confirmation without re-reading current risk.

    Durable ADMITTED history is immutable financial truth. A later RiskPolicy
    activation, proposal expiry, reconciliation advance or capability change may
    block a *new* attempt, but must not make an exact retry forget that the
    one-shot confirmation was already consumed. AuthorityService replay has
    already validated confirmation single-use and scope; this adapter additionally
    binds that terminal record back to the durable pending id and revalidates the
    historical financial writer evidence before returning it.
    """

    try:
        pending, claim = DurablePendingIntentRegistry(store)._read(
            pending_intent_id
        )
    except PendingIntentError as error:
        raise ConfirmedPendingAdmissionError(
            "durable pending intent history is unavailable"
        ) from error
    if claim is None:
        return None
    confirmation_id = _text(
        claim.get("confirmation_id"),
        name="durable confirmation_id",
    )
    if authority._confirmations.get(confirmation_id) is None:
        return None

    matches = [
        record
        for record in authority._admissions.values()
        if record.confirmation_id == confirmation_id
    ]
    if len(matches) > 1:
        raise ConfirmedPendingAdmissionError(
            "financial confirmation was consumed by multiple admissions"
        )
    if not matches:
        return None
    record = matches[0]
    if (
        record.outcome != "ADMITTED"
        or record.requested_confirmation_id != confirmation_id
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
            "durable confirmation consumption differs from pending financial authority"
        )
    policy = authority._policies.get(record.policy_id)
    if policy is None:
        raise ConfirmedPendingAdmissionError(
            "durable consumed admission AuthorityPolicy is unavailable"
        )
    try:
        authority._validate_historical_financial_retry_evidence(record, policy)
    except (AuthorityConflict, KeyError, TypeError, ValueError) as error:
        raise ConfirmedPendingAdmissionError(
            "durable consumed admission financial evidence is invalid"
        ) from error
    return record


def _stable_snapshot_identity(snapshot_payload: Mapping[str, object]) -> dict[str, object]:
    """Select authority identities that change only when financial truth changes.

    Global journal sequence/evaluation clock are deliberately excluded. An
    unrelated append, including the attempt's own risk rejection, must not mint
    a new semantic order attempt. Evidence/source, risk state/policy and exact
    provider-domain identities remain included, so a genuinely new financial
    cut can produce a new attempt while the confirmation remains unused.
    """

    resolved = snapshot_payload.get("resolved_risk_policy")
    episode: dict[str, object] | None = None
    if isinstance(resolved, Mapping):
        identity = resolved.get("identity")
        if isinstance(identity, Mapping):
            episode = {
                "policy_id": identity.get("policy_id"),
                "version": identity.get("version"),
                "content_digest": identity.get("content_digest"),
                "registration_event_id": resolved.get("registration_event_id"),
                "activation_event_id": resolved.get("activation_event_id"),
            }
    evidence_refs = snapshot_payload.get("evidence_refs")
    if not isinstance(evidence_refs, Mapping):
        raise ConfirmedPendingAdmissionError(
            "fresh authoritative risk snapshot evidence refs are malformed"
        )
    context_fingerprint = _text(
        snapshot_payload.get("context_fingerprint"),
        name="risk context fingerprint",
    )
    risk_policy_fingerprint = _text(
        snapshot_payload.get("risk_policy_fingerprint"),
        name="risk policy fingerprint",
    )
    return {
        "context_fingerprint": context_fingerprint,
        "risk_policy_fingerprint": risk_policy_fingerprint,
        "risk_policy_episode": episode,
        "provider_environment": snapshot_payload.get("provider_environment"),
        "entity_policy_id": snapshot_payload.get("entity_policy_id"),
        "instrument_family": snapshot_payload.get("instrument_family"),
        "evidence_refs": dict(sorted(evidence_refs.items())),
    }


def admit_confirmed_pending_intent(
    authority: AuthorityService,
    *,
    pending_intent_id: str,
    at: datetime,
    capability_snapshot_id: str | None = None,
    reservation_book: DurableReservationBook | None = None,
    reservation_checkpoint_event_id: str | None = None,
    reservation_provider_id: str | None = None,
    reservation_max_age_seconds=None,
) -> AdmissionRecord:
    """Run one fresh financial admission from server-owned confirmed authority.

    A risk rejection is returned as the canonical ``AdmissionRecord`` and does
    not consume the confirmation. Repeating the same semantic financial cut is
    idempotent even though the first rejection itself advanced the global
    journal. A genuinely changed current authority cut may produce a distinct
    fresh attempt while the confirmation remains unused.

    Once the confirmation is durably consumed by an ADMITTED record, this call
    returns that immutable historical record before checking proposal expiry or
    any current capability/risk/reconciliation authority.
    """

    if type(authority) is not AuthorityService:
        raise TypeError("authority must be exact AuthorityService")
    pending_id = _text(pending_intent_id, name="pending_intent_id")
    point = _utc(at, name="at")
    now_text = point.isoformat().replace("+00:00", "Z")

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

    consumed = _historical_consumption(authority, store, pending_id)
    if consumed is not None:
        return consumed

    if type(reservation_book) is not DurableReservationBook:
        raise TypeError(
            "reservation_book must be exact DurableReservationBook for a fresh attempt"
        )
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
    if reservation_book.store is not store:
        raise ConfirmedPendingAdmissionError(
            "reservation book and financial authority must share one JournalStore"
        )

    try:
        before_resolution = _authority_store_call(
            authority,
            "current_journal_sequence",
        )
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
        after_resolution = _authority_store_call(
            authority,
            "current_journal_sequence",
        )
    except (
        ConfirmedPendingIntentResolutionError,
        ConfirmedPendingSourceError,
    ) as error:
        raise ConfirmedPendingAdmissionError(
            "confirmed pending authority cannot be recovered"
        ) from error
    except (PermissionError, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingAdmissionError(
            "confirmed pending authority cut is unavailable"
        ) from error
    if (
        type(before_resolution) is not int
        or type(after_resolution) is not int
        or before_resolution < 0
        or before_resolution != after_resolution
    ):
        raise ConfirmedPendingAdmissionError(
            "financial authority changed while confirmed pending state was resolved"
        )

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
    intent_id = _text(source.intent_id, name="source intent_id")

    journal_cut = after_resolution
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

    snapshot_payload = snapshot.evidence_payload()
    availability_payload_hash = availability_evidence.get(
        "checkpoint_payload_hash"
    )
    if availability_payload_hash is not None:
        availability_payload_hash = _text(
            availability_payload_hash,
            name="reservation checkpoint payload hash",
        )
    identity_material = {
        "schema_version": 2,
        "pending_intent_id": pending_id,
        "confirmation_id": confirmed.confirmation_id,
        "source_admission_id": source.admission_id,
        "source_intent_id": intent_id,
        "capability_snapshot_id": capability,
        "reservation_checkpoint_event_id": checkpoint_id,
        "reservation_checkpoint_payload_hash": availability_payload_hash,
        "reservation_provider_id": provider_id,
        "reservation_version": reservation_book.version,
        "reservation_state_digest": reservation_book.state_digest,
        "risk_authority": _stable_snapshot_identity(snapshot_payload),
    }
    digest = _attempt_identity(identity_material)
    command_id = "confirmed-pending-command:sha256:" + digest
    idempotency_key = "confirmed-pending-idempotency:sha256:" + digest
    admission_id = "confirmed-pending-admission:sha256:" + digest
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
