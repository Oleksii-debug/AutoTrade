"""Test-only durable provider-write response evidence fixtures.

This helper deliberately does not issue PAPER/LIVE send authority and never
performs provider I/O.  It constructs the canonical durable response chronology
needed by provider parser tests after transport/send authority itself has already
been tested elsewhere.
"""

from __future__ import annotations

from hashlib import sha256
from typing import Mapping

from mvp.autotrade_mvp.dispatch import (
    GuardedDispatcher,
    SubmissionResponseBinding,
    load_submission_response_binding,
    provider_domain_submission_attempt_key,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json


def journal_sent_response(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
    intent_id: str,
    provider: str,
    request_hash: str,
    client_order_id: str,
    response_bytes: bytes,
    submission_scope: Mapping[str, object],
    now: str,
    provider_environment: str | None = None,
    intent_hash: str = "test-fixture-intent-hash",
) -> SubmissionResponseBinding:
    """Persist one exact response without manufacturing financial send authority."""

    scope = dict(submission_scope)
    scope_hash = "sha256:" + sha256(
        canonical_json(scope).encode("utf-8")
    ).hexdigest()
    dispatcher = GuardedDispatcher(
        store,
        environment=environment,
        account_id=account_id,
        owner_token="provider-response-fixture",
        owner_epoch=1,
    )
    durable_attempt_id = attempt_id
    if environment.strip().upper() in {"PAPER", "LIVE"}:
        durable_attempt_id = provider_domain_submission_attempt_key(
            attempt_id=attempt_id,
            provider_id=provider,
            environment=environment,
            provider_environment=provider_environment,
        )
    prepared_payload = {
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "provider": provider,
            "request_hash": request_hash,
            "client_order_id": client_order_id,
            "environment": environment,
            "account_id": account_id,
            "owner_token": dispatcher.owner_token,
            "owner_epoch": dispatcher.owner_epoch,
            "prepared_at": now,
            "submission_scope": scope,
            "submission_scope_hash": scope_hash,
    }
    if provider_environment is not None:
        prepared_payload["provider_environment"] = provider_environment
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionPrepared",
        version=1,
        payload=prepared_payload,
        now=now,
    )
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionSending",
        version=2,
        payload={
            "client_order_id": client_order_id,
            "owner_token": dispatcher.owner_token,
            "owner_epoch": dispatcher.owner_epoch,
            "reason": "final_send_barrier_passed",
        },
        now=now,
    )
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionSent",
        version=3,
        payload={
            "client_order_id": client_order_id,
            "response_text": response_bytes.decode("utf-8"),
            "response_sha256": "sha256:" + sha256(response_bytes).hexdigest(),
            "response_encoding": "utf-8-json",
        },
        now=now,
    )
    return load_submission_response_binding(
        store,
        environment=environment,
        account_id=account_id,
        attempt_id=attempt_id,
        provider_id=provider if environment.strip().upper() in {"PAPER", "LIVE"} else None,
        provider_environment=(
            provider_environment
            if environment.strip().upper() in {"PAPER", "LIVE"}
            else None
        ),
    )


def journal_unknown_submission(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    attempt_id: str,
    intent_id: str,
    provider: str,
    request_hash: str,
    client_order_id: str,
    submission_scope: Mapping[str, object] | None,
    now: str,
    reason: str,
    response_bytes: bytes | None = None,
    http_status: int | None = None,
    provider_environment: str | None = None,
    intent_hash: str = "test-fixture-intent-hash",
) -> None:
    """Persist an irreversible-send ambiguity without issuing send authority."""

    scope = {} if submission_scope is None else dict(submission_scope)
    scope_hash = "sha256:" + sha256(
        canonical_json(scope).encode("utf-8")
    ).hexdigest()
    dispatcher = GuardedDispatcher(
        store,
        environment=environment,
        account_id=account_id,
        owner_token="provider-response-fixture",
        owner_epoch=1,
    )
    durable_attempt_id = attempt_id
    if environment.strip().upper() in {"PAPER", "LIVE"}:
        durable_attempt_id = provider_domain_submission_attempt_key(
            attempt_id=attempt_id,
            provider_id=provider,
            environment=environment,
            provider_environment=provider_environment,
        )
    prepared_payload = {
            "attempt_id": attempt_id,
            "intent_id": intent_id,
            "intent_hash": intent_hash,
            "provider": provider,
            "request_hash": request_hash,
            "client_order_id": client_order_id,
            "environment": environment,
            "account_id": account_id,
            "owner_token": dispatcher.owner_token,
            "owner_epoch": dispatcher.owner_epoch,
            "prepared_at": now,
            "submission_scope": scope,
            "submission_scope_hash": scope_hash,
    }
    if provider_environment is not None:
        prepared_payload["provider_environment"] = provider_environment
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionPrepared",
        version=1,
        payload=prepared_payload,
        now=now,
    )
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionSending",
        version=2,
        payload={
            "client_order_id": client_order_id,
            "owner_token": dispatcher.owner_token,
            "owner_epoch": dispatcher.owner_epoch,
            "reason": "final_send_barrier_passed",
        },
        now=now,
    )
    payload: dict[str, object] = {
        "client_order_id": client_order_id,
        "reason": reason,
        "retry_disposition": "RECONCILE_FIRST",
    }
    if response_bytes is not None:
        payload.update(
            {
                "response_text": response_bytes.decode("utf-8"),
                "response_sha256": (
                    "sha256:" + sha256(response_bytes).hexdigest()
                ),
                "response_encoding": "utf-8-json",
            }
        )
    if http_status is not None:
        payload["http_status"] = http_status
    dispatcher._append(
        attempt_id=durable_attempt_id,
        event_type="SubmissionUnknown",
        version=3,
        payload=payload,
        now=now,
    )
