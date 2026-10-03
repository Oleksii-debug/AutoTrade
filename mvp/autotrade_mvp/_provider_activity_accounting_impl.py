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
from typing import Any, Iterable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5
import weakref

from .accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    Posting,
    ScopedEconomicBook,
    book_external_cash_flow,
    canonical_transaction,
)
from .durable_order_projection import (
    DurableOrderBookProjection,
    PreparedOrderMutation,
)
from .durable_reservations import (
    DurableReservationBook,
    reservation_snapshot_digest,
)
from .durable_settlement import DurableSettlementBook
from .fill_accounting import (
    ProjectedFillEvidence,
    ProviderFillFinancialPlan,
    _provider_fill_accounting_evidence_payload,
    build_provider_fill_correction_transactions,
    build_provider_fill_financial_plan,
)
from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_add,
    exact_subtract,
)
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .store_identity import same_journal_backing_object
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
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise AccountingConflict(
            "financial decimal exceeds exact decimal authority"
        ) from error


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



_PROVIDER_FILL_CORRECTION_BINDING_AGGREGATE_TYPE = (
    "provider_fill_reservation_correction_binding"
)
_PROVIDER_FILL_CORRECTION_BINDING_EVENT_TYPE = (
    "ProviderFillReservationCorrectionBound"
)


@dataclass(frozen=True)
class PreparedProviderFillCorrectionBinding:
    """Audit binding for conservative reservation effects of one fill correction.

    Financial authority remains in DurableReservationBook and
    DurableProviderEconomicBook. This append-only evidence records the
    per-provider-execution conservative high-water mark so a correction can
    consume additional capacity atomically without ever releasing authority.
    """

    aggregate_id: str
    envelope: dict[str, Any] | None
    request: dict[str, Any]
    result: dict[str, Any]
    aggregate_version: int
    additional_usage_items: tuple[tuple[str, Decimal], ...]
    reservation_cut_digest: str
    already_committed: bool = False

    @property
    def additional_usage(self) -> Mapping[str, Decimal]:
        return dict(self.additional_usage_items)


def _provider_fill_binding_aggregate_id(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_execution_id: str,
) -> str:
    return _scoped_identity(
        "provider-fill-financial-binding",
        _text(provider_id, name="provider_id").upper(),
        _text(account_id, name="account_id"),
        _environment(environment),
        _text(provider_execution_id, name="provider_execution_id"),
    )


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
    return {
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


def _require_plain_financial_json(
    value: object,
    *,
    name: str,
    depth: int = 0,
) -> None:
    if depth > 32:
        raise AccountingConflict(f"{name} exceeds the canonical JSON depth limit")
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise AccountingConflict(f"{name} contains a non-text JSON key")
            _require_plain_financial_json(
                item,
                name=name,
                depth=depth + 1,
            )
        return
    if type(value) is list:
        for item in value:
            _require_plain_financial_json(
                item,
                name=name,
                depth=depth + 1,
            )
        return
    if value is None or type(value) in {str, int, bool}:
        return
    raise AccountingConflict(f"{name} contains a non-canonical JSON value")


_PROJECTED_FILL_BINDING_FIELDS = frozenset({
    "fill_id",
    "provider_execution_id",
    "intent_id",
    "client_order_id",
    "side",
    "position_side",
    "position_effect",
    "quantity",
    "price",
    "provider_revision",
    "correction_of",
})
_PROVIDER_FILL_BINDING_FIELDS = frozenset({
    "provider_id",
    "account_id",
    "environment",
    "provider_execution_id",
    "client_order_id",
    "instrument",
    "quantity",
    "price",
    "fee_amount",
    "fee_currency",
    "trade_time",
    "side",
    "position_side",
    "position_effect",
    "evidence_refs",
})


def _projected_fill_from_binding_payload(
    payload: Mapping[str, Any],
    *,
    name: str,
) -> ProjectedFillEvidence:
    if type(payload) is not dict or set(payload) != _PROJECTED_FILL_BINDING_FIELDS:
        raise AccountingConflict(f"{name} shape is invalid")
    raw = dict(payload)
    try:
        evidence = ProjectedFillEvidence.create(
            fill_id=raw["fill_id"],
            provider_execution_id=raw["provider_execution_id"],
            intent_id=raw["intent_id"],
            client_order_id=raw["client_order_id"],
            side=raw["side"],
            quantity=raw["quantity"],
            price=raw["price"],
            position_side=raw["position_side"],
            position_effect=raw["position_effect"],
            provider_revision=raw["provider_revision"],
            correction_of=raw["correction_of"],
        )
    except (TypeError, ValueError) as error:
        raise AccountingConflict(f"{name} is invalid") from error
    if _projected_fill_binding_payload(evidence) != raw:
        raise AccountingConflict(f"{name} is not canonical")
    return evidence


def _provider_fill_from_binding_payload(
    payload: Mapping[str, Any],
    *,
    name: str,
) -> ProviderFillEvidence:
    if type(payload) is not dict or set(payload) != _PROVIDER_FILL_BINDING_FIELDS:
        raise AccountingConflict(f"{name} shape is invalid")
    raw = dict(payload)
    refs = raw["evidence_refs"]
    if type(refs) is not list:
        raise AccountingConflict(f"{name} evidence_refs are invalid")
    try:
        evidence = ProviderFillEvidence.create(
            provider_id=raw["provider_id"],
            account_id=raw["account_id"],
            environment=raw["environment"],
            provider_execution_id=raw["provider_execution_id"],
            client_order_id=raw["client_order_id"],
            instrument=raw["instrument"],
            quantity=raw["quantity"],
            price=raw["price"],
            fee_amount=raw["fee_amount"],
            fee_currency=raw["fee_currency"],
            trade_time=raw["trade_time"],
            side=raw["side"],
            position_side=raw["position_side"],
            position_effect=raw["position_effect"],
            evidence_refs=tuple(refs),
        )
    except (TypeError, ValueError) as error:
        raise AccountingConflict(f"{name} is invalid") from error
    if _provider_fill_binding_payload(evidence) != raw:
        raise AccountingConflict(f"{name} is not canonical")
    return evidence


def _prepare_provider_fill_binding(
    economic_book: "DurableProviderEconomicBook",
    *,
    plan: ProviderFillFinancialPlan,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    committed_at: str,
) -> PreparedProviderFillBinding:
    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    if not isinstance(plan, ProviderFillFinancialPlan):
        raise TypeError("plan must be ProviderFillFinancialPlan")
    if not isinstance(projected_fill, ProjectedFillEvidence):
        raise TypeError("projected_fill must be ProjectedFillEvidence")
    if not isinstance(provider_fill, ProviderFillEvidence):
        raise TypeError("provider_fill must be ProviderFillEvidence")
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
    events = _economic_store_load_events(economic_book,
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




def _positive_usage_map(value: object, *, name: str) -> dict[str, Decimal]:
    if not isinstance(value, Mapping) or not value:
        raise AccountingConflict(f"{name} must be a non-empty resource mapping")
    result: dict[str, Decimal] = {}
    for raw_resource, raw_amount in value.items():
        resource = _text(raw_resource, name=f"{name}.resource")
        if resource in result:
            raise AccountingConflict(f"{name} contains duplicate resource keys")
        amount = _decimal(raw_amount, name=f"{name}[{resource}]")
        if amount <= 0:
            raise AccountingConflict(f"{name} amounts must be strictly positive")
        result[resource] = amount
    return dict(sorted(result.items()))


def _usage_payload(usage: Mapping[str, Decimal]) -> dict[str, str]:
    try:
        return {
            key: canonical_decimal_text(value)
            for key, value in sorted(usage.items())
        }
    except ExactDecimalError as error:
        raise AccountingConflict(
            "financial usage exceeds exact decimal authority"
        ) from error


def _exact_usage_increase(amount: Decimal, previous: Decimal) -> Decimal:
    try:
        return exact_subtract(amount, previous)
    except ExactDecimalError as error:
        raise AccountingConflict(
            "financial usage delta exceeds exact decimal authority"
        ) from error


def _cash_leg_totals(
    transactions: Iterable[JournalTransaction],
) -> dict[tuple[str, str], Decimal]:
    totals: dict[tuple[str, str], Decimal] = {}
    try:
        for transaction in transactions:
            if not isinstance(transaction, JournalTransaction):
                raise TypeError("transactions must contain JournalTransaction")
            for posting in transaction.postings:
                if posting.ledger_account != f"CASH:{posting.asset_or_currency}":
                    continue
                key = (transaction.transaction_id, posting.asset_or_currency)
                totals[key] = exact_add(
                    totals.get(key, Decimal("0")),
                    posting.signed_amount,
                )
    except ExactDecimalError as error:
        raise AccountingConflict(
            "cash-leg aggregation exceeds exact decimal authority"
        ) from error
    return {
        key: amount
        for key, amount in totals.items()
        if amount != 0
    }


def _cash_outflow_usage(transaction: JournalTransaction) -> dict[str, Decimal]:
    """Derive conservative cash resource usage from canonical fill postings.

    Only negative CASH postings consume reserved capacity. Positive cash from a
    rebate never releases or manufactures authority.
    """

    if not isinstance(transaction, JournalTransaction):
        raise TypeError("transaction must be JournalTransaction")
    usage: dict[str, Decimal] = {}
    for posting in transaction.postings:
        expected_account = f"CASH:{posting.asset_or_currency}"
        if posting.ledger_account != expected_account or posting.signed_amount >= 0:
            continue
        try:
            usage[expected_account] = exact_subtract(
                usage.get(expected_account, Decimal("0")),
                posting.signed_amount,
            )
        except ExactDecimalError as error:
            raise AccountingConflict(
                "provider fill correction cash usage exceeds exact decimal authority"
            ) from error
    if not usage:
        raise AccountingConflict(
            "provider fill correction has no conservative cash outflow usage"
        )
    return dict(sorted(usage.items()))


def _provider_fill_correction_binding_aggregate_id(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_execution_id: str,
) -> str:
    return _scoped_identity(
        "provider-fill-reservation-correction-binding",
        _text(provider_id, name="provider_id").upper(),
        _text(account_id, name="account_id"),
        _environment(environment),
        _text(provider_execution_id, name="provider_execution_id"),
    )


def _prepare_provider_fill_correction_binding(
    economic_book: "DurableProviderEconomicBook",
    reservation_book: DurableReservationBook,
    *,
    reservation_id: str,
    original_projected_fill: ProjectedFillEvidence,
    original_provider_fill: ProviderFillEvidence,
    corrected_projected_fill: ProjectedFillEvidence,
    corrected_provider_fill: ProviderFillEvidence,
    replacement: JournalTransaction,
    asset_family: str,
    committed_at: str,
) -> PreparedProviderFillCorrectionBinding:
    """Prepare correction high-water evidence without mutating financial state."""

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        reservation_book,
        expected_type=DurableReservationBook,
        subject="reservation book",
    )
    if (
        economic_book.environment != reservation_book.environment
        or economic_book.account_id != reservation_book.account_id
    ):
        raise ValueError(
            "economic and reservation books must share account/environment scope"
        )
    if _text(asset_family, name="asset_family").upper() != "CASH_EQUITY":
        raise AccountingConflict(
            "provider fill correction reservation mapping is not qualified "
            "for this asset family"
        )
    if corrected_provider_fill.side != "BUY":
        raise AccountingConflict(
            "cash-equity correction reservation mapping is qualified only for BUY fills"
        )
    if corrected_provider_fill.position_side is not None:
        raise AccountingConflict(
            "cash-equity correction reservation mapping rejects derivative position_side"
        )

    rid = _text(reservation_id, name="reservation_id")
    execution_id = _text(
        corrected_provider_fill.provider_execution_id,
        name="provider_execution_id",
    )
    if original_provider_fill.provider_execution_id != execution_id:
        raise AccountingConflict("correction provider execution identity changed")
    if corrected_projected_fill.provider_execution_id != execution_id:
        raise AccountingConflict("corrected projection execution identity changed")

    initial_aggregate_id = _provider_fill_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_execution_id=execution_id,
    )
    initial_events = _economic_store_load_events(economic_book,
        _PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        initial_aggregate_id,
    )
    if len(initial_events) != 1:
        raise AccountingConflict(
            "provider fill correction requires exactly one initial financial binding"
        )
    initial_event = initial_events[0]
    if (
        initial_event.get("event_type") != _PROVIDER_FILL_BINDING_EVENT_TYPE
        or int(initial_event.get("aggregate_version", 0)) != 1
    ):
        raise AccountingConflict("initial provider fill financial binding is invalid")
    initial_payload = initial_event.get("payload")
    if type(initial_payload) is not dict:
        raise AccountingConflict("initial provider fill binding payload is invalid")
    _require_plain_financial_json(
        initial_payload,
        name="initial provider fill binding payload",
    )
    if payload_digest(initial_payload) != initial_event.get("payload_hash"):
        raise AccountingConflict("initial provider fill binding payload hash is invalid")
    initial_request = initial_payload.get("request")
    if type(initial_request) is not dict:
        raise AccountingConflict("initial provider fill binding request is invalid")
    initial_request = dict(initial_request)
    if initial_payload.get("request_digest") != payload_digest(initial_request):
        raise AccountingConflict("initial provider fill binding request digest is invalid")
    if (
        initial_request.get("provider_id") != economic_book.provider_id
        or initial_request.get("account_id") != economic_book.account_id
        or initial_request.get("environment") != economic_book.environment
        or initial_request.get("provider_execution_id") != execution_id
        or initial_request.get("reservation_id") != rid
        or initial_request.get("intent_id") != corrected_projected_fill.intent_id
    ):
        raise AccountingConflict(
            "provider fill correction does not match the initial financial binding"
        )
    initial_provider = initial_request.get("provider_fill")
    initial_projected = initial_request.get("projected_fill")
    initial_projected_evidence = _projected_fill_from_binding_payload(
        initial_projected,
        name="initial provider fill projected evidence",
    )
    initial_provider_evidence = _provider_fill_from_binding_payload(
        initial_provider,
        name="initial provider fill evidence",
    )
    if (
        initial_projected_evidence.fill_id != initial_request.get("fill_id")
        or initial_projected_evidence.provider_execution_id != execution_id
        or initial_projected_evidence.intent_id != corrected_projected_fill.intent_id
        or initial_projected_evidence.correction_of is not None
        or initial_provider_evidence.provider_id != economic_book.provider_id
        or initial_provider_evidence.account_id != economic_book.account_id
        or initial_provider_evidence.environment != economic_book.environment
        or initial_provider_evidence.provider_execution_id != execution_id
        or initial_provider_evidence.client_order_id
        != initial_projected_evidence.client_order_id
        or initial_provider_evidence.side != "BUY"
        or initial_projected_evidence.side != "BUY"
        or initial_provider_evidence.side != initial_projected_evidence.side
        or initial_provider_evidence.position_side is not None
        or initial_provider_evidence.position_side
        != initial_projected_evidence.position_side
        or initial_provider_evidence.position_effect
        != initial_projected_evidence.position_effect
        or initial_provider_evidence.quantity != initial_projected_evidence.quantity
        or initial_provider_evidence.price != initial_projected_evidence.price
    ):
        raise AccountingConflict(
            "initial provider fill binding evidence semantics are invalid"
        )
    active_projected_digest = _text(
        initial_request.get("projected_fill_digest"),
        name="initial projected_fill_digest",
    )
    active_provider_digest = _text(
        initial_request.get("provider_fill_digest"),
        name="initial provider_fill_digest",
    )
    if (
        active_projected_digest != payload_digest(initial_projected)
        or active_provider_digest != payload_digest(initial_provider)
    ):
        raise AccountingConflict(
            "initial provider fill binding evidence digests are invalid"
        )

    snapshot = reservation_book.get(rid)
    if snapshot.intent_id != corrected_projected_fill.intent_id:
        raise AccountingConflict(
            "provider fill correction intent does not match admitted reservation"
        )
    initial_usage = _positive_usage_map(
        initial_request.get("derived_usage"),
        name="initial provider fill usage",
    )
    for resource, amount in initial_usage.items():
        if resource not in snapshot.original or amount > snapshot.original[resource]:
            raise AccountingConflict(
                "initial provider fill usage exceeds the admitted reservation envelope"
            )

    initial_transaction_id = _text(
        initial_request.get("transaction_id"),
        name="initial transaction_id",
    )
    initial_transactions = [
        transaction
        for transaction in economic_book.transactions
        if transaction.transaction_id == initial_transaction_id
    ]
    if len(initial_transactions) != 1:
        raise AccountingConflict(
            "initial provider fill binding does not identify one durable economic transaction"
        )
    initial_transaction = initial_transactions[0]
    initial_transaction_digest = _text(
        initial_request.get("transaction_digest"),
        name="initial transaction_digest",
    )
    if initial_transaction_digest != payload_digest(
        canonical_transaction(initial_transaction)
    ):
        raise AccountingConflict(
            "initial provider fill binding transaction digest is invalid"
        )
    expected_order_key = (
        f"provider:{economic_book.provider_id}:execution:{execution_id}"
    )
    expected_cause_event_id = (
        f"provider:{economic_book.provider_id}:environment:{economic_book.environment}:"
        f"account:{economic_book.account_id}:execution:{execution_id}"
    )
    if (
        initial_transaction.economic_order_key != expected_order_key
        or initial_transaction.cause_event_id != expected_cause_event_id
    ):
        raise AccountingConflict(
            "initial provider fill binding transaction identity is invalid"
        )
    if _cash_outflow_usage(initial_transaction) != initial_usage:
        raise AccountingConflict(
            "initial provider fill usage does not match durable economic transaction"
        )

    aggregate_id = _provider_fill_correction_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_execution_id=execution_id,
    )
    events = _economic_store_load_events(economic_book,
        _PROVIDER_FILL_CORRECTION_BINDING_AGGREGATE_TYPE,
        aggregate_id,
    )
    conservative_usage = dict(initial_usage)
    active_fill_id = _text(initial_request.get("fill_id"), name="initial fill_id")
    active_transaction_id = initial_transaction.transaction_id
    active_projected_evidence = initial_projected_evidence
    active_provider_evidence = initial_provider_evidence
    seen_fill_ids = {active_fill_id}
    last_event: Mapping[str, Any] | None = None
    last_request: dict[str, Any] | None = None

    for expected_version, event in enumerate(events, 1):
        if (
            event.get("event_type") != _PROVIDER_FILL_CORRECTION_BINDING_EVENT_TYPE
            or int(event.get("aggregate_version", 0)) != expected_version
        ):
            raise AccountingConflict(
                "provider fill correction binding versions are invalid"
            )
        payload = event.get("payload")
        if type(payload) is not dict:
            raise AccountingConflict("provider fill correction binding payload is invalid")
        _require_plain_financial_json(
            payload,
            name="provider fill correction binding payload",
        )
        if payload_digest(payload) != event.get("payload_hash"):
            raise AccountingConflict(
                "provider fill correction binding payload hash is invalid"
            )
        if (
            payload.get("provider_id") != economic_book.provider_id
            or payload.get("account_id") != economic_book.account_id
            or payload.get("environment") != economic_book.environment
            or payload.get("provider_execution_id") != execution_id
        ):
            raise AccountingConflict(
                "provider fill correction binding scope is invalid"
            )
        request = payload.get("request")
        if type(request) is not dict:
            raise AccountingConflict("provider fill correction request is invalid")
        request = dict(request)
        if payload.get("request_digest") != payload_digest(request):
            raise AccountingConflict(
                "provider fill correction request digest is invalid"
            )
        if (
            request.get("reservation_id") != rid
            or request.get("intent_id") != corrected_projected_fill.intent_id
            or request.get("provider_execution_id") != execution_id
            or request.get("previous_fill_id") != active_fill_id
        ):
            raise AccountingConflict(
                "provider fill correction binding chain is invalid"
            )
        if (
            request.get("original_projected_fill_digest")
            != active_projected_digest
            or request.get("original_provider_fill_digest")
            != active_provider_digest
        ):
            raise AccountingConflict(
                "provider fill correction evidence lineage is invalid"
            )
        previous_usage = _positive_usage_map(
            request.get("previous_conservative_usage"),
            name="previous conservative correction usage",
        )
        if previous_usage != conservative_usage:
            raise AccountingConflict(
                "provider fill correction conservative usage chain is invalid"
            )
        corrected_usage = _positive_usage_map(
            request.get("corrected_active_usage"),
            name="corrected active usage",
        )
        replacement_transaction_digest = _text(
            request.get("replacement_transaction_digest"),
            name="replacement_transaction_digest",
        )
        replacement_transactions = [
            transaction
            for transaction in economic_book.transactions
            if payload_digest(canonical_transaction(transaction))
            == replacement_transaction_digest
        ]
        if len(replacement_transactions) != 1:
            raise AccountingConflict(
                "provider fill correction binding does not identify one durable replacement transaction"
            )
        historical_replacement = replacement_transactions[0]
        if (
            historical_replacement.economic_order_key != expected_order_key
            or historical_replacement.corrects_transaction_id
            != active_transaction_id
            or historical_replacement.reverses_transaction_id is not None
        ):
            raise AccountingConflict(
                "provider fill correction durable replacement lineage is invalid"
            )
        if _cash_outflow_usage(historical_replacement) != corrected_usage:
            raise AccountingConflict(
                "provider fill correction active usage does not match durable economic replacement"
            )
        expected_resulting = {
            resource: max(
                conservative_usage.get(resource, Decimal("0")),
                corrected_usage.get(resource, Decimal("0")),
            )
            for resource in set(conservative_usage) | set(corrected_usage)
        }
        expected_additional = {
            resource: _exact_usage_increase(
                amount,
                conservative_usage.get(resource, Decimal("0")),
            )
            for resource, amount in expected_resulting.items()
            if amount > conservative_usage.get(resource, Decimal("0"))
        }
        if _usage_payload(expected_resulting) != request.get(
            "resulting_conservative_usage"
        ):
            raise AccountingConflict(
                "provider fill correction resulting usage is invalid"
            )
        if _usage_payload(expected_additional) != request.get("additional_usage"):
            raise AccountingConflict(
                "provider fill correction additional usage is invalid"
            )
        corrected_fill_id = _text(
            request.get("corrected_fill_id"), name="corrected_fill_id"
        )
        if corrected_fill_id in seen_fill_ids:
            raise AccountingConflict(
                "provider fill correction reuses an existing fill identity"
            )
        corrected_projected_digest = _text(
            request.get("corrected_projected_fill_digest"),
            name="corrected_projected_fill_digest",
        )
        corrected_provider_digest = _text(
            request.get("corrected_provider_fill_digest"),
            name="corrected_provider_fill_digest",
        )
        corrected_projected_payload = request.get("corrected_projected_fill")
        corrected_provider_payload = request.get("corrected_provider_fill")
        if (
            type(corrected_projected_payload) is not dict
            or type(corrected_provider_payload) is not dict
            or corrected_projected_digest
            != payload_digest(corrected_projected_payload)
            or corrected_provider_digest
            != payload_digest(corrected_provider_payload)
        ):
            raise AccountingConflict(
                "provider fill correction corrected evidence digests are invalid"
            )
        historical_projected = _projected_fill_from_binding_payload(
            corrected_projected_payload,
            name="historical corrected projected fill evidence",
        )
        historical_provider = _provider_fill_from_binding_payload(
            corrected_provider_payload,
            name="historical corrected provider fill evidence",
        )
        if (
            request.get("correction_of") != active_fill_id
            or historical_projected.fill_id != corrected_fill_id
            or historical_projected.correction_of != active_fill_id
            or historical_projected.provider_revision is None
            or historical_projected.provider_execution_id != execution_id
            or historical_projected.intent_id
            != initial_projected_evidence.intent_id
            or historical_projected.client_order_id
            != initial_projected_evidence.client_order_id
            or historical_projected.side != initial_projected_evidence.side
            or historical_projected.position_side
            != initial_projected_evidence.position_side
            or historical_projected.position_effect
            != initial_projected_evidence.position_effect
            or historical_provider.provider_id != economic_book.provider_id
            or historical_provider.account_id != economic_book.account_id
            or historical_provider.environment != economic_book.environment
            or historical_provider.provider_execution_id != execution_id
            or historical_provider.client_order_id
            != historical_projected.client_order_id
            or historical_provider.instrument != initial_provider_evidence.instrument
            or historical_provider.side != historical_projected.side
            or historical_provider.position_side
            != historical_projected.position_side
            or historical_provider.position_effect
            != historical_projected.position_effect
            or historical_provider.quantity != historical_projected.quantity
            or historical_provider.price != historical_projected.price
        ):
            raise AccountingConflict(
                "provider fill correction corrected evidence semantics are invalid"
            )

        correction_observed_at = historical_replacement.observed_at
        if correction_observed_at is None:
            raise AccountingConflict(
                "provider fill correction durable replacement observation is missing"
            )
        correction_observed_at = _instant_text(
            correction_observed_at,
            name="historical correction observed_at",
        )
        original_accounting_evidence = _provider_fill_accounting_evidence_payload(
            provider=economic_book.provider_id,
            book=economic_book,
            projected_fill=active_projected_evidence,
            provider_fill=active_provider_evidence,
        )
        corrected_accounting_evidence = _provider_fill_accounting_evidence_payload(
            provider=economic_book.provider_id,
            book=economic_book,
            projected_fill=historical_projected,
            provider_fill=historical_provider,
        )
        historical_correction_evidence = {
            "schema_version": "1.1.0",
            "provider_id": economic_book.provider_id,
            "environment": economic_book.environment,
            "account_id": economic_book.account_id,
            "correction_of": historical_projected.correction_of,
            "correction_observed_at": correction_observed_at,
            "original_transaction_id": active_transaction_id,
            "original": original_accounting_evidence,
            "corrected": {
                **corrected_accounting_evidence,
                "correction_of": historical_projected.correction_of,
            },
        }
        historical_correction_digest = payload_digest(
            historical_correction_evidence
        ).removeprefix("sha256:")
        correction_prefix = (
            f"provider:{economic_book.provider_id}:"
            f"environment:{economic_book.environment}:"
            f"account:{economic_book.account_id}:"
            f"correction:{historical_correction_digest}"
        )
        if (
            historical_replacement.transaction_id
            != f"provider-fill-correction-replacement:{historical_correction_digest}"
            or historical_replacement.cause_event_id
            != f"{correction_prefix}:replacement"
        ):
            raise AccountingConflict(
                "provider fill correction revision-bearing durable identity is invalid"
            )
        matching_reversals = [
            transaction
            for transaction in economic_book.transactions
            if (
                transaction.transaction_id
                == f"provider-fill-correction-reversal:{historical_correction_digest}"
                and transaction.cause_event_id == f"{correction_prefix}:reversal"
                and transaction.reverses_transaction_id == active_transaction_id
                and transaction.observed_at == correction_observed_at
            )
        ]
        if len(matching_reversals) != 1:
            raise AccountingConflict(
                "provider fill correction durable reversal identity is invalid"
            )

        seen_fill_ids.add(corrected_fill_id)
        active_fill_id = corrected_fill_id
        active_transaction_id = historical_replacement.transaction_id
        active_projected_evidence = historical_projected
        active_provider_evidence = historical_provider
        active_projected_digest = corrected_projected_digest
        active_provider_digest = corrected_provider_digest
        conservative_usage = expected_resulting
        last_event = event
        last_request = request

    original_payload = _projected_fill_binding_payload(original_projected_fill)
    original_provider_payload = _provider_fill_binding_payload(original_provider_fill)
    corrected_payload = _projected_fill_binding_payload(corrected_projected_fill)
    corrected_provider_payload = _provider_fill_binding_payload(corrected_provider_fill)
    replacement_digest = payload_digest(canonical_transaction(replacement))
    stable_request = {
        "previous_fill_id": original_projected_fill.fill_id,
        "corrected_fill_id": corrected_projected_fill.fill_id,
        "correction_of": corrected_projected_fill.correction_of,
        "original_projected_fill_digest": payload_digest(original_payload),
        "original_provider_fill_digest": payload_digest(original_provider_payload),
        "corrected_projected_fill_digest": payload_digest(corrected_payload),
        "corrected_provider_fill_digest": payload_digest(corrected_provider_payload),
        "replacement_transaction_digest": replacement_digest,
    }

    if (
        last_request is not None
        and last_request.get("corrected_fill_id") == corrected_projected_fill.fill_id
    ):
        if any(last_request.get(key) != value for key, value in stable_request.items()):
            raise AccountingConflict(
                "existing provider fill correction binding conflicts with supplied evidence"
            )
        cut = _text(
            last_request.get("reservation_cut_digest"),
            name="reservation_cut_digest",
        )
        stored_additional_raw = last_request.get("additional_usage")
        if not isinstance(stored_additional_raw, Mapping):
            raise AccountingConflict(
                "existing provider fill correction additional usage is invalid"
            )
        stored_additional = (
            {}
            if not stored_additional_raw
            else _positive_usage_map(
                stored_additional_raw,
                name="existing provider fill correction additional usage",
            )
        )
        return PreparedProviderFillCorrectionBinding(
            aggregate_id=aggregate_id,
            envelope=None,
            request=last_request,
            result={
                "correction_binding_event_id": last_event["event_id"],
                "resulting_conservative_usage": last_request[
                    "resulting_conservative_usage"
                ],
            },
            aggregate_version=len(events),
            additional_usage_items=tuple(sorted(stored_additional.items())),
            reservation_cut_digest=cut,
            already_committed=True,
        )

    if active_fill_id != original_projected_fill.fill_id:
        raise AccountingConflict(
            "provider fill correction does not extend the active correction lineage"
        )
    if (
        stable_request["original_projected_fill_digest"]
        != active_projected_digest
        or stable_request["original_provider_fill_digest"]
        != active_provider_digest
    ):
        raise AccountingConflict(
            "provider fill correction original evidence does not match active lineage"
        )

    corrected_usage = _cash_outflow_usage(replacement)
    for resource, amount in corrected_usage.items():
        if resource not in snapshot.original:
            raise AccountingConflict(
                f"provider fill correction requires unreserved resource {resource}"
            )
        if amount > snapshot.original[resource]:
            raise AccountingConflict(
                f"provider fill correction usage exceeds admitted reservation for {resource}"
            )

    resulting_usage = {
        resource: max(
            conservative_usage.get(resource, Decimal("0")),
            corrected_usage.get(resource, Decimal("0")),
        )
        for resource in set(conservative_usage) | set(corrected_usage)
    }
    additional_usage = {
        resource: _exact_usage_increase(
            amount,
            conservative_usage.get(resource, Decimal("0")),
        )
        for resource, amount in resulting_usage.items()
        if amount > conservative_usage.get(resource, Decimal("0"))
    }
    reservation_cut = reservation_snapshot_digest(snapshot)
    request = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "reservation_id": rid,
        "intent_id": corrected_projected_fill.intent_id,
        "provider_execution_id": execution_id,
        **stable_request,
        "original_projected_fill": original_payload,
        "original_provider_fill": original_provider_payload,
        "corrected_projected_fill": corrected_payload,
        "corrected_provider_fill": corrected_provider_payload,
        "reservation_cut_digest": reservation_cut,
        "previous_conservative_usage": _usage_payload(conservative_usage),
        "corrected_active_usage": _usage_payload(corrected_usage),
        "resulting_conservative_usage": _usage_payload(resulting_usage),
        "additional_usage": _usage_payload(additional_usage),
    }
    request_digest = payload_digest(request)
    next_version = len(events) + 1
    event_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://events.autotrade.local/provider-fill-reservation-correction/"
            + aggregate_id
            + "/"
            + corrected_projected_fill.fill_id,
        )
    )
    payload = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "provider_execution_id": execution_id,
        "request_digest": request_digest,
        "request": request,
    }
    envelope = {
        "event_id": event_id,
        "event_type": _PROVIDER_FILL_CORRECTION_BINDING_EVENT_TYPE,
        "aggregate_type": _PROVIDER_FILL_CORRECTION_BINDING_AGGREGATE_TYPE,
        "aggregate_id": aggregate_id,
        "aggregate_version": str(next_version),
        "committed_at": _instant_text(committed_at, name="committed_at"),
        "payload": payload,
        "payload_hash": payload_digest(payload),
    }
    return PreparedProviderFillCorrectionBinding(
        aggregate_id=aggregate_id,
        envelope=envelope,
        request=request,
        result={
            "correction_binding_event_id": event_id,
            "resulting_conservative_usage": _usage_payload(resulting_usage),
        },
        aggregate_version=next_version,
        additional_usage_items=tuple(sorted(additional_usage.items())),
        reservation_cut_digest=reservation_cut,
    )


@dataclass
class _DurableProviderEconomicBookAuthority:
    """Detached snapshot of one selected durable financial-book generation."""

    store: JournalStore
    store_identity: object
    provider_id: str
    account_id: str
    environment: str
    book_id: str
    projection_digest: str


def _economic_projection_digest(value: object) -> str:
    if type(value) is not EconomicBook:
        raise AccountingConflict(
            "durable economic projection must be the canonical EconomicBook"
        )
    return EconomicBook.audit_digest(value)


def _install_durable_provider_economic_book_authority():
    """Keep original composition behind callback-free weak process state."""

    authorities: dict[
        int,
        tuple[weakref.ReferenceType, _DurableProviderEconomicBookAuthority],
    ] = {}

    def prune_dead() -> None:
        dead = [
            object_id
            for object_id, (value_ref, _authority) in authorities.items()
            if value_ref() is None
        ]
        for object_id in dead:
            authorities.pop(object_id, None)

    def registered_authority(
        value: object,
    ) -> _DurableProviderEconomicBookAuthority | None:
        entry = authorities.get(id(value))
        if entry is None:
            return None
        value_ref, authority = entry
        current = value_ref()
        if current is value:
            return authority
        if current is None:
            authorities.pop(id(value), None)
            return None
        raise AccountingConflict(
            "durable economic-book binding identity collision"
        )

    def is_registered(value: object) -> bool:
        try:
            prune_dead()
            return registered_authority(value) is not None
        except TypeError:
            return False

    def bound_authority(
        value: object,
    ) -> _DurableProviderEconomicBookAuthority:
        if type(value) is not DurableProviderEconomicBook:
            raise TypeError("economic_book must be exact DurableProviderEconomicBook")
        prune_dead()
        authority = registered_authority(value)
        if authority is None:
            raise AccountingConflict(
                "durable economic-book authority is not established"
            )

        state = object.__getattribute__(value, "__dict__")
        state_keys = tuple(state)
        if any(type(name) is not str for name in state_keys):
            raise AccountingConflict(
                "durable economic-book instance state keys must be exact str"
            )
        class_owned_names = {
            name
            for base in DurableProviderEconomicBook.__mro__
            for name in base.__dict__
        }
        if class_owned_names.intersection(state_keys):
            raise AccountingConflict(
                "durable economic-book instance state is shadowed"
            )

        if state.get("store") is not authority.store:
            raise AccountingConflict("durable economic-book JournalStore changed")
        if (
            state.get("provider_id") != authority.provider_id
            or state.get("account_id") != authority.account_id
            or state.get("environment") != authority.environment
            or state.get("book_id") != authority.book_id
        ):
            raise AccountingConflict(
                "durable economic-book financial scope changed"
            )

        projection = state.get("_book")
        if _economic_projection_digest(projection) != authority.projection_digest:
            raise AccountingConflict(
                "durable economic projection changed outside canonical reload"
            )

        current_identity = require_exact_journal_store_authority(
            authority.store,
            subject="durable provider economic JournalStore",
        )
        if current_identity != authority.store_identity:
            raise RuntimeError(
                "durable economic-book JournalStore generation changed"
            )
        return authority

    def detached_authority(
        value: object,
    ) -> _DurableProviderEconomicBookAuthority:
        authority = bound_authority(value)
        return _DurableProviderEconomicBookAuthority(
            store=authority.store,
            store_identity=authority.store_identity,
            provider_id=authority.provider_id,
            account_id=authority.account_id,
            environment=authority.environment,
            book_id=authority.book_id,
            projection_digest=authority.projection_digest,
        )

    def reload_projection(value: object) -> None:
        authority = bound_authority(value)
        with journal_store_authority_scope(
            authority.store,
            authority.store_identity,
        ):
            events = JournalStore.load_events(
                authority.store,
                "economic_book",
                authority.book_id,
            )
        candidate = DurableProviderEconomicBook._replay(value, events)
        projection = object.__getattribute__(candidate, "_book")
        object.__setattr__(value, "_book", projection)
        authority.projection_digest = _economic_projection_digest(projection)

    def initialize(
        value: object,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ) -> None:
        if type(value) is not DurableProviderEconomicBook:
            raise TypeError("economic_book must be exact DurableProviderEconomicBook")
        prune_dead()
        if registered_authority(value) is not None:
            raise AccountingConflict(
                "durable economic-book authority is already established"
            )
        if type(store) is not JournalStore:
            raise TypeError("store must be exact JournalStore")
        identity = require_exact_journal_store_authority(
            store,
            subject="durable provider economic JournalStore",
        )

        object.__setattr__(value, "store", store)
        object.__setattr__(
            value,
            "provider_id",
            _text(provider_id, name="provider_id").upper(),
        )
        ScopedEconomicBook.__init__(
            value,
            environment=environment,
            account_id=account_id,
        )
        object.__setattr__(
            value,
            "book_id",
            _book_id(
                provider_id=object.__getattribute__(value, "provider_id"),
                account_id=object.__getattribute__(value, "account_id"),
                environment=object.__getattribute__(value, "environment"),
            ),
        )
        state = object.__getattribute__(value, "__dict__")
        object_id = id(value)
        authorities[object_id] = (
            weakref.ref(value),
            _DurableProviderEconomicBookAuthority(
                store=store,
                store_identity=identity,
                provider_id=state["provider_id"],
                account_id=state["account_id"],
                environment=state["environment"],
                book_id=state["book_id"],
                projection_digest=_economic_projection_digest(state["_book"]),
            ),
        )
        try:
            reload_projection(value)
        except Exception:
            entry = authorities.get(object_id)
            if entry is not None and entry[0]() is value:
                authorities.pop(object_id, None)
            raise

    return is_registered, detached_authority, initialize, reload_projection


def _economic_store_load_events(
    economic_book: object,
    aggregate_type: str,
    aggregate_id: str,
) -> list[dict[str, Any]]:
    authority = _require_durable_provider_economic_book_authority(economic_book)
    with journal_store_authority_scope(
        authority.store,
        authority.store_identity,
    ):
        return JournalStore.load_events(
            authority.store,
            aggregate_type,
            aggregate_id,
        )


def _economic_store_commit_command(
    economic_book: object,
    **kwargs: Any,
):
    authority = _require_durable_provider_economic_book_authority(economic_book)
    with journal_store_authority_scope(
        authority.store,
        authority.store_identity,
    ):
        return JournalStore.commit_command(authority.store, **kwargs)


def _economic_store_load_command_event_batch(
    economic_book: object,
    **kwargs: Any,
):
    authority = _require_durable_provider_economic_book_authority(economic_book)
    with journal_store_authority_scope(
        authority.store,
        authority.store_identity,
    ):
        return JournalStore.load_command_event_batch(authority.store, **kwargs)



def _require_same_financial_journal_generation(
    economic_book: object,
    other_book: object,
    *,
    expected_type: type,
    subject: str,
) -> JournalStore:
    """Require two financial projections to resolve to one physical journal generation."""

    authority = _require_durable_provider_economic_book_authority(economic_book)
    if type(other_book) is not expected_type:
        raise TypeError(f"{subject} must be exact {expected_type.__name__}")
    state = object.__getattribute__(other_book, "__dict__")
    other_store = state.get("store")
    other_identity = require_exact_journal_store_authority(
        other_store,
        subject=f"{subject} JournalStore",
    )
    if not same_journal_backing_object(authority.store_identity, other_identity):
        raise ValueError(
            f"economic book and {subject} must share one JournalStore generation"
        )
    return other_store


class DurableProviderEconomicBook(ScopedEconomicBook):
    """JournalStore-backed provider/account economic book.

    This durable facade is sealed to one exact JournalStore generation and one
    provider/account/environment scope. Authority-bearing access revalidates raw
    instance state before callbacks, so an exact-base instance cannot gain new
    financial meaning through post-construction method/state shadowing.
    """

    _BATCH_EVENT = "EconomicTransactionBatchBooked"
    _SINGLE_EVENT = "EconomicTransactionBooked"
    _ACTOR = "provider-economic-accounting"
    _AUTHORITY_STATE_NAMES = frozenset(
        {"store", "provider_id", "account_id", "environment", "book_id", "_book"}
    )

    def __getattribute__(self, name: str):
        is_registered = globals().get(
            "_durable_provider_economic_book_authority_is_registered"
        )
        if (
            type(name) is str
            and name != "__dict__"
            and is_registered is not None
            and is_registered(self)
        ):
            class_owned = any(
                name in base.__dict__ for base in DurableProviderEconomicBook.__mro__
            )
            if (
                name in DurableProviderEconomicBook._AUTHORITY_STATE_NAMES
                or class_owned
            ):
                _require_durable_provider_economic_book_authority(self)
        return object.__getattribute__(self, name)

    def __setattr__(self, name: str, value: object) -> None:
        is_registered = globals().get(
            "_durable_provider_economic_book_authority_is_registered"
        )
        if is_registered is not None and is_registered(self):
            class_owned = any(
                name in base.__dict__ for base in DurableProviderEconomicBook.__mro__
            )
            if (
                name in DurableProviderEconomicBook._AUTHORITY_STATE_NAMES
                or class_owned
            ):
                raise AccountingConflict(
                    "durable economic-book authority state is immutable"
                )
        object.__setattr__(self, name, value)

    def __init__(
        self,
        store: JournalStore,
        *,
        provider_id: str,
        account_id: str,
        environment: str,
    ):
        _initialize_durable_provider_economic_book(
            self,
            store,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
        )

    def _events(self) -> list[dict[str, Any]]:
        authority = _require_durable_provider_economic_book_authority(self)
        return _economic_store_load_events(
            self,
            "economic_book",
            authority.book_id,
        )

    def _replay(self, events: list[dict[str, Any]]) -> ScopedEconomicBook:
        authority = _require_durable_provider_economic_book_authority(self)
        candidate = ScopedEconomicBook(
            environment=authority.environment,
            account_id=authority.account_id,
        )
        expected_version = 1
        for event in events:
            if event.get("aggregate_version") != expected_version:
                raise AccountingConflict(
                    "economic journal aggregate versions are not contiguous"
                )
            expected_version += 1
            event_type = event.get("event_type")
            if event_type not in {
                DurableProviderEconomicBook._SINGLE_EVENT,
                DurableProviderEconomicBook._BATCH_EVENT,
            }:
                raise AccountingConflict(
                    "economic_book contains an unsupported durable event type"
                )

            payload = event.get("payload")
            if not isinstance(payload, Mapping):
                raise AccountingConflict(
                    "economic durable event payload must be an object"
                )
            if (
                payload.get("provider_id") != authority.provider_id
                or payload.get("account_id") != authority.account_id
                or payload.get("environment") != authority.environment
            ):
                raise AccountingConflict(
                    "economic durable event scope does not match provider book"
                )

            if event_type == DurableProviderEconomicBook._SINGLE_EVENT:
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
                provider_id=authority.provider_id,
                account_id=authority.account_id,
                environment=authority.environment,
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
        _reload_durable_provider_economic_book(self)

    def prepare_batch_mutation(
        self,
        transactions: Iterable[JournalTransaction],
        *,
        committed_at: str | None = None,
    ) -> PreparedEconomicBatch:
        """Prepare one economic batch from one sealed durable journal cut."""

        authority = _require_durable_provider_economic_book_authority(self)
        batch = tuple(
            _transaction_from_payload(_transaction_payload(item))
            for item in transactions
        )
        if not batch:
            raise ValueError("atomic transaction batch must not be empty")

        events = DurableProviderEconomicBook._events(self)
        current = DurableProviderEconomicBook._replay(self, events)
        candidate = ScopedEconomicBook(
            environment=authority.environment,
            account_id=authority.account_id,
            transactions=current.transactions,
        )
        inserted_locally = candidate.append_batch(batch)
        transaction_payloads = [_transaction_payload(item) for item in batch]
        batch_digest = _economic_batch_digest(
            provider_id=authority.provider_id,
            account_id=authority.account_id,
            environment=authority.environment,
            transactions=batch,
        )
        request = {
            "schema_version": "1.0.0",
            "provider_id": authority.provider_id,
            "account_id": authority.account_id,
            "environment": authority.environment,
            "batch_digest": batch_digest,
            "transactions": transaction_payloads,
        }

        if not inserted_locally:
            matching_batches = [
                event
                for event in events
                if event.get("event_type") == DurableProviderEconomicBook._BATCH_EVENT
                and isinstance(event.get("payload"), Mapping)
                and event["payload"].get("batch_digest") == batch_digest
                and event["payload"].get("transactions") == transaction_payloads
            ]
            matching_single = []
            if len(batch) == 1:
                matching_single = [
                    event
                    for event in events
                    if event.get("event_type")
                    == DurableProviderEconomicBook._SINGLE_EVENT
                    and isinstance(event.get("payload"), Mapping)
                    and event["payload"].get("transaction")
                    == transaction_payloads[0]
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
                    authority.provider_id,
                    authority.account_id,
                    authority.environment,
                    batch_digest,
                ),
            )
        )
        payload = {
            "schema_version": "1.0.0",
            "provider_id": authority.provider_id,
            "account_id": authority.account_id,
            "environment": authority.environment,
            "batch_digest": batch_digest,
            "previous_book_digest": previous_digest,
            "resulting_book_digest": resulting_digest,
            "transactions": transaction_payloads,
        }
        envelope = {
            "event_id": event_identity,
            "event_type": DurableProviderEconomicBook._BATCH_EVENT,
            "aggregate_type": "economic_book",
            "aggregate_id": authority.book_id,
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

        _require_durable_provider_economic_book_authority(self)
        DurableProviderEconomicBook._reload(self)

    def append(
        self,
        transaction: JournalTransaction,
        *,
        expected_journal_sequence: int | None = None,
    ) -> bool:
        _require_durable_provider_economic_book_authority(self)
        return DurableProviderEconomicBook.append_batch(
            self,
            (transaction,),
            expected_journal_sequence=expected_journal_sequence,
        )

    def append_batch(
        self,
        transactions: Iterable[JournalTransaction],
        *,
        expected_journal_sequence: int | None = None,
    ) -> bool:
        authority = _require_durable_provider_economic_book_authority(self)
        plan = DurableProviderEconomicBook.prepare_batch_mutation(
            self,
            transactions,
        )
        if plan.already_committed:
            DurableProviderEconomicBook._reload(self)
            return False
        if plan.envelope is None:
            raise AccountingConflict("fresh economic batch is missing its durable event")

        command_identity = str(
            uuid5(
                NAMESPACE_URL,
                "https://commands.autotrade.local/economic-batch/"
                + _scoped_identity(
                    "economic-batch-command",
                    authority.provider_id,
                    authority.account_id,
                    authority.environment,
                    plan.batch_digest,
                ),
            )
        )
        try:
            _, inserted, _ = _economic_store_commit_command(
                self,
                command_id=command_identity,
                actor=DurableProviderEconomicBook._ACTOR,
                environment=authority.environment,
                idempotency_key=(
                    f"economic-batch:{authority.book_id}:{plan.batch_digest}"
                ),
                request=plan.request,
                result=plan.result,
                state_version=plan.aggregate_version,
                events=[(plan.envelope, "autotrade.economic.events")],
                expected_journal_sequence=expected_journal_sequence,
            )
        except Exception:
            DurableProviderEconomicBook._reload(self)
            raise
        DurableProviderEconomicBook._reload(self)
        return inserted


(
    _durable_provider_economic_book_authority_is_registered,
    _require_durable_provider_economic_book_authority,
    _initialize_durable_provider_economic_book,
    _reload_durable_provider_economic_book,
) = _install_durable_provider_economic_book_authority()
del _install_durable_provider_economic_book_authority

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
    _order_book: DurableOrderBookProjection | None = None,
    _order_fill_plan: PreparedOrderMutation | None = None,
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

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        reservation_book,
        expected_type=DurableReservationBook,
        subject="reservation book",
    )
    if (
        economic_book.environment != reservation_book.environment
        or economic_book.account_id != reservation_book.account_id
    ):
        raise ValueError(
            "economic and reservation books must share account/environment scope"
        )

    if (_order_book is None) != (_order_fill_plan is None):
        raise ValueError(
            "order_book and order_fill_plan must be supplied together"
        )
    if _order_book is not None:
        _require_same_financial_journal_generation(
            economic_book,
            _order_book,
            expected_type=DurableOrderBookProjection,
            subject="order projection",
        )
        if type(_order_fill_plan) is not PreparedOrderMutation:
            raise TypeError(
                "order_fill_plan must be exact PreparedOrderMutation"
            )
        if _order_fill_plan.operation != "RECORD_FILL":
            raise AccountingConflict(
                "atomic financial composition accepts only a prepared OMS fill"
            )
        if (
            _order_book.provider_id != economic_book.provider_id
            or _order_book.account_id != economic_book.account_id
            or _order_book.environment != economic_book.environment
        ):
            raise AccountingConflict(
                "OMS and financial books must share provider/account/environment scope"
            )
        if _order_fill_plan.envelope is not None:
            envelope = _order_fill_plan.envelope
            if (
                envelope.get("aggregate_id") != _order_book.aggregate_id
                or envelope.get("event_id") != _order_fill_plan.event_id
                or envelope.get("payload", {}).get("operation") != "RECORD_FILL"
            ):
                raise AccountingConflict(
                    "prepared OMS fill does not belong to the selected order book"
                )

    if provider_fill_binding is not None:
        if not isinstance(provider_fill_binding, PreparedProviderFillBinding):
            raise TypeError(
                "provider_fill_binding must be PreparedProviderFillBinding or None"
            )
        binding_request = provider_fill_binding.request
        if (
            binding_request.get("provider_id") != economic_book.provider_id
            or binding_request.get("account_id") != economic_book.account_id
            or binding_request.get("environment") != economic_book.environment
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
        _require_same_financial_journal_generation(
            economic_book,
            settlement_book,
            expected_type=DurableSettlementBook,
            subject="settlement book",
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
        economic_book.provider_id,
        economic_book.account_id,
        economic_book.environment,
        idem,
    )
    reservation_plan = reservation_book.prepare_consume_mutation(
        event_key=_scoped_identity(
            "atomic-fill-reservation-event",
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
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
    if _order_fill_plan is not None:
        provider_execution_id = _order_fill_plan.request.get(
            "provider_execution_id"
        )
        if not isinstance(provider_execution_id, str) or any(
            item.cause_event_id != provider_execution_id
            for item in economic_plan.transactions
        ):
            raise AccountingConflict(
                "OMS fill provider execution is absent from or does not exclusively own "
                "the atomic economic batch"
            )

    settlement_plan = None
    if settlement_book is not None:
        batch_transaction_ids = {
            item.transaction_id for item in economic_plan.transactions
        }
        expected_cash_legs = _cash_leg_totals(economic_plan.transactions)

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

    all_financial_committed = all(commit_states)
    if any(commit_states) and not all_financial_committed:
        reservation_book.refresh()
        economic_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        if _order_book is not None:
            _order_book.refresh()
        raise AccountingConflict(
            "reservation/economic/settlement fill state is only partially committed"
        )

    if all_financial_committed and _order_fill_plan is None:
        reservation_book.refresh()
        economic_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        return False

    if (
        all_financial_committed
        and _order_fill_plan is not None
        and not _order_fill_plan.already_committed
    ):
        reservation_book.refresh()
        economic_book.refresh()
        _order_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        raise AccountingConflict(
            "financial fill is committed without the matching OMS fill"
        )

    if not all_financial_committed:
        if reservation_plan.envelope is None or economic_plan.envelope is None:
            raise AccountingConflict(
                "fresh atomic fill plan is missing durable events"
            )
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
    if _order_fill_plan is not None:
        request["order_fill"] = {
            "event_id": _order_fill_plan.event_id,
            "event_key": _order_fill_plan.event_key,
            "operation": _order_fill_plan.operation,
            "request": _order_fill_plan.request,
            "mutation_hash": _order_fill_plan.mutation_hash,
            "snapshot_digest": payload_digest(
                _order_fill_plan.snapshot_payload
            ),
        }
        result["order_fill"] = {
            "event_id": _order_fill_plan.event_id,
            "snapshot": _order_fill_plan.snapshot_payload,
        }

    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/atomic-fill/"
            + _scoped_identity(
                "atomic-fill-command",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                cid,
            ),
        )
    )
    journal_idempotency_key = "atomic-fill:" + _scoped_identity(
        "atomic-fill-idempotency",
        economic_book.provider_id,
        economic_book.account_id,
        economic_book.environment,
        idem,
    )

    if all_financial_committed:
        try:
            authority = _economic_store_load_command_event_batch(
                economic_book,
                command_id=command_identity,
                actor="atomic-fill-financial-integration",
                environment=economic_book.environment,
                idempotency_key=journal_idempotency_key,
                request=request,
            )
        except ValueError as error:
            reservation_book.refresh()
            economic_book.refresh()
            _order_book.refresh()
            if settlement_book is not None:
                settlement_book.refresh()
            raise AccountingConflict(
                "OMS and financial effects exist without one atomic/recovery command authority"
            ) from error
        if authority is None or authority.get("result") != result:
            raise AccountingConflict(
                "OMS and financial effects lack matching atomic/recovery command result"
            )
        reservation_book.refresh()
        economic_book.refresh()
        _order_book.refresh()
        if settlement_book is not None:
            settlement_book.refresh()
        return False

    try:
        _, inserted, _ = _economic_store_commit_command(economic_book,
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
                0
                if _order_fill_plan is None
                else _order_fill_plan.aggregate_version,
            ),
            expected_journal_sequence=(
                _order_fill_plan.journal_sequence_cut
                if (
                    _order_fill_plan is not None
                    and _order_fill_plan.already_committed
                )
                else None
            ),
            events=(
                (
                    []
                    if (
                        _order_fill_plan is None
                        or _order_fill_plan.already_committed
                    )
                    else [
                        (
                            _order_fill_plan.envelope,
                            _order_fill_plan.outbox_topic,
                        )
                    ]
                )
                + [
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
        if _order_book is not None:
            _order_book.refresh()
        raise

    reservation_book.refresh()
    economic_book.refresh()
    if settlement_book is not None:
        settlement_book.refresh()
    if _order_book is not None:
        _order_book.refresh()
        recorded = _order_book.order(
            _order_fill_plan.request["client_order_id"]
        ).snapshot()
        if recorded != _order_fill_plan.snapshot:
            raise AccountingConflict(
                "atomic OMS fill replay differs from prepared snapshot"
            )
    return inserted



def commit_order_fill_with_reservation_consumption(
    order_book: DurableOrderBookProjection,
    economic_book: DurableProviderEconomicBook,
    reservation_book: DurableReservationBook,
    *,
    order_event_key: str,
    client_order_id: str,
    fill_id: str,
    provider_execution_id: str,
    quantity,
    price,
    command_id: str,
    idempotency_key: str,
    reservation_id: str,
    usage: Mapping[str, object],
    transactions: Iterable[JournalTransaction],
    committed_at: str,
    provider_revision: str | None = None,
    order_evidence_refs: Sequence[Mapping[str, object]] | None = None,
    reservation_expected_snapshot_digest: str | None = None,
    settlement_book: DurableSettlementBook | None = None,
    settlement_obligations: Iterable[SettlementObligation] = (),
    provider_fill_binding: PreparedProviderFillBinding | None = None,
) -> bool:
    """Compose one canonical OMS fill with its canonical financial effects.

    A fresh fill event enters the same JournalStore transaction as reservation
    consumption and economics. If an exact OMS fill already exists but finance
    does not, that immutable event may serve as recovery evidence and only the
    missing financial effects are committed. Finance-without-OMS and unproven
    fully split legacy state fail closed.
    """

    if type(order_book) is not DurableOrderBookProjection:
        raise TypeError("order_book must be exact DurableOrderBookProjection")
    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")

    normalized_execution = _text(
        provider_execution_id,
        name="provider_execution_id",
    )
    normalized_reservation = _text(reservation_id, name="reservation_id")
    normalized_fill = _text(fill_id, name="fill_id")
    transaction_batch = tuple(transactions)

    if economic_book.environment in {"PAPER", "LIVE"} and provider_fill_binding is None:
        raise AccountingConflict(
            "PAPER/LIVE OMS fill financial composition requires "
            "provider-derived financial binding"
        )

    if provider_fill_binding is not None:
        if type(provider_fill_binding) is not PreparedProviderFillBinding:
            raise TypeError(
                "provider_fill_binding must be exact PreparedProviderFillBinding"
            )
        binding_request = provider_fill_binding.request
        normalized_usage = _positive_usage_map(
            usage,
            name="order fill financial usage",
        )
        if (
            binding_request.get("provider_execution_id") != normalized_execution
            or binding_request.get("reservation_id") != normalized_reservation
            or binding_request.get("fill_id") != normalized_fill
        ):
            raise AccountingConflict(
                "provider-derived financial binding does not match OMS fill identity"
            )
        if len(transaction_batch) != 1:
            raise AccountingConflict(
                "provider-derived OMS fill must bind exactly one economic transaction"
            )
        bound_transaction = transaction_batch[0]
        if not isinstance(bound_transaction, JournalTransaction):
            raise TypeError("transactions must contain JournalTransaction")
        if (
            binding_request.get("transaction_id")
            != bound_transaction.transaction_id
            or binding_request.get("transaction_digest")
            != payload_digest(canonical_transaction(bound_transaction))
            or binding_request.get("derived_usage")
            != _usage_payload(normalized_usage)
        ):
            raise AccountingConflict(
                "provider-derived financial binding does not match supplied economics"
            )

    plan = order_book.prepare_record_fill_mutation(
        event_key=order_event_key,
        client_order_id=client_order_id,
        fill_id=fill_id,
        provider_execution_id=normalized_execution,
        quantity=quantity,
        price=price,
        committed_at=committed_at,
        provider_revision=provider_revision,
        evidence_refs=order_evidence_refs,
    )
    return commit_economic_batch_with_reservation_consumption(
        economic_book,
        reservation_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reservation_id=normalized_reservation,
        usage=usage,
        transactions=transaction_batch,
        reservation_expected_snapshot_digest=reservation_expected_snapshot_digest,
        committed_at=committed_at,
        settlement_book=settlement_book,
        settlement_obligations=settlement_obligations,
        provider_fill_binding=provider_fill_binding,
        _order_book=order_book,
        _order_fill_plan=plan,
    )


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
    reservation_book: DurableReservationBook | None = None,
    reservation_id: str | None = None,
    provider_fill_correction_binding: PreparedProviderFillCorrectionBinding | None = None,
) -> bool:
    """Atomically bind correction economics, settlement and conservative capacity.

    The reversal cancels the prior trade-date economic fact; it is not a new
    contractual cash settlement. Replacement settlement, any positive
    reservation-consumption delta, and correction high-water evidence commit in
    the same JournalStore transaction. A correction never releases reservation
    capacity here.
    """

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        settlement_book,
        expected_type=DurableSettlementBook,
        subject="settlement book",
    )
    if (
        settlement_book.scope.provider_id != economic_book.provider_id
        or settlement_book.scope.account_id != economic_book.account_id
        or settlement_book.scope.environment != economic_book.environment
    ):
        raise ValueError(
            "settlement book must share provider/account/environment scope"
        )
    if reservation_book is None:
        if reservation_id is not None or provider_fill_correction_binding is not None:
            raise ValueError(
                "reservation correction arguments require reservation_book"
            )
    else:
        _require_same_financial_journal_generation(
            economic_book,
            reservation_book,
            expected_type=DurableReservationBook,
            subject="reservation book",
        )
        if (
            reservation_book.account_id != economic_book.account_id
            or reservation_book.environment != economic_book.environment
        ):
            raise ValueError(
                "reservation book must share account/environment scope"
            )
        if provider_fill_correction_binding is None:
            raise ValueError(
                "reservation-aware correction requires correction binding evidence"
            )
        if not isinstance(
            provider_fill_correction_binding,
            PreparedProviderFillCorrectionBinding,
        ):
            raise TypeError(
                "provider_fill_correction_binding has invalid type"
            )
        rid = _text(reservation_id, name="reservation_id")
        binding_request = provider_fill_correction_binding.request
        if (
            binding_request.get("provider_id") != economic_book.provider_id
            or binding_request.get("account_id") != economic_book.account_id
            or binding_request.get("environment") != economic_book.environment
            or binding_request.get("reservation_id") != rid
        ):
            raise AccountingConflict(
                "provider fill correction binding scope does not match correction"
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

    cid = _text(command_id, name="command_id")
    idem = _text(idempotency_key, name="idempotency_key")
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

    expected_cash_legs = _cash_leg_totals((canonical_replacement,))
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
    reservation_plan = None
    if reservation_book is not None and provider_fill_correction_binding is not None:
        additional_usage = provider_fill_correction_binding.additional_usage
        if additional_usage:
            reservation_plan = reservation_book.prepare_consume_mutation(
                event_key=_scoped_identity(
                    "atomic-correction-reservation-event",
                    economic_book.provider_id,
                    economic_book.account_id,
                    economic_book.environment,
                    cid,
                ),
                idempotency_key=_scoped_identity(
                    "atomic-correction-reservation",
                    economic_book.provider_id,
                    economic_book.account_id,
                    economic_book.environment,
                    idem,
                ),
                reservation_id=_text(reservation_id, name="reservation_id"),
                usage=additional_usage,
                committed_at=when,
                expected_snapshot_digest=(
                    provider_fill_correction_binding.reservation_cut_digest
                ),
            )

    states = [
        economic_plan.already_committed,
        settlement_plan.already_committed,
    ]
    if reservation_plan is not None:
        states.append(reservation_plan.already_committed)
    if provider_fill_correction_binding is not None:
        states.append(provider_fill_correction_binding.already_committed)
    if any(states) and not all(states):
        economic_book.refresh()
        settlement_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        raise AccountingConflict(
            "economic/settlement/reservation correction state is only partially committed"
        )
    if all(states):
        economic_book.refresh()
        settlement_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        return False
    if economic_plan.envelope is None or settlement_plan.envelope is None:
        raise AccountingConflict(
            "fresh settlement-aware correction is missing durable events"
        )
    if reservation_plan is not None and reservation_plan.envelope is None:
        raise AccountingConflict(
            "fresh correction reservation plan is missing durable event"
        )
    if (
        provider_fill_correction_binding is not None
        and provider_fill_correction_binding.envelope is None
    ):
        raise AccountingConflict(
            "fresh provider correction binding is missing durable event"
        )

    if provider_fill_correction_binding is None:
        # Preserve the exact legacy durable command contract for upgrade-safe
        # retry of economics+settlement corrections created before reservation
        # correction binding existed.
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
    else:
        request = {
            "schema_version": "1.1.0",
            "provider_id": economic_book.provider_id,
            "account_id": economic_book.account_id,
            "environment": economic_book.environment,
            "economic_correction": economic_plan.request,
            "replacement_settlement": settlement_plan.request,
            "reservation_consumption": (
                None if reservation_plan is None else reservation_plan.request
            ),
            "provider_fill_correction_binding": (
                provider_fill_correction_binding.request
            ),
        }
        result = {
            "economic_correction": economic_plan.result,
            "replacement_settlement": settlement_plan.result,
            "reservation_consumption": (
                None if reservation_plan is None else reservation_plan.snapshot_payload
            ),
            "provider_fill_correction_binding": (
                provider_fill_correction_binding.result
            ),
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
    events: list[tuple[dict[str, Any], str | None]] = [
        (economic_plan.envelope, "autotrade.economic.events"),
        (settlement_plan.envelope, None),
    ]
    if reservation_plan is not None:
        events.append((reservation_plan.envelope, None))
    if provider_fill_correction_binding is not None:
        events.append((provider_fill_correction_binding.envelope, None))

    state_versions = [
        economic_plan.aggregate_version,
        settlement_plan.aggregate_version,
    ]
    if reservation_plan is not None:
        state_versions.append(reservation_plan.aggregate_version)
    if provider_fill_correction_binding is not None:
        state_versions.append(provider_fill_correction_binding.aggregate_version)

    try:
        _, inserted, _ = _economic_store_commit_command(economic_book,
            command_id=command_identity,
            actor="atomic-settlement-correction-integration",
            environment=economic_book.environment,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=max(state_versions),
            events=events,
        )
    except Exception:
        economic_book.refresh()
        settlement_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        raise

    economic_book.refresh()
    settlement_book.refresh()
    if reservation_book is not None:
        reservation_book.refresh()
    return inserted

def commit_provider_fill_correction_with_settlement_replacement(
    economic_book: DurableProviderEconomicBook,
    settlement_book: DurableSettlementBook,
    *,
    reservation_book: DurableReservationBook,
    reservation_id: str,
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
    asset_family: str = "CASH_EQUITY",
    committed_at: str | None = None,
) -> bool:
    """Atomically correct provider economics, settlement and reservation capacity.

    Capacity is conservative: a corrected fill may consume an additional
    evidence-derived delta, but a smaller correction never releases authority.
    The per-execution high-water binding prevents repeated corrections from
    double-consuming the same delta.
    """

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        reservation_book,
        expected_type=DurableReservationBook,
        subject="reservation book",
    )
    _require_same_financial_journal_generation(
        economic_book,
        settlement_book,
        expected_type=DurableSettlementBook,
        subject="settlement book",
    )

    when = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if committed_at is None
        else _instant_text(committed_at, name="committed_at")
    )
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
    binding = _prepare_provider_fill_correction_binding(
        economic_book,
        reservation_book,
        reservation_id=reservation_id,
        original_projected_fill=original_projected_fill,
        original_provider_fill=original_provider_fill,
        corrected_projected_fill=corrected_projected_fill,
        corrected_provider_fill=corrected_provider_fill,
        replacement=replacement,
        asset_family=asset_family,
        committed_at=when,
    )
    return commit_economic_correction_with_settlement_replacement(
        economic_book,
        settlement_book,
        command_id=command_id,
        idempotency_key=idempotency_key,
        reversal=reversal,
        replacement=replacement,
        settlement_obligations=settlement_obligations,
        committed_at=when,
        reservation_book=reservation_book,
        reservation_id=reservation_id,
        provider_fill_correction_binding=binding,
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
    order_book: DurableOrderBookProjection | None = None,
    order_event_key: str | None = None,
    order_evidence_refs: Sequence[Mapping[str, object]] | None = None,
) -> bool:
    """Atomically book one provider fill and consume only evidence-derived resources.

    This is the provider-fill entrypoint for the shared atomic integration
    barrier. It deliberately accepts no caller-authored transaction batch and
    no caller-authored reservation usage map. Both are derived from the same
    normalized provider/projected fill evidence and the admission-bound
    reservation envelope before JournalStore mutation.
    """

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        reservation_book,
        expected_type=DurableReservationBook,
        subject="reservation book",
    )
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
    if order_book is None:
        if order_event_key is not None or order_evidence_refs is not None:
            raise ValueError(
                "order_event_key/evidence require order_book"
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

    if type(order_book) is not DurableOrderBookProjection:
        raise TypeError("order_book must be exact DurableOrderBookProjection")
    if order_event_key is None:
        raise ValueError("order_event_key is required with order_book")
    if projected_fill.client_order_id is None:
        raise AccountingConflict(
            "provider-evidenced OMS composition requires client_order_id"
        )
    return commit_order_fill_with_reservation_consumption(
        order_book,
        economic_book,
        reservation_book,
        order_event_key=order_event_key,
        client_order_id=projected_fill.client_order_id,
        fill_id=projected_fill.fill_id,
        provider_execution_id=projected_fill.provider_execution_id,
        quantity=projected_fill.quantity,
        price=projected_fill.price,
        provider_revision=projected_fill.provider_revision,
        order_evidence_refs=order_evidence_refs,
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


def _snapshot_external_cash_activity(
    activity: ProviderActivityEvidence,
) -> ProviderActivityEvidence:
    """Detach one exact provider cash fact before durable financial callbacks."""

    if type(activity) is not ProviderActivityEvidence:
        raise TypeError("activity must be exact ProviderActivityEvidence")
    raw_state = object.__getattribute__(activity, "__dict__")
    if type(raw_state) is not dict:
        raise TypeError("provider activity state must use exact dict storage")
    state = raw_state.copy()
    expected_fields = {
        "provider_id",
        "account_id",
        "environment",
        "activity_id",
        "activity_type",
        "origin",
        "occurred_at",
        "instrument",
        "currency",
        "client_order_id",
        "provider_order_id",
        "provider_execution_id",
        "signed_amount",
    }
    if set(state) != expected_fields:
        raise TypeError("provider activity contains unexpected state fields")
    for name in (
        "provider_id",
        "account_id",
        "environment",
        "activity_id",
        "activity_type",
        "origin",
        "occurred_at",
    ):
        if type(state[name]) is not str:
            raise TypeError(f"provider activity {name} must be exact str")
    for name in (
        "instrument",
        "currency",
        "client_order_id",
        "provider_order_id",
        "provider_execution_id",
    ):
        if state[name] is not None and type(state[name]) is not str:
            raise TypeError(f"provider activity {name} must be exact str or None")
    if state["signed_amount"] is not None and type(state["signed_amount"]) is not Decimal:
        raise TypeError(
            "provider activity signed_amount must be exact Decimal or None"
        )

    snapshot = ProviderActivityEvidence.create(
        provider_id=state["provider_id"],
        account_id=state["account_id"],
        environment=state["environment"],
        activity_id=state["activity_id"],
        activity_type=state["activity_type"],
        origin=state["origin"],
        occurred_at=state["occurred_at"],
        instrument=state["instrument"],
        currency=state["currency"],
        client_order_id=state["client_order_id"],
        provider_order_id=state["provider_order_id"],
        provider_execution_id=state["provider_execution_id"],
        signed_amount=state["signed_amount"],
    )
    observed = tuple(
        state[name]
        for name in (
            "provider_id",
            "account_id",
            "environment",
            "activity_id",
            "activity_type",
            "origin",
            "occurred_at",
            "instrument",
            "currency",
            "client_order_id",
            "provider_order_id",
            "provider_execution_id",
            "signed_amount",
        )
    )
    canonical = (
        snapshot.provider_id,
        snapshot.account_id,
        snapshot.environment,
        snapshot.activity_id,
        snapshot.activity_type,
        snapshot.origin,
        snapshot.occurred_at,
        snapshot.instrument,
        snapshot.currency,
        snapshot.client_order_id,
        snapshot.provider_order_id,
        snapshot.provider_execution_id,
        snapshot.signed_amount,
    )
    if observed != canonical:
        raise ValueError(
            "provider activity changed from canonical normalized state"
        )
    return snapshot


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

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    store_identity = require_exact_journal_store_authority(
        store,
        subject="provider cash JournalStore",
    )

    def durable_get_event(event_id: str):
        with journal_store_authority_scope(store, store_identity):
            return JournalStore.get_event(store, event_id)

    def durable_next_aggregate_version(
        aggregate_type: str,
        aggregate_id: str,
    ) -> int:
        with journal_store_authority_scope(store, store_identity):
            return JournalStore.next_aggregate_version(
                store,
                aggregate_type,
                aggregate_id,
            )

    def durable_commit_command(**kwargs: Any):
        with journal_store_authority_scope(store, store_identity):
            return JournalStore.commit_command(store, **kwargs)

    activity = _snapshot_external_cash_activity(activity)

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

    existing_imported = durable_get_event(imported_event_id)
    existing_economic = durable_get_event(economic_event_id)
    if (existing_imported is None) != (existing_economic is None):
        raise AccountingConflict(
            "provider cash activity has only part of its durable financial effect"
        )
    if existing_imported is not None and existing_economic is not None:
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
                raise AccountingConflict(
                    "provider cash activity replay has invalid durable semantic owner"
                )
            version = event.get("aggregate_version")
            if type(version) is not int or version <= 0:
                raise AccountingConflict(
                    "provider cash activity replay has invalid aggregate version"
                )

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
            raise AccountingConflict(
                "provider cash activity replay found invalid durable payload"
            )
        if any(imported_payload.get(key) != value for key, value in request.items()):
            raise AccountingConflict(
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
            != _transaction_payload(transaction)
        ):
            raise AccountingConflict(
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
                raise AccountingConflict(
                    "provider cash activity replay has invalid aggregate version"
                )
            envelope["aggregate_version"] = str(version)
            return envelope

        saved_result, replay_inserted, _ = durable_commit_command(
            command_id=command_identity,
            actor="provider-activity-accounting",
            environment=scope,
            idempotency_key=f"provider-cash-import:{identity}",
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
            raise AccountingConflict(
                "provider cash effects exist without their durable command authority"
            )
        if saved_result != result:
            raise AccountingConflict(
                "provider cash durable command result conflicts with its financial effect"
            )
        return transaction, False

    activity_version = durable_next_aggregate_version(
        "provider_activity", identity
    )
    book_version = durable_next_aggregate_version("economic_book", book_id)

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

    saved_result, inserted, _ = durable_commit_command(
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
    if saved_result != result:
        raise AccountingConflict(
            "provider cash durable command result conflicts with its financial effect"
        )
    if not inserted:
        raced_imported = durable_get_event(imported_event_id)
        raced_economic = durable_get_event(economic_event_id)
        if raced_imported is None or raced_economic is None:
            raise AccountingConflict(
                "provider cash durable command exists without its financial effects"
            )
        replayed_transaction, replay_inserted = book_external_provider_cash_activity(
            store,
            provider_id=provider,
            account_id=account,
            environment=scope,
            activity=activity,
            observed_at=observed_at,
        )
        if replay_inserted or replayed_transaction != transaction:
            raise AccountingConflict(
                "provider cash concurrent replay did not resolve to the same financial effect"
            )
        return transaction, False
    return transaction, True


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
