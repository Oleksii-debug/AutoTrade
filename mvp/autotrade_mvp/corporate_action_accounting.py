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
    reverse_transaction,
    transaction_digest,
    validate_transaction,
)
from .corporate_action_evidence import (
    AuthoritativeCorporateAction,
    CorporateActionEvidenceConflict,
    DurableCorporateActionEvidenceStore,
    authoritative_corporate_action_projection,
)
from .corporate_actions import CorporateActionBook, CorporateEvent, EquityState, Transition
from .persistence import (
    JournalStore,
    canonical_json,
    journal_store_authority_scope,
    payload_digest,
    require_exact_journal_store_authority,
)
from .provider_activity_accounting import DurableProviderEconomicBook
from .reconciliation import ProviderActivityEvidence


_SUPPORTED_DURABLE_KINDS = frozenset({"CASH_DIVIDEND"})


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
        accepted.provider_fact_digest,
        suffix,
    )


def _corporate_action_reconciliation_id(
    payload: Mapping[str, object],
    _identity_fn=_identity,
    _error_type=CorporateActionEvidenceConflict,
) -> str:
    """Bind reconciliation to the stable provider fact, not its observation receipt."""

    required_text = (
        "provider_id",
        "account_id",
        "environment",
        "external_event_id",
        "provider_revision",
        "provider_fact_digest",
    )
    for name in required_text:
        if type(payload.get(name)) is not str or not payload[name]:
            raise _error_type(
                f"corporate-action reconciliation {name} is invalid"
            )
    correction = payload.get("corrects_external_event_id")
    if correction is not None and (
        type(correction) is not str or not correction
    ):
        raise _error_type(
            "corporate-action reconciliation correction identity is invalid"
        )
    return _identity_fn(
        "corporate-action-reconciliation",
        payload["provider_id"],
        payload["account_id"],
        payload["environment"],
        payload["external_event_id"],
        payload["provider_revision"],
        payload["provider_fact_digest"],
        "" if correction is None else correction,
    )


def _bind_corporate_action_reconciliation_inputs(
    evidence_store_type,
    action_type,
    authority_projection,
    provider_activity_type,
    reconciliation_id_fn,
    evidence_composition,
    evidence_events,
    evidence_payload,
    mapping_type,
    error_type,
):
    def corporate_action_reconciliation_inputs(
        evidence_store: DurableCorporateActionEvidenceStore,
        *,
        provider_actions: tuple[AuthoritativeCorporateAction, ...],
    ) -> tuple[tuple[str, ...], tuple[ProviderActivityEvidence, ...]]:
        """Project WP-31 history into the existing account-reconciliation authority.

        Local identities come only from durable accepted evidence. Provider-side
        activities come only from issuer-verified AuthoritativeCorporateAction
        objects. Revision/provenance changes therefore become ordinary
        missing/unexpected activity mismatches in reconcile_account(); no second
        reconciliation ledger or verdict engine is introduced here.

        Complete-history/coverage authority remains owned by the caller's existing
        provider reconciliation cut. This helper deliberately does not fabricate
        pagination, consistency or provider-origin coverage.
        """

        if type(evidence_store) is not evidence_store_type:
            raise TypeError(
                "evidence_store must be exact DurableCorporateActionEvidenceStore"
            )
        if type(provider_actions) is not tuple:
            raise TypeError("provider_actions must be an exact tuple")

        (
            _store,
            _store_identity,
            provider_id,
            account_id,
            environment,
            _aggregate_id,
        ) = evidence_composition(evidence_store)

        local_ids = tuple(
            sorted(
                reconciliation_id_fn(
                    evidence_payload(event)
                )
                for event in evidence_events(evidence_store)
            )
        )
        if len(local_ids) != len(set(local_ids)):
            raise error_type(
                "durable corporate-action reconciliation identities are not unique"
            )

        activities_by_id: dict[str, ProviderActivityEvidence] = {}
        for action in provider_actions:
            if type(action) is not action_type:
                raise TypeError(
                    "provider_actions must contain exact AuthoritativeCorporateAction"
                )
            projection = authority_projection(action)
            if (
                projection["provider_id"] != provider_id
                or projection["account_id"] != account_id
                or projection["environment"] != environment
            ):
                raise error_type(
                    "provider corporate-action reconciliation scope mismatch"
                )
            if provider_id == "BYBIT":
                raise error_type(
                    "BYBIT corporate-action reconciliation requires explicit "
                    "provider_environment authority"
                )

            activity_id = reconciliation_id_fn(projection)
            payload = projection["payload"]
            if not isinstance(payload, mapping_type):
                raise error_type(
                    "provider corporate-action payload projection is invalid"
                )
            currency = payload.get("currency")
            if currency is not None and type(currency) is not str:
                raise error_type(
                    "provider corporate-action currency must be exact text"
                )
            activity = provider_activity_type(
                provider_id=provider_id,
                account_id=account_id,
                environment=environment,
                activity_id=activity_id,
                activity_type=f"CORPORATE_ACTION:{projection['kind']}",
                origin="EXTERNAL",
                occurred_at=projection["observed_at"],
                instrument=projection["instrument_id"],
                currency=currency,
            )
            prior = activities_by_id.get(activity_id)
            if prior is not None and prior != activity:
                raise error_type(
                    "provider corporate-action reconciliation identity conflicts"
                )
            activities_by_id[activity_id] = activity

        return (
            local_ids,
            tuple(
                activities_by_id[key]
                for key in sorted(activities_by_id)
            ),
        )

    return corporate_action_reconciliation_inputs


corporate_action_reconciliation_inputs = (
    _bind_corporate_action_reconciliation_inputs(
        DurableCorporateActionEvidenceStore,
        AuthoritativeCorporateAction,
        authoritative_corporate_action_projection,
        ProviderActivityEvidence,
        _corporate_action_reconciliation_id,
        DurableCorporateActionEvidenceStore._composition,
        DurableCorporateActionEvidenceStore._events,
        DurableCorporateActionEvidenceStore._payload,
        Mapping,
        CorporateActionEvidenceConflict,
    )
)
del _bind_corporate_action_reconciliation_inputs


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
    source_journal_sequence: int,
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
    if (
        type(source_journal_sequence) is not int
        or source_journal_sequence < 0
    ):
        raise TypeError(
            "source_journal_sequence must be a non-negative integer"
        )
    observed_cut = activation_cut.astimezone(timezone.utc)
    symbol = version.provider_symbol
    position_account = f"POSITION:{symbol}"
    quantity = Decimal("0")
    contributors: list[dict[str, str]] = []

    economic_book.refresh()
    for transaction in economic_book.transactions:
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
        "schema_version": "1.1.0",
        "source_journal_sequence": source_journal_sequence,
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
            accepted.provider_fact_digest,
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
            observed_at=committed_reversal.observed_at,
        )
        if rebuilt_reversal != committed_reversal:
            raise AccountingConflict(
                "corporate-action correction reversal conflicts with retained evidence"
            )
        committed_replacement = next(
            (
                item
                for item in all_transactions
                if item.transaction_id == expected_replacement_id
            ),
            None,
        )
        replacement = _dividend_transaction(
            accepted,
            transition,
            order_key=original.economic_order_key or order_key,
            corrects_transaction_id=original.transaction_id,
            economic_effective_at=original.economic_effective_at,
            observed_at=(
                accepted.observed_at
                if committed_replacement is None
                else committed_replacement.observed_at
            ),
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
            accepted.provider_fact_digest,
            "reversal",
        ),
        observed_at=accepted.observed_at,
    )
    replacement = _dividend_transaction(
        accepted,
        transition,
        order_key=original.economic_order_key or order_key,
        corrects_transaction_id=original.transaction_id,
        economic_effective_at=original.economic_effective_at,
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
        return _correction_transactions(
            economic_book,
            accepted,
            transition,
            exact_retry=exact_retry,
        )

    order_key = _order_key(accepted.external_event_id)
    active = _active_for_order_key(economic_book, order_key)
    retained_observed_at = (
        active[0].observed_at
        if exact_retry and len(active) == 1
        else transaction_observed_at
    )
    transaction = _dividend_transaction(
        accepted,
        transition,
        order_key=order_key,
        observed_at=retained_observed_at,
    )
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

    store_identity = require_exact_journal_store_authority(
        store,
        subject="corporate-action financial JournalStore",
    )
    if type(evidence_store) is not DurableCorporateActionEvidenceStore:
        raise TypeError(
            "evidence_store must be exact DurableCorporateActionEvidenceStore"
        )
    if type(economic_book) is not DurableProviderEconomicBook:
        raise TypeError(
            "economic_book must be exact DurableProviderEconomicBook"
        )
    if type(corporate_book) is not CorporateActionBook:
        raise TypeError("corporate_book must be exact CorporateActionBook")
    if type(accepted) is not AuthoritativeCorporateAction:
        raise TypeError(
            "accepted must be exact AuthoritativeCorporateAction from sealed provider evidence"
        )
    (
        evidence_journal,
        _,
        evidence_provider_id,
        evidence_account_id,
        evidence_environment,
        _,
    ) = DurableCorporateActionEvidenceStore._composition(evidence_store)
    if evidence_journal is not store or economic_book.store is not store:
        raise ValueError(
            "corporate-action evidence and economics must share one JournalStore"
        )
    if (
        evidence_provider_id != economic_book.provider_id
        or evidence_account_id != economic_book.account_id
        or evidence_environment != economic_book.environment
    ):
        raise ValueError("corporate-action durable authorities have different scope")

    # Cross the resolver-issuance boundary before any financial interpretation.
    # The evidence store returns a detached canonical copy reconstructed from its
    # closure-owned issuance snapshot, so later caller mutation cannot alter the
    # economic inputs selected for this operation.
    initial_evidence_plan = (
        DurableCorporateActionEvidenceStore.prepare_record_mutation(
            evidence_store,
            accepted,
        )
    )
    accepted = initial_evidence_plan.accepted

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
        retained = DurableCorporateActionEvidenceStore.record(
            evidence_store,
            accepted,
        )
        return CorporateActionFinancialResult(
            inserted=retained.inserted,
            source_event_id=retained.event_id,
            accepted_event=accepted.event,
            transition=None,
            next_state=corporate_book.state,
            transaction_ids=(),
            economically_active=False,
        )

    evidence_plan = DurableCorporateActionEvidenceStore.prepare_record_mutation(
        evidence_store,
        accepted,
    )
    candidate, transition = _candidate_book(corporate_book, accepted)
    activation_text = activation_cut.isoformat().replace("+00:00", "Z")

    # A fully committed exact retry authorizes no new financial mutation.  Verify
    # its retained economics without relabelling historical proof with today's
    # unrelated journal tail.
    if evidence_plan.already_committed:
        retry_transactions = _economic_transactions(
            economic_book,
            accepted,
            transition,
            exact_retry=True,
            transaction_observed_at=activation_text,
        )
        retry_economic_plan = (
            economic_book.prepare_batch_mutation(
                retry_transactions,
                committed_at=activation_text,
            )
            if retry_transactions
            else None
        )
        retry_economic_committed = (
            retry_economic_plan is None
            or retry_economic_plan.already_committed
        )
        if retry_economic_committed:
            return CorporateActionFinancialResult(
                inserted=False,
                source_event_id=evidence_plan.event_id,
                accepted_event=accepted.event,
                transition=transition,
                next_state=candidate.state,
                transaction_ids=tuple(
                    item.transaction_id for item in retry_transactions
                ),
                economically_active=True,
            )

    # Fresh economic authority must be derived from one durable global journal
    # cut and CAS that exact cut at commit.
    with journal_store_authority_scope(store, store_identity):
        source_journal_sequence = JournalStore.current_journal_sequence(store)
    entitlement_position = _canonical_entitlement_position_proof(
        economic_book,
        corporate_book,
        accepted,
        activation_cut=activation_cut,
        source_journal_sequence=source_journal_sequence,
    )

    # Re-prepare after the captured cut so every mutable plan is derived from
    # the same attempted journal state.  Any later advance is rejected by CAS.
    evidence_plan = DurableCorporateActionEvidenceStore.prepare_record_mutation(
        evidence_store,
        accepted,
    )
    candidate, transition = _candidate_book(corporate_book, accepted)
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
        "source_journal_sequence": source_journal_sequence,
        "evidence_previously_retained": evidence_plan.already_committed,
        "entitlement_position": entitlement_position,
    }
    result = {
        "source_event_id": evidence_plan.event_id,
        "external_event_id": accepted.external_event_id,
        "provider_fact_digest": accepted.provider_fact_digest,
        "provenance_digest": accepted.provenance_digest,
        "transaction_ids": [item.transaction_id for item in transactions],
        "activation_at": activation_text,
        "source_journal_sequence": source_journal_sequence,
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
                    accepted.provider_fact_digest,
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
        accepted.provider_fact_digest,
    )
    try:
        with journal_store_authority_scope(store, store_identity):
            _, inserted, _ = JournalStore.commit_command(
                store,
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
                expected_journal_sequence=source_journal_sequence,
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

