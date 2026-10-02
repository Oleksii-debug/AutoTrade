"""Compatibility facade with atomic provider-cash replay authority.

The established provider-accounting implementation remains byte-for-byte in
_provider_activity_accounting_impl. Only the external provider cash bridge is
overridden here so exact replay resolves command + EVENT_BATCH rows from one
JournalStore snapshot rather than two independent event reads.
"""

from __future__ import annotations

from typing import Any, Mapping
from uuid import NAMESPACE_URL, uuid5

from . import _provider_activity_accounting_impl as _impl
from ._provider_activity_accounting_impl import *  # noqa: F401,F403
from .persistence import JournalStore


def __getattr__(name: str):
    """Preserve legacy access to private helpers from the retained implementation."""

    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


def book_external_provider_cash_activity(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
    activity: _impl.ProviderActivityEvidence,
    observed_at: str,
) -> tuple[_impl.JournalTransaction, bool]:
    """Atomically import one confirmed external cash activity and book economics.

    Exact retries resolve the typed command authority and both financial events
    from one held SQLite snapshot. A same-command commit racing after an initial
    absence is re-resolved from that authority instead of being misclassified as
    a partial financial effect.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(activity, _impl.ProviderActivityEvidence):
        raise TypeError("activity must be ProviderActivityEvidence")

    provider = _impl._text(provider_id, name="provider_id").upper()
    account = _impl._text(account_id, name="account_id")
    scope = _impl._environment(environment)
    domain = _impl._provider_environment(
        provider_id=provider,
        environment=scope,
        provider_environment=provider_environment,
    )
    if activity.provider_id != provider:
        raise ValueError("provider activity evidence provider_id mismatch")
    if activity.account_id != account:
        raise ValueError("provider activity evidence account_id mismatch")
    if activity.environment != scope:
        raise ValueError("provider activity evidence environment mismatch")
    if activity.provider_environment != domain:
        raise ValueError(
            "provider activity evidence provider_environment mismatch"
        )
    if activity.origin not in _impl._ALLOWED_EXTERNAL_ORIGINS:
        raise ValueError(
            "only MANUAL or EXTERNAL provider activity may be booked as an external cash flow"
        )
    if activity.activity_type not in _impl._ALLOWED_EXTERNAL_CASH_TYPES:
        raise ValueError(
            "provider activity type is not qualified for external cash-flow accounting"
        )
    if activity.currency is None:
        raise ValueError("external cash activity requires currency")
    linked_fields = {
        "instrument": activity.instrument,
        "client_order_id": activity.client_order_id,
        "provider_order_id": activity.provider_order_id,
        "provider_execution_id": activity.provider_execution_id,
    }
    present_links = tuple(
        name for name, value in linked_fields.items() if value is not None
    )
    if present_links:
        raise ValueError(
            "external cash activity must not discard trading linkage: "
            + ", ".join(present_links)
        )

    if activity.signed_amount is None:
        raise ValueError(
            "external cash activity requires provider-evidenced signed_amount"
        )
    value = activity.signed_amount
    if value == 0:
        raise ValueError("external cash activity signed_amount must be non-zero")
    if activity.activity_type == "DEPOSIT" and value <= 0:
        raise ValueError("DEPOSIT amount must be positive")
    if activity.activity_type == "WITHDRAWAL" and value >= 0:
        raise ValueError("WITHDRAWAL amount must be negative")

    observed = _impl._instant(observed_at, name="observed_at")
    occurred = _impl._instant(activity.occurred_at, name="occurred_at")
    if observed < occurred:
        raise ValueError("observed_at must not precede provider activity occurred_at")
    observed_text = observed.isoformat().replace("+00:00", "Z")

    identity = _impl._activity_identity(
        provider_id=provider,
        account_id=account,
        environment=scope,
        provider_environment=domain,
        activity_id=activity.activity_id,
    )
    book_id = _impl._book_id(
        provider_id=provider,
        account_id=account,
        environment=scope,
        provider_environment=domain,
    )
    if domain != scope:
        legacy_id = _impl._legacy_book_id(
            provider_id=provider,
            account_id=account,
            environment=scope,
        )
        if store.load_events("economic_book", legacy_id):
            raise _impl.AccountingConflict(
                "ambiguous legacy provider economic book requires explicit migration"
            )
    cause_event_id = f"provider-activity:{identity}"
    transaction_id = str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/economic-transaction/{identity}",
        )
    )
    transaction = _impl.book_external_cash_flow(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        currency=activity.currency,
        amount=value,
    )
    amount_text = _impl._decimal_text(value)

    request = {
        **_impl._scope_fields(
            provider_id=provider,
            account_id=account,
            environment=scope,
            provider_environment=domain,
        ),
        "activity": {
            "provider_id": activity.provider_id,
            "account_id": activity.account_id,
            "environment": activity.environment,
            "activity_id": activity.activity_id,
            "activity_type": activity.activity_type,
            "origin": activity.origin,
            "occurred_at": activity.occurred_at,
            "currency": activity.currency,
            "signed_amount": amount_text,
        },
        "amount": amount_text,
    }
    result = {
        **_impl._scope_fields(
            provider_id=provider,
            account_id=account,
            environment=scope,
            provider_environment=domain,
        ),
        "activity_id": activity.activity_id,
        "transaction_id": transaction_id,
        "amount": amount_text,
        "currency": activity.currency,
    }

    imported_event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/provider-activity-import/{identity}",
        )
    )
    economic_event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/economic-booking/{identity}",
        )
    )
    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            f"https://commands.autotrade.local/provider-cash-import/{identity}",
        )
    )
    actor = "provider-activity-accounting"
    journal_idempotency_key = f"provider-cash-import:{identity}"

    def require_semantic_owner(
        event: Mapping[str, object],
        *,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
    ) -> None:
        if (
            event.get("event_type") != event_type
            or event.get("aggregate_type") != aggregate_type
            or event.get("aggregate_id") != aggregate_id
        ):
            raise _impl.AccountingConflict(
                "provider cash activity replay has invalid durable semantic owner"
            )
        version = event.get("aggregate_version")
        if type(version) is not int or version <= 0:
            raise _impl.AccountingConflict(
                "provider cash activity replay has invalid aggregate version"
            )

    def resolve_existing_effect() -> tuple[_impl.JournalTransaction, bool] | None:
        try:
            snapshot = store.load_command_event_batch(
                command_id=command_identity,
                actor=actor,
                environment=scope,
                idempotency_key=journal_idempotency_key,
                request=request,
            )
        except ValueError as error:
            raise _impl.AccountingConflict(
                "provider cash durable command/effect authority is invalid"
            ) from error
        if snapshot is None:
            return None
        if snapshot.get("result") != result:
            raise _impl.AccountingConflict(
                "provider cash durable command result conflicts with its financial effect"
            )

        raw_events = snapshot.get("events")
        if not isinstance(raw_events, tuple) or len(raw_events) != 2:
            raise _impl.AccountingConflict(
                "provider cash durable command has an invalid financial effect cardinality"
            )
        events_by_id: dict[str, Mapping[str, Any]] = {}
        for raw_event in raw_events:
            if not isinstance(raw_event, Mapping):
                raise _impl.AccountingConflict(
                    "provider cash durable command contains an invalid event"
                )
            raw_event_id = raw_event.get("event_id")
            if not isinstance(raw_event_id, str) or raw_event_id in events_by_id:
                raise _impl.AccountingConflict(
                    "provider cash durable command contains invalid event identity"
                )
            events_by_id[raw_event_id] = raw_event
        if set(events_by_id) != {imported_event_id, economic_event_id}:
            raise _impl.AccountingConflict(
                "provider cash durable command is bound to unexpected financial effects"
            )

        existing_imported = events_by_id[imported_event_id]
        existing_economic = events_by_id[economic_event_id]
        require_semantic_owner(
            existing_imported,
            event_type="ProviderActivityImported",
            aggregate_type="provider_activity",
            aggregate_id=identity,
        )
        require_semantic_owner(
            existing_economic,
            event_type="EconomicTransactionBooked",
            aggregate_type="economic_book",
            aggregate_id=book_id,
        )

        imported_payload = existing_imported.get("payload")
        economic_payload = existing_economic.get("payload")
        if not isinstance(imported_payload, Mapping) or not isinstance(
            economic_payload, Mapping
        ):
            raise _impl.AccountingConflict(
                "provider cash activity replay found invalid durable payload"
            )
        if any(imported_payload.get(key) != value for key, value in request.items()):
            raise _impl.AccountingConflict(
                "provider cash activity replay conflicts with durable provider fact"
            )
        if (
            imported_payload.get("economic_transaction_id") != transaction_id
            or imported_payload.get("cause_event_id") != cause_event_id
            or economic_payload.get("provider_id") != provider
            or economic_payload.get("account_id") != account
            or economic_payload.get("environment") != scope
            or economic_payload.get("provider_environment")
            != (domain if domain != scope else None)
            or economic_payload.get("source_activity_identity") != identity
            or economic_payload.get("transaction")
            != _impl._transaction_payload(transaction)
        ):
            raise _impl.AccountingConflict(
                "provider cash activity replay conflicts with durable economic effect"
            )

        def replay_envelope(event: Mapping[str, object]) -> dict[str, object]:
            envelope = {
                key: value
                for key, value in event.items()
                if key != "journal_sequence"
            }
            version = event.get("aggregate_version")
            if type(version) is not int or version <= 0:
                raise _impl.AccountingConflict(
                    "provider cash activity replay has invalid aggregate version"
                )
            envelope["aggregate_version"] = str(version)
            return envelope

        saved_result, replay_inserted, _ = store.commit_command(
            command_id=command_identity,
            actor=actor,
            environment=scope,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=int(existing_economic["aggregate_version"]),
            events=[
                (replay_envelope(existing_imported), None),
                (
                    replay_envelope(existing_economic),
                    "autotrade.economic.events",
                ),
            ],
        )
        if replay_inserted:
            raise _impl.AccountingConflict(
                "provider cash effects exist without their durable command authority"
            )
        if saved_result != result:
            raise _impl.AccountingConflict(
                "provider cash durable command result conflicts with its financial effect"
            )
        return transaction, False

    resolved = resolve_existing_effect()
    if resolved is not None:
        return resolved

    activity_version = store.next_aggregate_version(
        "provider_activity", identity
    )
    book_version = store.next_aggregate_version("economic_book", book_id)

    imported_payload = {
        **request,
        "observed_at": observed_text,
        "economic_transaction_id": transaction_id,
        "cause_event_id": cause_event_id,
    }
    imported_envelope = {
        "event_id": imported_event_id,
        "event_type": "ProviderActivityImported",
        "aggregate_type": "provider_activity",
        "aggregate_id": identity,
        "aggregate_version": str(activity_version),
        "committed_at": observed_text,
        "payload": imported_payload,
        "payload_hash": _impl.payload_digest(imported_payload),
    }

    economic_payload = {
        **_impl._scope_fields(
            provider_id=provider,
            account_id=account,
            environment=scope,
            provider_environment=domain,
        ),
        "source_activity_identity": identity,
        "observed_at": observed_text,
        "transaction": _impl._transaction_payload(transaction),
    }
    economic_envelope = {
        "event_id": economic_event_id,
        "event_type": "EconomicTransactionBooked",
        "aggregate_type": "economic_book",
        "aggregate_id": book_id,
        "aggregate_version": str(book_version),
        "committed_at": observed_text,
        "payload": economic_payload,
        "payload_hash": _impl.payload_digest(economic_payload),
    }

    try:
        saved_result, inserted, _ = store.commit_command(
            command_id=command_identity,
            actor=actor,
            environment=scope,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=book_version,
            events=[
                (imported_envelope, None),
                (economic_envelope, "autotrade.economic.events"),
            ],
        )
    except ValueError:
        # A same-command writer can commit after the initial held read and
        # before our aggregate-version probes/commit. Re-resolve the exact
        # durable command/effects from one post-race snapshot. If no such
        # authority exists, preserve the original write conflict.
        resolved = resolve_existing_effect()
        if resolved is not None:
            return resolved
        raise

    if saved_result != result:
        raise _impl.AccountingConflict(
            "provider cash durable command result conflicts with its financial effect"
        )
    if not inserted:
        resolved = resolve_existing_effect()
        if resolved is None:
            raise _impl.AccountingConflict(
                "provider cash durable command exists without its financial effects"
            )
        return resolved
    return transaction, True
