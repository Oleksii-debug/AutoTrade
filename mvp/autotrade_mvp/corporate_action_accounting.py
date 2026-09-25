"""Atomic authoritative corporate-action evidence and economic accounting.

The provider-evidence module owns source authenticity. CorporateActionBook stays
pure. DurableProviderEconomicBook remains the only economic ledger. This module
only composes their prepared mutations in one JournalStore transaction.

The first qualified economic mapping is CASH_DIVIDEND. Other action kinds fail
closed until their exact position/basis/settlement semantics are implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from typing import Mapping
from uuid import NAMESPACE_URL, uuid5

from .accounting import (
    AccountingConflict,
    JournalTransaction,
    Posting,
    book_equity_split_adjustment,
    reverse_transaction,
    transaction_digest,
    validate_transaction,
)
from .corporate_action_evidence import (
    AuthoritativeCorporateAction,
    CorporateActionEvidenceConflict,
    DurableCorporateActionEvidenceStore,
)
from .corporate_actions import CorporateActionBook, CorporateEvent, EquityState, Transition
from .persistence import JournalStore, canonical_json, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook


_SUPPORTED_DURABLE_KINDS = frozenset({"CASH_DIVIDEND", "SPLIT"})


def _identity(kind: str, *parts: str) -> str:
    return f"{kind}:" + sha256(
        canonical_json(list(parts)).encode("utf-8")
    ).hexdigest()


def _order_key(external_event_id: str) -> str:
    return _identity("corporate-action-order", external_event_id)


def _transaction_id(accepted: AuthoritativeCorporateAction, suffix: str) -> str:
    return _identity(
        "corporate-action-transaction",
        accepted.provider_id,
        accepted.account_id,
        accepted.environment,
        accepted.external_event_id,
        accepted.provenance_digest,
        suffix,
    )


@dataclass(frozen=True)
class CorporateActionFinancialResult:
    inserted: bool
    source_event_id: str
    accepted_event: CorporateEvent
    transition: Transition | None
    next_state: EquityState
    transaction_ids: tuple[str, ...]
    economically_active: bool = True


def _initial_book_state(book: CorporateActionBook) -> tuple[EquityState, object]:
    checkpoint = book.checkpoint("corporate-action-financial-candidate")
    if not checkpoint.records:
        return book.state, book.instrument_version
    first_event, first_transition = checkpoint.records[0]
    versions = tuple(
        value
        for value in book.registry.versions(first_event.instrument_id)
        if value.version == first_event.instrument_version
    )
    if len(versions) != 1:
        raise AccountingConflict(
            "initial corporate-action instrument version is not uniquely registered"
        )
    return first_transition.before, versions[0]


def _canonical_entitlement_position_proof(
    economic_book: DurableProviderEconomicBook,
    corporate_book: CorporateActionBook,
    accepted: AuthoritativeCorporateAction,
    *,
    activation_cut: datetime,
) -> dict[str, object]:
    """Prove dividend quantity from causal durable position history.

    The pure CorporateActionBook remains a calculator, not a position authority.
    Only POSITION postings already durable and causally knowable at the provider
    observation may authorize the quantity used for dividend economics.
    """

    version = corporate_book.instrument_version
    event = accepted.event
    if (
        event.instrument_id != version.instrument_id
        or event.instrument_version != version.version
    ):
        raise AccountingConflict(
            "corporate-action calculator instrument does not match accepted durable instrument"
        )
    if event.effective_at is None:
        raise AccountingConflict(
            "corporate-action entitlement requires exact economic effective cut"
        )

    if (
        not isinstance(activation_cut, datetime)
        or activation_cut.tzinfo is None
        or activation_cut.utcoffset() is None
    ):
        raise TypeError("activation_cut must be timezone-aware")
    observed_cut = activation_cut.astimezone(timezone.utc)
    symbol = version.provider_symbol
    position_account = f"POSITION:{symbol}"
    quantity = Decimal("0")
    contributors: list[dict[str, str]] = []

    economic_book.refresh()
    current_order_key = _order_key(accepted.external_event_id)
    for transaction in economic_book.transactions:
        # Exact retry of a position-changing action must reconstruct the
        # pre-action entitlement cut, not count its own already-durable effect.
        if transaction.economic_order_key == current_order_key:
            continue
        position_postings = tuple(
            posting
            for posting in transaction.postings
            if posting.ledger_account == position_account
            and posting.asset_or_currency == symbol
        )
        if not position_postings:
            continue
        if (
            transaction.economic_effective_at is None
            or transaction.observed_at is None
        ):
            raise AccountingConflict(
                "canonical position history lacks causal entitlement timestamps"
            )
        try:
            effective = datetime.fromisoformat(
                transaction.economic_effective_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            observed = datetime.fromisoformat(
                transaction.observed_at.replace("Z", "+00:00")
            ).astimezone(timezone.utc)
        except ValueError as error:
            raise AccountingConflict(
                "canonical position history contains invalid entitlement timestamps"
            ) from error
        if effective <= event.effective_at and observed <= observed_cut:
            quantity += sum(
                (posting.signed_amount for posting in position_postings),
                Decimal("0"),
            )
            contributors.append(
                {
                    "transaction_id": transaction.transaction_id,
                    "transaction_digest": transaction_digest(transaction),
                }
            )

    if quantity != corporate_book.state.quantity:
        raise AccountingConflict(
            "corporate-action calculator quantity does not match canonical durable position at entitlement cut"
        )

    proof: dict[str, object] = {
        "schema_version": "1.0.0",
        "instrument_version": (
            f"{version.instrument_id}@{version.version}"
        ),
        "provider_symbol": symbol,
        "economic_effective_cut": event.effective_at.isoformat().replace(
            "+00:00", "Z"
        ),
        "causal_observed_cut": observed_cut.isoformat().replace(
            "+00:00", "Z"
        ),
        "quantity": str(quantity),
        "contributing_transactions": contributors,
    }
    proof["digest"] = payload_digest(proof)
    return proof


def _candidate_book(
    book: CorporateActionBook,
    accepted: AuthoritativeCorporateAction,
) -> tuple[CorporateActionBook, Transition]:
    if not isinstance(book, CorporateActionBook):
        raise TypeError("corporate_book must be CorporateActionBook")

    event = accepted.event
    events = list(book.events)
    exact_index = next(
        (index for index, item in enumerate(events) if item.event_id == event.event_id),
        None,
    )
    if exact_index is not None:
        if events[exact_index] != event:
            raise AccountingConflict(
                "accepted corporate-action identity conflicts with pure transition history"
            )
    elif accepted.corrects_external_event_id is not None:
        target_index = next(
            (
                index
                for index, item in enumerate(events)
                if item.event_id == accepted.corrects_external_event_id
            ),
            None,
        )
        if target_index is None:
            raise AccountingConflict(
                "corporate-action correction target is absent from pure transition history"
            )
        events[target_index] = event
    else:
        events.append(event)

    initial_state, initial_version = _initial_book_state(book)
    candidate = CorporateActionBook.replay(
        initial_state,
        instrument_version=initial_version,
        registry=book.registry,
        events=tuple(events),
    )
    transition = next(
        transition
        for retained, transition in candidate.checkpoint("candidate").records
        if retained.event_id == event.event_id
    )
    return candidate, transition


def _dividend_transaction(
    accepted: AuthoritativeCorporateAction,
    transition: Transition,
    *,
    order_key: str,
    corrects_transaction_id: str | None = None,
    economic_effective_at: str | None = None,
    observed_at: str | None = None,
) -> JournalTransaction | None:
    amount = transition.after.unsettled_cash - transition.before.unsettled_cash
    if amount == 0:
        return None
    currency = transition.after.currency
    transaction = JournalTransaction(
        transaction_id=_transaction_id(accepted, "effect"),
        cause_event_id=_identity(
            "corporate-action-cause",
            accepted.external_event_id,
            accepted.provenance_digest,
            "effect",
        ),
        postings=(
            Posting(f"UNSETTLED_CASH:{currency}", currency, amount),
            Posting(f"CORPORATE_ACTION_INCOME:{currency}", currency, -amount),
        ),
        economic_effective_at=(
            economic_effective_at
            or accepted.event.effective_at.isoformat().replace("+00:00", "Z")
        ),
        economic_order_key=order_key,
        observed_at=observed_at or accepted.observed_at,
        corrects_transaction_id=corrects_transaction_id,
    )
    validate_transaction(transaction)
    return transaction


def _split_transaction(
    accepted: AuthoritativeCorporateAction,
    transition: Transition,
    *,
    order_key: str,
    observed_at: str,
    corrects_transaction_id: str | None = None,
    economic_effective_at: str | None = None,
) -> JournalTransaction | None:
    before = transition.before
    after = transition.after
    if (
        after.total_basis != before.total_basis
        or after.settled_cash != before.settled_cash
        or after.unsettled_cash != before.unsettled_cash
        or after.currency != before.currency
        or after.symbol != before.symbol
    ):
        raise AccountingConflict(
            "equity split durable mapping requires zero cash/P&L and unchanged basis"
        )
    if after.quantity == before.quantity:
        return None
    try:
        numerator = accepted.event.payload["numerator"]
        denominator = accepted.event.payload["denominator"]
    except KeyError as error:
        raise AccountingConflict(
            "equity split evidence lacks exact numerator/denominator"
        ) from error
    return book_equity_split_adjustment(
        transaction_id=_transaction_id(accepted, "effect"),
        cause_event_id=_identity(
            "corporate-action-cause",
            accepted.external_event_id,
            accepted.provenance_digest,
            "effect",
        ),
        instrument=after.symbol,
        pre_split_quantity=before.quantity,
        numerator=numerator,
        denominator=denominator,
        economic_effective_at=(
            economic_effective_at
            or accepted.event.effective_at.isoformat().replace("+00:00", "Z")
        ),
        economic_order_key=order_key,
        observed_at=observed_at,
        corrects_transaction_id=corrects_transaction_id,
    )


def _active_for_order_key(
    economic_book: DurableProviderEconomicBook,
    order_key: str,
) -> tuple[JournalTransaction, ...]:
    transactions = tuple(economic_book.transactions)
    reversed_ids = {
        item.reverses_transaction_id
        for item in transactions
        if item.reverses_transaction_id is not None
    }
    return tuple(
        item
        for item in transactions
        if item.economic_order_key == order_key
        and item.reverses_transaction_id is None
        and item.transaction_id not in reversed_ids
    )


def _correction_transactions(
    economic_book: DurableProviderEconomicBook,
    accepted: AuthoritativeCorporateAction,
    transition: Transition,
    *,
    exact_retry: bool,
    transaction_observed_at: str,
) -> tuple[JournalTransaction, ...]:
    target_id = accepted.corrects_external_event_id
    if target_id is None:
        raise AssertionError("correction target is required")
    order_key = _order_key(target_id)
    expected_reversal_id = _transaction_id(accepted, "reversal")
    expected_replacement_id = _transaction_id(accepted, "effect")

    if exact_retry:
        all_transactions = tuple(economic_book.transactions)
        committed_reversal = next(
            (
                item
                for item in all_transactions
                if item.transaction_id == expected_reversal_id
            ),
            None,
        )
        if committed_reversal is None:
            raise AccountingConflict(
                "durable correction evidence exists without reversal economics"
            )
        original_id = committed_reversal.reverses_transaction_id
        original = next(
            (item for item in all_transactions if item.transaction_id == original_id),
            None,
        )
        if original is None:
            raise AccountingConflict(
                "corporate-action correction reversal lacks original economics"
            )
        rebuilt_reversal = reverse_transaction(
            original,
            transaction_id=expected_reversal_id,
            cause_event_id=committed_reversal.cause_event_id,
            observed_at=transaction_observed_at,
        )
        if rebuilt_reversal != committed_reversal:
            raise AccountingConflict(
                "corporate-action correction reversal conflicts with retained evidence"
            )
        if accepted.event.kind == "CASH_DIVIDEND":
            replacement = _dividend_transaction(
                accepted,
                transition,
                order_key=original.economic_order_key or order_key,
                corrects_transaction_id=original.transaction_id,
                economic_effective_at=original.economic_effective_at,
                observed_at=transaction_observed_at,
            )
        elif accepted.event.kind == "SPLIT":
            if _canonical_equity_split_terms(
                original,
                instrument=transition.after.symbol,
            ) is None:
                raise AccountingConflict(
                    "equity split correction target lacks canonical split economics"
                )
            replacement = _split_transaction(
                accepted,
                transition,
                order_key=original.economic_order_key or order_key,
                observed_at=transaction_observed_at,
                corrects_transaction_id=original.transaction_id,
                economic_effective_at=original.economic_effective_at,
            )
        else:
            raise AccountingConflict(
                f"{accepted.event.kind} correction has no qualified durable mapping"
            )
        committed_replacement = next(
            (
                item
                for item in all_transactions
                if item.transaction_id == expected_replacement_id
            ),
            None,
        )
        if replacement is None:
            if committed_replacement is not None:
                raise AccountingConflict(
                    "zero-effect correction has unexpected replacement economics"
                )
            return (rebuilt_reversal,)
        if committed_replacement != replacement:
            raise AccountingConflict(
                "corporate-action correction replacement conflicts with retained evidence"
            )
        return rebuilt_reversal, replacement

    active = _active_for_order_key(economic_book, order_key)
    if len(active) != 1:
        raise AccountingConflict(
            "corporate-action correction requires one active target economic fact"
        )
    original = active[0]
    effective = accepted.event.effective_at.isoformat().replace("+00:00", "Z")
    if original.economic_effective_at != effective:
        raise AccountingConflict(
            "corporate-action correction cannot change economic effective time"
        )
    reversal = reverse_transaction(
        original,
        transaction_id=expected_reversal_id,
        cause_event_id=_identity(
            "corporate-action-cause",
            accepted.external_event_id,
            accepted.provenance_digest,
            "reversal",
        ),
        observed_at=transaction_observed_at,
    )
    if accepted.event.kind == "CASH_DIVIDEND":
        replacement = _dividend_transaction(
            accepted,
            transition,
            order_key=original.economic_order_key or order_key,
            corrects_transaction_id=original.transaction_id,
            economic_effective_at=original.economic_effective_at,
            observed_at=transaction_observed_at,
        )
    elif accepted.event.kind == "SPLIT":
        if _canonical_equity_split_terms(
            original,
            instrument=transition.after.symbol,
        ) is None:
            raise AccountingConflict(
                "equity split correction target lacks canonical split economics"
            )
        replacement = _split_transaction(
            accepted,
            transition,
            order_key=original.economic_order_key or order_key,
            observed_at=transaction_observed_at,
            corrects_transaction_id=original.transaction_id,
            economic_effective_at=original.economic_effective_at,
        )
    else:
        raise AccountingConflict(
            f"{accepted.event.kind} correction has no qualified durable mapping"
        )
    return (reversal,) if replacement is None else (reversal, replacement)


def _economic_transactions(
    economic_book: DurableProviderEconomicBook,
    accepted: AuthoritativeCorporateAction,
    transition: Transition,
    *,
    exact_retry: bool,
    transaction_observed_at: str | None = None,
) -> tuple[JournalTransaction, ...]:
    if accepted.event.kind not in _SUPPORTED_DURABLE_KINDS:
        raise AccountingConflict(
            f"{accepted.event.kind} has no qualified durable corporate-action accounting mapping"
        )

    if accepted.corrects_external_event_id is not None:
        if transaction_observed_at is None:
            raise AccountingConflict(
                "corporate-action correction requires causal observation time"
            )
        return _correction_transactions(
            economic_book,
            accepted,
            transition,
            exact_retry=exact_retry,
            transaction_observed_at=transaction_observed_at,
        )

    order_key = _order_key(accepted.external_event_id)
    if accepted.event.kind == "CASH_DIVIDEND":
        transaction = _dividend_transaction(
            accepted,
            transition,
            order_key=order_key,
            observed_at=transaction_observed_at,
        )
    elif accepted.event.kind == "SPLIT":
        if transaction_observed_at is None:
            raise AccountingConflict(
                "equity split durable accounting requires causal observation time"
            )
        transaction = _split_transaction(
            accepted,
            transition,
            order_key=order_key,
            observed_at=transaction_observed_at,
        )
    else:
        raise AccountingConflict(
            f"{accepted.event.kind} has no qualified durable corporate-action accounting mapping"
        )
    active = _active_for_order_key(economic_book, order_key)
    if active and not exact_retry:
        raise AccountingConflict(
            "corporate-action economics exist without retained exact source identity"
        )
    if exact_retry and transaction is None and active:
        raise AccountingConflict(
            "zero-effect corporate action conflicts with active retained economics"
        )
    return () if transaction is None else (transaction,)


def commit_authoritative_corporate_action(
    *,
    store: JournalStore,
    evidence_store: DurableCorporateActionEvidenceStore,
    economic_book: DurableProviderEconomicBook,
    corporate_book: CorporateActionBook,
    accepted: AuthoritativeCorporateAction,
    activation_at: datetime | None = None,
) -> CorporateActionFinancialResult:
    """Retain sealed evidence immediately; activate economics only at a causal cut.

    Provider observations made at/after the action effective time can activate
    directly.  Earlier announcements are retained without economic mutation and
    require an explicit later activation cut.  The activation cut is persisted
    as the economic commit time; production callers must source it from the
    qualified host clock/scheduler boundary.
    """

    if not isinstance(store, JournalStore):
        raise TypeError("store must be JournalStore")
    if not isinstance(evidence_store, DurableCorporateActionEvidenceStore):
        raise TypeError(
            "evidence_store must be DurableCorporateActionEvidenceStore"
        )
    if not isinstance(economic_book, DurableProviderEconomicBook):
        raise TypeError("economic_book must be DurableProviderEconomicBook")
    if not isinstance(corporate_book, CorporateActionBook):
        raise TypeError("corporate_book must be CorporateActionBook")
    if not isinstance(accepted, AuthoritativeCorporateAction):
        raise TypeError(
            "accepted must be AuthoritativeCorporateAction from sealed provider evidence"
        )
    if evidence_store.store is not store or economic_book.store is not store:
        raise ValueError(
            "corporate-action evidence and economics must share one JournalStore"
        )
    if (
        evidence_store.provider_id != economic_book.provider_id
        or evidence_store.account_id != economic_book.account_id
        or evidence_store.environment != economic_book.environment
    ):
        raise ValueError("corporate-action durable authorities have different scope")

    observed_at = datetime.fromisoformat(
        accepted.observed_at.replace("Z", "+00:00")
    ).astimezone(timezone.utc)
    effective_at = accepted.event.effective_at
    if effective_at is None:
        raise AccountingConflict(
            "durable corporate-action accounting requires exact effective_at"
        )

    cut = activation_at
    if cut is not None:
        if not isinstance(cut, datetime) or cut.tzinfo is None:
            raise TypeError("activation_at must be timezone-aware")
        cut = cut.astimezone(timezone.utc)
    activation_cut = observed_at if observed_at >= effective_at else cut

    if activation_cut is None or activation_cut < effective_at:
        # Admission and activation are deliberately separate.  This preserves
        # causal provider truth without making a future-effective posting visible.
        retained = evidence_store.record(accepted)
        return CorporateActionFinancialResult(
            inserted=retained.inserted,
            source_event_id=retained.event_id,
            accepted_event=accepted.event,
            transition=None,
            next_state=corporate_book.state,
            transaction_ids=(),
            economically_active=False,
        )

    entitlement_position = _canonical_entitlement_position_proof(
        economic_book,
        corporate_book,
        accepted,
        activation_cut=activation_cut,
    )
    evidence_plan = evidence_store.prepare_record_mutation(accepted)
    candidate, transition = _candidate_book(corporate_book, accepted)
    activation_text = activation_cut.isoformat().replace("+00:00", "Z")
    transactions = _economic_transactions(
        economic_book,
        accepted,
        transition,
        exact_retry=evidence_plan.already_committed,
        transaction_observed_at=activation_text,
    )
    economic_plan = (
        economic_book.prepare_batch_mutation(
            transactions,
            committed_at=activation_text,
        )
        if transactions
        else None
    )

    economic_committed = (
        economic_plan is None or economic_plan.already_committed
    )
    if evidence_plan.already_committed and economic_committed:
        return CorporateActionFinancialResult(
            inserted=False,
            source_event_id=evidence_plan.event_id,
            accepted_event=accepted.event,
            transition=transition,
            next_state=candidate.state,
            transaction_ids=tuple(item.transaction_id for item in transactions),
            economically_active=True,
        )
    if (
        not evidence_plan.already_committed
        and economic_plan is not None
        and economic_plan.already_committed
    ):
        raise AccountingConflict(
            "corporate-action economics are durable without matching provider evidence"
        )
    if (
        not evidence_plan.already_committed
        and evidence_plan.envelope is None
    ):
        raise CorporateActionEvidenceConflict(
            "fresh corporate-action evidence lacks durable envelope"
        )
    if economic_plan is not None and economic_plan.envelope is None:
        raise AccountingConflict(
            "fresh corporate-action economics lack durable envelope"
        )

    events: list[tuple[dict[str, object], str | None]] = []
    if not evidence_plan.already_committed:
        events.append((evidence_plan.envelope, None))
    if economic_plan is not None and not economic_plan.already_committed:
        events.append((economic_plan.envelope, "autotrade.economic.events"))
    if not events:
        return CorporateActionFinancialResult(
            inserted=False,
            source_event_id=evidence_plan.event_id,
            accepted_event=accepted.event,
            transition=transition,
            next_state=candidate.state,
            transaction_ids=tuple(item.transaction_id for item in transactions),
            economically_active=True,
        )

    request = {
        "schema_version": "1.1.0",
        "provider_evidence": evidence_plan.request,
        "economic_batch": (
            None if economic_plan is None else economic_plan.request
        ),
        "activation_at": activation_text,
        "evidence_previously_retained": evidence_plan.already_committed,
        "entitlement_position": entitlement_position,
    }
    result = {
        "source_event_id": evidence_plan.event_id,
        "external_event_id": accepted.external_event_id,
        "provenance_digest": accepted.provenance_digest,
        "transaction_ids": [item.transaction_id for item in transactions],
        "activation_at": activation_text,
        "entitlement_position_digest": entitlement_position["digest"],
        "next_state_digest": payload_digest(
            {
                "symbol": candidate.state.symbol,
                "quantity": str(candidate.state.quantity),
                "total_basis": str(candidate.state.total_basis),
                "settled_cash": str(candidate.state.settled_cash),
                "unsettled_cash": str(candidate.state.unsettled_cash),
                "currency": candidate.state.currency,
            }
        ),
    }
    command_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://commands.autotrade.local/corporate-action-financial/"
            + canonical_json(
                [
                    accepted.provider_id,
                    accepted.account_id,
                    accepted.environment,
                    accepted.external_event_id,
                    accepted.provenance_digest,
                ]
            ),
        )
    )
    idempotency_key = _identity(
        "corporate-action-financial",
        accepted.provider_id,
        accepted.account_id,
        accepted.environment,
        accepted.external_event_id,
        accepted.provenance_digest,
    )
    try:
        _, inserted, _ = store.commit_command(
            command_id=command_id,
            actor="corporate-action-financial-integration",
            environment=accepted.environment,
            idempotency_key=idempotency_key,
            request=request,
            result=result,
            state_version=max(
                evidence_plan.aggregate_version,
                0 if economic_plan is None else economic_plan.aggregate_version,
            ),
            events=events,
        )
    except Exception:
        economic_book.refresh()
        raise

    economic_book.refresh()
    return CorporateActionFinancialResult(
        inserted=inserted,
        source_event_id=evidence_plan.event_id,
        accepted_event=accepted.event,
        transition=transition,
        next_state=candidate.state,
        transaction_ids=tuple(item.transaction_id for item in transactions),
        economically_active=True,
    )

