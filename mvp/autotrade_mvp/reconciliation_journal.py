"""Durable reconciliation evidence and dispatch-ambiguity recovery.

This module bridges the append-only financial journal to provider reconciliation.
It never sends network requests and never converts ambiguity into retry authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .persistence import JournalStore, payload_digest
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


def reconciliation_payload(
    result: ReconciliationResult,
    *,
    observed_at: str,
) -> dict[str, Any]:
    if not isinstance(result, ReconciliationResult):
        raise TypeError("result must be ReconciliationResult")
    timestamp = _instant(observed_at, name="observed_at")
    return {
        "observed_at": timestamp,
        "complete": result.complete,
        "snapshot_consistent": result.snapshot_consistent,
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
                "client_order_id": item.client_order_id,
                "outcome": item.outcome,
                "evidence_reason": item.evidence_reason,
                "provider_order_ids": list(item.provider_order_ids),
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
        "blocking_resources": list(result.blocking_resources),
        "reasons": list(result.reasons),
    }


def record_reconciliation_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str,
    result: ReconciliationResult,
    observed_at: str,
) -> dict[str, Any]:
    """Persist one exact reconciliation outcome, idempotently for retries."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    rid = _text(reconciliation_id, name="reconciliation_id")
    payload = reconciliation_payload(result, observed_at=observed_at)
    existing = store.load_events("account_reconciliation", rid)
    if existing and existing[-1]["payload"] == payload:
        return existing[-1]

    version = store.next_aggregate_version("account_reconciliation", rid)
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/reconciliation/"
            f"{rid}/{version}/{payload_digest(payload)}",
        )
    )
    envelope = {
        "event_id": event_id,
        "event_type": "AccountReconciled",
        "schema_version": "1.0.0",
        "aggregate_type": "account_reconciliation",
        "aggregate_id": rid,
        "aggregate_version": str(version),
        "host_id": "local-mvp",
        "owner_epoch": "1",
        "environment": "SIMULATION",
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
) -> dict[str, Any] | None:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    rid = _text(reconciliation_id, name="reconciliation_id")
    events = store.load_events("account_reconciliation", rid)
    return events[-1] if events else None


def unresolved_attempt_ids_from_checkpoint(
    checkpoint: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = checkpoint.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("checkpoint payload is required")
    resolutions = payload.get("submission_resolutions")
    if not isinstance(resolutions, list):
        raise ValueError("checkpoint submission_resolutions must be a list")
    unresolved: list[str] = []
    for item in resolutions:
        if not isinstance(item, Mapping):
            raise ValueError("submission resolution must be an object")
        attempt_id = _text(str(item.get("attempt_id", "")), name="attempt_id")
        outcome = _text(str(item.get("outcome", "")), name="outcome").upper()
        if outcome == "UNKNOWN":
            unresolved.append(attempt_id)
    return tuple(sorted(set(unresolved)))


def unresolved_provider_activity_ids_from_checkpoint(
    checkpoint: Mapping[str, Any] | None,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = checkpoint.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("checkpoint payload is required")
    unexpected = payload.get("unexpected_provider_activity_ids", [])
    missing = payload.get("missing_local_provider_activity_ids", [])
    for values, name in (
        (unexpected, "unexpected_provider_activity_ids"),
        (missing, "missing_local_provider_activity_ids"),
    ):
        if not isinstance(values, list):
            raise ValueError(f"checkpoint {name} must be a list")
    normalized = [
        _text(str(value), name="provider_activity_id")
        for value in [*unexpected, *missing]
    ]
    return tuple(sorted(set(normalized)))


def unknown_submissions_from_dispatch(
    store: JournalStore,
    *,
    attempt_ids: Iterable[str],
) -> tuple[UnknownSubmission, ...]:
    """Rebuild ambiguous outbound attempts from durable Submission* events.

    SubmissionSending is ambiguous after a crash because the external request may
    already have crossed the final send barrier.  SubmissionUnknown is explicitly
    ambiguous.  Neither state is converted to a retryable state here.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    normalized = tuple(_text(value, name="attempt_id") for value in attempt_ids)
    if len(normalized) != len(set(normalized)):
        raise ValueError("attempt_ids must be unique")

    recovered: list[UnknownSubmission] = []
    for attempt_id in normalized:
        events = store.load_events("submission_attempt", attempt_id)
        if not events:
            raise KeyError(f"Unknown submission attempt: {attempt_id}")
        first = events[0]
        if first["event_type"] != "SubmissionPrepared":
            raise ValueError(
                f"submission attempt {attempt_id} does not start with SubmissionPrepared"
            )
        payload = first["payload"]
        client_order_id = _text(
            str(payload.get("client_order_id", "")),
            name="client_order_id",
        )
        started_at = _instant(
            str(payload.get("prepared_at", "")),
            name="prepared_at",
        )
        last_type = events[-1]["event_type"]
        if last_type in {"SubmissionSending", "SubmissionUnknown"}:
            recovered.append(
                UnknownSubmission.create(
                    attempt_id=attempt_id,
                    client_order_id=client_order_id,
                    started_at=started_at,
                )
            )
    return tuple(recovered)
