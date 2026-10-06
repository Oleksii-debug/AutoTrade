"""Compatibility facade with atomic provider-cash replay authority.

The established provider-accounting implementation remains byte-for-byte in
_provider_activity_accounting_impl. Only the external provider cash bridge is
overridden here so exact replay resolves command + EVENT_BATCH rows from one
JournalStore snapshot rather than two independent event reads.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from . import _provider_activity_accounting_impl as _impl
from ._provider_activity_accounting_impl import *  # noqa: F401,F403
from .persistence import (
    JournalStore, journal_store_authority_scope, require_exact_journal_store_authority,
)


def __getattr__(name: str):
    """Preserve legacy access to private helpers from the retained implementation."""

    return getattr(_impl, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_impl)))


_original_economic_store_load_command_event_batch = (
    _impl._economic_store_load_command_event_batch
)


def _economic_store_load_command_event_batch(
    economic_book: object,
    **kwargs: Any,
):
    """Require an OMS-bound EVENT_BATCH to own the OMS event it names.

    JournalStore authenticates the stored EVENT_BATCH descriptors and returns
    their integrity-checked events. Atomic fill and fill-bust requests both name
    their canonical OMS event. Exact replay is authoritative only when command
    provenance and the immutable OMS event join on the same durable fact.
    """

    request_value = kwargs.get("request")
    order_value = None
    order_field = None
    expected_operation = None
    if type(request_value) is dict:
        fill_value = request_value.get("order_fill")
        bust_value = request_value.get("order_bust")
        if fill_value is not None and bust_value is not None:
            raise ValueError(
                "atomic OMS command request cannot bind fill and bust authorities together"
            )
        if fill_value is not None:
            order_field = "order_fill"
            order_value = fill_value
            expected_operation = "RECORD_FILL"
        elif bust_value is not None:
            order_field = "order_bust"
            order_value = bust_value
            expected_operation = "BUST_FILL"

    if type(order_value) is dict and type(order_value.get("event_id")) is str:
        kwargs["referenced_event_ids"] = (order_value["event_id"],)
    authority = _original_economic_store_load_command_event_batch(
        economic_book,
        **kwargs,
    )
    if authority is None:
        return None

    request = kwargs.get("request")
    if not isinstance(request, Mapping) or order_field is None:
        return authority
    order_value = request.get(order_field)
    if not isinstance(order_value, Mapping):
        raise ValueError("atomic OMS command request has invalid order authority")

    event_id = order_value.get("event_id")
    event_key = order_value.get("event_key")
    operation = order_value.get("operation")
    order_request = order_value.get("request")
    if (
        not isinstance(event_id, str)
        or not event_id
        or not isinstance(event_key, str)
        or not event_key
        or operation != expected_operation
        or not isinstance(order_request, Mapping)
    ):
        raise ValueError("atomic OMS command request has invalid order authority")

    raw_events = authority.get("events")
    if not isinstance(raw_events, tuple):
        raise ValueError("atomic OMS command authority has invalid event batch")
    matching = tuple(
        event
        for event in raw_events
        if isinstance(event, Mapping) and event.get("event_id") == event_id
    )
    if not matching:
        references = authority.get("referenced_events", ())
        matching = (
            tuple(
                event
                for event in references
                if type(event) is dict and event.get("event_id") == event_id
            )
            if type(references) is tuple
            else ()
        )
        if (
            len(matching) != 1
            or not raw_events
            or matching[0]["journal_sequence"]
            >= min(event["journal_sequence"] for event in raw_events)
        ):
            raise ValueError(
                "atomic OMS command authority does not own the referenced OMS event"
            )
    if len(matching) != 1:
        raise ValueError(
            "atomic OMS command authority does not own the referenced OMS event"
        )

    event = matching[0]
    payload = event.get("payload")
    if (
        event.get("event_type") != "OrderProjectionMutationCommitted"
        or event.get("aggregate_type") != "order_projection_book"
        or not isinstance(payload, Mapping)
        or payload.get("event_key") != event_key
        or payload.get("operation") != expected_operation
        or payload.get("request") != order_request
        or payload_digest(
            {
                "request_hash": payload.get("request_hash"),
                "evidence_refs": event.get("evidence_refs", []),
            }
        )
        != order_value.get("mutation_hash")
        or payload_digest(payload.get("snapshot"))
        != order_value.get("snapshot_digest")
        or payload.get("scope", {}).get("provider_id") != economic_book.provider_id
        or payload.get("scope", {}).get("account_id") != economic_book.account_id
        or payload.get("scope", {}).get("environment") != economic_book.environment
    ):
        raise ValueError("atomic OMS command authority has invalid OMS semantic owner")
    return authority


# The retained implementation's atomic fill replay resolves this helper through
# its module globals. Install the stronger ownership join once at facade import
# so provider-evidence and direct SIMULATION/REPLAY paths share one authority.
_impl._economic_store_load_command_event_batch = (
    _economic_store_load_command_event_batch
)


def book_external_provider_cash_activity(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    activity: _impl.ProviderActivityEvidence,
    observed_at: str,
) -> tuple[_impl.JournalTransaction, bool]:
    """Atomically import one confirmed external cash activity and book economics.

    Exact retries resolve the typed command authority and both financial events
    from one held SQLite snapshot. A same-command commit racing after an initial
    absence is re-resolved from that authority instead of being misclassified as
    a partial financial effect.
    """

    store_identity = require_exact_journal_store_authority(
        store, subject="provider cash JournalStore",
    )
    activity = _impl._snapshot_external_cash_activity(activity)

    def durable_call(method, *args, **kwargs):
        with journal_store_authority_scope(store, store_identity):
            return method(store, *args, **kwargs)

    provider = _impl._text(provider_id, name="provider_id").upper()
    account = _impl._text(account_id, name="account_id")
    scope = _impl._environment(environment)
    if activity.provider_id != provider:
        raise ValueError("provider activity evidence provider_id mismatch")
    if activity.account_id != account:
        raise ValueError("provider activity evidence account_id mismatch")
    if activity.environment != scope:
        raise ValueError("provider activity evidence environment mismatch")
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
        activity_id=activity.activity_id,
    )
    book_id = _impl._book_id(
        provider_id=provider,
        account_id=account,
        environment=scope,
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
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
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
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
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
            snapshot = durable_call(JournalStore.load_command_event_batch,
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

        saved_result, replay_inserted, _ = durable_call(JournalStore.commit_command,
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

    activity_version = durable_call(JournalStore.next_aggregate_version,
        "provider_activity", identity
    )
    book_version = durable_call(JournalStore.next_aggregate_version,"economic_book", book_id)

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
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
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
        saved_result, inserted, _ = durable_call(JournalStore.commit_command,
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


def _provider_fill_transaction_is_bound(
    economic_book: _impl.DurableProviderEconomicBook,
    transaction_id: str,
) -> bool:
    """Resolve provider-fill ownership from durable financial-binding events."""

    target = _impl._text(transaction_id, name="reversed transaction_id")
    store, store_identity = _impl._require_exact_financial_book(
        economic_book,
        _impl.DurableProviderEconomicBook,
        name="economic_book",
    )
    with journal_store_authority_scope(store, store_identity):
        events = JournalStore.load_events_by_aggregate_type(
            store,
            _impl._PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        )

    matches = 0
    for event in events:
        if event.get("event_type") != _impl._PROVIDER_FILL_BINDING_EVENT_TYPE:
            raise _impl.AccountingConflict(
                "provider fill financial binding contains unsupported event type"
            )
        if event.get("aggregate_version") != 1:
            raise _impl.AccountingConflict(
                "provider fill financial binding version is invalid"
            )
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise _impl.AccountingConflict(
                "provider fill financial binding payload is invalid"
            )
        if _impl.payload_digest(payload) != event.get("payload_hash"):
            raise _impl.AccountingConflict(
                "provider fill financial binding payload hash is invalid"
            )
        request = payload.get("request")
        if not isinstance(request, Mapping):
            raise _impl.AccountingConflict(
                "provider fill financial binding request is invalid"
            )
        request = dict(request)
        if payload.get("request_digest") != _impl.payload_digest(request):
            raise _impl.AccountingConflict(
                "provider fill financial binding request digest is invalid"
            )
        if (
            payload.get("provider_id") != request.get("provider_id")
            or payload.get("account_id") != request.get("account_id")
            or payload.get("environment") != request.get("environment")
            or payload.get("provider_execution_id")
            != request.get("provider_execution_id")
        ):
            raise _impl.AccountingConflict(
                "provider fill financial binding scope is inconsistent"
            )

        in_scope = (
            request.get("provider_id") == economic_book.provider_id
            and request.get("account_id") == economic_book.account_id
            and request.get("environment") == economic_book.environment
        )
        if not in_scope:
            continue
        if request.get("transaction_id") == target:
            matches += 1

    if matches > 1:
        raise _impl.AccountingConflict(
            "provider fill transaction has duplicate financial-binding ownership"
        )
    return matches == 1


def _legacy_provider_fill_correction_is_already_durable(
    economic_book: _impl.DurableProviderEconomicBook,
    settlement_book: _impl.DurableSettlementBook,
    *,
    reversal: _impl.JournalTransaction,
    replacement: _impl.JournalTransaction,
    settlement_obligations: tuple[_impl.SettlementObligation, ...],
    committed_at: str | None,
) -> bool:
    """Admit the no-reservation shape only for already-durable legacy effects."""

    economic_plan = economic_book.prepare_batch_mutation(
        (reversal, replacement),
        committed_at=committed_at,
    )
    settlement_plan = settlement_book.prepare_register_mutation(
        settlement_obligations,
        committed_at=committed_at,
    )
    return economic_plan.already_committed and settlement_plan.already_committed


def commit_economic_correction_with_settlement_replacement(
    economic_book: _impl.DurableProviderEconomicBook,
    settlement_book: _impl.DurableSettlementBook,
    *,
    command_id: str,
    idempotency_key: str,
    reversal: _impl.JournalTransaction,
    replacement: _impl.JournalTransaction,
    settlement_obligations: Iterable[_impl.SettlementObligation],
    committed_at: str | None = None,
    reservation_book: _impl.DurableReservationBook | None = None,
    reservation_id: str | None = None,
    provider_fill_correction_binding: _impl.PreparedProviderFillCorrectionBinding | None = None,
) -> bool:
    """Fence fresh generic corrections of reservation-owned provider fills."""

    items = tuple(settlement_obligations)
    if (
        reservation_book is None
        and provider_fill_correction_binding is None
        and type(reversal) is _impl.JournalTransaction
        and reversal.reverses_transaction_id is not None
        and _provider_fill_transaction_is_bound(
            economic_book,
            reversal.reverses_transaction_id,
        )
        and not _legacy_provider_fill_correction_is_already_durable(
            economic_book,
            settlement_book,
            reversal=reversal,
            replacement=replacement,
            settlement_obligations=items,
            committed_at=committed_at,
        )
    ):
        raise _impl.AccountingConflict(
            "provider-fill-owned correction requires reservation-aware correction authority"
        )

    return _impl.commit_economic_correction_with_settlement_replacement(
        economic_book,
        settlement_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reversal=reversal,
        replacement=replacement,
        settlement_obligations=items,
        committed_at=committed_at,
        reservation_book=reservation_book,
        reservation_id=reservation_id,
        provider_fill_correction_binding=provider_fill_correction_binding,
    )


def commit_economic_batch_with_reservation_consumption(
    economic_book: _impl.DurableProviderEconomicBook,
    reservation_book: _impl.DurableReservationBook,
    *,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    usage: Mapping[str, object],
    transactions: Iterable[_impl.JournalTransaction],
    reservation_expected_snapshot_digest: str | None = None,
    committed_at: str | None = None,
    settlement_book: _impl.DurableSettlementBook | None = None,
    settlement_obligations: Iterable[_impl.SettlementObligation] = (),
    provider_fill_binding: _impl.PreparedProviderFillBinding | None = None,
) -> bool:
    """Fence generic publication of provider-fill-bound financial effects."""

    if provider_fill_binding is not None:
        raise _impl.AccountingConflict(
            "provider-fill-bound atomic commit requires evidence-derived provider fill entrypoint"
        )

    return _impl.commit_economic_batch_with_reservation_consumption(
        economic_book,
        reservation_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reservation_id=reservation_id,
        usage=usage,
        transactions=transactions,
        reservation_expected_snapshot_digest=reservation_expected_snapshot_digest,
        committed_at=committed_at,
        settlement_book=settlement_book,
        settlement_obligations=settlement_obligations,
        provider_fill_binding=None,
    )


# Install provider-fill provenance only when the financial surface is loaded.
# Foundation-only JournalStore imports must remain independent of research.
from . import _provider_fill_atomic_authority as _provider_fill_atomic_authority  # noqa: E402,F401
