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

from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from threading import Lock
import weakref
from typing import Any, Iterable, Mapping, Sequence
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    AccountingConflict,
    EconomicBook,
    JournalTransaction,
    Posting,
    ScopedEconomicBook,
    book_external_cash_flow,
    canonical_transaction,
    transaction_digest,
)
from .exact_decimal import exact_sum
from .durable_order_projection import (
    DurableOrderBookProjection,
    PreparedOrderMutation,
    require_exact_order_projection_authority,
    _OUTBOX_TOPIC as _ORDER_PROJECTION_OUTBOX_TOPIC,
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
    build_provider_fill_bust_transaction,
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
from .store_identity import (
    JournalStoreIdentity,
    require_exact_journal_store_identity,
)
from .store_identity import same_journal_backing_object
from .reconciliation import ProviderActivityEvidence, ProviderFillEvidence
from .settlement import SettlementObligation


_ALLOWED_EXTERNAL_CASH_TYPES = frozenset({"DEPOSIT", "WITHDRAWAL"})
_ALLOWED_EXTERNAL_ORIGINS = frozenset({"MANUAL", "EXTERNAL"})


def _text(value: str, *, name: str) -> str:
    # Trust-sensitive financial identities must be exact built-in text before
    # normalization. A str subclass can override strip() and execute caller
    # code while this bridge is establishing durable financial authority.
    if type(value) is not str or not value.strip():
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


def _provider_environment(
    *,
    provider_id: str,
    environment: str,
    provider_environment: str | None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    runtime = _environment(environment)
    if provider == "BYBIT" and provider_environment is None:
        raise AccountingConflict(
            "BYBIT durable economic authority requires explicit provider_environment"
        )
    normalized = (
        runtime
        if provider_environment is None
        else _text(provider_environment, name="provider_environment").upper()
    )
    if provider == "BYBIT" and normalized not in {"MAINNET", "TESTNET", "DEMO"}:
        raise AccountingConflict(
            "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
        )
    if provider == "BYBIT":
        expected_runtime = "LIVE" if normalized == "MAINNET" else "PAPER"
        if runtime != expected_runtime:
            raise AccountingConflict(
                "BYBIT provider_environment does not match runtime environment"
            )
    return normalized


def _provider_environment_payload(
    *,
    provider_environment: str,
    environment: str,
) -> str | None:
    return (
        provider_environment
        if provider_environment != environment
        else None
    )


def _activity_identity(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    activity_id: str,
    provider_environment: str | None = None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=scope,
        provider_environment=provider_environment,
    )
    parts = [provider, account, scope]
    if provider_scope != scope:
        parts.append(provider_scope)
    parts.append(_text(activity_id, name="activity_id"))
    return _scoped_identity("provider-activity", *parts)


def _book_id(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    provider_environment: str | None = None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=scope,
        provider_environment=provider_environment,
    )
    parts = [provider, account, scope]
    if provider_scope != scope:
        parts.append(provider_scope)
    return _scoped_identity("economic-book", *parts)


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
    provider_environment: str | None = None,
    transactions: Iterable[JournalTransaction],
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    scope = _environment(environment)
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=scope,
        provider_environment=provider_environment,
    )
    material = {
        "schema_version": "1.0.0",
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
        "transactions": [_transaction_payload(item) for item in transactions],
    }
    provider_scope_payload = _provider_environment_payload(
        provider_environment=provider_scope,
        environment=scope,
    )
    if provider_scope_payload is not None:
        material["provider_environment"] = provider_scope_payload
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


@dataclass(frozen=True)
class EconomicBookCut:
    """Read-only canonical economic projection from one durable journal cut.

    This value is descriptive, not an independent write authority. Mutation
    preparation must revalidate book_digest against the durable book before
    constructing an envelope, and JournalStore aggregate-version fencing still
    protects the later atomic commit.
    """

    provider_id: str
    account_id: str
    environment: str
    transactions: tuple[JournalTransaction, ...]
    book_digest: str
    aggregate_version: int

    def position(self, instrument: str) -> Decimal:
        value = _text(instrument, name="instrument")
        try:
            return exact_sum(
                item.signed_amount
                for transaction in self.transactions
                for item in transaction.postings
                if item.ledger_account == f"POSITION:{value}"
                and item.asset_or_currency == value
            )
        except ExactDecimalError as error:
            raise AccountingConflict(
                "economic cut position exceeds exact-decimal resource authority"
            ) from error


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
    provider_environment: str | None = None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    runtime = _environment(environment)
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=runtime,
        provider_environment=provider_environment,
    )
    parts = [provider, account, runtime]
    if provider_scope != runtime:
        parts.append(provider_scope)
    parts.append(_text(provider_execution_id, name="provider_execution_id"))
    return _scoped_identity(
        "provider-fill-financial-binding",
        *parts,
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
    provider_scope = _provider_environment_payload(
        provider_environment=provider_fill.provider_environment,
        environment=provider_fill.environment,
    )
    if provider_scope is not None:
        payload["provider_environment"] = provider_scope
    return payload


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
_PROVIDER_FILL_BINDING_FIELDS_WITH_PROVIDER_ENVIRONMENT = (
    _PROVIDER_FILL_BINDING_FIELDS | {"provider_environment"}
)


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
    if (
        type(payload) is not dict
        or set(payload)
        not in {
            _PROVIDER_FILL_BINDING_FIELDS,
            _PROVIDER_FILL_BINDING_FIELDS_WITH_PROVIDER_ENVIRONMENT,
        }
    ):
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
            provider_environment=raw.get("provider_environment"),
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
    if type(plan) is not ProviderFillFinancialPlan:
        raise TypeError("plan must be exact ProviderFillFinancialPlan")
    if type(projected_fill) is not ProjectedFillEvidence:
        raise TypeError("projected_fill must be ProjectedFillEvidence")
    if type(provider_fill) is not ProviderFillEvidence:
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
    if provider_fill.provider_environment != economic_book.provider_environment:
        raise AccountingConflict(
            "provider fill binding provider_environment does not match economic book"
        )

    aggregate_id = _provider_fill_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_environment=economic_book.provider_environment,
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
    provider_scope_payload = _provider_environment_payload(
        provider_environment=economic_book.provider_environment,
        environment=economic_book.environment,
    )
    if provider_scope_payload is not None:
        request["provider_environment"] = provider_scope_payload
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
            or payload.get(
                "provider_environment",
                economic_book.environment,
            ) != economic_book.provider_environment
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
        expected_plan_material = {
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
        if provider_scope_payload is not None:
            expected_plan_material["provider_environment"] = provider_scope_payload
        expected_plan_digest = payload_digest(expected_plan_material)
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
    if provider_scope_payload is not None:
        payload["provider_environment"] = provider_scope_payload
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
    provider_environment: str | None = None,
) -> str:
    provider = _text(provider_id, name="provider_id").upper()
    account = _text(account_id, name="account_id")
    runtime = _environment(environment)
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=runtime,
        provider_environment=provider_environment,
    )
    parts = [provider, account, runtime]
    if provider_scope != runtime:
        parts.append(provider_scope)
    parts.append(_text(provider_execution_id, name="provider_execution_id"))
    return _scoped_identity(
        "provider-fill-reservation-correction-binding",
        *parts,
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
    if type(original_projected_fill) is not ProjectedFillEvidence:
        raise TypeError("original_projected_fill must be exact ProjectedFillEvidence")
    if type(original_provider_fill) is not ProviderFillEvidence:
        raise TypeError("original_provider_fill must be exact ProviderFillEvidence")
    if type(corrected_projected_fill) is not ProjectedFillEvidence:
        raise TypeError("corrected_projected_fill must be exact ProjectedFillEvidence")
    if type(corrected_provider_fill) is not ProviderFillEvidence:
        raise TypeError("corrected_provider_fill must be exact ProviderFillEvidence")
    if type(replacement) is not JournalTransaction:
        raise TypeError("replacement must be an exact JournalTransaction")
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
    for label, evidence in (
        ("original", original_provider_fill),
        ("corrected", corrected_provider_fill),
    ):
        if evidence.provider_environment != economic_book.provider_environment:
            raise AccountingConflict(
                f"{label} provider fill provider_environment does not match economic book"
            )

    initial_aggregate_id = _provider_fill_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_environment=economic_book.provider_environment,
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
        or initial_request.get(
            "provider_environment",
            economic_book.environment,
        ) != economic_book.provider_environment
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
        or initial_provider_evidence.provider_environment
        != economic_book.provider_environment
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
    provider_domain = (
        ""
        if economic_book.provider_environment == economic_book.environment
        else f"provider-environment:{economic_book.provider_environment}:"
    )
    expected_cause_event_id = (
        f"provider:{economic_book.provider_id}:environment:{economic_book.environment}:"
        f"{provider_domain}"
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
        provider_environment=economic_book.provider_environment,
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
            or payload.get(
                "provider_environment",
                economic_book.environment,
            ) != economic_book.provider_environment
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
            or historical_provider.provider_environment
            != economic_book.provider_environment
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
    provider_scope_payload = _provider_environment_payload(
        provider_environment=economic_book.provider_environment,
        environment=economic_book.environment,
    )
    if provider_scope_payload is not None:
        request["provider_environment"] = provider_scope_payload
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
    if provider_scope_payload is not None:
        payload["provider_environment"] = provider_scope_payload
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


_PROVIDER_ECONOMIC_CUT_TOKEN = object()
_PROVIDER_ECONOMIC_CUT_LOCK = Lock()
_ISSUED_PROVIDER_ECONOMIC_CUTS: dict[
    int,
    tuple[weakref.ReferenceType["ProviderEconomicCut"], str],
] = {}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderEconomicCut:
    """Immutable evidence for one exact durable economic-book prefix."""

    store_identity: JournalStoreIdentity
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    book_id: str
    aggregate_version: int
    journal_sequence: int
    visibility_journal_sequence: int
    event_id: str
    payload_hash: str
    transaction_digests: tuple[tuple[str, str], ...]
    resulting_book_digest: str
    cut_digest: str
    _token: InitVar[object | None] = None

    def __post_init__(self, _token: object | None) -> None:
        if _token is not _PROVIDER_ECONOMIC_CUT_TOKEN:
            raise AccountingConflict(
                "provider economic cut must come from canonical durable replay"
            )
        require_exact_journal_store_identity(
            self.store_identity,
            subject="provider economic cut store identity",
        )
        for name in (
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "book_id",
            "event_id",
            "payload_hash",
            "resulting_book_digest",
            "cut_digest",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise AccountingConflict(
                    f"provider economic cut {name} must be exact canonical text"
                )
        if type(self.aggregate_version) is not int or self.aggregate_version <= 0:
            raise AccountingConflict(
                "provider economic cut aggregate_version must be positive"
            )
        if type(self.journal_sequence) is not int or self.journal_sequence <= 0:
            raise AccountingConflict(
                "provider economic cut journal_sequence must be positive"
            )
        if (
            type(self.visibility_journal_sequence) is not int
            or self.visibility_journal_sequence < self.journal_sequence
        ):
            raise AccountingConflict(
                "provider economic cut visibility_journal_sequence must be an "
                "integer at or after the terminal economic event"
            )
        if type(self.transaction_digests) is not tuple:
            raise AccountingConflict(
                "provider economic cut transaction_digests must be immutable"
            )
        for item in self.transaction_digests:
            if (
                type(item) is not tuple
                or len(item) != 2
                or any(type(value) is not str or not value for value in item)
            ):
                raise AccountingConflict(
                    "provider economic cut transaction digest entry is invalid"
                )


def _provider_economic_cut_seal_digest(value: ProviderEconomicCut) -> str:
    if type(value) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    return payload_digest(
        {
            "schema_version": "provider-economic-cut-seal.v1",
            "cut_digest": value.cut_digest,
            "provider_id": value.provider_id,
            "account_id": value.account_id,
            "environment": value.environment,
            "provider_environment": value.provider_environment,
            "book_id": value.book_id,
            "aggregate_version": value.aggregate_version,
            "journal_sequence": value.journal_sequence,
            "visibility_journal_sequence": value.visibility_journal_sequence,
            "event_id": value.event_id,
            "payload_hash": value.payload_hash,
            "transaction_digests": [list(item) for item in value.transaction_digests],
            "resulting_book_digest": value.resulting_book_digest,
            "store_identity": {
                "canonical_path": value.store_identity.canonical_path,
                "filesystem_device": value.store_identity.filesystem_device,
                "filesystem_inode": value.store_identity.filesystem_inode,
                "identity_source": value.store_identity.identity_source,
                "windows_volume_serial": value.store_identity.windows_volume_serial,
                "windows_file_index_high": value.store_identity.windows_file_index_high,
                "windows_file_index_low": value.store_identity.windows_file_index_low,
            },
        }
    )


def _seal_provider_economic_cut(
    value: ProviderEconomicCut,
) -> ProviderEconomicCut:
    key = id(value)
    seal = _provider_economic_cut_seal_digest(value)

    def cleanup(ref: weakref.ReferenceType[ProviderEconomicCut]) -> None:
        with _PROVIDER_ECONOMIC_CUT_LOCK:
            current = _ISSUED_PROVIDER_ECONOMIC_CUTS.get(key)
            if current is not None and current[0] is ref:
                _ISSUED_PROVIDER_ECONOMIC_CUTS.pop(key, None)

    ref = weakref.ref(value, cleanup)
    with _PROVIDER_ECONOMIC_CUT_LOCK:
        _ISSUED_PROVIDER_ECONOMIC_CUTS[key] = (ref, seal)
    return value


def require_provider_economic_cut(value: object) -> ProviderEconomicCut:
    """Require an unchanged cut issued by canonical replay in this process."""

    if type(value) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    seal = _provider_economic_cut_seal_digest(value)
    with _PROVIDER_ECONOMIC_CUT_LOCK:
        issued = _ISSUED_PROVIDER_ECONOMIC_CUTS.get(id(value))
    if (
        issued is None
        or issued[0]() is not value
        or issued[1] != seal
    ):
        raise AccountingConflict(
            "provider economic cut was not issued by canonical durable replay "
            "or changed after issuance"
        )
    return value


def reverify_provider_economic_cut(
    book: "DurableProviderEconomicBook",
    cut: object,
    *,
    expected_visibility_journal_sequence: int,
) -> ProviderEconomicCut:
    """Reconstruct one cut against an independently selected frozen visibility.

    This terminal verification seam does not rely on the process-local issuance
    registry and does not let the candidate select its own causal cutoff. A
    value crossing a restart/artifact boundary is accepted only when exact
    canonical replay of the selected JournalStore at the independently supplied
    global visibility sequence reproduces every authority-bearing field.
    """

    if type(book) is not DurableProviderEconomicBook:
        raise TypeError("book must be exact DurableProviderEconomicBook")
    if type(cut) is not ProviderEconomicCut:
        raise TypeError("cut must be exact ProviderEconomicCut")
    if (
        type(expected_visibility_journal_sequence) is not int
        or expected_visibility_journal_sequence <= 0
    ):
        raise ValueError(
            "expected_visibility_journal_sequence must be an exact positive integer"
        )
    if cut.visibility_journal_sequence != expected_visibility_journal_sequence:
        raise AccountingConflict(
            "provider economic cut visibility does not match expected authority"
        )
    if (
        cut.provider_id != book.provider_id
        or cut.account_id != book.account_id
        or cut.environment != book.environment
        or cut.provider_environment != book.provider_environment
        or cut.book_id != book.book_id
    ):
        raise AccountingConflict(
            "provider economic cut scope does not match selected durable book"
        )
    exact_store_identity = require_exact_journal_store_authority(
        book.store,
        subject="provider economic cut verification JournalStore",
    )
    if cut.store_identity != exact_store_identity:
        raise AccountingConflict(
            "provider economic cut store identity does not match selected authority"
        )
    replayed = book.resolve_historical_cut(
        cut.aggregate_version,
        expected_journal_sequence=cut.journal_sequence,
        expected_event_id=cut.event_id,
        visibility_journal_sequence=expected_visibility_journal_sequence,
    )
    if replayed != cut:
        raise AccountingConflict(
            "provider economic cut does not match canonical durable replay"
        )
    return replayed


@dataclass
class _DurableProviderEconomicBookAuthority:
    """Detached snapshot of one selected durable financial-book generation."""

    store: JournalStore
    store_identity: object
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
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
            or state.get("provider_environment") != authority.provider_environment
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
            provider_environment=authority.provider_environment,
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
        provider_environment: str | None = None,
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
        provider_environment_value = _provider_environment(
            provider_id=object.__getattribute__(value, "provider_id"),
            environment=object.__getattribute__(value, "environment"),
            provider_environment=provider_environment,
        )
        object.__setattr__(value, "provider_environment", provider_environment_value)
        object.__setattr__(
            value,
            "book_id",
            _book_id(
                provider_id=object.__getattribute__(value, "provider_id"),
                account_id=object.__getattribute__(value, "account_id"),
                environment=object.__getattribute__(value, "environment"),
                provider_environment=provider_environment_value,
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
                provider_environment=state["provider_environment"],
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



def _economic_store_current_journal_sequence(
    economic_book: object,
) -> int:
    authority = _require_durable_provider_economic_book_authority(economic_book)
    with journal_store_authority_scope(
        authority.store,
        authority.store_identity,
    ):
        return JournalStore.current_journal_sequence(authority.store)


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
        {"store", "provider_id", "account_id", "environment", "provider_environment", "book_id", "_book"}
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
        provider_environment: str | None = None,
    ):
        _initialize_durable_provider_economic_book(
            self,
            store,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
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
                or payload.get("provider_environment", authority.environment)
                != authority.provider_environment
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
                provider_environment=authority.provider_environment,
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

    def read_cut(self) -> EconomicBookCut:
        """Replay and return the exact current durable economic-book cut."""

        events = self._events()
        current = self._replay(events)
        return EconomicBookCut(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            transactions=current.transactions,
            book_digest=current.audit_digest(),
            aggregate_version=(
                0 if not events else int(events[-1]["aggregate_version"])
            ),
        )

    @staticmethod
    def _store_identity_payload(
        identity: JournalStoreIdentity,
    ) -> dict[str, object]:
        exact = require_exact_journal_store_identity(
            identity,
            subject="provider economic cut store identity",
        )
        return {
            "canonical_path": exact.canonical_path,
            "filesystem_device": exact.filesystem_device,
            "filesystem_inode": exact.filesystem_inode,
            "identity_source": exact.identity_source,
            "windows_volume_serial": exact.windows_volume_serial,
            "windows_file_index_high": exact.windows_file_index_high,
            "windows_file_index_low": exact.windows_file_index_low,
        }

    def resolve_historical_cut(
        self,
        aggregate_version: int,
        *,
        expected_journal_sequence: int | None = None,
        expected_event_id: str | None = None,
        visibility_journal_sequence: int | None = None,
    ) -> ProviderEconomicCut:
        """Resolve one immutable prefix at one frozen global journal cut.

        When visibility_journal_sequence is omitted, the canonical JournalStore
        freezes its current global journal sequence before aggregate replay.
        That default prevents a caller from minting an older favorable economic
        revision after a later correction is already durable. Reconstructing a
        previously frozen historical cut must supply that cut's recorded
        visibility_journal_sequence explicitly.

        The durable book identity, event scope and batch digest retain exact
        provider_environment when it differs from the runtime environment.
        Historical cuts therefore remain isolated across BYBIT MAINNET,
        TESTNET and DEMO while preserving legacy identity for providers whose
        provider environment is identical to the runtime environment.
        """

        if type(aggregate_version) is not int or aggregate_version <= 0:
            raise ValueError(
                "aggregate_version must be an exact positive integer"
            )
        if (
            expected_journal_sequence is not None
            and (
                type(expected_journal_sequence) is not int
                or expected_journal_sequence <= 0
            )
        ):
            raise ValueError(
                "expected_journal_sequence must be an exact positive integer"
            )
        if (
            expected_event_id is not None
            and (
                type(expected_event_id) is not str
                or not expected_event_id
                or expected_event_id != expected_event_id.strip()
            )
        ):
            raise ValueError("expected_event_id must be exact canonical text")
        if (
            visibility_journal_sequence is not None
            and (
                type(visibility_journal_sequence) is not int
                or visibility_journal_sequence <= 0
            )
        ):
            raise ValueError(
                "visibility_journal_sequence must be an exact positive integer"
            )
        if type(self) is not DurableProviderEconomicBook:
            raise TypeError(
                "historical economic cut requires exact DurableProviderEconomicBook"
            )
        expected_book_id = _book_id(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
        )
        if self.book_id != expected_book_id:
            raise AccountingConflict(
                "historical economic cut book identity does not match owner scope"
            )
        # Provider environment is part of the durable book identity, so
        # historical cuts are unambiguous across BYBIT TESTNET/DEMO/MAINNET.
        if type(self.store) is not JournalStore:
            raise AccountingConflict(
                "historical economic cut requires exact canonical JournalStore"
            )
        identity = require_exact_journal_store_authority(
            self.store,
            subject="provider economic cut JournalStore",
        )
        with journal_store_authority_scope(self.store, identity):
            current_journal_sequence = JournalStore.current_journal_sequence(
                self.store
            )
            if (
                visibility_journal_sequence is not None
                and visibility_journal_sequence > current_journal_sequence
            ):
                raise AccountingConflict(
                    "historical economic visibility cut is beyond durable journal"
                )
            resolved_visibility_journal_sequence = (
                current_journal_sequence
                if visibility_journal_sequence is None
                else visibility_journal_sequence
            )
            events = JournalStore.load_events(
                self.store,
                "economic_book",
                self.book_id,
            )

        if len(events) < aggregate_version:
            raise AccountingConflict(
                "requested historical economic cut is beyond durable history"
            )
        visible_events = []
        for event in events:
            sequence = event.get("journal_sequence")
            if type(sequence) is not int or sequence <= 0:
                raise AccountingConflict(
                    "historical economic event journal sequence is invalid"
                )
            if sequence <= resolved_visibility_journal_sequence:
                visible_events.append(event)
        if not visible_events:
            raise AccountingConflict(
                "historical economic book has no state at visibility cut"
            )
        visible_version = visible_events[-1].get("aggregate_version")
        if visible_version != aggregate_version:
            raise AccountingConflict(
                "requested aggregate version is stale at visibility cut"
            )

        prefix = events[:aggregate_version]
        if (
            len(prefix) != aggregate_version
            or prefix[-1].get("aggregate_version") != aggregate_version
        ):
            raise AccountingConflict(
                "historical economic cut aggregate prefix is invalid"
            )
        candidate = DurableProviderEconomicBook._replay(self, prefix)
        terminal = prefix[-1]
        journal_sequence = terminal.get("journal_sequence")
        if type(journal_sequence) is not int or journal_sequence <= 0:
            raise AccountingConflict(
                "historical economic cut terminal journal sequence is invalid"
            )
        event_id = terminal.get("event_id")
        payload_hash = terminal.get("payload_hash")
        if (
            type(event_id) is not str
            or not event_id
            or type(payload_hash) is not str
            or not payload_hash
        ):
            raise AccountingConflict(
                "historical economic cut terminal durable identity is invalid"
            )
        if (
            expected_journal_sequence is not None
            and journal_sequence != expected_journal_sequence
        ):
            raise AccountingConflict(
                "historical economic cut journal sequence mismatch"
            )
        if expected_event_id is not None and event_id != expected_event_id:
            raise AccountingConflict(
                "historical economic cut event identity mismatch"
            )

        ordered = tuple(
            (transaction.transaction_id, transaction_digest(transaction))
            for transaction in candidate.transactions
        )
        resulting = candidate.audit_digest()
        visibility_sequence = resolved_visibility_journal_sequence
        material = {
            "schema_version": "provider-economic-cut.v1",
            "store_identity": self._store_identity_payload(identity),
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "book_id": self.book_id,
            "aggregate_version": aggregate_version,
            "journal_sequence": journal_sequence,
            "visibility_journal_sequence": visibility_sequence,
            "event_id": event_id,
            "payload_hash": payload_hash,
            "transaction_digests": [list(item) for item in ordered],
            "resulting_book_digest": resulting,
        }
        cut_digest = payload_digest(material)
        cut = ProviderEconomicCut(
            store_identity=identity,
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            provider_environment=self.provider_environment,
            book_id=self.book_id,
            aggregate_version=aggregate_version,
            journal_sequence=journal_sequence,
            visibility_journal_sequence=visibility_sequence,
            event_id=event_id,
            payload_hash=payload_hash,
            transaction_digests=ordered,
            resulting_book_digest=resulting,
            cut_digest=cut_digest,
            _token=_PROVIDER_ECONOMIC_CUT_TOKEN,
        )
        return _seal_provider_economic_cut(cut)

    def prepare_batch_mutation(
        self,
        transactions: Iterable[JournalTransaction],
        *,
        committed_at: str | None = None,
        expected_previous_book_digest: str | None = None,
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
        previous_digest = current.audit_digest()
        if (
            expected_previous_book_digest is not None
            and previous_digest != expected_previous_book_digest
        ):
            raise AccountingConflict(
                "economic book changed after validated read cut"
            )
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
            provider_environment=authority.provider_environment,
            transactions=batch,
        )
        request = {
            "schema_version": "1.0.0",
            "provider_id": authority.provider_id,
            "account_id": authority.account_id,
            "environment": authority.environment,
            "provider_environment": authority.provider_environment,
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
            matching_event_index = next(
                index for index, event in enumerate(events) if event is matches[0]
            )
            historical = DurableProviderEconomicBook._replay(
                self, events[: matching_event_index + 1]
            )
            result = {
                "batch_digest": batch_digest,
                "transaction_ids": [item.transaction_id for item in batch],
                "resulting_book_digest": historical.audit_digest(),
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
            "provider_environment": authority.provider_environment,
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
        committed_at: str | None = None,
        expected_journal_sequence: int | None = None,
        expected_whole_store_counts: Mapping[str, int] | None = None,
    ) -> bool:
        _require_durable_provider_economic_book_authority(self)
        return DurableProviderEconomicBook.append_batch(
            self,
            (transaction,),
            committed_at=committed_at,
            expected_journal_sequence=expected_journal_sequence,
            expected_whole_store_counts=expected_whole_store_counts,
        )

    def append_batch(
        self,
        transactions: Iterable[JournalTransaction],
        *,
        committed_at: str | None = None,
        expected_journal_sequence: int | None = None,
        expected_whole_store_counts: Mapping[str, int] | None = None,
    ) -> bool:
        authority = _require_durable_provider_economic_book_authority(self)
        plan = DurableProviderEconomicBook.prepare_batch_mutation(self, transactions, committed_at=committed_at)
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
                expected_whole_store_counts=expected_whole_store_counts,
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
    if economic_book.environment in {"PAPER", "LIVE"} and (
        provider_fill_binding is None
        or _order_book is None
        or _order_fill_plan is None
    ):
        raise AccountingConflict(
            "PAPER/LIVE economic batches require provider fill binding "
            "and atomic canonical order projection"
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
        if type(provider_fill_binding) is not PreparedProviderFillBinding:
            raise TypeError(
                "provider_fill_binding must be exact PreparedProviderFillBinding or None"
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
    reservation_event_key = _scoped_identity(
        "atomic-fill-reservation-event",
        economic_book.provider_id,
        economic_book.account_id,
        economic_book.environment,
        cid,
    )
    if _order_fill_plan is not None and _order_fill_plan.snapshot.state == "FILLED":
        if _order_fill_plan.snapshot.open_quantity != 0:
            raise AccountingConflict(
                "FILLED OMS snapshot must have zero open quantity before reservation release"
            )
        order_fill_envelope = _order_fill_plan.envelope
        if type(order_fill_envelope) is not dict:
            raise AccountingConflict(
                "FILLED OMS reservation release requires exact durable fill envelope"
            )
        order_fill_request = _order_fill_plan.request
        order_fill_client_order_id = _text(
            order_fill_request.get("client_order_id"),
            name="client_order_id",
        )
        order_fill_fill_id = _text(
            order_fill_request.get("fill_id"),
            name="fill_id",
        )
        order_fill_provider_execution_id = _text(
            order_fill_request.get("provider_execution_id"),
            name="provider_execution_id",
        )
        reservation_plan = reservation_book.prepare_consume_and_mark_filled_mutation(
            event_key=reservation_event_key,
            idempotency_key=reservation_component_key,
            reservation_id=rid,
            usage=usage,
            order_fill_event_id=_order_fill_plan.event_id,
            order_fill_payload_hash=_text(
                order_fill_envelope.get("payload_hash"),
                name="order_fill_payload_hash",
            ),
            order_fill_snapshot_digest=payload_digest(
                _order_fill_plan.snapshot_payload
            ),
            order_fill_mutation_hash=_order_fill_plan.mutation_hash,
            order_fill_provider_id=economic_book.provider_id,
            order_fill_client_order_id=order_fill_client_order_id,
            order_fill_fill_id=order_fill_fill_id,
            order_fill_provider_execution_id=order_fill_provider_execution_id,
            committed_at=when,
            expected_snapshot_digest=reservation_expected_snapshot_digest,
        )
    else:
        reservation_plan = reservation_book.prepare_consume_mutation(
            event_key=reservation_event_key,
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
        if not isinstance(provider_execution_id, str):
            raise AccountingConflict(
                "OMS fill provider execution is absent from the atomic economic batch"
            )
        expected_cause_event_id = provider_execution_id
        if provider_fill_binding is not None:
            expected_cause_event_id = (
                f"provider:{economic_book.provider_id}:"
                f"environment:{economic_book.environment}:"
                f"account:{economic_book.account_id}:"
                f"execution:{provider_execution_id}"
            )
        if any(
            item.cause_event_id != expected_cause_event_id
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
                if _order_fill_plan is not None
                else None
            ),
            events=(
                (
                    []
                    if _order_fill_plan is None or _order_fill_plan.already_committed
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
    expected_journal_sequence: int | None = None,
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

    if economic_book.environment in {"PAPER", "LIVE"}:
        raise AccountingConflict(
            "PAPER/LIVE OMS fill financial composition must use "
            "the provider-evidence entrypoint"
        )
    if expected_journal_sequence is not None and (
        type(expected_journal_sequence) is not int
        or expected_journal_sequence < 0
    ):
        raise ValueError(
            "expected_journal_sequence must be an exact non-negative integer"
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
    if (
        expected_journal_sequence is not None
        and plan.journal_sequence_cut != expected_journal_sequence
    ):
        raise AccountingConflict(
            "journal sequence changed after validated recovery cut"
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
        if type(provider_fill_correction_binding) is not PreparedProviderFillCorrectionBinding:
            raise TypeError(
                "provider_fill_correction_binding must be exact "
                "PreparedProviderFillCorrectionBinding"
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

    if type(reversal) is not JournalTransaction:
        raise TypeError("reversal must be an exact JournalTransaction")
    if type(replacement) is not JournalTransaction:
        raise TypeError("replacement must be an exact JournalTransaction")
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
    if type(original_projected_fill) is not ProjectedFillEvidence:
        raise TypeError("original_projected_fill must be exact ProjectedFillEvidence")
    if type(original_provider_fill) is not ProviderFillEvidence:
        raise TypeError("original_provider_fill must be exact ProviderFillEvidence")
    if type(corrected_projected_fill) is not ProjectedFillEvidence:
        raise TypeError("corrected_projected_fill must be exact ProjectedFillEvidence")
    if type(corrected_provider_fill) is not ProviderFillEvidence:
        raise TypeError("corrected_provider_fill must be exact ProviderFillEvidence")
    normalized_instrument = _text(
        expected_instrument,
        name="expected_instrument",
    )
    normalized_settlement = _text(
        settlement_currency,
        name="settlement_currency",
    ).upper()
    normalized_family = _text(asset_family, name="asset_family").upper()
    normalized_correction_observed_at = _instant_text(
        correction_observed_at,
        name="correction_observed_at",
    )
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
        expected_instrument=normalized_instrument,
        settlement_currency=normalized_settlement,
        correction_observed_at=normalized_correction_observed_at,
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
        asset_family=normalized_family,
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



def _settled_obligation_ids_for_economic_source(
    economic_book: DurableProviderEconomicBook,
    source_transaction_id: str,
) -> tuple[str, ...]:
    """Find durable settlement completion already applied to one economic source.

    A post-settlement bust needs a distinct provider compensation authority.
    Merely reversing trade-date economics would otherwise let a BUY reversal
    restore settled cash before the provider has evidenced that cash return.
    Read the immutable settlement journal directly so callers cannot bypass this
    safety check by omitting a DurableSettlementBook object.
    """

    source_id = _text(
        source_transaction_id,
        name="source_transaction_id",
    )
    scope_material = canonical_json(
        [
            economic_book.provider_id,
            economic_book.account_id,
            economic_book.environment,
            "settlement-book",
        ]
    )
    scope_id = str(
        uuid5(
            NAMESPACE_URL,
            "settlement-book:" + scope_material,
        )
    )
    events = _economic_store_load_events(
        economic_book,
        "settlement_book",
        scope_id,
    )
    if not events:
        return ()

    expected_scope = {
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
    }
    source_obligation_ids: set[str] = set()
    settled_ids: set[str] = set()
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, Mapping) or payload.get("scope") != expected_scope:
            raise AccountingConflict(
                "fill bust found invalid durable settlement scope"
            )
        event_type = event.get("event_type")
        if event_type == "SettlementObligationsRegistered":
            obligations = payload.get("obligations")
            if not isinstance(obligations, list) or not obligations:
                raise AccountingConflict(
                    "fill bust found invalid durable settlement registration"
                )
            for obligation in obligations:
                if not isinstance(obligation, Mapping):
                    raise AccountingConflict(
                        "fill bust found invalid durable settlement obligation"
                    )
                if obligation.get("source_transaction_id") != source_id:
                    continue
                obligation_id = obligation.get("obligation_id")
                if not isinstance(obligation_id, str) or not obligation_id:
                    raise AccountingConflict(
                        "fill bust found settlement obligation without identity"
                    )
                source_obligation_ids.add(obligation_id)
        elif event_type == "SettlementEvidenceApplied":
            evidence = payload.get("evidence")
            if not isinstance(evidence, Mapping):
                raise AccountingConflict(
                    "fill bust found invalid durable settlement evidence"
                )
            obligation_id = evidence.get("obligation_id")
            if not isinstance(obligation_id, str) or not obligation_id:
                raise AccountingConflict(
                    "fill bust found settlement evidence without obligation identity"
                )
            settled_ids.add(obligation_id)
        else:
            raise AccountingConflict(
                "fill bust found unsupported durable settlement event"
            )
    return tuple(sorted(source_obligation_ids & settled_ids))

def commit_provider_fill_bust_with_economic_reversal(
    economic_book: DurableProviderEconomicBook,
    order_book: DurableOrderBookProjection,
    *,
    command_id: str,
    idempotency_key: str,
    projected_fill: ProjectedFillEvidence,
    provider_fill: ProviderFillEvidence,
    expected_instrument: str,
    settlement_currency: str,
    bust_provider_revision: str,
    bust_observed_at: str,
    order_event_key: str,
    reservation_book: DurableReservationBook | None = None,
    reservation_id: str | None = None,
    correction_fill_id: str | None = None,
    committed_at: str | None = None,
    order_evidence_refs: Sequence[Mapping[str, object]] | None = None,
) -> bool:
    """Atomically compose provider BUST_FILL with its exact economic reversal.

    A bust never releases reservation capacity. When the caller supplies the
    canonical reservation authority, the exact provider-fill binding is loaded
    from durable history and its original derived usage is moved from consumed
    back to remaining in the same JournalStore transaction as the OMS bust and
    economic reversal. That restores held capacity; it does not free it.

    Unsettled settlement obligations remain immutable historical evidence and
    become inactive with their reversed economic source. A source that already
    has settlement-completion evidence is blocked until a distinct provider
    settlement-compensation authority exists. An exact OMS-only legacy split
    may recover the missing financial effects under a global journal-sequence
    CAS. Finance-without-OMS and unproven split state fail closed.
    """

    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError("economic_book must be exact DurableProviderEconomicBook")
    if type(order_book) is not DurableOrderBookProjection:
        raise TypeError("order_book must be exact DurableOrderBookProjection")
    if type(projected_fill) is not ProjectedFillEvidence:
        raise TypeError("projected_fill must be ProjectedFillEvidence")
    if type(provider_fill) is not ProviderFillEvidence:
        raise TypeError("provider_fill must be ProviderFillEvidence")
    normalized_instrument = _text(
        expected_instrument,
        name="expected_instrument",
    )
    normalized_settlement = _text(
        settlement_currency,
        name="settlement_currency",
    ).upper()
    normalized_bust_observed_at = _instant_text(
        bust_observed_at,
        name="bust_observed_at",
    )
    _require_durable_provider_economic_book_authority(economic_book)
    _require_same_financial_journal_generation(
        economic_book,
        order_book,
        expected_type=DurableOrderBookProjection,
        subject="order projection",
    )
    if (
        order_book.provider_id != economic_book.provider_id
        or order_book.account_id != economic_book.account_id
        or order_book.environment != economic_book.environment
    ):
        raise AccountingConflict(
            "OMS and economic bust books must share provider/account/environment scope"
        )
    if economic_book.environment in {"PAPER", "LIVE"}:
        raise AccountingConflict(
            "PAPER/LIVE fill bust compensation requires canonical provider bust authority"
        )
    evidence_journal_sequence = _economic_store_current_journal_sequence(
        economic_book
    )
    economic_book.refresh()
    order_book.refresh()
    if projected_fill.client_order_id is None:
        raise AccountingConflict(
            "provider-evidenced fill bust requires client_order_id"
        )
    if projected_fill.provider_execution_id != provider_fill.provider_execution_id:
        raise AccountingConflict(
            "fill bust provider execution identity changed"
        )

    when = (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        if committed_at is None
        else _instant_text(committed_at, name="committed_at")
    )
    revision = _text(
        bust_provider_revision,
        name="bust_provider_revision",
    )
    cid = _text(command_id, name="command_id")
    idem = _text(idempotency_key, name="idempotency_key")

    reservation_plan = None
    reservation_usage: Mapping[str, Decimal] | None = None
    reservation_cut_digest: str | None = None
    initial_fill_binding_event_id: str | None = None
    initial_fill_binding_request_digest: str | None = None
    initial_source_transaction_id: str | None = None
    rid: str | None = None

    binding_aggregate_id = _provider_fill_binding_aggregate_id(
        provider_id=economic_book.provider_id,
        account_id=economic_book.account_id,
        environment=economic_book.environment,
        provider_execution_id=projected_fill.provider_execution_id,
    )
    binding_events = _economic_store_load_events(
        economic_book,
        _PROVIDER_FILL_BINDING_AGGREGATE_TYPE,
        binding_aggregate_id,
    )

    if reservation_book is None:
        if reservation_id is not None:
            raise ValueError("reservation_id requires reservation_book")
        if binding_events:
            raise AccountingConflict(
                "reservation-bound fill bust requires reservation authority"
            )
    else:
        if type(reservation_book) is not DurableReservationBook:
            raise TypeError(
                "reservation_book must be exact DurableReservationBook"
            )
        if reservation_id is None:
            raise ValueError("reservation_id is required with reservation_book")
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
        if projected_fill.correction_of is not None:
            raise AccountingConflict(
                "reservation-aware fill bust is not qualified for corrected fill evidence"
            )

        rid = _text(reservation_id, name="reservation_id")
        if len(binding_events) != 1:
            raise AccountingConflict(
                "reservation-aware fill bust requires exactly one initial financial binding"
            )
        binding_event = binding_events[0]
        if (
            binding_event.get("event_type") != _PROVIDER_FILL_BINDING_EVENT_TYPE
            or int(binding_event.get("aggregate_version", 0)) != 1
        ):
            raise AccountingConflict(
                "initial provider fill financial binding is invalid"
            )
        binding_payload = binding_event.get("payload")
        if type(binding_payload) is not dict:
            raise AccountingConflict(
                "initial provider fill financial binding payload is invalid"
            )
        _require_plain_financial_json(
            binding_payload,
            name="initial provider fill financial binding payload",
        )
        if payload_digest(binding_payload) != binding_event.get("payload_hash"):
            raise AccountingConflict(
                "initial provider fill financial binding payload hash is invalid"
            )
        binding_request = binding_payload.get("request")
        if type(binding_request) is not dict:
            raise AccountingConflict(
                "initial provider fill financial binding request is invalid"
            )
        binding_request = dict(binding_request)
        binding_request_digest = payload_digest(binding_request)
        if binding_payload.get("request_digest") != binding_request_digest:
            raise AccountingConflict(
                "initial provider fill financial binding request digest is invalid"
            )

        expected_projected_payload = _projected_fill_binding_payload(
            projected_fill
        )
        expected_provider_payload = _provider_fill_binding_payload(
            provider_fill
        )
        if (
            binding_request.get("provider_id") != economic_book.provider_id
            or binding_request.get("account_id") != economic_book.account_id
            or binding_request.get("environment") != economic_book.environment
            or binding_request.get("provider_execution_id")
            != projected_fill.provider_execution_id
            or binding_request.get("reservation_id") != rid
            or binding_request.get("intent_id") != projected_fill.intent_id
            or binding_request.get("fill_id") != projected_fill.fill_id
            or binding_request.get("provider_revision")
            != projected_fill.provider_revision
            or binding_request.get("projected_fill")
            != expected_projected_payload
            or binding_request.get("provider_fill")
            != expected_provider_payload
            or binding_request.get("projected_fill_digest")
            != payload_digest(expected_projected_payload)
            or binding_request.get("provider_fill_digest")
            != payload_digest(expected_provider_payload)
        ):
            raise AccountingConflict(
                "fill bust evidence does not match the initial financial binding"
            )

        reservation_usage = _positive_usage_map(
            binding_request.get("derived_usage"),
            name="initial provider fill usage",
        )
        snapshot = reservation_book.get(rid)
        if snapshot.intent_id != projected_fill.intent_id:
            raise AccountingConflict(
                "fill bust intent does not match admitted reservation"
            )
        for resource, amount in reservation_usage.items():
            if resource not in snapshot.original or amount > snapshot.original[resource]:
                raise AccountingConflict(
                    "fill bust usage exceeds the admitted reservation envelope"
                )

        source_transaction_id = _text(
            binding_request.get("transaction_id"),
            name="initial transaction_id",
        )
        initial_source_transaction_id = source_transaction_id
        source_transactions = tuple(
            transaction
            for transaction in economic_book.transactions
            if transaction.transaction_id == source_transaction_id
        )
        if len(source_transactions) != 1:
            raise AccountingConflict(
                "initial provider fill binding does not identify one economic transaction"
            )
        source_transaction = source_transactions[0]
        if binding_request.get("transaction_digest") != payload_digest(
            canonical_transaction(source_transaction)
        ):
            raise AccountingConflict(
                "initial provider fill transaction digest is invalid"
            )
        stored_cut = _text(
            binding_request.get("reservation_cut_digest"),
            name="initial reservation_cut_digest",
        )
        if (
            not stored_cut.startswith("sha256:")
            or len(stored_cut) != 71
            or any(ch not in "0123456789abcdef" for ch in stored_cut[7:])
        ):
            raise AccountingConflict(
                "initial provider fill reservation cut is invalid"
            )
        usage_payload = _usage_payload(reservation_usage)
        expected_plan_digest = payload_digest(
            {
                "schema_version": "1.1.0",
                "provider_id": economic_book.provider_id,
                "account_id": economic_book.account_id,
                "environment": economic_book.environment,
                "reservation_id": rid,
                "intent_id": projected_fill.intent_id,
                "provider_execution_id": projected_fill.provider_execution_id,
                "reservation_cut_digest": stored_cut,
                "transaction": canonical_transaction(source_transaction),
                "derived_usage": usage_payload,
            }
        )
        if binding_request.get("plan_digest") != expected_plan_digest:
            raise AccountingConflict(
                "initial provider fill financial plan digest is invalid"
            )
        reservation_cut_digest = reservation_snapshot_digest(snapshot)
        initial_fill_binding_event_id = _text(
            binding_event.get("event_id"),
            name="initial fill binding event_id",
        )
        initial_fill_binding_request_digest = binding_request_digest

    order = order_book.order(projected_fill.client_order_id)
    if order.side != projected_fill.side:
        raise AccountingConflict("fill bust OMS side differs from provider evidence")
    roots = tuple(
        item
        for item in order.fill_history
        if item.provider_execution_id == projected_fill.provider_execution_id
        and item.correction_of is None
    )
    if len(roots) != 1:
        raise AccountingConflict(
            "fill bust requires one immutable OMS root for provider execution"
        )
    root_fill_id = roots[0].fill_id
    active_before = tuple(
        item
        for item in order.active_fills
        if item.provider_execution_id == projected_fill.provider_execution_id
    )

    reversal = build_provider_fill_bust_transaction(
        book=economic_book,
        provider_id=economic_book.provider_id,
        projected_fill=projected_fill,
        provider_fill=provider_fill,
        expected_instrument=normalized_instrument,
        settlement_currency=normalized_settlement,
        bust_provider_revision=revision,
        bust_observed_at=normalized_bust_observed_at,
    )
    if reversal.reverses_transaction_id is None:
        raise AccountingConflict(
            "fill bust compensation is not bound to one reversed economic source"
        )
    if (
        initial_source_transaction_id is not None
        and reversal.reverses_transaction_id != initial_source_transaction_id
    ):
        raise AccountingConflict(
            "fill bust reversal does not target the reservation-bound source transaction"
        )
    settled_source_obligations = _settled_obligation_ids_for_economic_source(
        economic_book,
        reversal.reverses_transaction_id,
    )
    if settled_source_obligations:
        raise AccountingConflict(
            "settled fill bust requires provider settlement compensation authority: "
            + ", ".join(settled_source_obligations)
        )
    economic_plan = economic_book.prepare_batch_mutation(
        (reversal,),
        committed_at=when,
    )
    order_plan = order_book.prepare_bust_fill_mutation(
        event_key=order_event_key,
        client_order_id=projected_fill.client_order_id,
        fill_id=root_fill_id,
        provider_revision=revision,
        correction_fill_id=correction_fill_id,
        committed_at=when,
        evidence_refs=order_evidence_refs,
    )
    if reservation_book is not None:
        assert rid is not None
        assert reservation_usage is not None
        assert reservation_cut_digest is not None
        reservation_plan = reservation_book.prepare_restore_consumption_mutation(
            event_key=_scoped_identity(
                "atomic-fill-bust-reservation-event",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                cid,
            ),
            idempotency_key=_scoped_identity(
                "atomic-fill-bust-reservation",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                idem,
            ),
            reservation_id=rid,
            usage=reservation_usage,
            committed_at=when,
            expected_snapshot_digest=reservation_cut_digest,
        )

    if not order_plan.already_committed:
        if len(active_before) != 1:
            raise AccountingConflict(
                "fresh fill bust does not target one active OMS provider execution"
            )
        current = active_before[0]
        if (
            current.provider_execution_id != projected_fill.provider_execution_id
            or current.fill_id != projected_fill.fill_id
            or current.correction_of != projected_fill.correction_of
            or current.quantity != projected_fill.quantity
            or current.price != projected_fill.price
            or current.provider_revision != projected_fill.provider_revision
        ):
            raise AccountingConflict(
                "fresh fill bust OMS state differs from active provider evidence"
            )

    if economic_plan.already_committed and not order_plan.already_committed:
        economic_book.refresh()
        order_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        raise AccountingConflict(
            "economic fill reversal is committed without the matching OMS bust"
        )
    if reservation_plan is not None:
        if reservation_plan.already_committed and not (
            economic_plan.already_committed and order_plan.already_committed
        ):
            economic_book.refresh()
            order_book.refresh()
            reservation_book.refresh()
            raise AccountingConflict(
                "reservation restoration is committed without the matching OMS/economic bust"
            )
        if (
            economic_plan.already_committed
            and order_plan.already_committed
            and not reservation_plan.already_committed
        ):
            economic_book.refresh()
            order_book.refresh()
            reservation_book.refresh()
            raise AccountingConflict(
                "committed OMS/economic bust is missing reservation restoration"
            )

    request = {
        "schema_version": "1.0.0",
        "provider_id": economic_book.provider_id,
        "account_id": economic_book.account_id,
        "environment": economic_book.environment,
        "order_bust": {
            "event_id": order_plan.event_id,
            "event_key": order_plan.event_key,
            "operation": order_plan.operation,
            "request": order_plan.request,
            "mutation_hash": order_plan.mutation_hash,
            "snapshot_digest": payload_digest(order_plan.snapshot_payload),
        },
        "economic_reversal": economic_plan.request,
        "provider_execution_id": projected_fill.provider_execution_id,
        "source_transaction_id": reversal.reverses_transaction_id,
        "bust_transaction_id": reversal.transaction_id,
        "bust_provider_revision": revision,
        "bust_observed_at": _instant_text(
            bust_observed_at,
            name="bust_observed_at",
        ),
    }
    result = {
        "order_bust": {
            "event_id": order_plan.event_id,
            "snapshot": order_plan.snapshot_payload,
        },
        "economic_reversal": economic_plan.result,
    }
    if reservation_plan is not None:
        assert rid is not None
        assert initial_fill_binding_event_id is not None
        assert initial_fill_binding_request_digest is not None
        request = {
            **request,
            "schema_version": "1.1.0",
            "reservation_restoration": {
                "reservation_id": rid,
                "operation": "RESTORE_CONSUMPTION",
                "idempotency_key": reservation_plan.idempotency_key,
                "request": reservation_plan.request,
                "aggregate_version": reservation_plan.aggregate_version,
                "snapshot_digest": payload_digest(
                    reservation_plan.snapshot_payload
                ),
                "source_fill_binding_event_id": (
                    initial_fill_binding_event_id
                ),
                "source_fill_binding_request_digest": (
                    initial_fill_binding_request_digest
                ),
            },
        }
        result = {
            **result,
            "reservation_restoration": {
                "reservation_id": rid,
                "snapshot": reservation_plan.snapshot_payload,
            },
        }
    command_identity = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/atomic-fill-bust/"
            + _scoped_identity(
                "atomic-fill-bust-command",
                economic_book.provider_id,
                economic_book.account_id,
                economic_book.environment,
                cid,
            ),
        )
    )
    journal_idempotency_key = "atomic-fill-bust:" + _scoped_identity(
        "atomic-fill-bust-idempotency",
        economic_book.provider_id,
        economic_book.account_id,
        economic_book.environment,
        idem,
    )
    actor = "atomic-fill-bust-financial-integration"

    def validate_command_authority(authority: Mapping[str, object]) -> None:
        if authority.get("result") != result:
            raise AccountingConflict(
                "OMS bust and economic reversal lack matching durable command result"
            )
        raw_events = authority.get("events")
        if not isinstance(raw_events, tuple):
            raise AccountingConflict(
                "atomic fill bust command has invalid event batch"
            )
        order_matches = []
        economic_matches = []
        reservation_matches = []
        unexpected = []
        for event in raw_events:
            if not isinstance(event, Mapping):
                unexpected.append(event)
                continue
            payload = event.get("payload")
            if event.get("event_id") == order_plan.event_id:
                if (
                    event.get("event_type") != "OrderProjectionMutationCommitted"
                    or event.get("aggregate_type") != "order_projection_book"
                    or event.get("aggregate_id") != order_book.aggregate_id
                    or event.get("aggregate_version") != order_plan.aggregate_version
                    or not isinstance(payload, Mapping)
                    or payload.get("event_key") != order_plan.event_key
                    or payload.get("operation") != "BUST_FILL"
                    or payload.get("request") != order_plan.request
                    or payload.get("snapshot") != order_plan.snapshot_payload
                ):
                    raise AccountingConflict(
                        "atomic fill bust command has invalid OMS semantic owner"
                    )
                order_matches.append(event)
                continue
            if (
                event.get("event_type")
                == DurableProviderEconomicBook._BATCH_EVENT
                and event.get("aggregate_type") == "economic_book"
                and event.get("aggregate_id") == economic_book.book_id
                and event.get("aggregate_version") == economic_plan.aggregate_version
                and isinstance(payload, Mapping)
                and payload.get("provider_id") == economic_book.provider_id
                and payload.get("account_id") == economic_book.account_id
                and payload.get("environment") == economic_book.environment
                and payload.get("batch_digest") == economic_plan.batch_digest
                and payload.get("transactions")
                == [
                    canonical_transaction(reversal)
                ]
            ):
                economic_matches.append(event)
                continue
            if reservation_plan is not None and (
                event.get("event_type") == "ReservationMutationCommitted"
                and event.get("aggregate_type") == "reservation_book"
                and event.get("aggregate_id") == reservation_book.scope_id
                and event.get("aggregate_version")
                == reservation_plan.aggregate_version
                and isinstance(payload, Mapping)
                and payload.get("environment")
                == reservation_book.environment
                and payload.get("account_id") == reservation_book.account_id
                and payload.get("operation") == "RESTORE_CONSUMPTION"
                and payload.get("idempotency_key")
                == reservation_plan.idempotency_key
                and payload.get("request") == reservation_plan.request
                and payload.get("request_hash")
                == payload_digest(reservation_plan.request)
                and payload.get("snapshot")
                == reservation_plan.snapshot_payload
                and payload_digest(payload) == event.get("payload_hash")
            ):
                reservation_matches.append(event)
                continue
            unexpected.append(event)

        if reservation_plan is None:
            if len(economic_matches) != 1 or len(order_matches) > 1 or unexpected:
                raise AccountingConflict(
                    "atomic fill bust command is bound to unexpected durable effects"
                )
            # Fresh legacy composition owns both events. OMS-only recovery owns
            # the missing finance event and names the immutable pre-existing OMS event.
            if len(raw_events) == 2 and len(order_matches) != 1:
                raise AccountingConflict(
                    "fresh atomic fill bust command does not own its OMS event"
                )
            if len(raw_events) not in {1, 2}:
                raise AccountingConflict(
                    "atomic fill bust command has invalid effect cardinality"
                )
        else:
            if (
                len(economic_matches) != 1
                or len(reservation_matches) != 1
                or len(order_matches) > 1
                or unexpected
            ):
                raise AccountingConflict(
                    "atomic fill bust command is bound to unexpected durable effects"
                )
            # Fresh composition owns OMS + economic + reservation events.
            # Exact OMS-only recovery owns only the two missing financial effects.
            if len(raw_events) == 3 and len(order_matches) != 1:
                raise AccountingConflict(
                    "fresh reservation-aware fill bust command does not own its OMS event"
                )
            if len(raw_events) == 2 and order_matches:
                raise AccountingConflict(
                    "reservation-aware OMS recovery has invalid effect ownership"
                )
            if len(raw_events) not in {2, 3}:
                raise AccountingConflict(
                    "reservation-aware fill bust command has invalid effect cardinality"
                )

    if economic_plan.already_committed and order_plan.already_committed:
        try:
            authority = _economic_store_load_command_event_batch(
                economic_book,
                command_id=command_identity,
                actor=actor,
                environment=economic_book.environment,
                idempotency_key=journal_idempotency_key,
                request=request,
            )
        except ValueError as error:
            economic_book.refresh()
            order_book.refresh()
            if reservation_book is not None:
                reservation_book.refresh()
            raise AccountingConflict(
                "OMS bust/economic/reservation durable command authority is invalid"
            ) from error
        if authority is None:
            raise AccountingConflict(
                "OMS bust and financial effects exist without one atomic/recovery command authority"
            )
        validate_command_authority(authority)
        economic_book.refresh()
        order_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        return False

    if economic_plan.envelope is None:
        raise AccountingConflict(
            "fresh fill bust reversal is missing its durable economic event"
        )
    if not order_plan.already_committed and order_plan.envelope is None:
        raise AccountingConflict(
            "fresh fill bust is missing its durable OMS event"
        )
    if reservation_plan is not None and reservation_plan.envelope is None:
        raise AccountingConflict(
            "fresh fill bust is missing its durable reservation restoration event"
        )

    events: list[tuple[dict[str, Any], str | None]] = []
    if not order_plan.already_committed:
        assert order_plan.envelope is not None
        events.append((order_plan.envelope, order_plan.outbox_topic))
    if reservation_plan is not None:
        assert reservation_plan.envelope is not None
        events.append((reservation_plan.envelope, None))
    events.append((economic_plan.envelope, "autotrade.economic.events"))

    try:
        _, inserted, _ = _economic_store_commit_command(
            economic_book,
            command_id=command_identity,
            actor=actor,
            environment=economic_book.environment,
            idempotency_key=journal_idempotency_key,
            request=request,
            result=result,
            state_version=max(
                order_plan.aggregate_version,
                economic_plan.aggregate_version,
                0
                if reservation_plan is None
                else reservation_plan.aggregate_version,
            ),
            expected_journal_sequence=evidence_journal_sequence,
            events=events,
        )
    except Exception:
        economic_book.refresh()
        order_book.refresh()
        if reservation_book is not None:
            reservation_book.refresh()
        raise

    economic_book.refresh()
    order_book.refresh()
    if reservation_book is not None:
        reservation_book.refresh()
    recorded = order_book.order(
        projected_fill.client_order_id
    ).snapshot()
    if recorded != order_plan.snapshot:
        raise AccountingConflict(
            "atomic fill bust OMS replay differs from prepared snapshot"
        )
    if reversal not in economic_book.transactions:
        raise AccountingConflict(
            "atomic fill bust economic reversal did not replay"
        )
    if reservation_plan is not None:
        assert reservation_book is not None
        assert rid is not None
        restored = reservation_book.get(rid)
        if restored != reservation_plan.snapshot:
            raise AccountingConflict(
                "atomic fill bust reservation restoration did not replay"
            )
    return inserted

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
    expected_journal_sequence: int | None = None,
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
    if type(projected_fill) is not ProjectedFillEvidence:
        raise TypeError("projected_fill must be exact ProjectedFillEvidence")
    if type(provider_fill) is not ProviderFillEvidence:
        raise TypeError("provider_fill must be exact ProviderFillEvidence")
    normalized_instrument = _text(
        expected_instrument,
        name="expected_instrument",
    )
    normalized_settlement = _text(
        settlement_currency,
        name="settlement_currency",
    ).upper()
    normalized_family = _text(asset_family, name="asset_family").upper()
    normalized_observed_at = (
        None
        if observed_at is None
        else _instant_text(observed_at, name="observed_at")
    )
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
        expected_instrument=normalized_instrument,
        settlement_currency=normalized_settlement,
        reservation_snapshot=snapshot,
        asset_family=normalized_family,
        observed_at=normalized_observed_at,
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
        if (
            order_event_key is not None
            or order_evidence_refs is not None
            or expected_journal_sequence is not None
        ):
            raise ValueError(
                "order_event_key/evidence/expected journal sequence require order_book"
            )
        if economic_book.environment in {"PAPER", "LIVE"}:
            raise AccountingConflict(
                "PAPER/LIVE provider fills require atomic canonical order projection"
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
    if expected_journal_sequence is not None and (
        type(expected_journal_sequence) is not int
        or expected_journal_sequence < 0
    ):
        raise ValueError(
            "expected_journal_sequence must be an exact non-negative integer"
        )
    order_plan = order_book.prepare_record_fill_mutation(
        event_key=order_event_key,
        client_order_id=projected_fill.client_order_id,
        fill_id=projected_fill.fill_id,
        provider_execution_id=projected_fill.provider_execution_id,
        quantity=projected_fill.quantity,
        price=projected_fill.price,
        provider_revision=projected_fill.provider_revision,
        committed_at=when,
        evidence_refs=order_evidence_refs,
    )
    if (
        expected_journal_sequence is not None
        and order_plan.journal_sequence_cut != expected_journal_sequence
    ):
        raise AccountingConflict(
            "journal sequence changed after validated recovery cut"
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
        _order_book=order_book,
        _order_fill_plan=order_plan,
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
        "provider_environment",
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
        "provider_environment",
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
        provider_environment=state["provider_environment"],
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
            "provider_environment",
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
        snapshot.provider_environment,
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
    provider_scope = _provider_environment(
        provider_id=provider,
        environment=scope,
        provider_environment=activity.provider_environment,
    )
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
    provider_scope_payload = _provider_environment_payload(
        provider_environment=provider_scope,
        environment=scope,
    )

    identity = _activity_identity(
        provider_id=provider,
        account_id=account,
        environment=scope,
        provider_environment=provider_scope,
        activity_id=activity.activity_id,
    )
    book_id = _book_id(
        provider_id=provider,
        account_id=account,
        environment=scope,
        provider_environment=provider_scope,
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
    if provider_scope_payload is not None:
        request["provider_environment"] = provider_scope_payload
        request["activity"]["provider_environment"] = provider_scope_payload

    result = {
        "provider_id": provider,
        "account_id": account,
        "environment": scope,
        "activity_id": activity.activity_id,
        "transaction_id": transaction_id,
        "amount": amount_text,
        "currency": activity.currency,
    }
    if provider_scope_payload is not None:
        result["provider_environment"] = provider_scope_payload

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
            or economic_payload.get("provider_environment", scope) != provider_scope
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
    if provider_scope_payload is not None:
        economic_payload["provider_environment"] = provider_scope_payload
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
