"""Recover the exact source financial rejection behind a confirmed pending intent.

The pending/confirmation envelopes intentionally expose only the economics needed
for operator confirmation.  A later fresh financial admission still needs source
semantics that are not safe to guess, notably ``risk_reducing`` and whether the
original financial decision carried allocation evidence.  This adapter reconnects
an opaque pending id to its one canonical ``confirmation_required`` admission by
recomputing the publication id from durable AuthorityService evidence.

It does not admit risk, reserve capital, or send a provider request.
"""

from __future__ import annotations

from datetime import datetime

from .authority import AdmissionRecord, AuthorityConflict, AuthorityService
from .confirmed_pending_intent import (
    ConfirmedPendingIntentResolutionError,
    resolve_confirmed_pending_intent,
)
from .pending_intent_publication import _pending_id, _risk_intent
from .persistence import JournalStore


class ConfirmedPendingSourceError(RuntimeError):
    """Raised when a confirmed pending id cannot prove one exact source admission."""


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ConfirmedPendingSourceError(
            f"{name} must be exact canonical non-empty text"
        )
    return value


def _source_candidate(record: object, *, pending_intent_id: str) -> bool:
    if type(record) is not AdmissionRecord:
        raise ConfirmedPendingSourceError(
            "durable financial authority contains invalid admission record"
        )
    if (
        record.outcome != "REJECTED"
        or record.reason != "confirmation_required"
        or record.confirmation_id is not None
        or record.requested_confirmation_id is not None
        or record.risk_decision_id is None
        or record.financial_command_id is None
    ):
        return False
    return _pending_id(
        admission_id=record.admission_id,
        request_fingerprint=record.request_fingerprint,
        risk_decision_id=record.risk_decision_id,
        financial_command_id=record.financial_command_id,
    ) == pending_intent_id


def resolve_confirmed_pending_source_admission(
    store: JournalStore,
    *,
    pending_intent_id: str,
    at: datetime,
) -> AdmissionRecord:
    """Return the unique canonical source admission for one current confirmation.

    ``pending_intent_id`` and ``at`` are the only public inputs.  All financial
    and source-admission fields are re-read from the canonical JournalStore.
    """

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    pending_id = _text(pending_intent_id, name="pending_intent_id")

    try:
        confirmed = resolve_confirmed_pending_intent(
            store,
            pending_intent_id=pending_id,
            at=at,
        )
    except ConfirmedPendingIntentResolutionError as error:
        raise ConfirmedPendingSourceError(
            "confirmed pending authority is unavailable"
        ) from error

    try:
        authority = AuthorityService(store)
    except (AuthorityConflict, TypeError, ValueError, RuntimeError) as error:
        raise ConfirmedPendingSourceError(
            "canonical financial authority cannot be reconstructed"
        ) from error

    matches = [
        record
        for record in authority._admissions.values()
        if _source_candidate(record, pending_intent_id=pending_id)
    ]
    if len(matches) != 1:
        raise ConfirmedPendingSourceError(
            "confirmed pending id does not identify exactly one source financial admission"
        )
    source = matches[0]
    pending = confirmed.pending_intent
    binding = confirmed.financial_binding

    if (
        source.policy_id != pending.policy_id
        or source.intent_hash != pending.intent_hash
        or source.account_id != pending.account_id
        or source.environment != pending.environment
        or source.instrument_version.instrument_id != pending.instrument_id
        or source.instrument_version.version != pending.instrument_version
        or source.action != pending.authority_action
        or source.notional != pending.notional
        or source.policy_version != pending.authority_policy_version
        or source.admitted_at != pending.registered_at
        or source.risk_valid_until != pending.expires_at
    ):
        raise ConfirmedPendingSourceError(
            "source financial admission differs from confirmed pending authority"
        )

    policy = authority._policies.get(source.policy_id)
    if policy is None:
        raise ConfirmedPendingSourceError(
            "source financial admission authority policy is unavailable"
        )
    try:
        evidence = authority._validate_historical_financial_retry_evidence(
            source,
            policy,
        )
    except (AuthorityConflict, KeyError, TypeError, ValueError) as error:
        raise ConfirmedPendingSourceError(
            "source financial admission evidence is invalid"
        ) from error

    if evidence.get("verdict") != "ALLOW":
        raise ConfirmedPendingSourceError(
            "source confirmation-required admission does not retain ALLOW risk evidence"
        )
    try:
        source_risk_intent = _risk_intent(evidence.get("risk_intent"))
    except Exception as error:
        raise ConfirmedPendingSourceError(
            "source financial risk intent is invalid"
        ) from error
    if source_risk_intent != pending.risk_intent:
        raise ConfirmedPendingSourceError(
            "source financial risk intent differs from confirmed pending authority"
        )

    requirements = evidence.get("reservation_requirements")
    if type(requirements) is not dict or requirements != dict(
        binding.reservation_requirements
    ):
        raise ConfirmedPendingSourceError(
            "source reservation requirements differ from confirmed financial binding"
        )

    snapshot = evidence.get("authoritative_risk_snapshot")
    resolved_policy = (
        snapshot.get("resolved_risk_policy")
        if type(snapshot) is dict
        else None
    )
    identity = (
        resolved_policy.get("identity")
        if type(resolved_policy) is dict
        else None
    )
    if type(identity) is not dict:
        raise ConfirmedPendingSourceError(
            "source admission lacks registry-issued quantitative RiskPolicy identity"
        )
    if (
        identity.get("policy_id") != binding.risk_policy_id
        or identity.get("version") != binding.risk_policy_version
        or resolved_policy.get("registration_event_id")
        != binding.risk_policy_registration_event_id
        or resolved_policy.get("activation_event_id")
        != binding.risk_policy_activation_event_id
    ):
        raise ConfirmedPendingSourceError(
            "source quantitative RiskPolicy episode differs from confirmed binding"
        )

    return source
