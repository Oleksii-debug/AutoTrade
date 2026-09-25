"""Durable reconciliation evidence and dispatch-ambiguity recovery.

This module bridges the append-only financial journal to provider reconciliation.
It never sends network requests and never converts ambiguity into retry authority.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from .dispatch import submission_attempt_aggregate_id
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_core import ProviderResponseObservation
from .reconciliation import ReconciliationResult, UnknownSubmission
from .securities_borrow import (
    BorrowAvailabilityEvidence,
    verify_provider_borrow_evidence,
)


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


def _provider_environment(
    value: str | None,
    *,
    environment: str,
    provider_id: str | None = None,
) -> str:
    runtime_environment = _text(environment, name="environment").upper()
    provider = (
        None
        if provider_id is None
        else _text(provider_id, name="provider_id").upper()
    )
    if provider == "BYBIT" and value is None:
        raise ValueError("BYBIT scope requires explicit provider_environment")
    provider_environment = (
        runtime_environment
        if value is None
        else _text(value, name="provider_environment").upper()
    )
    if provider == "BYBIT" and provider_environment not in {
        "MAINNET",
        "TESTNET",
        "DEMO",
    }:
        raise ValueError(
            "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
        )
    return provider_environment


def _reconciliation_aggregate_id(
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> str:
    rid = _text(reconciliation_id, name="reconciliation_id")
    provider, account, scope = _scope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    provider_scope = _provider_environment(
        provider_environment,
        environment=scope,
        provider_id=provider,
    )
    identity_parts = [provider, account, scope]
    if provider_scope != scope:
        identity_parts.append(provider_scope)
    identity_parts.append(rid)
    scoped_identity = canonical_json(identity_parts)
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
    provider_environment: str | None = None,
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
    expected_provider_environment = _provider_environment(
        provider_environment,
        environment=scope,
        provider_id=provider,
    )
    actual_provider_environment = payload.get(
        "provider_environment",
        payload.get("environment"),
    )
    if actual_provider_environment != expected_provider_environment:
        raise ValueError(
            "checkpoint reconciliation provider_environment mismatch"
        )
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
        "provider_environment": result.provider_environment,
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
        "borrow_differences": _decimal_map(
            result.borrow_differences or {}
        ),
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
                **(
                    {
                        "provider_environment":
                            result.resource_availability.provider_environment
                    }
                    if result.resource_availability.provider_environment
                    != result.resource_availability.environment
                    else {}
                ),
                "snapshot_id": result.resource_availability.snapshot_id,
                "query_started_at": result.resource_availability.query_started_at,
                "query_completed_at": result.resource_availability.query_completed_at,
                "provider_as_of": result.resource_availability.provider_as_of,
                "valid_until": result.resource_availability.valid_until,
                "available_resources": _decimal_map(
                    result.resource_availability.available_resources
                ),
                "evidence_refs": list(result.resource_availability.evidence_refs),
                **(
                    {
                        "resource_details": {
                            resource: dict(detail)
                            for resource, detail in sorted(
                                result.resource_availability.resource_details.items()
                            )
                        }
                    }
                    if result.resource_availability.resource_details
                    else {}
                ),
            }
        ),
        "blocking_resources": list(result.blocking_resources),
        "reasons": list(result.reasons),
    }


def _verify_borrow_checkpoint_evidence(
    result: ReconciliationResult,
    artifact_store: ArtifactStore | None,
) -> None:
    availability = result.resource_availability
    if availability is None:
        return
    borrow_details = [
        detail
        for resource, detail in availability.resource_details.items()
        if resource.startswith("BORROW:")
    ]
    if not borrow_details:
        return
    if not isinstance(artifact_store, ArtifactStore):
        raise ValueError(
            "securities-borrow checkpoint requires trusted ArtifactStore"
        )
    for detail in borrow_details:
        evidence = BorrowAvailabilityEvidence.from_resource_detail(detail)
        verify_provider_borrow_evidence(evidence, artifact_store)


def record_reconciliation_checkpoint(
    store: JournalStore,
    *,
    reconciliation_id: str,
    result: ReconciliationResult,
    observed_at: str,
    host_id: str,
    owner_epoch: str,
    evidence_artifact_store: ArtifactStore | None = None,
    snapshot_observations: Iterable[ProviderResponseObservation] = (),
) -> dict[str, Any]:
    """Persist one exact reconciliation outcome, idempotently for retries."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    _verify_borrow_checkpoint_evidence(result, evidence_artifact_store)
    rid = _text(reconciliation_id, name="reconciliation_id")
    host = _text(host_id, name="host_id")
    epoch = _text(owner_epoch, name="owner_epoch")
    payload = reconciliation_payload(result, observed_at=observed_at)
    observations = tuple(snapshot_observations)
    if observations:
        if (
            result.snapshot_consistent is not True
            or result.snapshot_mode not in {"ATOMIC", "COMPOSED"}
            or result.snapshot_query_started_at is None
            or result.snapshot_query_completed_at is None
        ):
            raise ValueError(
                "snapshot observations require a consistent timestamped reconciliation snapshot"
            )
        started_text = _instant(
            result.snapshot_query_started_at,
            name="snapshot_query_started_at",
        )
        completed_text = _instant(
            result.snapshot_query_completed_at,
            name="snapshot_query_completed_at",
        )
        started = datetime.fromisoformat(started_text.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_text.replace("Z", "+00:00"))
        refs: list[str] = []
        for observation in observations:
            if not isinstance(observation, ProviderResponseObservation):
                raise TypeError(
                    "snapshot_observations must contain ProviderResponseObservation"
                )
            if (
                observation.provider_id != result.provider_id
                or observation.account_id != result.account_id
                or observation.environment != result.environment
                or observation.provider_environment != result.provider_environment
            ):
                raise ValueError(
                    "snapshot observation scope differs from reconciliation"
                )
            prepared = datetime.fromisoformat(
                observation.query_binding.prepared_at.replace("Z", "+00:00")
            )
            observed = datetime.fromisoformat(
                observation.observed_at.replace("Z", "+00:00")
            )
            if not (started <= prepared <= observed <= completed):
                raise ValueError(
                    "snapshot observation lies outside reconciliation query window"
                )
            if observation.evidence_ref in refs:
                raise ValueError(
                    "snapshot observation evidence identities must be unique"
                )
            refs.append(observation.evidence_ref)
        payload["snapshot_evidence_refs"] = sorted(refs)
    payload["checkpoint_owner"] = {
        "host_id": host,
        "owner_epoch": epoch,
    }
    aggregate_id = _reconciliation_aggregate_id(
        reconciliation_id=rid,
        provider_id=result.provider_id,
        account_id=result.account_id,
        environment=result.environment,
        provider_environment=result.provider_environment,
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
    provider_environment: str | None = None,
) -> dict[str, Any] | None:
    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    rid = _text(reconciliation_id, name="reconciliation_id")
    aggregate_id = _reconciliation_aggregate_id(
        reconciliation_id=rid,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
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
        provider_environment=provider_environment,
    )
    return event


def load_latest_reconciliation_checkpoint_for_scope(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> dict[str, Any] | None:
    """Return the latest durably recorded reconciliation fact for one scope.

    Provider observed_at is evidence about when external truth was observed; it
    is not the ordering authority for local financial state. A later durable
    checkpoint must supersede an earlier one even when provider clocks regress,
    equal timestamps are reused, or a delayed response describes an older
    provider instant. Journal schema v6 assigns every event one unique monotonic
    journal_sequence inside the same transaction as the event itself, so the
    greatest matching sequence is the canonical scope head.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    provider, account, scope = _scope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    provider_scope = _provider_environment(
        provider_environment,
        environment=scope,
        provider_id=provider,
    )
    latest: dict[str, Any] | None = None
    latest_sequence = 0
    for event in store.load_events_by_aggregate_type("account_reconciliation"):
        if event.get("event_type") != "AccountReconciled":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("reconciliation checkpoint payload is required")
        if (
            payload.get("provider_id") != provider
            or payload.get("account_id") != account
            or payload.get("environment") != scope
            or payload.get(
                "provider_environment",
                payload.get("environment"),
            )
            != provider_scope
        ):
            continue
        aggregate_version = event.get("aggregate_version")
        if type(aggregate_version) is not int or aggregate_version <= 0:
            raise ValueError(
                "reconciliation checkpoint aggregate_version must be a positive integer"
            )
        _instant(
            payload.get("observed_at"),
            name="reconciliation checkpoint observed_at",
        )
        journal_sequence = event.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence <= 0:
            raise ValueError(
                "reconciliation checkpoint lacks durable journal sequence"
            )
        if journal_sequence <= latest_sequence:
            raise ValueError(
                "reconciliation journal sequence is not strictly increasing"
            )
        latest_sequence = journal_sequence
        latest = event

    return latest


def require_current_reconciliation_checkpoint(
    store: JournalStore,
    *,
    checkpoint_event_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> dict[str, Any]:
    """Fail closed unless an exact checkpoint is current scope-wide truth."""

    event_id = _text(checkpoint_event_id, name="checkpoint_event_id")
    latest = load_latest_reconciliation_checkpoint_for_scope(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
    )
    if latest is None:
        raise ValueError("no reconciliation checkpoint exists for account scope")
    if latest.get("event_id") != event_id:
        raise ValueError(
            "selected reconciliation checkpoint is superseded by newer provider truth"
        )
    return latest


def load_reconciliation_checkpoint_for_readiness(
    store: JournalStore,
    *,
    reconciliation_id: str,
    provider_id: str,
    account_id: str,
    environment: str,
    host_id: str,
    owner_epoch: str,
    provider_environment: str | None = None,
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
        provider_environment=provider_environment,
    )
    if checkpoint is None:
        return None
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
    )
    latest_scope_checkpoint = load_latest_reconciliation_checkpoint_for_scope(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
    )
    if (
        latest_scope_checkpoint is None
        or latest_scope_checkpoint.get("event_id") != checkpoint.get("event_id")
    ):
        raise ValueError(
            "readiness requires the latest reconciliation checkpoint for scope"
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
    provider_environment: str | None = None,
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
        provider_environment=provider_environment,
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

    evidence = {
        "checkpoint_event_id": event_id,
        "checkpoint_payload_hash": payload_hash,
        "checkpoint_aggregate_id": aggregate_id,
        "checkpoint_aggregate_version": aggregate_version,
        "observed_at": observed_at,
        "provider_id": _text(payload.get("provider_id"), name="provider_id").upper(),
        "account_id": _text(payload.get("account_id"), name="account_id"),
        "environment": _text(payload.get("environment"), name="environment").upper(),
        "provider_environment": _provider_environment(
            payload.get("provider_environment"),
            environment=_text(payload.get("environment"), name="environment"),
            provider_id=_text(payload.get("provider_id"), name="provider_id"),
        ),
        "attempt_id": expected_attempt,
        "intent_id": item_intent,
        "client_order_id": item_client,
        "outcome": outcome,
        "evidence_reason": _text(item.get("evidence_reason"), name="evidence_reason"),
        "provider_order_ids": provider_order_ids,
        "provider_execution_ids": provider_execution_ids,
    }
    return evidence


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
    evidence_artifact_store: ArtifactStore | None = None,
    require_latest_scope: bool = False,
    provider_environment: str | None = None,
) -> dict[str, Any]:
    """Return exact reservable availability from a fresh provider snapshot.

    CASH is supported directly. Securities-borrow resources are supported only
    when typed BorrowAvailabilityEvidence in the checkpoint proves exact
    provider/account/environment/instrument/version identity and freshness.
    Caller-supplied numeric availability is never authority.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(require_latest_scope, bool):
        raise TypeError("require_latest_scope must be boolean")
    event_id = _text(checkpoint_event_id, name="checkpoint_event_id")
    current_scope_head = None
    if require_latest_scope:
        current_scope_head = require_current_reconciliation_checkpoint(
            store,
            checkpoint_event_id=event_id,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        )
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
        provider_environment=provider_environment,
    )
    if (
        payload.get("complete") is not True
        or payload.get("snapshot_consistent") is not True
    ):
        raise ValueError(
            "availability evidence requires complete consistent reconciliation"
        )
    raw_blocking = payload.get("blocking_resources")
    if not isinstance(raw_blocking, list):
        raise ValueError("checkpoint blocking_resources must be a list")
    blocking_resources = tuple(
        _text(value, name="blocking_resource")
        for value in raw_blocking
    )
    if len(blocking_resources) != len(set(blocking_resources)):
        raise ValueError("checkpoint blocking_resources must be unique")

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

    resource_evidence = payload.get("resource_availability")
    if not isinstance(resource_evidence, Mapping):
        raise ValueError(
            "availability checkpoint lacks explicit provider resource availability"
        )
    provider, account, scope = _scope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    if (
        resource_evidence.get("provider_id") != provider
        or resource_evidence.get("account_id") != account
        or resource_evidence.get("environment") != scope
    ):
        raise ValueError("resource availability evidence scope mismatch")

    snapshot_started_text = _instant(
        snapshot.get("query_started_at"),
        name="snapshot.query_started_at",
    )
    resource_started_text = _instant(
        resource_evidence.get("query_started_at"),
        name="resource_availability.query_started_at",
    )
    resource_completed_text = _instant(
        resource_evidence.get("query_completed_at"),
        name="resource_availability.query_completed_at",
    )
    if (
        resource_started_text != snapshot_started_text
        or resource_completed_text != completed_text
    ):
        raise ValueError(
            "resource availability snapshot cut differs from reconciliation"
        )

    valid_until_text = _instant(
        resource_evidence.get("valid_until"),
        name="resource_availability.valid_until",
    )
    valid_until = datetime.fromisoformat(
        valid_until_text.replace("Z", "+00:00")
    )
    if current >= valid_until:
        raise ValueError("resource availability evidence is expired")

    raw_available = resource_evidence.get("available_resources")
    if not isinstance(raw_available, Mapping) or not raw_available:
        raise ValueError(
            "availability checkpoint lacks explicit available resources"
        )
    canonical_available: dict[str, Decimal] = {}
    for raw_resource, raw_amount in raw_available.items():
        resource = _text(
            raw_resource,
            name="resource_availability resource",
        )
        if resource in canonical_available:
            raise ValueError(
                "resource availability keys must be unique after normalization"
            )
        if isinstance(raw_amount, bool) or isinstance(raw_amount, float):
            raise TypeError(
                "resource availability must use exact decimal encoding"
            )
        try:
            amount = Decimal(raw_amount)
        except Exception as error:
            raise ValueError(
                "resource availability must be a finite decimal"
            ) from error
        if not amount.is_finite() or amount < 0:
            raise ValueError(
                "resource availability must be a non-negative finite decimal"
            )
        canonical_available[resource] = amount

    requested = tuple(_text(value, name="resource") for value in resources)
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("resources must be non-empty and unique")

    # A provider availability snapshot is only a safe CASH reservation authority
    # for the exact financial cut it reconciled. Durable settlement registration
    # or settlement-evidence events advance local trade-date/settled-cash truth.
    # Two fences are required:
    #   1. the reconciliation checkpoint must be journaled after that truth; and
    #   2. its provider resource query must have started after the settlement
    #      fact became available. Merely wrapping an old provider snapshot in a
    #      newer reconciliation event must never restore reservation authority.
    if any(resource.startswith("CASH:") for resource in requested):
        checkpoint_sequence = checkpoint.get("journal_sequence")
        if type(checkpoint_sequence) is not int or checkpoint_sequence <= 0:
            raise ValueError(
                "availability checkpoint lacks durable journal sequence"
            )
        resource_started = datetime.fromisoformat(
            resource_started_text.replace("Z", "+00:00")
        )
        for settlement_event in store.load_events_by_aggregate_type(
            "settlement_book"
        ):
            settlement_sequence = settlement_event.get("journal_sequence")
            settlement_payload = settlement_event.get("payload")
            if (
                type(settlement_sequence) is not int
                or not isinstance(settlement_payload, Mapping)
            ):
                continue
            settlement_scope = settlement_payload.get("scope")
            if not isinstance(settlement_scope, Mapping):
                continue
            if (
                settlement_scope.get("provider_id") != provider
                or settlement_scope.get("account_id") != account
                or settlement_scope.get("environment") != scope
            ):
                continue
            if settlement_sequence >= checkpoint_sequence:
                raise ValueError(
                    "availability checkpoint predates settlement financial truth"
                )
            settlement_committed = datetime.fromisoformat(
                _instant(
                    settlement_event.get("committed_at"),
                    name="settlement.committed_at",
                ).replace("Z", "+00:00")
            )
            if resource_started <= settlement_committed:
                raise ValueError(
                    "resource availability snapshot predates settlement financial truth"
                )

        # Option exercise/assignment/expiry is also account-scoped financial truth.
        # It can retire option inventory and create cash/delivery obligations, so a
        # provider cash snapshot taken before the lifecycle fact must not authorize
        # fresh capital reuse.  Reuse this single availability authority rather
        # than teaching reservation or option accounting a second freshness rule.
        for lifecycle_event in store.load_events_by_aggregate_type(
            "option_lifecycle"
        ):
            lifecycle_sequence = lifecycle_event.get("journal_sequence")
            lifecycle_payload = lifecycle_event.get("payload")
            if (
                type(lifecycle_sequence) is not int
                or not isinstance(lifecycle_payload, Mapping)
            ):
                continue
            if (
                lifecycle_payload.get("provider_id") != provider
                or lifecycle_payload.get("account_id") != account
                or lifecycle_payload.get("environment") != scope
            ):
                continue
            if lifecycle_sequence >= checkpoint_sequence:
                raise ValueError(
                    "availability checkpoint predates option lifecycle financial truth"
                )
            lifecycle_committed = datetime.fromisoformat(
                _instant(
                    lifecycle_event.get("committed_at"),
                    name="option_lifecycle.committed_at",
                ).replace("Z", "+00:00")
            )
            if resource_started <= lifecycle_committed:
                raise ValueError(
                    "resource availability snapshot predates option lifecycle financial truth"
                )
    if "ACCOUNT" in blocking_resources or any(
        resource in blocking_resources for resource in requested
    ):
        raise ValueError(
            "requested reservation resource is blocked by reconciliation"
        )

    raw_details = resource_evidence.get("resource_details", {})
    if not isinstance(raw_details, Mapping):
        raise ValueError("resource availability details must be an object")

    availability: dict[str, Decimal] = {}
    selected_details: dict[str, dict[str, str]] = {}
    snapshot_started = datetime.fromisoformat(
        snapshot_started_text.replace("Z", "+00:00")
    )
    for resource in requested:
        if resource not in canonical_available:
            raise ValueError(
                "provider snapshot does not contain requested available resource"
            )
        if resource.startswith("CASH:"):
            availability[resource] = canonical_available[resource]
            continue
        if not resource.startswith("BORROW:"):
            raise ValueError(
                "resource availability semantics are not canonically supported"
            )
        detail = raw_details.get(resource)
        if not isinstance(detail, Mapping):
            raise ValueError(
                "BORROW resource lacks typed securities-borrow evidence"
            )
        borrow = BorrowAvailabilityEvidence.from_resource_detail(detail)
        if not isinstance(evidence_artifact_store, ArtifactStore):
            raise ValueError(
                "BORROW availability requires trusted ArtifactStore"
            )
        verify_provider_borrow_evidence(
            borrow,
            evidence_artifact_store,
        )
        if (
            borrow.resource_key != resource
            or borrow.provider_id != provider
            or borrow.account_id != account
            or borrow.environment != scope
        ):
            raise ValueError("borrow availability evidence scope mismatch")
        if borrow.capacity_quantity != canonical_available[resource]:
            raise ValueError(
                "borrow capacity differs from available resource amount"
            )
        observed = datetime.fromisoformat(
            borrow.observed_at.replace("Z", "+00:00")
        )
        expires = datetime.fromisoformat(
            borrow.expires_at.replace("Z", "+00:00")
        )
        if observed < snapshot_started or observed > completed:
            raise ValueError(
                "borrow availability observation is outside snapshot cut"
            )
        if valid_until > expires or current >= expires:
            raise ValueError("borrow availability evidence is expired")
        availability[resource] = canonical_available[resource]
        selected_details[resource] = {
            _text(key, name="borrow detail key"): _text(
                value,
                name=f"borrow detail {key}",
            )
            for key, value in detail.items()
        }

    raw_evidence_refs = resource_evidence.get("evidence_refs")
    if not isinstance(raw_evidence_refs, list) or not raw_evidence_refs:
        raise ValueError(
            "resource availability evidence_refs must be a non-empty list"
        )
    normalized_evidence_refs = tuple(
        _text(value, name="resource_availability.evidence_ref")
        for value in raw_evidence_refs
    )
    if len(normalized_evidence_refs) != len(set(normalized_evidence_refs)):
        raise ValueError("resource availability evidence_refs must be unique")

    aggregate_version = checkpoint.get("aggregate_version")
    if type(aggregate_version) is not int or aggregate_version <= 0:
        raise ValueError("checkpoint aggregate_version must be a positive integer")
    evidence = {
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
        "provider_environment": _provider_environment(
            payload.get("provider_environment"),
            environment=_text(payload.get("environment"), name="environment"),
            provider_id=_text(payload.get("provider_id"), name="provider_id"),
        ),
        "snapshot_mode": _text(snapshot.get("mode"), name="snapshot.mode").upper(),
        "snapshot_query_completed_at": completed_text,
        "resource_snapshot_id": _text(
            resource_evidence.get("snapshot_id"),
            name="resource_availability.snapshot_id",
        ),
        "resource_valid_until": valid_until_text,
        # JSON-canonical representation: this evidence is persisted inside
        # risk/admission events and must compare identically after restart.
        "resource_evidence_refs": list(normalized_evidence_refs),
        "observed_at": _instant(payload.get("observed_at"), name="observed_at"),
        "age_seconds": str(age_seconds),
        "availability": {
            resource: str(amount)
            for resource, amount in sorted(availability.items())
        },
        "resource_details": {
            resource: dict(sorted(detail.items()))
            for resource, detail in sorted(selected_details.items())
        },
    }
    if current_scope_head is not None:
        scope_journal_sequence = current_scope_head.get("journal_sequence")
        if type(scope_journal_sequence) is not int or scope_journal_sequence <= 0:
            raise ValueError(
                "current reconciliation scope head lacks durable journal sequence"
            )
        head_payload = current_scope_head.get("payload")
        if not isinstance(head_payload, Mapping):
            raise ValueError("current reconciliation scope head payload is malformed")
        evidence.update(
            {
                "scope_latest_checkpoint_event_id": _text(
                    current_scope_head.get("event_id"),
                    name="scope_latest_checkpoint_event_id",
                ),
                "scope_latest_checkpoint_aggregate_id": _text(
                    current_scope_head.get("aggregate_id"),
                    name="scope_latest_checkpoint_aggregate_id",
                ),
                "scope_latest_checkpoint_aggregate_version": current_scope_head.get(
                    "aggregate_version"
                ),
                "scope_latest_checkpoint_journal_sequence": scope_journal_sequence,
                "scope_latest_checkpoint_observed_at": _instant(
                    head_payload.get("observed_at"),
                    name="scope_latest_checkpoint_observed_at",
                ),
            }
        )
    return evidence

def unresolved_attempt_ids_from_checkpoint(
    checkpoint: Mapping[str, Any] | None,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
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
    provider_environment: str | None = None,
) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    payload = _require_checkpoint_scope(
        checkpoint,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
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
    environment: str | None = None,
    account_id: str | None = None,
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
    scoped_lookup = environment is not None or account_id is not None
    if scoped_lookup:
        if environment is None or account_id is None:
            raise ValueError(
                "environment and account_id must be supplied together"
            )
        if aggregate_ids is not None:
            raise ValueError(
                "aggregate_ids cannot be combined with environment/account_id"
            )
        lookup_environment = _text(environment, name="environment").upper()
        lookup_account = _text(account_id, name="account_id")
        for attempt_key in normalized:
            durable_ids[attempt_key] = submission_attempt_aggregate_id(
                environment=lookup_environment,
                account_id=lookup_account,
                attempt_id=attempt_key,
            )
    elif aggregate_ids is not None:
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
        durable_environment = _text(
            payload.get("environment"), name="environment"
        ).upper()
        if scoped_lookup and (
            durable_environment != lookup_environment
            or account_id != lookup_account
        ):
            raise ValueError(
                "SubmissionPrepared durable scope does not match requested scope"
            )
        environment = durable_environment
        submission_scope = payload.get("submission_scope")
        provider_environment = None
        if isinstance(submission_scope, Mapping):
            raw_provider_environment = submission_scope.get(
                "provider_environment"
            )
            if raw_provider_environment is not None:
                provider_environment = _text(
                    raw_provider_environment,
                    name="submission_scope.provider_environment",
                ).upper()
        if provider_id == "BYBIT" and provider_environment is None:
            raise ValueError(
                "BYBIT SubmissionPrepared scope requires explicit "
                "provider_environment"
            )
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
                    provider_environment=provider_environment,
                )
            )
    return tuple(recovered)
