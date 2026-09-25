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

from dataclasses import dataclass
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
from .durable_reservations import DurableReservationBook
from .durable_settlement import DurableSettlementBook
from .fill_accounting import (
    ProjectedFillEvidence,
    ProviderFillFinancialPlan,
    build_provider_fill_correction_transactions,
    build_provider_fill_financial_plan,
)
from .persistence import JournalStore, canonical_json, payload_digest
from .reconciliation import ProviderActivityEvidence, ProviderFillEvidence
from .settlement import SettlementObligation


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


def _economic_provider_environment(
    *,
    provider_id: str,
    environment: str,
    provider_environment: str | None,
) -> str:
    """Normalize exact provider environment without collapsing distinct endpoints."""

    provider = _text(provider_id, name="provider_id").upper()
    runtime_environment = _environment(environment)
    if provider == "BYBIT" and provider_environment is None:
        raise AccountingConflict(
            "BYBIT economic scope requires explicit provider_environment"
        )
    exact = (
        runtime_environment
        if provider_environment is None
        else _text(provider_environment, name="provider_environment").upper()
    )
    if provider == "BYBIT":
        if exact not in {"MAINNET", "TESTNET", "DEMO"}:
            raise AccountingConflict(
                "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
            )
        expected_runtime = "LIVE" if exact == "MAINNET" else "PAPER"
        if runtime_environment != expected_runtime:
            raise AccountingConflict(
                "BYBIT provider_environment does not match runtime environment"
            )
    return exact


def _economic_scope_parts(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None,
) -> tuple[str, str, str, str]:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    runtime_environment = _environment(environment)
    exact_provider_environment = _economic_provider_environment(
        provider_id=provider,
        environment=runtime_environment,
        provider_environment=provider_environment,
    )
    return provider, account, runtime_environment, exact_provider_environment


def _scoped_environment_identity_parts(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None,
) -> tuple[str, ...]:
    provider, account, runtime_environment, exact_provider_environment = (
        _economic_scope_parts(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        )
    )
    parts = [provider, account, runtime_environment]
    if exact_provider_environment != runtime_environment:
        parts.append(exact_provider_environment)
    return tuple(parts)


def _legacy_book_id(
    *, provider_id: str, account_id: str, environment: str
) -> str:
    """Return the pre-provider-environment economic-book identity."""

    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    runtime_environment = _environment(environment)
    return _scoped_identity(
        "economic-book", provider, account, runtime_environment
    )


def _activity_identity(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    activity_id: str,
    provider_environment: str | None = None,
) -> str:
    activity = _text(activity_id, name="activity_id")
    return _scoped_identity(
        "provider-activity",
        *_scoped_environment_identity_parts(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        ),
        activity,
    )


def _book_id(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> str:
    # The legacy identity remains readable for upgrade diagnostics only.
    # DurableProviderEconomicBook itself requires explicit BYBIT provider scope.
    if provider_environment is None:
        return _legacy_book_id(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
        )
    return _scoped_identity(
        "economic-book",
        *_scoped_environment_identity_parts(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        ),
    )


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
    provider_environment: str | None = None,
) -> str:
    provider, account, runtime_environment, exact_provider_environment = (
        _economic_scope_parts(
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        )
    )
    material = {
        "schema_version": "1.0.0",
        "provider_id": provider,
        "account_id": account,
        "environment": runtime_environment,
        "transactions": [_transaction_payload(item) for item in transactions],
    }
    if exact_provider_environment != runtime_environment:
        material["provider_environment"] = exact_provider_environment
    return payload_digest(material)


@dataclass(frozen=True)
class PreparedEconomicBatch:
    """One canonical economic batch prepared from a single durable journal cut."""

    transactions: tuple[JournalTransaction, ...]
    batch_digest: str
    envelope: dict[str, Any] | None
    request: dict[str, Any]
    result: dict[str, Any]
    aggregate_version: int
    already_committed: bool = False


_PROVIDER_FILL_BINDING_AGGREGATE_TYPE = "provider_fill_financial_binding"
_PROVIDER_FILL_BINDING_EVENT_TYPE = "ProviderFillFinancialPlanBound"


@dataclass(frozen=True)
class PreparedProviderFillBinding:
    """Immutable audit binding for one provider fill financial plan.

    The binding is committed in the same JournalStore transaction as the
    reservation consumption and economic batch. It owns no financial state; it
    proves which provider execution produced which canonical plan so later
    corrections cannot accidentally borrow aggregate reservation consumption
    from an unrelated partial fill.
    """

    aggregate_id: str
    envelope: dict[str, Any] | None
    request: dict[str, Any]
    result: dict[str, Any]
    aggregate_version: int
    already_committed: bool = False


def _provider_fill_binding_aggregate_id(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_execution_id: str,
    provider_environment: str | None = None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    runtime_environment = _environment(environment)
    exact_provider_environment = (
        runtime_environment
        if provider_environment is None
        else _text(provider_environment, name="provider_environment").upper()
    )
    if provider == "BYBIT":
        if provider_environment is None:
            raise AccountingConflict(
                "BYBIT provider fill binding requires explicit provider_environment"
            )
        if exact_provider_environment not in {"MAINNET", "TESTNET", "DEMO"}:
            raise AccountingConflict(
                "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
            )
    identity = [
        "provider-fill-financial-binding",
        provider,
        _text(account_id, name="account_id"),
        runtime_environment,
    ]
    if exact_provider_environment != runtime_environment:
        identity.append(exact_provider_environment)
    identity.append(_text(provider_execution_id, name="provider_execution_id"))
    return _scoped_identity(*identity)


def _projected_fill_binding_payload(
    projected_fill: ProjectedFillEvidence,
) -> dict[str, Any]:
    return {
        "fill_id": projected_fill.fill_id,
        "provider_execution_id": projected_fill.provider_execution_id,
        "intent_id": projected_fill.intent_id,
        "client_order_id": projected_fill.client_order_id,
        "side": projected_fill.side,
        "position_side": getattr(projected_fill, "position_side", None),
        "position_effect": getattr(projected_fill, "position_effect", None),
        "quantity": _decimal_text(projected_fill.quantity),
        "price": _decimal_text(projected_fill.price),
        "provider_revision": projected_fill.provider_revision,
        "correction_of": projected_fill.correction_of,
    }


def _provider_fill_binding_payload(
    provider_fill: ProviderFillEvidence,
) -> dict[str, Any]:
    payload = {
        "provider_id": provider_fill.provider_id,
        "account_id": provider_fill.account_id,
        "environment": provider_fill.environment,
        "provider_execution_id": provider_fill.provider_execution_id,
        "client_order_id": provider_fill.client_order_id,
        "instrument": provider_fill.instrument,
        "quantity": _decimal_text(provider_fill.quantity),
        "price": _decimal_text(provider_fill.price),
        "fee_amount": _decimal_text(provider_fill.fee_amount),
        "fee_currency": provider_fill.fee_currency,
        "trade_time": _instant_text(provider_fill.trade_time, name="trade_time"),
        "side": provider_fill.side,
        "position_side": provider_fill.position_side,
        "position_effect": getattr(provider_fill, "position_effect", None),
        "evidence_refs": list(provider_fill.evidence_refs),
    }
    if provider_fill.provider_environment != provider_fill.environment:
        payload["provider_environment"] = provider_fill.provider_environment
    return payload


def _prepare_provider_fill_binding(
    economic_book: "DurableProviderEconomicBook",
    *,
    plan: ProviderFillFinancialPlan,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    committed_at: str,
) -> PreparedProviderFillBinding:
    if not isinstance(plan, ProviderFillFinancialPlan):
        raise TypeError("plan must be ProviderFillFinancialPlan")
    if not isinstance(projected_fill, ProjectedFillEvidence):
        raise TypeError("projected_fill must be ProjectedFillEvidence")
    if not isinstance(provider_fill, ProviderFillEvidence):
        raise TypeError("provider_fill must be ProviderFillEvidence")
    if provider_fill.provider_environment != economic_book.provider_environment:
        raise AccountingConflict(
            "provider fill binding provider_environment does not match economic book"
        )
    if projected_fill.correction_of is not None:
        raise AccountingConflict(
            "initial provider fill binding cannot be created from correction evidence"
        )
    if plan.provider_execution_id != provider_fill.provider_execution_id:
        raise AccountingConflict(
            "provider fill binding execution identity changed"
        )
    if plan.intent_id != projected_fill.intent_id:
        raise AccountingConflict("provider fill binding intent identity changed")

    aggregate_id = _provider_fill_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_environment=provider_fill.provider_environment,
        provider_execution_id=provider_fill.provider_execution_id,
    )
    usage = {
        key: _decimal_text(value)
        for key, value in plan.usage_items
    }
    projected_payload = _projected_fill_binding_payload(projected_fill)
    provider_payload = _provider_fill_binding_payload(provider_fill)
    request = {
        "schema_version": "1.1.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "reservation_id": plan.reservation_id,
        "intent_id": plan.intent_id,
        "provider_execution_id": plan.provider_execution_id,
        "fill_id": projected_fill.fill_id,
        "provider_revision": projected_fill.provider_revision,
        "projected_fill": projected_payload,
        "projected_fill_digest": payload_digest(projected_payload),
        "provider_fill": provider_payload,
        "provider_fill_digest": payload_digest(provider_payload),
        "reservation_cut_digest": plan.reservation_cut_digest,
        "plan_digest": plan.plan_digest,
        "transaction_id": plan.transaction.transaction_id,
        "transaction_digest": payload_digest(
            canonical_transaction(plan.transaction)
        ),
        "derived_usage": usage,
    }
    if provider_fill.provider_environment != economic_book.environment:
        request["provider_environment"] = provider_fill.provider_environment
    events = economic_book.store.load_events(
        _PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        aggregate_id,
    )
    for event in events:
        if event.get("event_type") != _PROVIDER_FILL_BINDING_EVENT_TYPE:
            raise AccountingConflict(
                "provider fill financial binding contains unsupported event type"
            )
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            raise AccountingConflict(
                "provider fill financial binding payload is invalid"
            )
        if (
            payload.get("provider_id") != economic_book.provider_id
            or payload.get("account_id") != economic_book.account_id
            or payload.get("environment") != economic_book.environment
            or payload.get(
                "provider_environment",
                payload.get("environment"),
            )
            != provider_fill.provider_environment
            or payload.get("provider_execution_id") != plan.provider_execution_id
        ):
            raise AccountingConflict(
                "provider fill financial binding scope is invalid"
            )

    if events:
        if len(events) != 1:
            raise AccountingConflict(
                "provider fill financial binding identity is duplicated"
            )
        event = events[0]
        payload = event["payload"]
        stored_request = payload.get("request")
        if not isinstance(stored_request, Mapping):
            raise AccountingConflict(
                "provider fill financial binding request is invalid"
            )
        if set(stored_request) != set(request):
            raise AccountingConflict(
                "provider fill financial binding request shape changed"
            )
        stored_request = dict(stored_request)
        if payload.get("request_digest") != payload_digest(stored_request):
            raise AccountingConflict(
                "provider fill financial binding request digest is invalid"
            )

        stable_keys = set(request) - {
            "reservation_cut_digest",
            "plan_digest",
        }
        if any(
            stored_request.get(key) != request.get(key)
            for key in stable_keys
        ):
            raise AccountingConflict(
                "provider execution already has a different financial binding"
            )

        stored_cut = stored_request.get("reservation_cut_digest")
        if (
            not isinstance(stored_cut, str)
            or not stored_cut.startswith("sha256:")
            or len(stored_cut) != 71
            or any(ch not in "0123456789abcdef" for ch in stored_cut[7:])
        ):
            raise AccountingConflict(
                "provider fill financial binding reservation cut is invalid"
            )
        stored_plan_digest = stored_request.get("plan_digest")
        expected_plan_digest = payload_digest(
            {
                "schema_version": "1.1.0",
                "provider_id": economic_book.provider_id,
                "account_id": economic_book.account_id,
                "environment": economic_book.environment,
                "reservation_id": plan.reservation_id,
                "intent_id": plan.intent_id,
                "provider_execution_id": plan.provider_execution_id,
                "reservation_cut_digest": stored_cut,
                "transaction": canonical_transaction(plan.transaction),
                "derived_usage": usage,
            }
        )
        if stored_plan_digest != expected_plan_digest:
            raise AccountingConflict(
                "provider fill financial binding plan digest is invalid"
            )
        return PreparedProviderFillBinding(
            aggregate_id=aggregate_id,
            envelope=None,
            request=stored_request,
            result={
                "binding_event_id": event["event_id"],
                "plan_digest": stored_plan_digest,
            },
            aggregate_version=int(event["aggregate_version"]),
            already_committed=True,
        )

    request_digest = payload_digest(request)
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/provider-fill-financial-binding/"
            + aggregate_id,
        )
    )
    payload = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "provider_execution_id": plan.provider_execution_id,
        "request_digest": request_digest,
        "request": request,
    }
    if provider_fill.provider_environment != economic_book.environment:
        payload["provider_environment"] = provider_fill.provider_environment
    envelope = {
        "event_id": event_id,
        "event_type": _PROVIDER_FILL_BINDING_EVENT_TYPE,
        "aggregate_type": _PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": "1",
        "committed_at": _instant_text(committed_at, name="committed_at"),
        "payload": payload,
        "payload_hash": payload_digest(payload),
    }
    return PreparedProviderFillBinding(
        aggregate_id=aggregate_id,
        envelope=envelope,
        request=request,
        result={
            "binding_event_id": event_id,
            "plan_digest": plan.plan_digest,
        },
        aggregate_version=1,
    )


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
        provider_environment: str | None = None,
    ):
        if not isinstance(store, JournalStore):
            raise TypeError("store must be JournalStore")
        self.store = store
        self.provider_id = _text(provider_id, name="provider_id").upper()
        super().__init__(environment=environment, account_id=account_id)
        self.provider_environment = _economic_provider_environment(
            provider_id=self.provider_id,
            environment=self.environment,
            provider_environment=provider_environment,
        )
        self.book_id = _book_id(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
        )
        legacy_book_id = _legacy_book_id(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
        )
        if (
            legacy_book_id != self.book_id
            and self.store.load_events("economic_book", legacy_book_id)
        ):
            raise AccountingConflict(
                "legacy ambiguous provider economic-book state requires "
                "explicit migration/reconciliation before provider-environment scoped use"
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
            expected_provider_environment = (
                self.provider_environment
                if self.provider_environment != self.environment
                else None
            )
            if (
                payload.get("provider_id") != self.provider_id
                or payload.get("account_id") != self.account_id
                or payload.get("environment") != self.environment
                or payload.get("provider_environment")
                != expected_provider_environment
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
                provider_environment=self.provider_environment,
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

    def prepare_batch_mutation(
        self,
        transactions: Iterable[JournalTransaction],
        *,
        committed_at: str | None = None,
    ) -> PreparedEconomicBatch:
        """Prepare one economic batch without mutating durable state.

        The plan is derived from exactly one economic-book journal cut. A
        caller may combine its event with other aggregate events in one
        JournalStore.commit_command transaction; aggregate-version fencing then
        rejects any concurrent economic writer before any member is committed.
        """

        batch = tuple(
            _transaction_from_payload(_transaction_payload(item))
            for item in transactions
        )
        if not batch:
            raise ValueError("atomic transaction batch must not be empty")

        events = self._events()
        current = self._replay(events)
        candidate = ScopedEconomicBook(
            environment=self.environment,
            account_id=self.account_id,
            transactions=current.transactions,
        )
        inserted_locally = candidate.append_batch(batch)
        transaction_payloads = [_transaction_payload(item) for item in batch]
        batch_digest = _economic_batch_digest(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
            transactions=batch,
        )
        request = {
            "schema_version": "1.0.0",
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "batch_digest": batch_digest,
            "transactions": transaction_payloads,
        }
        if self.provider_environment != self.environment:
            request["provider_environment"] = self.provider_environment

        if not inserted_locally:
            matching_batches = [
                event
                for event in events
                if event.get("event_type") == self._BATCH_EVENT
                and isinstance(event.get("payload"), Mapping)
                and event["payload"].get("batch_digest") == batch_digest
                and event["payload"].get("transactions") == transaction_payloads
            ]
            matching_single = []
            if len(batch) == 1:
                matching_single = [
                    event
                    for event in events
                    if event.get("event_type") == self._SINGLE_EVENT
                    and isinstance(event.get("payload"), Mapping)
                    and event["payload"].get("transaction") == transaction_payloads[0]
                ]
            matches = matching_batches + matching_single
            if len(matches) != 1:
                raise AccountingConflict(
                    "economic transactions already exist without one canonical durable batch"
                )
            result = {
                "batch_digest": batch_digest,
                "transaction_ids": [item.transaction_id for item in batch],
                "resulting_book_digest": current.audit_digest(),
            }
            return PreparedEconomicBatch(
                transactions=batch,
                batch_digest=batch_digest,
                envelope=None,
                request=request,
                result=result,
                aggregate_version=int(matches[0]["aggregate_version"]),
                already_committed=True,
            )

        previous_digest = current.audit_digest()
        resulting_digest = candidate.audit_digest()
        next_version = (
            1 if not events else int(events[-1]["aggregate_version"]) + 1
        )
        when = (
            datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            if committed_at is None
            else _instant_text(committed_at, name="committed_at")
        )
        event_identity = str(
            uuid5(
                NAMESPACE_URL,
                "https://events.autotrade.local/economic-batch/"
                + _scoped_identity(
                    "economic-batch",
                    *_scoped_environment_identity_parts(
                        provider_id=self.provider_id,
                        account_id=self.account_id,
                        environment=self.environment,
                        provider_environment=self.provider_environment,
                    ),
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
        if self.provider_environment != self.environment:
            payload["provider_environment"] = self.provider_environment
        envelope = {
            "event_id": event_identity,
            "event_type": self._BATCH_EVENT,
            "aggregate_type": "economic_book",
            "aggregate_id": self.book_id,
            "aggregate_version": str(next_version),
            "committed_at": when,
            "payload": payload,
            "payload_hash": payload_digest(payload),
        }
        result = {
            "batch_digest": batch_digest,
            "transaction_ids": [item.transaction_id for item in batch],
            "resulting_book_digest": resulting_digest,
        }
        return PreparedEconomicBatch(
            transactions=batch,
            batch_digest=batch_digest,
            envelope=envelope,
            request=request,
            result=result,
            aggregate_version=next_version,
        )

    def refresh(self) -> None:
        """Reload the economic projection after an external atomic commit."""

        self._reload()

    def append(self, transaction: JournalTransaction) -> bool:
        return self.append_batch((transaction,))

    def append_batch(self, transactions: Iterable[JournalTransaction]) -> bool:
        plan = self.prepare_batch_mutation(transactions)
        if plan.already_committed:
            self._reload()
            return False
        if plan.envelope is None:
            raise AccountingConflict("fresh economic batch is missing its durable event")

        command_identity = str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/economic-batch/"
                + _scoped_identity(
                    "economic-batch-command",
                    *_scoped_environment_identity_parts(
                        provider_id=self.provider_id,
                        account_id=self.account_id,
                        environment=self.environment,
                        provider_environment=self.provider_environment,
                    ),
                    plan.batch_digest,
                ),
            )
        )
        try:
            _, inserted, _ = self.store.commit_command(
                command_id=command_identity,
                actor=self._ACTOR,
                environment=self.environment,
                idempotency_key=f"economic-batch:{self.book_id}:{plan.batch_digest}",
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, "autotrade.economic.events")],
            )
        except Exception:
            self._reload()
            raise
        self._reload()
        return inserted


def commit_economic_batch_with_reservation_consumption(
    economic_book: DurableProviderEconomicBook,
    reservation_book: DurableReservationBook,
    *,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    usage: Mapping[str, object],
    transactions: Iterable[JournalTransaction],
    reservation_expected_snapshot_digest: str | None = None,
    committed_at: str | None = None,
    settlement_book: DurableSettlementBook | None = None,
    settlement_obligations: Iterable[SettlementObligation] = (),
    provider_fill_binding: PreparedProviderFillBinding | None = None,
) -> bool:
    """Atomically commit canonical economics and reservation consumption.

    This function is an integration barrier, not a new finance authority.
    Reservation semantics remain owned by DurableReservationBook and economic
    semantics remain owned by DurableProviderEconomicBook. Both prepared
    events are inserted by one SQLite transaction, so a crash cannot make a
    fill durable in only one of those projections.

    The caller remains responsible for deriving the exact usage map from
    independently validated provider/order evidence. This barrier guarantees
    persistence atomicity and replay identity; it does not invent that mapping.
    """

    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(reservation_book, DurableReservationBook):
        raise TypeError("reservation_book must be DurableReservationBook")
    if economic_book.store is not reservation_book.store:
        raise ValueError("economic and reservation books must share one JournalStore")
    if (
        economic_book.environment != reservation_book.environment
        or economic_book.account_id != reservation_book.account_id
    ):
        raise ValueError(
            "economic and reservation books must share account/environment scope"
        )

    if provider_fill_binding is not None:
        if not isinstance(provider_fill_binding, PreparedProviderFillBinding):
            raise TypeError(
                "provider_fill_binding must be PreparedProviderFillBinding or None"
            )
        binding_request = provider_fill_binding.request
        binding_provider_environment = binding_request.get(
            "provider_environment",
            binding_request.get("environment"),
        )
        if (
            binding_request.get("provider_id") != economic_book.provider_id
            or binding_request.get("account_id") != economic_book.account_id
            or binding_request.get("environment") != economic_book.environment
            or binding_provider_environment != economic_book.provider_environment
            or binding_request.get("reservation_id") != _text(
                reservation_id, name="reservation_id"
            )
        ):
            raise AccountingConflict(
                "provider fill financial binding scope does not match atomic fill"
            )

    settlement_items = tuple(settlement_obligations)
    if settlement_book is None and settlement_items:
        raise ValueError(
            "settlement obligations require the canonical durable settlement book"
        )
    if settlement_book is not None:
        if not isinstance(settlement_book, DurableSettlementBook):
            raise TypeError("settlement_book must be DurableSettlementBook or None")
        if settlement_book.store is not economic_book.store:
            raise ValueError(
                "economic, reservation and settlement books must share one JournalStore"
            )
        if (
            settlement_book.scope.provider_id != economic_book.provider_id
            or settlement_book.scope.account_id != economic_book.account_id
            or settlement_book.scope.environment != economic_book.environment
        ):
            raise ValueError(
                "settlement book must share provider/account/environment scope"
            )
        if not settlement_items:
            raise ValueError(
                "settlement_book requires explicit settlement obligations"
            )

    cid = _text(command_id, name="command_id")
    idem = _text(idempotency_key, name="idempotency_key")
    rid = _text(reservation_id, name="reservation_id")
    when = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if committed_at is None
        else _instant_text(committed_at, name="committed_at")
    )
    reservation_component_key = _scoped_identity(
        "atomic-fill-reservation",
        *_scoped_environment_identity_parts(
            provider_id=economic_book.provider_id,
            account_id=economic_book.account_id,
            environment=economic_book.environment,
            provider_environment=economic_book.provider_environment,
        ),
        idem,
    )
    reservation_plan = reservation_book.prepare_consume_mutation(
        event_key=_scoped_identity(
            "atomic-fill-reservation-event",
            *_scoped_environment_identity_parts(
                provider_id=economic_book.provider_id,
                account_id=economic_book.account_id,
                environment=economic_book.environment,
                provider_environment=economic_book.provider_environment,
            ),
            cid,
        ),
        idempotency_key=reservation_component_key,
        reservation_id=rid,
        usage=usage,
        committed_at=when,
        expected_snapshot_digest=reservation_expected_snapshot_digest,
    )
    economic_plan = economic_book.prepare_batch_mutation(
        transactions,
        committed_at=when,
    )

    settlement_plan = None
    if settlement_book is not None:
        batch_transaction_ids = {
            item.transaction_id for item in economic_plan.transactions
        }
        expected_cash_legs: dict[tuple[str, str], Decimal] = {}
        for transaction in economic_plan.transactions:
            for posting in transaction.postings:
                if (
                    posting.ledger_account
                    == f"CASH:{posting.asset_or_currency}"
                ):
                    key = (
                        transaction.transaction_id,
                        posting.asset_or_currency,
                    )
                    expected_cash_legs[key] = (
                        expected_cash_legs.get(key, Decimal("0"))
                        + posting.signed_amount
                    )
        expected_cash_legs = {
            key: value
            for key, value in expected_cash_legs.items()
            if value != 0
        }

        bound_cash_legs: dict[tuple[str, str], Decimal] = {}
        for obligation in settlement_items:
            if not isinstance(obligation, SettlementObligation):
                raise TypeError(
                    "settlement_obligations must contain SettlementObligation"
                )
            if obligation.source_transaction_id not in batch_transaction_ids:
                raise AccountingConflict(
                    "settlement obligation source is absent from atomic economic batch"
                )
            key = (
                obligation.source_transaction_id,
                obligation.currency,
            )
            if key in bound_cash_legs:
                raise AccountingConflict(
                    "atomic fill has duplicate settlement coverage for one cash leg"
                )
            bound_cash_legs[key] = obligation.amount

        if set(bound_cash_legs) != set(expected_cash_legs):
            raise AccountingConflict(
                "settlement obligations do not cover every atomic fill cash leg"
            )
        for key, expected_amount in expected_cash_legs.items():
            if bound_cash_legs[key] != expected_amount:
                raise AccountingConflict(
                    "settlement obligation amount differs from atomic fill cash effect"
                )

        settlement_plan = settlement_book.prepare_register_mutation(
            settlement_items,
            committed_at=when,
        )

    commit_states = [
        reservation_plan.already_committed,
        economic_plan.already_committed,
    ]
    if settlement_plan is not None:
        commit_states.append(settlement_plan.already_committed)
    if provider_fill_binding is not None:
        commit_states.append(provider_fill_binding.already_committed)
    if any(commit_states) and not all(commit_states):
        reservation_book.refresh()
        economic_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        raise AccountingConflict(
            "reservation/economic/settlement fill state is only partially committed"
        )
    if all(commit_states):
        reservation_book.refresh()
        economic_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        return False

    if reservation_plan.envelope is None or economic_plan.envelope is None:
        raise AccountingConflict("fresh atomic fill plan is missing durable events")
    if settlement_plan is not None and settlement_plan.envelope is None:
        raise AccountingConflict(
            "fresh atomic fill settlement plan is missing durable event"
        )
    if (
        provider_fill_binding is not None
        and provider_fill_binding.envelope is None
    ):
        raise AccountingConflict(
            "fresh provider fill financial binding is missing durable event"
        )

    request = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "reservation": reservation_plan.request,
        "economic_batch": economic_plan.request,
        "settlement": (
            None if settlement_plan is None else settlement_plan.request
        ),
        "provider_fill_binding": (
            None
            if provider_fill_binding is None
            else provider_fill_binding.request
        ),
    }
    if economic_book.provider_environment != economic_book.environment:
        request["provider_environment"] = economic_book.provider_environment
    result = {
        "reservation": reservation_plan.snapshot_payload,
        "economic_batch": economic_plan.result,
        "settlement": (
            None if settlement_plan is None else settlement_plan.result
        ),
        "provider_fill_binding": (
            None
            if provider_fill_binding is None
            else provider_fill_binding.result
        ),
    }
    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/atomic-fill/"
            + _scoped_identity(
                "atomic-fill-command",
                *_scoped_environment_identity_parts(
                    provider_id=economic_book.provider_id,
                    account_id=economic_book.account_id,
                    environment=economic_book.environment,
                    provider_environment=economic_book.provider_environment,
                ),
                cid,
            ),
        )
    )
    journal_idempotency_key = "atomic-fill:" + _scoped_identity(
        "atomic-fill-idempotency",
        *_scoped_environment_identity_parts(
            provider_id=economic_book.provider_id,
            account_id=economic_book.account_id,
            environment=economic_book.environment,
            provider_environment=economic_book.provider_environment,
        ),
        idem,
    )
    try:
        _, inserted, _ = economic_book.store.commit_command(
            command_id=command_identity,
            actor="atomic-fill-financial-integration",
            environment=economic_book.environment,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=max(
                reservation_plan.aggregate_version,
                economic_plan.aggregate_version,
                0
                if settlement_plan is None
                else settlement_plan.aggregate_version,
                0
                if provider_fill_binding is None
                else provider_fill_binding.aggregate_version,
            ),
            events=(
                [
                    (reservation_plan.envelope, None),
                    (economic_plan.envelope, "autotrade.economic.events"),
                ]
                + (
                    []
                    if settlement_plan is None
                    else [(settlement_plan.envelope, None)]
                )
                + (
                    []
                    if provider_fill_binding is None
                    else [(provider_fill_binding.envelope, None)]
                )
            ),
        )
    except Exception:
        reservation_book.refresh()
        economic_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        raise

    reservation_book.refresh()
    economic_book.refresh()
    if settlement_book is not None:
        settlement_book.refresh()
    return inserted




def commit_economic_correction_with_settlement_replacement(
    economic_book: DurableProviderEconomicBook,
    settlement_book: DurableSettlementBook,
    *,
    command_id: str,
    idempotency_key: str,
    reversal: JournalTransaction,
    replacement: JournalTransaction,
    settlement_obligations: Iterable[SettlementObligation],
    committed_at: str | None = None,
) -> bool:
    """Atomically bind correction economics to replacement settlement truth.

    A correction reversal cancels the prior trade-date economic fact; it is not
    a new contractual cash settlement. Only the replacement's active CASH legs
    receive new settlement obligations. The prior obligation/evidence remains
    immutable in the settlement journal, while SettlementBook.project() excludes
    it because its source transaction is reversed.

    This integration barrier intentionally owns no new ledger, reservation or
    provider authority. It only composes the existing economic and settlement
    authorities in one JournalStore transaction.
    """

    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(settlement_book, DurableSettlementBook):
        raise TypeError("settlement_book must be DurableSettlementBook")
    if economic_book.store is not settlement_book.store:
        raise ValueError(
            "economic and settlement books must share one JournalStore"
        )
    if (
        settlement_book.scope.provider_id != economic_book.provider_id
        or settlement_book.scope.account_id != economic_book.account_id
        or settlement_book.scope.environment != economic_book.environment
    ):
        raise ValueError(
            "settlement book must share provider/account/environment scope"
        )
    if not isinstance(reversal, JournalTransaction):
        raise TypeError("reversal must be a JournalTransaction")
    if not isinstance(replacement, JournalTransaction):
        raise TypeError("replacement must be a JournalTransaction")
    if reversal.reverses_transaction_id is None:
        raise AccountingConflict(
            "settlement-aware correction requires an explicit reversal"
        )
    if reversal.corrects_transaction_id is not None:
        raise AccountingConflict(
            "correction reversal cannot also be a replacement"
        )
    if replacement.reverses_transaction_id is not None:
        raise AccountingConflict(
            "correction replacement cannot also be a reversal"
        )
    if replacement.corrects_transaction_id != reversal.reverses_transaction_id:
        raise AccountingConflict(
            "correction replacement must target the transaction reversed in the same batch"
        )

    items = tuple(settlement_obligations)
    if not items:
        raise ValueError(
            "correction replacement requires explicit settlement obligations"
        )

    when = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if committed_at is None
        else _instant_text(committed_at, name="committed_at")
    )
    economic_plan = economic_book.prepare_batch_mutation(
        (reversal, replacement),
        committed_at=when,
    )
    canonical_reversal, canonical_replacement = economic_plan.transactions
    if (
        canonical_reversal.reverses_transaction_id is None
        or canonical_replacement.corrects_transaction_id
        != canonical_reversal.reverses_transaction_id
    ):
        raise AccountingConflict(
            "canonical correction batch lost reversal/replacement lineage"
        )

    expected_cash_legs: dict[tuple[str, str], Decimal] = {}
    for posting in canonical_replacement.postings:
        if posting.ledger_account == f"CASH:{posting.asset_or_currency}":
            key = (
                canonical_replacement.transaction_id,
                posting.asset_or_currency,
            )
            expected_cash_legs[key] = (
                expected_cash_legs.get(key, Decimal("0"))
                + posting.signed_amount
            )
    expected_cash_legs = {
        key: amount
        for key, amount in expected_cash_legs.items()
        if amount != 0
    }
    if not expected_cash_legs:
        raise AccountingConflict(
            "settlement-aware correction replacement has no active cash leg"
        )

    bound_cash_legs: dict[tuple[str, str], Decimal] = {}
    for obligation in items:
        if not isinstance(obligation, SettlementObligation):
            raise TypeError(
                "settlement_obligations must contain SettlementObligation"
            )
        if obligation.source_transaction_id != canonical_replacement.transaction_id:
            raise AccountingConflict(
                "correction settlement obligation must bind the replacement transaction"
            )
        key = (
            obligation.source_transaction_id,
            obligation.currency,
        )
        if key in bound_cash_legs:
            raise AccountingConflict(
                "correction has duplicate settlement coverage for one replacement cash leg"
            )
        bound_cash_legs[key] = obligation.amount

    if set(bound_cash_legs) != set(expected_cash_legs):
        raise AccountingConflict(
            "correction settlement obligations do not cover every replacement cash leg"
        )
    for key, expected_amount in expected_cash_legs.items():
        if bound_cash_legs[key] != expected_amount:
            raise AccountingConflict(
                "correction settlement obligation amount differs from replacement cash effect"
            )

    settlement_plan = settlement_book.prepare_register_mutation(
        items,
        committed_at=when,
    )
    states = (
        economic_plan.already_committed,
        settlement_plan.already_committed,
    )
    if any(states) and not all(states):
        economic_book.refresh()
        settlement_book.refresh()
        raise AccountingConflict(
            "economic correction and replacement settlement state are only partially committed"
        )
    if all(states):
        economic_book.refresh()
        settlement_book.refresh()
        return False
    if economic_plan.envelope is None or settlement_plan.envelope is None:
        raise AccountingConflict(
            "fresh settlement-aware correction is missing durable events"
        )

    cid = _text(command_id, name="command_id")
    idem = _text(idempotency_key, name="idempotency_key")
    request = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "economic_correction": economic_plan.request,
        "replacement_settlement": settlement_plan.request,
    }
    result = {
        "economic_correction": economic_plan.result,
        "replacement_settlement": settlement_plan.result,
    }
    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/atomic-settlement-correction/"
            + _scoped_identity(
                "atomic-settlement-correction-command",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                cid,
            ),
        )
    )
    journal_idempotency_key = (
        "atomic-settlement-correction:"
        + _scoped_identity(
            "atomic-settlement-correction-idempotency",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            idem,
        )
    )
    try:
        _, inserted, _ = economic_book.store.commit_command(
            command_id=command_identity,
            actor="atomic-settlement-correction-integration",
            environment=economic_book.environment,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=max(
                economic_plan.aggregate_version,
                settlement_plan.aggregate_version,
            ),
            events=[
                (economic_plan.envelope, "autotrade.economic.events"),
                (settlement_plan.envelope, None),
            ],
        )
    except Exception:
        economic_book.refresh()
        settlement_book.refresh()
        raise

    economic_book.refresh()
    settlement_book.refresh()
    return inserted


def commit_provider_fill_correction_with_settlement_replacement(
    economic_book: DurableProviderEconomicBook,
    settlement_book: DurableSettlementBook,
    *,
    command_id: str,
    idempotency_key: str,
    original_projected_fill: ProjectedFillEvidence,
    original_provider_fill: ProviderFillEvidence,
    corrected_projected_fill: ProjectedFillEvidence,
    corrected_provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    correction_observed_at: str,
    settlement_obligations: Iterable[SettlementObligation],
    committed_at: str | None = None,
) -> bool:
    """Build the canonical provider correction and commit settlement atomically."""

    reversal, replacement = build_provider_fill_correction_transactions(
        book=economic_book,
        provider_id=economic_book.provider_id,
        original_projected_fill=original_projected_fill,
        original_provider_fill=original_provider_fill,
        corrected_projected_fill=corrected_projected_fill,
        corrected_provider_fill=corrected_provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        correction_observed_at=correction_observed_at,
    )
    return commit_economic_correction_with_settlement_replacement(
        economic_book,
        settlement_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reversal=reversal,
        replacement=replacement,
        settlement_obligations=settlement_obligations,
        committed_at=committed_at,
    )


def commit_provider_fill_with_reservation_consumption(
    economic_book: DurableProviderEconomicBook,
    reservation_book: DurableReservationBook,
    *,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    asset_family: str = "CASH_EQUITY",
    observed_at: str | None = None,
    committed_at: str | None = None,
    settlement_book: DurableSettlementBook | None = None,
    settlement_obligations: Iterable[SettlementObligation] = (),
) -> bool:
    """Atomically book one provider fill and consume only evidence-derived resources.

    This is the provider-fill entrypoint for the shared atomic integration
    barrier. It deliberately accepts no caller-authored transaction batch and
    no caller-authored reservation usage map. Both are derived from the same
    normalized provider/projected fill evidence and the admission-bound
    reservation envelope before JournalStore mutation.
    """

    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(reservation_book, DurableReservationBook):
        raise TypeError("reservation_book must be DurableReservationBook")
    rid = _text(reservation_id, name="reservation_id")
    snapshot = reservation_book.get(rid)
    plan = build_provider_fill_financial_plan(
        book=economic_book,
        provider_id=economic_book.provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=expected_instrument,
        settlement_currency=settlement_currency,
        reservation_snapshot=snapshot,
        asset_family=asset_family,
        observed_at=observed_at,
    )
    if plan.reservation_id != rid:
        raise AccountingConflict(
            "provider fill financial plan reservation identity changed"
        )

    caller_idempotency = _text(idempotency_key, name="idempotency_key")
    when = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if committed_at is None
        else _instant_text(committed_at, name="committed_at")
    )
    binding = _prepare_provider_fill_binding(
        economic_book,
        plan=plan,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        committed_at=when,
    )
    return commit_economic_batch_with_reservation_consumption(
        economic_book,
        reservation_book,
        command_id=command_id,
        idempotency_key=f"{caller_idempotency}:provider-fill",
        reservation_id=rid,
        usage=plan.usage,
        transactions=(plan.transaction,),
        reservation_expected_snapshot_digest=plan.reservation_cut_digest,
        committed_at=when,
        settlement_book=settlement_book,
        settlement_obligations=settlement_obligations,
        provider_fill_binding=binding,
    )


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
        provider_environment=activity.provider_environment,
        activity_id=activity.activity_id,
    )
    book_id = _book_id(
        provider_id=provider,
        account_id=account,
        environment=scope,
        provider_environment=activity.provider_environment,
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
    if activity.provider_environment != scope:
        request["provider_environment"] = activity.provider_environment
        request["activity"]["provider_environment"] = (
            activity.provider_environment
        )
    result = {
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
        "activity_id": activity.activity_id,
        "transaction_id": transaction_id,
        "amount": amount_text,
        "currency": activity.currency,
    }
    if activity.provider_environment != scope:
        result["provider_environment"] = activity.provider_environment

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
    if activity.provider_environment != scope:
        economic_payload["provider_environment"] = activity.provider_environment
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
    provider_environment: str | None = None,
) -> EconomicBook:
    """Rebuild canonical economics from the one durable provider/account journal."""

    durable = DurableProviderEconomicBook(
        store,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        provider_environment=provider_environment,
    )
    return EconomicBook(durable.transactions)
