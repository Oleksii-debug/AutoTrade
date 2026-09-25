"""Durable reconciliation evidence and dispatch-ambiguity recovery.

This module bridges the append-only financial journal to provider reconciliation.
It never sends network requests and never converts ambiguity into retry authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, canonical_json, payload_digest
from .reconciliation import ReconciliationResult, UnknownSubmission


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _instant(value: str, *, name: str) -> str:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decimal_map(values: Mapping[str, Decimal]) -> dict[str, str]:
    return {key: str(value) for key, value in sorted(values.items())}


def _scope(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> tuple[str, str, str]:
    return (
        _text(provider_id, name="provider_id").upper(),
        _text(account_id, name="account_id"),
        _text(environment, name="environment").upper(),
    )


def _reconciliation_aggregate_id(
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
) -> str:
    rid = _text(reconciliation_id, name="reconciliation_id")
    provider, account, scope = _scope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    scoped_identity = canonical_json([provider, account, scope, rid])
    return "account-reconciliation:" + str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/reconciliation-scope/"
            + scoped_identity,
        )
    )


def _require_checkpoint_scope(
    checkpoint: Mapping[str, Any],
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> Mapping[str, Any]:
    payload = checkpoint.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("checkpoint payload is required")
    provider, account, scope = _scope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if (
        payload.get("provider_id") != provider
        or payload.get("account_id") != account
        or payload.get("environment") != scope
    ):
        raise ValueError("checkpoint reconciliation scope mismatch")
    return payload


def _checkpoint_owner(
    payload: Mapping[str, Any],
) -> tuple[str, str]:
    owner = payload.get("checkpoint_owner")
    if not isinstance(owner, Mapping):
        raise ValueError("checkpoint owner identity is required")
    return (
        _text(owner.get("host_id"), name="checkpoint_owner.host_id"),
        _text(owner.get("owner_epoch"), name="checkpoint_owner.owner_epoch"),
    )


def reconciliation_payload(
    result: ReconciliationResult,
    *,
    observed_at: str,
) -> dict[str, Any]:
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be ReconciliationResult")
    timestamp = _instant(observed_at, name="observed_at")
    return {
        "provider_id": result.provider_id,
        "account_id": result.account_id,
        "environment": result.environment,
        "observed_at": timestamp,
        "complete": result.complete,
        "snapshot_consistent": result.snapshot_consistent,
        "provider_cash": _decimal_map(result.provider_cash),
        "provider_positions": _decimal_map(result.provider_positions),
        "snapshot": (
            {
                "mode": result.snapshot_mode,
                "query_started_at": result.snapshot_query_started_at,
                "query_completed_at": result.snapshot_query_completed_at,
            }
            if result.snapshot_mode is not None
            else None
        ),
        "matched_execution_ids": list(result.matched_execution_ids),
        "unexpected_execution_ids": list(result.unexpected_execution_ids),
        "missing_local_execution_ids": list(result.missing_local_execution_ids),
        "matched_working_client_order_ids": list(
            result.matched_working_client_order_ids
        ),
        "unexpected_working_provider_order_ids": list(
            result.unexpected_working_provider_order_ids
        ),
        "missing_local_working_client_order_ids": list(
            result.missing_local_working_client_order_ids
        ),
        "cash_differences": _decimal_map(result.cash_differences),
        "position_differences": _decimal_map(result.position_differences),
        "submission_resolutions": [
            {
                "attempt_id": item.attempt_id,
                "intent_id": item.intent_id,
                "client_order_id": item.client_order_id,
                "outcome": item.outcome,
                "evidence_reason": item.evidence_reason,
                "provider_order_ids": list(item.provider_order_ids),
                "provider_execution_ids": list(item.provider_execution_ids),
            }
            for item in result.submission_resolutions
        ],
        "matched_provider_activity_ids": list(
            result.matched_provider_activity_ids
        ),
        "unexpected_provider_activity_ids": list(
            result.unexpected_provider_activity_ids
        ),
        "missing_local_provider_activity_ids": list(
            result.missing_local_provider_activity_ids
        ),
        "manual_or_external_activity_ids": list(
            result.manual_or_external_activity_ids
        ),
        "activity_coverage_complete": result.activity_coverage_complete,
        "resource_availability": (
            None
            if result.resource_availability is None
            else {
                "provider_id": result.resource_availability.provider_id,
                "account_id": result.resource_availability.account_id,
                "environment": result.resource_availability.environment,
                "snapshot_id": result.resource_availability.snapshot_id,
                "query_started_at": result.resource_availability.query_started_at,
                "query_completed_at": result.resource_availability.query_completed_at,
                "provider_as_of": result.resource_availability.provider_as_of,
                "valid_until": result.resource_availability.valid_until,
                "available_resources": _decimal_map(
                    result.resource_availability.available_resources
                ),
                "evidence_refs": list(result.resource_availability.evidence_refs),
            }
        ),
        "blocking_resources": list(result.blocking_resources),
        "reasons": list(result.reasons),
    }


def record_reconciliation_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str,
    result: ReconciliationResult,
    observed_at: str,
    host_id: str,
    owner_epoch: str,
) -> dict[str, Any]:
    """Persist one exact reconciliation outcome, idempotently for retries."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    rid = _text(reconciliation_id, name="reconciliation_id")
    host = _text(host_id, name="host_id")
    epoch = _text(owner_epoch, name="owner_epoch")
    payload = reconciliation_payload(result, observed_at=observed_at)
    payload["checkpoint_owner"] = {
        "host_id": host,
        "owner_epoch": epoch,
    }
    aggregate_id = _reconciliation_aggregate_id(
        reconciliation_id=rid,
        provider_id=result.provider_id,
        account_id=result.account_id,
        environment=result.environment,
    )
    existing = store.load_events("account_reconciliation", aggregate_id)
    if existing and existing[-1]["payload"] == payload:
        return existing[-1]

    version = store.next_aggregate_version(
        "account_reconciliation", aggregate_id
    )
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/reconciliation/"
            f"{aggregate_id}/{version}/{payload_digest(payload)}",
        )
    )
    envelope = {
        "event_id": event_id,
        "event_type": "AccountReconciled",
        "schema_version": "1.0.0",
        "aggregate_type": "account_reconciliation",
        "aggregate_id": aggregate_id,
        "aggregate_version": str(version),
        "host_id": host,
        "owner_epoch": epoch,
        "environment": result.environment,
        "occurred_at": payload["observed_at"],
        "observed_at": payload["observed_at"],
        "committed_at": payload["observed_at"],
        "correlation_id": event_id,
        "causation_id": None,
        "payload": payload,
        "payload_hash": payload_digest(payload),
        "evidence_refs": [],
    }
    store.append_event(
        envelope,
        outbox_topic="autotrade.reconciliation.events",
    )
    event = store.get_event(event_id)
    if event is None:
        raise RuntimeError("reconciliation checkpoint was not persisted")
    return event


def load_latest_reconciliation_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
) -> dict[str, Any] | None:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    rid = _text(reconciliation_id, name="reconciliation_id")
    aggregate_id = _reconciliation_aggregate_id(
        reconciliation_id=rid,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    events = store.load_events("account_reconciliation", aggregate_id)
    if not events:
        return None
    event = events[-1]
    _require_checkpoint_scope(
        event,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    return event


def load_reconciliation_checkpoint_for_readiness(
    store: JournalStore,
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    host_id: str,
    owner_epoch: str,
) -> dict[str, Any] | None:
    """Return only a checkpoint eligible to authorize the current owner.

    Historical checkpoints remain available through
    load_latest_reconciliation_checkpoint(), but ownership transfer must force a
    new checkpoint before reconciliation can clear recovery/UNKNOWN gates.
    """

    checkpoint = load_latest_reconciliation_checkpoint(
        store,
        reconciliation_id=reconciliation_id,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if checkpoint is None:
        return None
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    checkpoint_host, checkpoint_epoch = _checkpoint_owner(payload)
    expected_host = _text(host_id, name="host_id")
    expected_epoch = _text(owner_epoch, name="owner_epoch")
    if checkpoint_host != expected_host or checkpoint_epoch != expected_epoch:
        return None
    return checkpoint



def load_submission_resolution_evidence(
    store: JournalStore,
    *,
    checkpoint_event_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    attempt_id: str,
    intent_id: str,
    client_order_id: str,
) -> dict[str, Any]:
    """Load one exact durable reconciliation verdict for a submission attempt.

    This is a read-only authority boundary for downstream financial consumers.
    Callers identify the immutable AccountReconciled event; the verdict itself is
    recovered from JournalStore and re-scoped here. Caller-authored outcome or
    reconciliation-complete booleans are intentionally not accepted as inputs.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    event_id = _text(checkpoint_event_id, name="checkpoint_event_id")
    expected_attempt = _text(attempt_id, name="attempt_id")
    expected_intent = _text(intent_id, name="intent_id")
    expected_client = _text(client_order_id, name="client_order_id")

    checkpoint = store.get_event(event_id)
    if checkpoint is None:
        raise KeyError(f"Unknown reconciliation checkpoint event: {event_id}")
    if checkpoint.get("event_type") != "AccountReconciled":
        raise ValueError("checkpoint event is not AccountReconciled")
    if checkpoint.get("aggregate_type") != "account_reconciliation":
        raise ValueError("checkpoint event has invalid reconciliation aggregate type")

    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    resolutions = payload.get("submission_resolutions")
    if not isinstance(resolutions, list):
        raise ValueError("checkpoint submission_resolutions must be a list")

    matches: list[Mapping[str, Any]] = []
    for item in resolutions:
        if not isinstance(item, Mapping):
            raise ValueError("submission resolution must be an object")
        item_attempt = _text(item.get("attempt_id"), name="attempt_id")
        if item_attempt == expected_attempt:
            matches.append(item)
    if len(matches) != 1:
        raise ValueError(
            "checkpoint must contain exactly one resolution for the submission attempt"
        )

    item = matches[0]
    item_intent = _text(item.get("intent_id"), name="intent_id")
    item_client = _text(item.get("client_order_id"), name="client_order_id")
    if item_intent != expected_intent or item_client != expected_client:
        raise ValueError("checkpoint submission identity mismatch")

    outcome = _text(item.get("outcome"), name="outcome").upper()
    if outcome not in {
        "UNKNOWN",
        "PROVEN_ABSENT",
        "OBSERVED_EXECUTION",
        "OBSERVED_WORKING_ORDER",
    }:
        raise ValueError("checkpoint contains unsupported submission outcome")

    def identities(field: str) -> tuple[str, ...]:
        values = item.get(field, [])
        if not isinstance(values, list):
            raise ValueError(f"checkpoint {field} must be a list")
        normalized = tuple(_text(value, name=field) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError(f"checkpoint {field} must be unique")
        return tuple(sorted(normalized))

    provider_order_ids = identities("provider_order_ids")
    provider_execution_ids = identities("provider_execution_ids")
    if outcome in {"UNKNOWN", "PROVEN_ABSENT"} and (
        provider_order_ids or provider_execution_ids
    ):
        raise ValueError(
            "absence/unknown resolution cannot carry provider order or execution identity"
        )
    if outcome == "OBSERVED_EXECUTION" and not provider_execution_ids:
        raise ValueError(
            "observed execution resolution requires provider execution identity"
        )
    if outcome == "OBSERVED_WORKING_ORDER":
        if not provider_order_ids or provider_execution_ids:
            raise ValueError(
                "working-order resolution requires order identity and no execution identity"
            )

    observed_at = _instant(payload.get("observed_at"), name="observed_at")
    payload_hash = _text(checkpoint.get("payload_hash"), name="payload_hash")
    aggregate_id = _text(checkpoint.get("aggregate_id"), name="aggregate_id")
    aggregate_version = checkpoint.get("aggregate_version")
    if type(aggregate_version) is not int or aggregate_version <= 0:
        raise ValueError("checkpoint aggregate_version must be a positive integer")

    return {
        "checkpoint_event_id": event_id,
        "checkpoint_payload_hash": payload_hash,
        "checkpoint_aggregate_id": aggregate_id,
        "checkpoint_aggregate_version": aggregate_version,
        "observed_at": observed_at,
        "provider_id": _text(payload.get("provider_id"), name="provider_id").upper(),
        "account_id": _text(payload.get("account_id"), name="account_id"),
        "environment": _text(payload.get("environment"), name="environment").upper(),
        "attempt_id": expected_attempt,
        "intent_id": item_intent,
        "client_order_id": item_client,
        "outcome": outcome,
        "evidence_reason": _text(item.get("evidence_reason"), name="evidence_reason"),
        "provider_order_ids": provider_order_ids,
        "provider_execution_ids": provider_execution_ids,
    }


def load_account_resource_availability_evidence(
    store: JournalStore,
    *,
    checkpoint_event_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    resources: Iterable[str],
    now: str,
    max_age_seconds: Decimal | str | int,
) -> dict[str, Any]:
    """Return exact cash availability from one fresh, complete provider snapshot.

    The reconciliation checkpoint is the authority. Callers may choose which
    resource keys they need, but may not supply the numeric availability. Only
    CASH resources are exposed until canonical provider semantics exist for
    margin, borrow and position-availability resources.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    event_id = _text(checkpoint_event_id, name="checkpoint_event_id")
    checkpoint = store.get_event(event_id)
    if checkpoint is None:
        raise KeyError(f"Unknown reconciliation checkpoint event: {event_id}")
    if (
        checkpoint.get("event_type") != "AccountReconciled"
        or checkpoint.get("aggregate_type") != "account_reconciliation"
    ):
        raise ValueError("availability evidence requires AccountReconciled checkpoint")

    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if (
        payload.get("complete") is not True
        or payload.get("snapshot_consistent") is not True
        or payload.get("blocking_resources") != []
    ):
        raise ValueError(
            "availability evidence requires complete non-blocking reconciliation"
        )

    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, Mapping):
        raise ValueError("availability evidence requires snapshot timing")
    completed_text = _instant(
        snapshot.get("query_completed_at"),
        name="snapshot.query_completed_at",
    )
    now_text = _instant(now, name="now")
    completed = datetime.fromisoformat(completed_text.replace("Z", "+00:00"))
    current = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    if current < completed:
        raise ValueError("availability checkpoint cannot be from the future")

    if isinstance(max_age_seconds, bool) or isinstance(max_age_seconds, float):
        raise TypeError("max_age_seconds must use Decimal, string or integer input")
    try:
        max_age = (
            max_age_seconds
            if isinstance(max_age_seconds, Decimal)
            else Decimal(max_age_seconds)
        )
    except Exception as error:
        raise ValueError("max_age_seconds must be a finite decimal") from error
    if not max_age.is_finite() or max_age < 0:
        raise ValueError("max_age_seconds must be a non-negative finite decimal")
    delta = current - completed
    age_microseconds = (
        (delta.days * 86400 + delta.seconds) * 1_000_000
        + delta.microseconds
    )
    age_seconds = Decimal(age_microseconds) / Decimal(1_000_000)
    if age_seconds > max_age:
        raise ValueError("availability checkpoint is stale")

    raw_cash = payload.get("provider_cash")
    if not isinstance(raw_cash, Mapping):
        raise ValueError("availability checkpoint lacks provider cash truth")
    provider_cash: dict[str, Decimal] = {}
    for raw_currency, raw_amount in raw_cash.items():
        currency = _text(raw_currency, name="provider_cash currency")
        if isinstance(raw_amount, bool) or isinstance(raw_amount, float):
            raise TypeError("provider cash must use exact decimal encoding")
        try:
            amount = Decimal(raw_amount)
        except Exception as error:
            raise ValueError("provider cash must be a finite decimal") from error
        if not amount.is_finite():
            raise ValueError("provider cash must be a finite decimal")
        provider_cash[currency] = amount

    requested = tuple(_text(value, name="resource") for value in resources)
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("resources must be non-empty and unique")
    availability: dict[str, Decimal] = {}
    for resource in requested:
        if not resource.startswith("CASH:"):
            raise ValueError(
                "resource availability semantics are not canonically supported"
            )
        currency = _text(resource.removeprefix("CASH:"), name="cash currency")
        if currency not in provider_cash:
            raise ValueError("provider snapshot does not contain requested cash resource")
        amount = provider_cash[currency]
        if amount < 0:
            raise ValueError("negative provider cash cannot authorize new reservation")
        availability[resource] = amount

    aggregate_version = checkpoint.get("aggregate_version")
    if type(aggregate_version) is not int or aggregate_version <= 0:
        raise ValueError("checkpoint aggregate_version must be a positive integer")
    return {
        "checkpoint_event_id": event_id,
        "checkpoint_payload_hash": _text(
            checkpoint.get("payload_hash"),
            name="payload_hash",
        ),
        "checkpoint_aggregate_id": _text(
            checkpoint.get("aggregate_id"),
            name="aggregate_id",
        ),
        "checkpoint_aggregate_version": aggregate_version,
        "provider_id": _text(payload.get("provider_id"), name="provider_id").upper(),
        "account_id": _text(payload.get("account_id"), name="account_id"),
        "environment": _text(payload.get("environment"), name="environment").upper(),
        "snapshot_mode": _text(snapshot.get("mode"), name="snapshot.mode").upper(),
        "snapshot_query_completed_at": completed_text,
        "observed_at": _instant(payload.get("observed_at"), name="observed_at"),
        "age_seconds": str(age_seconds),
        "availability": {
            resource: str(amount)
            for resource, amount in sorted(availability.items())
        },
    }

def unresolved_attempt_ids_from_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    resolutions = payload.get("submission_resolutions")
    if not isinstance(resolutions, list):
        raise ValueError("checkpoint submission_resolutions must be a list")
    unresolved: list[str] = []
    for item in resolutions:
        if not isinstance(item, Mapping):
            raise ValueError("submission resolution must be an object")
        attempt_id = _text(item.get("attempt_id"), name="attempt_id")
        outcome = _text(item.get("outcome"), name="outcome").upper()
        if outcome == "UNKNOWN":
            unresolved.append(attempt_id)
    return tuple(sorted(set(unresolved)))


def unresolved_provider_activity_ids_from_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    unexpected = payload.get("unexpected_provider_activity_ids", [])
    missing = payload.get("missing_local_provider_activity_ids", [])
    for values, name in (
        (unexpected, "unexpected_provider_activity_ids"),
        (missing, "missing_local_provider_activity_ids"),
    ):
        if not isinstance(values, list):
            raise ValueError(f"checkpoint {name} must be a list")
    normalized = [
        _text(value, name="provider_activity_id")
        for value in [*unexpected, *missing]
    ]
    return tuple(sorted(set(normalized)))


def unknown_submissions_from_dispatch(
    store: JournalStore,
    *,
    attempt_ids: Iterable[str],
    aggregate_ids: Mapping[str, str] | None = None,
) -> tuple[UnknownSubmission, ...]:
    """Rebuild ambiguous outbound attempts from durable Submission* events.

    SubmissionSending is ambiguous after a crash because the external request may
    already have crossed the final send barrier. SubmissionUnknown is explicitly
    ambiguous. Neither state is converted to retry authority here. `aggregate_ids`
    lets callers bind a logical attempt id to the exact durable aggregate identity
    used by a scoped dispatcher, without duplicating dispatch identity logic.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    normalized = tuple(_text(value, name="attempt_id") for value in attempt_ids)
    if len(normalized) != len(set(normalized)):
        raise ValueError("attempt_ids must be unique")

    durable_ids: dict[str, str] = {}
    if aggregate_ids is not None:
        if not isinstance(aggregate_ids, Mapping):
            raise TypeError("aggregate_ids must be a mapping")
        for raw_attempt_id, raw_aggregate_id in aggregate_ids.items():
            attempt_key = _text(raw_attempt_id, name="aggregate_ids attempt_id")
            aggregate_id = _text(raw_aggregate_id, name="aggregate_id")
            if attempt_key in durable_ids and durable_ids[attempt_key] != aggregate_id:
                raise ValueError("aggregate_ids contains conflicting attempt identity")
            durable_ids[attempt_key] = aggregate_id
        extra = set(durable_ids) - set(normalized)
        if extra:
            raise ValueError("aggregate_ids contains identities outside attempt_ids")

    recovered: list[UnknownSubmission] = []
    for attempt_id in normalized:
        aggregate_id = durable_ids.get(attempt_id, attempt_id)
        events = store.load_events("submission_attempt", aggregate_id)
        if not events:
            raise KeyError(f"Unknown submission attempt: {attempt_id}")
        first = events[0]
        if first["event_type"] != "SubmissionPrepared":
            raise ValueError(
                f"submission attempt {attempt_id} does not start with SubmissionPrepared"
            )
        payload = first["payload"]
        if not isinstance(payload, Mapping):
            raise ValueError("SubmissionPrepared payload must be an object")
        provider_id = _text(
            payload.get("provider"), name="provider"
        ).upper()
        account_id = _text(
            payload.get("account_id"), name="account_id"
        )
        environment = _text(
            payload.get("environment"), name="environment"
        ).upper()
        intent_id = _text(
            payload.get("intent_id"), name="intent_id"
        )
        if _text(first.get("environment"), name="event.environment").upper() != environment:
            raise ValueError(
                "SubmissionPrepared envelope environment does not match payload"
            )
        client_order_id = _text(
            payload.get("client_order_id"),
            name="client_order_id",
        )
        started_at = _instant(
            payload.get("prepared_at"),
            name="prepared_at",
        )
        last_type = events[-1]["event_type"]
        if last_type in {"SubmissionSending", "SubmissionUnknown"}:
            recovered.append(
                UnknownSubmission.create(
                    attempt_id=attempt_id,
                    intent_id=intent_id,
                    client_order_id=client_order_id,
                    provider_id=provider_id,
                    account_id=account_id,
                    environment=environment,
                    started_at=started_at,
                )
            )
    return tuple(recovered)
