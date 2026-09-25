"""Durable accounting bridge for confirmed external provider cash activity.

The bridge deliberately handles only provider-normalized DEPOSIT/WITHDRAWAL
activity with MANUAL or EXTERNAL origin. Ambiguous/unknown activity remains a
reconciliation blocker rather than being guessed into economic truth.

Provider evidence and the corresponding economic transaction are committed in
one JournalStore command transaction. Exact retries are idempotent; reuse of the
same provider/account/activity identity with changed economic content fails
closed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Any, Iterable, Mapping
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    Posting,
    ScopedEconomicBook,
    book_external_cash_flow,
    canonical_transaction,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .reconciliation import ProviderActivityEvidence


_ALLOWED_EXTERNAL_CASH_TYPES = frozenset({"DEPOSIT", "WITHDRAWAL"})
_ALLOWED_EXTERNAL_ORIGINS = frozenset({"MANUAL", "EXTERNAL"})


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _environment(value: str) -> str:
    environment = _text(value, name="environment").upper()
    if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
        raise ValueError(
            "environment must be REPLAY, SIMULATION, PAPER, or LIVE"
        )
    return environment


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    return format(normalized, "f")


def _instant(value: str, *, name: str) -> datetime:
    text = _text(value, name=name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _instant_text(value: str, *, name: str) -> str:
    return _instant(value, name=name).isoformat().replace("+00:00", "Z")


def _scoped_identity(kind: str, *parts: str) -> str:
    """Build an unambiguous durable identity from opaque external identifiers."""
    normalized_kind = _text(kind, name="identity_kind").lower()
    digest = sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()
    return f"{normalized_kind}:{digest}"


def _activity_identity(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    activity_id: str,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    activity = _text(activity_id, name="activity_id")
    return _scoped_identity(
        "provider-activity",
        provider,
        account,
        scope,
        activity,
    )


def _book_id(*, provider_id: str, account_id: str, environment: str) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    return _scoped_identity("economic-book", provider, account, scope)


def _transaction_payload(transaction: JournalTransaction) -> dict[str, Any]:
    """Serialize one economic fact using the canonical immutable transaction shape."""
    return canonical_transaction(transaction)


def _transaction_from_payload(value: Mapping[str, Any]) -> JournalTransaction:
    """Restore canonical v1.2 transactions while retaining legacy journal readability."""
    if not isinstance(value, Mapping):
        raise ValueError("economic transaction payload must be an object")

    schema_version = value.get("schema_version")
    legacy_keys = {
        "transaction_id",
        "cause_event_id",
        "reverses_transaction_id",
        "postings",
    }
    canonical_keys = legacy_keys | {
        "schema_version",
        "economic_effective_at",
        "economic_order_key",
        "observed_at",
        "corrects_transaction_id",
    }
    if schema_version is None:
        if set(value) != legacy_keys:
            raise ValueError("legacy economic transaction payload shape is invalid")
    elif schema_version == "1.2.0":
        if set(value) != canonical_keys:
            raise ValueError("canonical economic transaction payload shape is invalid")
    else:
        raise ValueError("unsupported economic transaction schema_version")

    postings = value.get("postings")
    if not isinstance(postings, list):
        raise ValueError("economic transaction postings must be a list")
    parsed_postings: list[Posting] = []
    for item in postings:
        if not isinstance(item, Mapping) or set(item) != {
            "ledger_account",
            "asset_or_currency",
            "signed_amount",
        }:
            raise ValueError("economic posting must use the canonical shape")
        parsed_postings.append(
            Posting(
                ledger_account=_text(
                    item.get("ledger_account"), name="ledger_account"
                ),
                asset_or_currency=_text(
                    item.get("asset_or_currency"), name="asset_or_currency"
                ),
                signed_amount=_decimal(
                    item.get("signed_amount"), name="signed_amount"
                ),
            )
        )

    def optional_text(name: str) -> str | None:
        raw = value.get(name)
        return _text(raw, name=name) if raw is not None else None

    transaction = JournalTransaction(
        transaction_id=_text(value.get("transaction_id"), name="transaction_id"),
        cause_event_id=_text(value.get("cause_event_id"), name="cause_event_id"),
        postings=tuple(parsed_postings),
        reverses_transaction_id=optional_text("reverses_transaction_id"),
        economic_effective_at=optional_text("economic_effective_at"),
        economic_order_key=optional_text("economic_order_key"),
        observed_at=optional_text("observed_at"),
        corrects_transaction_id=optional_text("corrects_transaction_id"),
    )
    if schema_version == "1.2.0" and canonical_transaction(transaction) != dict(value):
        raise ValueError("economic transaction payload is not canonical")
    return transaction


def _economic_batch_digest(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    transactions: Iterable[JournalTransaction],
) -> str:
    material = {
        "schema_version": "1.0.0",
        "provider_id": _text(provider_id, name="provider_id").upper(),
        "account_id": _text(account_id, name="account_id"),
        "environment": _environment(environment),
        "transactions": [_transaction_payload(item) for item in transactions],
    }
    return payload_digest(material)


class DurableProviderEconomicBook(ScopedEconomicBook):
    """JournalStore-backed provider/account economic book.

    This is a durable facade over the canonical ScopedEconomicBook, not a second
    ledger. One EconomicTransactionBatchBooked event contains an entire
    correction reversal+replacement group, so process failure cannot expose a
    half-correction. Economic ordering remains inside JournalTransaction;
    commit time is audit chronology only.
    """

    _BATCH_EVENT = "EconomicTransactionBatchBooked"
    _SINGLE_EVENT = "EconomicTransactionBooked"
    _ACTOR = "provider-economic-accounting"

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        super().__init__(environment=environment, account_id=account_id)
        self.book_id = _book_id(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
        )
        self._reload()

    def _events(self) -> list[dict[str, Any]]:
        return self.store.load_events("economic_book", self.book_id)

    def _replay(self, events: list[dict[str, Any]]) -> ScopedEconomicBook:
        candidate = ScopedEconomicBook(
            environment=self.environment,
            account_id=self.account_id,
        )
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise AccountingConflict(
                    "economic journal aggregate versions are not contiguous"
                )
            expected_version += 1
            event_type = event.get("event_type")
            if event_type not in {self._SINGLE_EVENT, self._BATCH_EVENT}:
                raise AccountingConflict(
                    "economic_book contains an unsupported durable event type"
                )

            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise AccountingConflict(
                    "economic durable event payload must be an object"
                )
            if (
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
            ):
                raise AccountingConflict(
                    "economic durable event scope does not match provider book"
                )

            if event_type == self._SINGLE_EVENT:
                transactions = (
                    _transaction_from_payload(payload.get("transaction")),
                )
                candidate.append_batch(transactions)
                continue
            raw_transactions = payload.get("transactions")
            if not isinstance(raw_transactions, list) or not raw_transactions:
                raise AccountingConflict(
                    "economic batch must contain canonical transactions"
                )
            transactions = tuple(
                _transaction_from_payload(item) for item in raw_transactions
            )
            batch_digest = _economic_batch_digest(
                provider_id=self.provider_id,
                account_id=self.account_id,
                environment=self.environment,
                transactions=transactions,
            )
            if payload.get("batch_digest") != batch_digest:
                raise AccountingConflict("economic batch digest is invalid")
            if payload.get("previous_book_digest") != candidate.audit_digest():
                raise AccountingConflict(
                    "economic batch previous-book binding is invalid"
                )
            candidate.append_batch(transactions)
            if payload.get("resulting_book_digest") != candidate.audit_digest():
                raise AccountingConflict(
                    "economic batch resulting-book binding is invalid"
                )
        return candidate

    def _reload(self) -> None:
        candidate = self._replay(self._events())
        self._book = candidate._book

    def append(self, transaction: JournalTransaction) -> bool:
        return self.append_batch((transaction,))

    def append_batch(self, transactions: Iterable[JournalTransaction]) -> bool:
        batch = tuple(
            _transaction_from_payload(_transaction_payload(item))
            for item in transactions
        )
        if not batch:
            raise ValueError("atomic transaction batch must not be empty")

        self._reload()
        candidate = ScopedEconomicBook(
            environment=self.environment,
            account_id=self.account_id,
            transactions=self.transactions,
        )
        inserted_locally = candidate.append_batch(batch)
        if not inserted_locally:
            return False

        transaction_payloads = [_transaction_payload(item) for item in batch]
        batch_digest = _economic_batch_digest(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            transactions=batch,
        )
        previous_digest = self.audit_digest()
        resulting_digest = candidate.audit_digest()
        next_version = self.store.next_aggregate_version(
            "economic_book", self.book_id
        )
        committed_at = datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        event_identity = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/economic-batch/"
                + _scoped_identity(
                    "economic-batch",
                    self.provider_id,
                    self.account_id,
                    self.environment,
                    batch_digest,
                ),
            )
        )
        payload = {
            "schema_version": "1.0.0",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "batch_digest": batch_digest,
            "previous_book_digest": previous_digest,
            "resulting_book_digest": resulting_digest,
            "transactions": transaction_payloads,
        }
        envelope = {
            "event_id": event_identity,
            "event_type": self._BATCH_EVENT,
            "aggregate_type": "economic_book",
            "aggregate_id": self.book_id,
            "aggregate_version": str(next_version),
            "committed_at": committed_at,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        command_identity = str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/economic-batch/"
                + _scoped_identity(
                    "economic-batch-command",
                    self.provider_id,
                    self.account_id,
                    self.environment,
                    batch_digest,
                ),
            )
        )
        request = {
            "schema_version": "1.0.0",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "batch_digest": batch_digest,
            "transactions": transaction_payloads,
        }
        result = {
            "batch_digest": batch_digest,
            "transaction_ids": [item.transaction_id for item in batch],
            "resulting_book_digest": resulting_digest,
        }
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=command_identity,
                actor=self._ACTOR,
                environment=self.environment,
                idempotency_key=f"economic-batch:{self.book_id}:{batch_digest}",
                request=request,
                result=result,
                state_version=next_version,
                events=[(envelope, "autotrade.economic.events")],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return inserted


def book_external_provider_cash_activity(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    activity: ProviderActivityEvidence,
    observed_at: str,
) -> tuple[JournalTransaction, bool]:
    """Atomically import one confirmed external cash activity and book economics.

    This function is intentionally narrow. Only a provider-normalized DEPOSIT or
    WITHDRAWAL may become an external equity flow. Fees, dividends, funding,
    corrections, assignments and generic cash adjustments require their own
    economic mappings and remain blocked until such a mapping is qualified.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(activity, ProviderActivityEvidence):
        raise TypeError("activity must be ProviderActivityEvidence")

    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    if activity.provider_id != provider:
        raise ValueError("provider activity evidence provider_id mismatch")
    if activity.account_id != account:
        raise ValueError("provider activity evidence account_id mismatch")
    if activity.environment != scope:
        raise ValueError("provider activity evidence environment mismatch")
    if activity.origin not in _ALLOWED_EXTERNAL_ORIGINS:
        raise ValueError(
            "only MANUAL or EXTERNAL provider activity may be booked as an external cash flow"
        )
    if activity.activity_type not in _ALLOWED_EXTERNAL_CASH_TYPES:
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

    observed = _instant(observed_at, name="observed_at")
    occurred = _instant(activity.occurred_at, name="occurred_at")
    if observed < occurred:
        raise ValueError("observed_at must not precede provider activity occurred_at")
    observed_text = observed.isoformat().replace("+00:00", "Z")

    identity = _activity_identity(
        provider_id=provider,
        account_id=account,
        environment=scope,
        activity_id=activity.activity_id,
    )
    book_id = _book_id(
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
    transaction = book_external_cash_flow(
        transaction_id=transaction_id,
        cause_event_id=cause_event_id,
        currency=activity.currency,
        amount=value,
    )
    amount_text = _decimal_text(value)

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
    imported_event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/provider-activity-import/{identity}",
        )
    )
    imported_envelope = {
        "event_id": imported_event_id,
        "event_type": "ProviderActivityImported",
        "aggregate_type": "provider_activity",
        "aggregate_id": identity,
        "aggregate_version": str(activity_version),
        "committed_at": observed_text,
        "payload": imported_payload,
        "payload_hash": payload_digest(imported_payload),
    }

    economic_payload = {
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
        "source_activity_identity": identity,
        "observed_at": observed_text,
        "transaction": _transaction_payload(transaction),
    }
    economic_event_id = str(
        uuid5(
            NAMESPACE_URL,
            f"https://events.autotrade.local/economic-booking/{identity}",
        )
    )
    economic_envelope = {
        "event_id": economic_event_id,
        "event_type": "EconomicTransactionBooked",
        "aggregate_type": "economic_book",
        "aggregate_id": book_id,
        "aggregate_version": str(book_version),
        "committed_at": observed_text,
        "payload": economic_payload,
        "payload_hash": payload_digest(economic_payload),
    }

    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            f"https://commands.autotrade.local/provider-cash-import/{identity}",
        )
    )
    _, inserted, _ = store.commit_command(
        command_id=command_identity,
        actor="provider-activity-accounting",
        environment=scope,
        idempotency_key=f"provider-cash-import:{identity}",
        request=request,
        result=result,
        state_version=book_version,
        events=[
            (imported_envelope, None),
            (economic_envelope, "autotrade.economic.events"),
        ],
    )
    return transaction, inserted


def load_provider_account_economic_book(
    store: JournalStore,
    *,
    provider_id: str,
    account_id: str,
    environment: str,
) -> EconomicBook:
    """Rebuild canonical economics from the one durable provider/account journal."""

    durable = DurableProviderEconomicBook(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
    )
    return EconomicBook(durable.transactions)
