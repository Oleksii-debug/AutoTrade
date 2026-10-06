"""Canonical financial-accounting semantic owner for WP-36 scientific gates.

This module deliberately does not turn a healthy accounting/reconciliation replay
into proof of economic edge. It binds one scientific GateProfile/ScientificRegistry
owner to one exact ScientificFinancialCut, replays the existing durable provider
economic book from the same JournalStore, and cross-checks that book against the
same current reconciliation checkpoint.

The broader document-06 G2 financial/operational verdict remains INCONCLUSIVE:
execution-fidelity qualification and other document-04 oracles are separate
owners. In particular, caller ``EvaluationEvidence.financial_invariants_passed``
is ignored by the wrapper below rather than being upgraded to PASS.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp.exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    parse_bounded_exact_decimal,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.reconciliation_journal import (
    require_current_reconciliation_checkpoint,
)
from mvp.autotrade_mvp.scientific_financial_cut import (
    FinancialCutConflict,
    FinancialCutUnavailable,
    ScientificFinancialCut,
    capture_current_scientific_financial_cut,
)

from ..science.registry import ProtocolViolation, ScientificRegistry
from .gates import EvaluationEvidence, GateDecision, GateProfile, _detached_gate_input
from .scientific_trial_owner import (
    _canonical_gate_profile_authority_view,
    evaluate_gates_with_scientific_trial_owner,
    gate_profile_subject_digest,
    resolve_gate_profile_protocol_binding,
)


class ScientificFinancialOwnerConflict(RuntimeError):
    """The requested scientific/financial authorities do not describe one cut."""


@dataclass(frozen=True, slots=True)
class ScientificFinancialAccountingEvidence:
    """Digest-only evidence for one valid canonical accounting/reconciliation cut."""

    scientific_protocol_id: str
    gate_profile_digest: str
    financial_cut_digest: str
    journal_population_digest: str
    reconciliation_checkpoint_digest: str
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str
    economic_book_digest: str
    economic_transaction_count: int
    reconciled_cash_digest: str
    reconciled_position_digest: str
    owner_digest: str = ""

    def __post_init__(self) -> None:
        if type(self) is not ScientificFinancialAccountingEvidence:
            raise TypeError(
                "financial accounting evidence must be exact "
                "ScientificFinancialAccountingEvidence"
            )
        if type(self.economic_transaction_count) is not int:
            raise TypeError("economic_transaction_count must be an exact integer")
        if self.economic_transaction_count < 0:
            raise ValueError("economic_transaction_count cannot be negative")
        subject = {
            "schema_version": "wp36-financial-accounting-owner-v1",
            "scientific_protocol_id": self.scientific_protocol_id,
            "gate_profile_digest": self.gate_profile_digest,
            "financial_cut_digest": self.financial_cut_digest,
            "journal_population_digest": self.journal_population_digest,
            "reconciliation_checkpoint_digest": self.reconciliation_checkpoint_digest,
            "provider_id": self.provider_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "provider_environment": self.provider_environment,
            "economic_book_digest": self.economic_book_digest,
            "economic_transaction_count": self.economic_transaction_count,
            "reconciled_cash_digest": self.reconciled_cash_digest,
            "reconciled_position_digest": self.reconciled_position_digest,
        }
        object.__setattr__(self, "owner_digest", payload_digest(subject))


def _financial_cut_identity(cut: ScientificFinancialCut) -> tuple[object, ...]:
    if type(cut) is not ScientificFinancialCut:
        raise TypeError("financial_cut must be exact ScientificFinancialCut")
    return (
        object.__getattribute__(cut, "scientific_protocol_id"),
        object.__getattribute__(cut, "gate_profile_digest"),
        object.__getattribute__(cut, "provider_id"),
        object.__getattribute__(cut, "account_id"),
        object.__getattribute__(cut, "environment"),
        object.__getattribute__(cut, "provider_environment"),
        object.__getattribute__(cut, "reconciliation_event_id"),
        object.__getattribute__(cut, "reconciliation_journal_sequence"),
        object.__getattribute__(cut, "journal_sequence"),
        object.__getattribute__(cut, "journal_population_digest"),
        object.__getattribute__(cut, "reconciliation_checkpoint_digest"),
        object.__getattribute__(cut, "cut_digest"),
    )


def _recapture_exact_cut(
    store: JournalStore,
    *,
    expected: ScientificFinancialCut,
    protocol_id: str,
    profile_digest: str,
) -> ScientificFinancialCut:
    current = capture_current_scientific_financial_cut(
        store,
        scientific_protocol_id=protocol_id,
        gate_profile_digest=profile_digest,
        provider_id=object.__getattribute__(expected, "provider_id"),
        account_id=object.__getattribute__(expected, "account_id"),
        environment=object.__getattribute__(expected, "environment"),
        provider_environment=object.__getattribute__(expected, "provider_environment"),
        reconciliation_event_id=object.__getattribute__(
            expected, "reconciliation_event_id"
        ),
    )
    if _financial_cut_identity(current) != _financial_cut_identity(expected):
        raise ScientificFinancialOwnerConflict(
            "supplied scientific financial cut is not the current exact journal cut"
        )
    return current


def _canonical_decimal_map(
    value: object,
    *,
    name: str,
    omit_zero: bool = False,
) -> dict[str, str]:
    """Normalize authenticated local Decimal presentation without changing value.

    Reconciliation persists str(Decimal), so harmless scale such as 900.0 may
    remain in the checkpoint. The checkpoint digest authenticates those original
    bytes; this helper only converts them to one exact comparison representation.
    """

    if type(omit_zero) is not bool:
        raise TypeError("omit_zero must be an exact boolean")
    if type(value) is not dict:
        raise ScientificFinancialOwnerConflict(f"{name} must be an exact dictionary")
    result: dict[str, str] = {}
    for key, raw in value.items():
        if type(key) is not str or not key or key != key.strip():
            raise ScientificFinancialOwnerConflict(
                f"{name} keys must be non-empty exact text"
            )
        if type(raw) is not str:
            raise ScientificFinancialOwnerConflict(
                f"{name} values must use exact decimal text"
            )
        try:
            amount = parse_bounded_exact_decimal(raw)
            canonical = canonical_decimal_text(amount)
        except (ExactDecimalError, TypeError, ValueError) as error:
            raise ScientificFinancialOwnerConflict(
                f"{name} contains invalid exact decimal evidence"
            ) from error
        if omit_zero and amount == 0:
            continue
        result[key] = canonical
    return result


def _require_zero_differences(payload: Mapping[str, object], field: str) -> None:
    differences = _canonical_decimal_map(payload.get(field), name=field)
    if any(amount != "0" for amount in differences.values()):
        raise ScientificFinancialOwnerConflict(
            f"reconciliation {field} is non-zero"
        )


def _book_scope_balances(
    book: DurableProviderEconomicBook,
) -> tuple[dict[str, str], dict[str, str], int, str]:
    transactions = book.transactions
    cash_units: set[str] = set()
    position_units: set[str] = set()
    for transaction in transactions:
        for posting in transaction.postings:
            account = posting.ledger_account
            unit = posting.asset_or_currency
            if account == f"CASH:{unit}":
                cash_units.add(unit)
            if account == f"POSITION:{unit}":
                position_units.add(unit)

    cash: dict[str, str] = {}
    for unit in sorted(cash_units):
        amount = book.cash(unit)
        if amount != 0:
            cash[unit] = canonical_decimal_text(amount)

    positions: dict[str, str] = {}
    for unit in sorted(position_units):
        amount = book.position(unit)
        if amount != 0:
            positions[unit] = canonical_decimal_text(amount)

    return cash, positions, len(transactions), book.audit_digest()


def _require_clean_reconciliation(
    store: JournalStore,
    *,
    cut: ScientificFinancialCut,
    book: DurableProviderEconomicBook,
) -> tuple[str, str]:
    checkpoint = require_current_reconciliation_checkpoint(
        store,
        checkpoint_event_id=cut.reconciliation_event_id,
        provider_id=cut.provider_id,
        account_id=cut.account_id,
        environment=cut.environment,
        provider_environment=cut.provider_environment,
    )
    if type(checkpoint) is not dict:
        raise ScientificFinancialOwnerConflict(
            "reconciliation checkpoint must be an exact dictionary"
        )
    if payload_digest(checkpoint) != cut.reconciliation_checkpoint_digest:
        raise ScientificFinancialOwnerConflict(
            "current reconciliation checkpoint digest differs from financial cut"
        )
    payload = checkpoint.get("payload")
    if type(payload) is not dict:
        raise ScientificFinancialOwnerConflict(
            "reconciliation checkpoint payload must be an exact dictionary"
        )
    if payload.get("snapshot_consistent") is not True:
        raise ScientificFinancialOwnerConflict(
            "financial accounting owner requires a consistent provider snapshot"
        )

    for field in ("cash_differences", "position_differences", "borrow_differences"):
        _require_zero_differences(payload, field)

    for field in (
        "unexpected_execution_ids",
        "missing_local_execution_ids",
        "unexpected_working_provider_order_ids",
        "missing_local_working_client_order_ids",
        "unexpected_provider_activity_ids",
        "missing_local_provider_activity_ids",
        "blocking_resources",
    ):
        value = payload.get(field)
        if type(value) is not list:
            raise ScientificFinancialOwnerConflict(
                f"reconciliation {field} must be an exact list"
            )
        if value:
            raise ScientificFinancialOwnerConflict(
                f"reconciliation {field} must be empty"
            )

    if payload.get("complete") is not True:
        raise ScientificFinancialOwnerConflict(
            "financial accounting owner requires complete reconciliation"
        )

    provider_cash = _canonical_decimal_map(
        payload.get("provider_cash"),
        name="provider_cash",
        omit_zero=True,
    )
    provider_positions = _canonical_decimal_map(
        payload.get("provider_positions"),
        name="provider_positions",
        omit_zero=True,
    )
    local_cash, local_positions, _, _ = _book_scope_balances(book)
    if local_cash != provider_cash:
        raise ScientificFinancialOwnerConflict(
            "canonical economic-book cash does not match reconciled provider cash"
        )
    if local_positions != provider_positions:
        raise ScientificFinancialOwnerConflict(
            "canonical economic-book positions do not match reconciled provider positions"
        )

    return payload_digest(local_cash), payload_digest(local_positions)


def resolve_scientific_financial_accounting_owner(
    *,
    store: JournalStore,
    financial_cut: ScientificFinancialCut,
    scientific_registry: ScientificRegistry,
    profile: GateProfile,
) -> ScientificFinancialAccountingEvidence:
    """Resolve one stable registry/profile/cut/accounting authority composition."""

    if type(store) is not JournalStore:
        raise TypeError("store must be exact JournalStore")
    if type(financial_cut) is not ScientificFinancialCut:
        raise TypeError("financial_cut must be exact ScientificFinancialCut")
    if type(scientific_registry) is not ScientificRegistry:
        raise TypeError("scientific_registry must be exact ScientificRegistry")

    canonical_profile = _canonical_gate_profile_authority_view(profile)
    binding_before = resolve_gate_profile_protocol_binding(
        registry=scientific_registry,
        profile=canonical_profile,
    )
    profile_digest = gate_profile_subject_digest(canonical_profile)

    if binding_before.profile_digest != profile_digest:
        raise ScientificFinancialOwnerConflict(
            "scientific protocol binding does not match canonical gate profile digest"
        )
    if financial_cut.scientific_protocol_id != binding_before.protocol_id:
        raise ScientificFinancialOwnerConflict(
            "financial cut scientific protocol does not match registry owner"
        )
    if financial_cut.gate_profile_digest != profile_digest:
        raise ScientificFinancialOwnerConflict(
            "financial cut gate profile does not match registry owner"
        )

    before = _recapture_exact_cut(
        store,
        expected=financial_cut,
        protocol_id=binding_before.protocol_id,
        profile_digest=profile_digest,
    )

    economic_book = DurableProviderEconomicBook(
        store,
        provider_id=before.provider_id,
        account_id=before.account_id,
        environment=before.environment,
        provider_environment=before.provider_environment,
    )
    local_cash_digest, local_position_digest = _require_clean_reconciliation(
        store,
        cut=before,
        book=economic_book,
    )
    _, _, transaction_count, economic_book_digest = _book_scope_balances(
        economic_book
    )

    after = _recapture_exact_cut(
        store,
        expected=before,
        protocol_id=binding_before.protocol_id,
        profile_digest=profile_digest,
    )
    binding_after = resolve_gate_profile_protocol_binding(
        registry=scientific_registry,
        profile=canonical_profile,
    )
    if binding_after != binding_before:
        raise ScientificFinancialOwnerConflict(
            "scientific protocol owner changed while financial evidence was resolved"
        )

    return ScientificFinancialAccountingEvidence(
        scientific_protocol_id=binding_before.protocol_id,
        gate_profile_digest=profile_digest,
        financial_cut_digest=after.cut_digest,
        journal_population_digest=after.journal_population_digest,
        reconciliation_checkpoint_digest=after.reconciliation_checkpoint_digest,
        provider_id=after.provider_id,
        account_id=after.account_id,
        environment=after.environment,
        provider_environment=after.provider_environment,
        economic_book_digest=economic_book_digest,
        economic_transaction_count=transaction_count,
        reconciled_cash_digest=local_cash_digest,
        reconciled_position_digest=local_position_digest,
    )


def _decision_with_owner_check(
    base: GateDecision,
    *,
    check: str,
    reason: str | None = None,
    owner: ScientificFinancialAccountingEvidence | None = None,
) -> GateDecision:
    checks = dict(base.checks)
    provenance = dict(base.provenance or {})
    reasons = list(base.reasons)

    checks["scientific_financial_accounting_owner"] = check
    if reason:
        reasons.append(reason)
    if owner is not None:
        provenance.update(
            {
                "scientific_financial_accounting_owner_digest": owner.owner_digest,
                "scientific_financial_cut_digest": owner.financial_cut_digest,
                "scientific_financial_journal_population_digest": (
                    owner.journal_population_digest
                ),
                "scientific_financial_reconciliation_digest": (
                    owner.reconciliation_checkpoint_digest
                ),
                "scientific_financial_provider_environment": owner.provider_environment,
                "scientific_financial_economic_book_digest": owner.economic_book_digest,
                "scientific_financial_transaction_count": str(
                    owner.economic_transaction_count
                ),
                "scientific_financial_cash_digest": owner.reconciled_cash_digest,
                "scientific_financial_position_digest": (
                    owner.reconciled_position_digest
                ),
            }
        )

    status = base.status
    if check == "FAIL":
        status = "FAIL"
    elif check == "INCONCLUSIVE" and status != "FAIL":
        status = "INCONCLUSIVE"

    return GateDecision(
        status=status,
        reasons=tuple(reasons),
        checks=MappingProxyType(checks),
        provenance=MappingProxyType(provenance) if provenance else None,
    )


def evaluate_gates_with_scientific_financial_accounting_owner(
    profile: GateProfile,
    evidence: EvaluationEvidence,
    *,
    scientific_registry: ScientificRegistry,
    financial_store: JournalStore,
    financial_cut: ScientificFinancialCut,
    **gate_kwargs: object,
) -> GateDecision:
    """Evaluate WP-36 with canonical accounting/reconciliation provenance."""

    canonical_profile = _canonical_gate_profile_authority_view(profile)
    canonical_evidence = _detached_gate_input(evidence, EvaluationEvidence)
    owner_neutral_evidence = replace(
        canonical_evidence,
        financial_invariants_passed=None,
    )

    try:
        owner = resolve_scientific_financial_accounting_owner(
            store=financial_store,
            financial_cut=financial_cut,
            scientific_registry=scientific_registry,
            profile=canonical_profile,
        )
    except (KeyError, FinancialCutUnavailable) as error:
        base = evaluate_gates_with_scientific_trial_owner(
            canonical_profile,
            owner_neutral_evidence,
            scientific_registry=scientific_registry,
            **gate_kwargs,
        )
        return _decision_with_owner_check(
            base,
            check="INCONCLUSIVE",
            reason=(
                "canonical scientific financial accounting owner is unavailable: "
                + str(error)
            ),
        )
    except (
        AccountingConflict,
        FinancialCutConflict,
        ProtocolViolation,
        ScientificFinancialOwnerConflict,
        TypeError,
        ValueError,
    ) as error:
        base = evaluate_gates_with_scientific_trial_owner(
            canonical_profile,
            owner_neutral_evidence,
            scientific_registry=scientific_registry,
            **gate_kwargs,
        )
        return _decision_with_owner_check(
            base,
            check="FAIL",
            reason=(
                "canonical scientific financial accounting owner is invalid: "
                + str(error)
            ),
        )

    base = evaluate_gates_with_scientific_trial_owner(
        canonical_profile,
        owner_neutral_evidence,
        scientific_registry=scientific_registry,
        **gate_kwargs,
    )
    return _decision_with_owner_check(base, check="PASS", owner=owner)
