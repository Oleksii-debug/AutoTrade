"""Crash-safe persistence adapter for canonical provider-fill accounting.

This module does not define a second ledger. ScopedEconomicBook remains the
canonical economic projection and JournalStore remains the canonical durable
journal. The adapter only gives provider-fill acceptance one atomic durable
boundary so reversal + replacement cannot be published as separate financial
facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    Posting,
    ScopedEconomicBook,
    canonical_transaction,
    transaction_digest,
    validate_transaction,
)
from .fill_accounting import (
    ProjectedFillEvidence,
    build_provider_fill_correction_transactions,
    build_provider_fill_transaction,
)
from .persistence import JournalStore, payload_digest
from .reconciliation import ProviderFillEvidence


_AGGREGATE_TYPE = "EconomicAccounting"
_EVENT_TYPE = "EconomicTransactionCommitted"
_SCHEMA_VERSION = "1.0.0"
_COMMAND_ACTOR = "ACCOUNTING"
_FILL_COMMAND = "PROVIDER_FILL"
_CORRECTION_COMMAND = "PROVIDER_FILL_CORRECTION"

_TX_KEYS = frozenset(
    {
        "schema_version",
        "transaction_id",
        "cause_event_id",
        "reverses_transaction_id",
        "economic_effective_at",
        "economic_order_key",
        "observed_at",
        "corrects_transaction_id",
        "postings",
    }
)
_POSTING_KEYS = frozenset(
    {"ledger_account", "asset_or_currency", "signed_amount"}
)
_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "environment",
        "account_id",
        "command_kind",
        "command_digest",
        "batch_size",
        "batch_index",
        "transaction",
        "transaction_digest",
    }
)


@dataclass(frozen=True)
class DurableAccountingCommit:
    """One accepted durable accounting command and its reopened projection."""

    inserted: bool
    command_id: str
    command_digest: str
    transactions: tuple[JournalTransaction, ...]
    book: ScopedEconomicBook


@dataclass(frozen=True)
class _DecodedEvent:
    command_kind: str
    command_digest: str
    batch_size: int
    batch_index: int
    transaction: JournalTransaction


def _scope(environment: str, account_id: str) -> ScopedEconomicBook:
    return ScopedEconomicBook(environment=environment, account_id=account_id)


def _aggregate_id(environment: str, account_id: str) -> str:
    normalized = _scope(environment, account_id)
    digest = payload_digest(
        {
            "schema_version": _SCHEMA_VERSION,
            "environment": normalized.environment,
            "account_id": normalized.account_id,
        }
    )
    return "economic-account:" + digest.removeprefix("sha256:")


def _transaction_from_canonical(value: object) -> JournalTransaction:
    if type(value) is not dict or frozenset(value) != _TX_KEYS:
        raise AccountingConflict(
            "durable accounting transaction has a noncanonical schema"
        )
    if value.get("schema_version") != "1.2.0":
        raise AccountingConflict(
            "durable accounting transaction schema version is unsupported"
        )
    postings_value = value.get("postings")
    if not isinstance(postings_value, list):
        raise AccountingConflict("durable accounting postings must be a list")
    postings: list[Posting] = []
    for item in postings_value:
        if type(item) is not dict or frozenset(item) != _POSTING_KEYS:
            raise AccountingConflict(
                "durable accounting posting has a noncanonical schema"
            )
        try:
            amount = Decimal(item["signed_amount"])
        except Exception as error:
            raise AccountingConflict(
                "durable accounting posting amount is invalid"
            ) from error
        postings.append(
            Posting(
                ledger_account=item["ledger_account"],
                asset_or_currency=item["asset_or_currency"],
                signed_amount=amount,
            )
        )
    transaction = JournalTransaction(
        transaction_id=value["transaction_id"],
        cause_event_id=value["cause_event_id"],
        postings=tuple(postings),
        reverses_transaction_id=value["reverses_transaction_id"],
        economic_effective_at=value["economic_effective_at"],
        economic_order_key=value["economic_order_key"],
        observed_at=value["observed_at"],
        corrects_transaction_id=value["corrects_transaction_id"],
    )
    try:
        validate_transaction(transaction)
    except (TypeError, ValueError) as error:
        raise AccountingConflict(
            "durable accounting transaction is semantically invalid"
        ) from error
    if canonical_transaction(transaction) != value:
        raise AccountingConflict(
            "durable accounting transaction is not canonically encoded"
        )
    return transaction


def _decode_event(
    event: dict[str, object],
    *,
    environment: str,
    account_id: str,
) -> _DecodedEvent:
    if event.get("event_type") != _EVENT_TYPE:
        raise AccountingConflict("unexpected durable accounting event type")
    payload = event.get("payload")
    if type(payload) is not dict or frozenset(payload) != _PAYLOAD_KEYS:
        raise AccountingConflict(
            "durable accounting event payload has a noncanonical schema"
        )
    if payload.get("schema_version") != _SCHEMA_VERSION:
        raise AccountingConflict(
            "durable accounting event schema version is unsupported"
        )
    if (
        payload.get("environment") != environment
        or payload.get("account_id") != account_id
    ):
        raise AccountingConflict("durable accounting event scope mismatch")
    command_kind = payload.get("command_kind")
    if command_kind not in {_FILL_COMMAND, _CORRECTION_COMMAND}:
        raise AccountingConflict("durable accounting command kind is invalid")
    command_digest = payload.get("command_digest")
    if (
        not isinstance(command_digest, str)
        or not command_digest.startswith("sha256:")
        or len(command_digest) != 71
    ):
        raise AccountingConflict("durable accounting command digest is invalid")
    batch_size = payload.get("batch_size")
    batch_index = payload.get("batch_index")
    if (
        type(batch_size) is not int
        or type(batch_index) is not int
        or batch_size <= 0
        or batch_index < 0
        or batch_index >= batch_size
    ):
        raise AccountingConflict("durable accounting batch identity is invalid")
    expected_size = 1 if command_kind == _FILL_COMMAND else 2
    if batch_size != expected_size:
        raise AccountingConflict(
            "durable accounting batch size does not match command kind"
        )
    transaction = _transaction_from_canonical(payload.get("transaction"))
    if payload.get("transaction_digest") != transaction_digest(transaction):
        raise AccountingConflict(
            "durable accounting transaction digest mismatch"
        )
    expected_event_id = (
        "accounting-event:"
        + transaction_digest(transaction).removeprefix("sha256:")
    )
    if event.get("event_id") != expected_event_id:
        raise AccountingConflict("durable accounting event identity mismatch")
    return _DecodedEvent(
        command_kind=command_kind,
        command_digest=command_digest,
        batch_size=batch_size,
        batch_index=batch_index,
        transaction=transaction,
    )


def load_durable_scoped_book(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
) -> ScopedEconomicBook:
    """Reopen the canonical economic projection from durable accounting facts."""

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    empty = _scope(environment, account_id)
    aggregate_id = _aggregate_id(empty.environment, empty.account_id)
    events = store.load_events(_AGGREGATE_TYPE, aggregate_id)
    decoded = [
        _decode_event(
            event,
            environment=empty.environment,
            account_id=empty.account_id,
        )
        for event in events
    ]

    groups: dict[str, list[_DecodedEvent]] = {}
    for item in decoded:
        groups.setdefault(item.command_digest, []).append(item)
    for command_digest, group in groups.items():
        expected_size = group[0].batch_size
        if (
            len(group) != expected_size
            or {item.batch_index for item in group}
            != set(range(expected_size))
            or any(
                item.batch_size != expected_size
                or item.command_kind != group[0].command_kind
                for item in group
            )
        ):
            raise AccountingConflict(
                "durable accounting command is only partially committed: "
                + command_digest
            )

    book = _scope(empty.environment, empty.account_id)
    for item in decoded:
        book.append(item.transaction)
    return book


def _command_request(
    *,
    command_kind: str,
    book: ScopedEconomicBook,
    provider_id: str,
    transactions: tuple[JournalTransaction, ...],
) -> dict[str, object]:
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ValueError("provider_id is required")
    return {
        "schema_version": _SCHEMA_VERSION,
        "command_kind": command_kind,
        "environment": book.environment,
        "account_id": book.account_id,
        "provider_id": provider_id.strip().upper(),
        "transactions": [
            {
                "transaction": canonical_transaction(transaction),
                "transaction_digest": transaction_digest(transaction),
            }
            for transaction in transactions
        ],
    }


def _event_envelopes(
    *,
    book: ScopedEconomicBook,
    command_kind: str,
    command_digest: str,
    transactions: tuple[JournalTransaction, ...],
    first_version: int,
) -> list[tuple[dict[str, object], None]]:
    aggregate_id = _aggregate_id(book.environment, book.account_id)
    events: list[tuple[dict[str, object], None]] = []
    for index, transaction in enumerate(transactions):
        tx_payload = canonical_transaction(transaction)
        tx_digest = transaction_digest(transaction)
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "environment": book.environment,
            "account_id": book.account_id,
            "command_kind": command_kind,
            "command_digest": command_digest,
            "batch_size": len(transactions),
            "batch_index": index,
            "transaction": tx_payload,
            "transaction_digest": tx_digest,
        }
        committed_at = (
            transaction.observed_at
            or transaction.economic_effective_at
        )
        if committed_at is None:
            raise AccountingConflict(
                "durable provider accounting requires event-time evidence"
            )
        envelope = {
            "event_id": "accounting-event:" + tx_digest.removeprefix("sha256:"),
            "event_type": _EVENT_TYPE,
            "aggregate_type": _AGGREGATE_TYPE,
            "aggregate_id": aggregate_id,
            "aggregate_version": str(first_version + index),
            "payload": payload,
            "payload_hash": payload_digest(payload),
            "committed_at": committed_at,
        }
        events.append((envelope, None))
    return events


def _commit_transactions(
    store: JournalStore,
    *,
    book: ScopedEconomicBook,
    provider_id: str,
    command_kind: str,
    transactions: Iterable[JournalTransaction],
) -> DurableAccountingCommit:
    batch = tuple(transactions)
    expected_size = 1 if command_kind == _FILL_COMMAND else 2
    if len(batch) != expected_size:
        raise AccountingConflict(
            "durable provider accounting command has invalid batch size"
        )
    for transaction in batch:
        validate_transaction(transaction)

    request = _command_request(
        command_kind=command_kind,
        book=book,
        provider_id=provider_id,
        transactions=batch,
    )
    command_digest = payload_digest(request)
    command_id = "accounting-command:" + command_digest.removeprefix("sha256:")
    idempotency_key = command_id
    existing_events = store.load_events(
        _AGGREGATE_TYPE,
        _aggregate_id(book.environment, book.account_id),
    )
    first_version = len(existing_events) + 1
    events = _event_envelopes(
        book=book,
        command_kind=command_kind,
        command_digest=command_digest,
        transactions=batch,
        first_version=first_version,
    )
    result = {
        "schema_version": _SCHEMA_VERSION,
        "command_id": command_id,
        "command_digest": command_digest,
        "transaction_ids": [item.transaction_id for item in batch],
        "transaction_digests": [transaction_digest(item) for item in batch],
    }
    saved_result, inserted, _appended = store.commit_command(
        command_id=command_id,
        actor=_COMMAND_ACTOR,
        environment=book.environment,
        idempotency_key=idempotency_key,
        request=request,
        result=result,
        state_version=first_version + len(batch) - 1,
        events=events,
    )
    if saved_result != result:
        raise AccountingConflict(
            "durable accounting idempotency result conflicts with requested facts"
        )

    reopened = load_durable_scoped_book(
        store,
        environment=book.environment,
        account_id=book.account_id,
    )
    durable_by_id = {
        item.transaction_id: item for item in reopened.transactions
    }
    for transaction in batch:
        if durable_by_id.get(transaction.transaction_id) != transaction:
            raise AccountingConflict(
                "durable accounting command is missing an accepted transaction"
            )
    return DurableAccountingCommit(
        inserted=inserted,
        command_id=command_id,
        command_digest=command_digest,
        transactions=batch,
        book=reopened,
    )


def commit_provider_fill(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    provider_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    observed_at: str | None = None,
) -> DurableAccountingCommit:
    """Atomically accept one provider fill into the canonical journal."""

    book = load_durable_scoped_book(
        store,
        environment=environment,
        account_id=account_id,
    )
    transaction = build_provider_fill_transaction(
        book=book,
        provider_id=provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        observed_at=observed_at,
    )
    return _commit_transactions(
        store,
        book=book,
        provider_id=provider_id,
        command_kind=_FILL_COMMAND,
        transactions=(transaction,),
    )


def commit_provider_fill_correction(
    store: JournalStore,
    *,
    environment: str,
    account_id: str,
    provider_id: str,
    original_projected_fill: ProjectedFillEvidence,
    original_provider_fill: ProviderFillEvidence,
    corrected_projected_fill: ProjectedFillEvidence,
    corrected_provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    correction_observed_at: str,
) -> DurableAccountingCommit:
    """Atomically accept reversal + corrected replacement through JournalStore."""

    book = load_durable_scoped_book(
        store,
        environment=environment,
        account_id=account_id,
    )
    reversal, replacement = build_provider_fill_correction_transactions(
        book=book,
        provider_id=provider_id,
        original_projected_fill=original_projected_fill,
        original_provider_fill=original_provider_fill,
        corrected_projected_fill=corrected_projected_fill,
        corrected_provider_fill=corrected_provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        correction_observed_at=correction_observed_at,
    )
    return _commit_transactions(
        store,
        book=book,
        provider_id=provider_id,
        command_kind=_CORRECTION_COMMAND,
        transactions=(reversal, replacement),
    )
