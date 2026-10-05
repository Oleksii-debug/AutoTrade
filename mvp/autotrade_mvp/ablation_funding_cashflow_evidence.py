"""Frozen provider-authenticated perpetual-funding cashflow evidence for WP-63.

The perpetual-funding authority already binds sealed provider evidence, exact
instrument economics, a causal position cut, correction lineage and the canonical
provider economic book.  This module only freezes that owner graph at one global
journal cut and exposes signed cashflow by value unit.

Signed funding cashflow is deliberately not relabelled as the non-negative WP-63
``cost`` scalar. Receipts and payments require a preregistered projection rule in
the common value dimension before terminal cost composition.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re

from .accounting import AccountingConflict, JournalTransaction, transaction_digest
from .exact_decimal import ExactDecimalError, canonical_decimal_text, exact_sum, parse_bounded_exact_decimal
from .perpetual_funding import DurablePerpetualFundingAuthority, PerpetualFundingConflict
from .persistence import JournalStore, payload_digest
from .provider_activity_accounting import DurableProviderEconomicBook, ProviderEconomicCut


_FUNDING_EVENTS_RAW = DurablePerpetualFundingAuthority.__dict__["_events"]
_FUNDING_BINDING_RAW = DurablePerpetualFundingAuthority.__dict__["_require_bound_financial_authority"]
_ECONOMIC_EVENTS_RAW = DurableProviderEconomicBook.__dict__["_events"]
_RESOLVE_ECONOMIC_CUT_RAW = DurableProviderEconomicBook.__dict__["resolve_historical_cut"]
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVIDENCE_KIND = "CANONICAL_PERPETUAL_FUNDING_CASHFLOW"
_BLOCKER = "registered_funding_cost_projection_unavailable"


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise PerpetualFundingConflict(f"{name} must be exact canonical non-empty text")
    return value


def _digest(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if _SHA256.fullmatch(text) is None:
        raise PerpetualFundingConflict(f"{name} must be an exact canonical sha256 digest")
    return text


def _positive(value: object, *, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise PerpetualFundingConflict(f"{name} must be an exact positive integer")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    try:
        result = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise PerpetualFundingConflict(f"{name} must be an exact bounded decimal") from error
    if not result.is_finite():
        raise PerpetualFundingConflict(f"{name} must be finite")
    return result


def _decimal_text(value: Decimal) -> str:
    try:
        return canonical_decimal_text(value)
    except ExactDecimalError as error:
        raise PerpetualFundingConflict(
            "funding cashflow exceeds exact-decimal resource authority"
        ) from error


def _assert_owner(owner: DurablePerpetualFundingAuthority) -> DurableProviderEconomicBook:
    if type(owner) is not DurablePerpetualFundingAuthority:
        raise TypeError("owner must be exact DurablePerpetualFundingAuthority")
    if DurablePerpetualFundingAuthority.__dict__.get("_events") is not _FUNDING_EVENTS_RAW:
        raise PerpetualFundingConflict("funding event reader changed after composition")
    if (
        DurablePerpetualFundingAuthority.__dict__.get("_require_bound_financial_authority")
        is not _FUNDING_BINDING_RAW
    ):
        raise PerpetualFundingConflict("funding durable binding changed after composition")
    state = object.__getattribute__(owner, "__dict__")
    if type(state) is not dict:
        raise PerpetualFundingConflict("funding authority state is not canonical")
    shadowed = tuple(
        name
        for name in ("_events", "_require_bound_financial_authority")
        if name in state
    )
    if shadowed:
        raise PerpetualFundingConflict(
            "funding authority shadows canonical methods: " + ", ".join(shadowed)
        )
    store = state.get("store")
    economic = state.get("economic_book")
    if type(store) is not JournalStore or type(economic) is not DurableProviderEconomicBook:
        raise PerpetualFundingConflict("funding owner graph is not canonical")
    if economic.store is not store:
        raise PerpetualFundingConflict("funding and economic authorities must share one JournalStore")
    _FUNDING_BINDING_RAW(owner)
    if DurableProviderEconomicBook.__dict__.get("_events") is not _ECONOMIC_EVENTS_RAW:
        raise PerpetualFundingConflict("provider economic event reader changed after composition")
    if (
        DurableProviderEconomicBook.__dict__.get("resolve_historical_cut")
        is not _RESOLVE_ECONOMIC_CUT_RAW
    ):
        raise PerpetualFundingConflict("provider economic cut resolver changed after composition")
    return economic


def _funding_event_identity(event: dict[str, object]) -> tuple[str, str, int, int]:
    payload = event.get("payload")
    if type(payload) is not dict:
        raise PerpetualFundingConflict("funding durable payload is not canonical")
    expected_hash = payload_digest(payload)
    if event.get("payload_hash") != expected_hash:
        raise PerpetualFundingConflict("funding durable payload hash is invalid")
    if event.get("event_type") != "PerpetualFundingApplied":
        raise PerpetualFundingConflict("funding durable event type is not canonical")
    return (
        _text(event.get("event_id"), name="funding event_id"),
        _digest(expected_hash, name="funding payload_hash"),
        _positive(event.get("aggregate_version"), name="funding aggregate_version"),
        _positive(event.get("journal_sequence"), name="funding journal_sequence"),
    )


def _economic_cut(
    economic: DurableProviderEconomicBook,
    visibility: int,
) -> ProviderEconomicCut:
    visible = [
        event
        for event in _ECONOMIC_EVENTS_RAW(economic)
        if _positive(event.get("journal_sequence"), name="economic journal_sequence") <= visibility
    ]
    if not visible:
        raise PerpetualFundingConflict("funding evidence lacks provider economics at visibility cut")
    version = _positive(
        visible[-1].get("aggregate_version"),
        name="economic aggregate_version",
    )
    try:
        cut = _RESOLVE_ECONOMIC_CUT_RAW(
            economic,
            version,
            visibility_journal_sequence=visibility,
        )
    except (AccountingConflict, TypeError, ValueError) as error:
        raise PerpetualFundingConflict(
            "funding provider economic cut cannot be reconstructed"
        ) from error
    if type(cut) is not ProviderEconomicCut:
        raise PerpetualFundingConflict("funding provider economic cut is not canonical")
    return cut


def _material(
    *,
    provider_id: str,
    account_id: str,
    environment: str,
    funding_aggregate_version: int,
    visibility_journal_sequence: int,
    funding_event_identities: tuple[tuple[str, str, int, int], ...],
    provider_economic_cut_digest: str,
    provider_economic_book_digest: str,
    net_cashflow_by_unit: tuple[tuple[str, Decimal], ...],
    contributing_transactions: tuple[tuple[str, str], ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "evidence_kind": _EVIDENCE_KIND,
        "terminal_cost_component": False,
        "cost_projection_ready": False,
        "blocking_reason": _BLOCKER,
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "funding_aggregate_version": funding_aggregate_version,
        "visibility_journal_sequence": visibility_journal_sequence,
        "funding_event_identities": [list(item) for item in funding_event_identities],
        "provider_economic_cut_digest": provider_economic_cut_digest,
        "provider_economic_book_digest": provider_economic_book_digest,
        "net_cashflow_by_unit": [
            [unit, _decimal_text(amount)] for unit, amount in net_cashflow_by_unit
        ],
        "contributing_transactions": [list(item) for item in contributing_transactions],
    }


def _hash(material: dict[str, object]) -> str:
    raw = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(raw).hexdigest()


@dataclass(frozen=True)
class ResolvedAblationFundingCashflowEvidence:
    evidence_kind: str
    terminal_cost_component: bool
    cost_projection_ready: bool
    blocking_reason: str
    provider_id: str
    account_id: str
    environment: str
    funding_aggregate_version: int
    visibility_journal_sequence: int
    funding_event_identities: tuple[tuple[str, str, int, int], ...]
    provider_economic_cut_digest: str
    provider_economic_book_digest: str
    net_cashflow_by_unit: tuple[tuple[str, Decimal], ...]
    contributing_transactions: tuple[tuple[str, str], ...]
    evidence_digest: str

    def __post_init__(self) -> None:
        if self.evidence_kind != _EVIDENCE_KIND:
            raise PerpetualFundingConflict("funding cashflow evidence kind is not canonical")
        if type(self.terminal_cost_component) is not bool or self.terminal_cost_component:
            raise PerpetualFundingConflict("signed funding cashflow is not terminal cost evidence")
        if type(self.cost_projection_ready) is not bool or self.cost_projection_ready:
            raise PerpetualFundingConflict("funding cashflow cannot self-author a cost projection")
        if self.blocking_reason != _BLOCKER:
            raise PerpetualFundingConflict("funding projection blocker is not canonical")
        for name in ("provider_id", "account_id", "environment"):
            _text(getattr(self, name), name=name)
        _positive(self.funding_aggregate_version, name="funding_aggregate_version")
        _positive(self.visibility_journal_sequence, name="visibility_journal_sequence")
        if type(self.funding_event_identities) is not tuple or not self.funding_event_identities:
            raise PerpetualFundingConflict("funding event identities must be a non-empty tuple")
        for item in self.funding_event_identities:
            if type(item) is not tuple or len(item) != 4:
                raise PerpetualFundingConflict("funding event identity is not canonical")
            _text(item[0], name="funding event_id")
            _digest(item[1], name="funding payload_hash")
            _positive(item[2], name="funding aggregate_version")
            _positive(item[3], name="funding journal_sequence")
        _digest(self.provider_economic_cut_digest, name="provider_economic_cut_digest")
        _digest(self.provider_economic_book_digest, name="provider_economic_book_digest")
        if type(self.net_cashflow_by_unit) is not tuple:
            raise PerpetualFundingConflict("net_cashflow_by_unit must be an exact tuple")
        units: list[str] = []
        for item in self.net_cashflow_by_unit:
            if type(item) is not tuple or len(item) != 2:
                raise PerpetualFundingConflict("funding cashflow entry is not canonical")
            units.append(_text(item[0], name="funding currency"))
            _decimal_text(item[1])
        if units != sorted(units) or len(units) != len(set(units)):
            raise PerpetualFundingConflict("funding currencies must use unique canonical order")
        if type(self.contributing_transactions) is not tuple:
            raise PerpetualFundingConflict("funding contributing transactions must be an exact tuple")
        ids: list[str] = []
        for item in self.contributing_transactions:
            if type(item) is not tuple or len(item) != 2:
                raise PerpetualFundingConflict("funding transaction identity is not canonical")
            ids.append(_text(item[0], name="funding transaction_id"))
            _digest(item[1], name="funding transaction_digest")
        if len(ids) != len(set(ids)):
            raise PerpetualFundingConflict("funding contributing transaction identities must be unique")
        _digest(self.evidence_digest, name="evidence_digest")
        ResolvedAblationFundingCashflowEvidence.verify_integrity(self)

    def verify_integrity(self) -> None:
        material = _material(
            provider_id=self.provider_id,
            account_id=self.account_id,
            environment=self.environment,
            funding_aggregate_version=self.funding_aggregate_version,
            visibility_journal_sequence=self.visibility_journal_sequence,
            funding_event_identities=self.funding_event_identities,
            provider_economic_cut_digest=self.provider_economic_cut_digest,
            provider_economic_book_digest=self.provider_economic_book_digest,
            net_cashflow_by_unit=self.net_cashflow_by_unit,
            contributing_transactions=self.contributing_transactions,
        )
        if self.evidence_digest != _hash(material):
            raise PerpetualFundingConflict(
                "funding cashflow evidence digest does not match canonical material"
            )


def resolve_ablation_funding_cashflow_evidence(
    owner: DurablePerpetualFundingAuthority,
    *,
    expected_aggregate_version: int,
    visibility_journal_sequence: int | None = None,
) -> ResolvedAblationFundingCashflowEvidence:
    """Resolve signed provider funding cashflow at one frozen global journal cut."""

    economic = _assert_owner(owner)
    expected_version = _positive(expected_aggregate_version, name="expected_aggregate_version")
    state = object.__getattribute__(owner, "__dict__")
    store = state["store"]
    current = JournalStore.current_journal_sequence(store)
    visibility = (
        current
        if visibility_journal_sequence is None
        else _positive(visibility_journal_sequence, name="visibility_journal_sequence")
    )
    if visibility > current:
        raise PerpetualFundingConflict("funding visibility cut is beyond durable journal")

    visible = [
        event
        for event in _FUNDING_EVENTS_RAW(owner)
        if _positive(event.get("journal_sequence"), name="funding journal_sequence") <= visibility
    ]
    if not visible:
        raise PerpetualFundingConflict("funding authority has no state at visibility cut")
    if visible[-1].get("aggregate_version") != expected_version:
        raise PerpetualFundingConflict("requested funding aggregate version is stale at visibility cut")
    if len(visible) != expected_version:
        raise PerpetualFundingConflict("funding visible aggregate prefix is not contiguous")
    event_identities = tuple(_funding_event_identity(event) for event in visible)

    cut = _economic_cut(economic, visibility)
    cut_index = dict(cut.transaction_digests)
    by_id = {transaction.transaction_id: transaction for transaction in economic.transactions}
    amounts: dict[str, list[Decimal]] = {}
    contributors: list[tuple[str, str]] = []
    seen: set[str] = set()

    for event in visible:
        payload = event["payload"]
        if (
            payload.get("provider_id") != economic.provider_id
            or payload.get("account_id") != economic.account_id
            or payload.get("environment") != economic.environment
        ):
            raise PerpetualFundingConflict("funding durable scope differs from economic authority")
        currency = _text(payload.get("currency"), name="funding currency")
        cashflow = _decimal(payload.get("cashflow"), name="funding cashflow")
        _digest(payload.get("provider_evidence_digest"), name="provider_evidence_digest")
        position_cut = payload.get("position_cut")
        if type(position_cut) is not dict:
            raise PerpetualFundingConflict("funding event lacks canonical frozen position cut")
        _digest(position_cut.get("digest"), name="position_cut digest")
        active_id = _text(payload.get("active_transaction_id"), name="active_transaction_id")
        active = by_id.get(active_id)
        if type(active) is not JournalTransaction:
            raise PerpetualFundingConflict("funding active transaction is absent from economic authority")
        active_digest = transaction_digest(active)
        if cut_index.get(active_id) != active_digest:
            raise PerpetualFundingConflict("funding active transaction is absent from frozen economic cut")
        active_cash = tuple(
            posting
            for posting in active.postings
            if posting.ledger_account == f"CASH:{currency}"
            and posting.asset_or_currency == currency
        )
        if len(active_cash) != 1 or active_cash[0].signed_amount != cashflow:
            raise PerpetualFundingConflict("funding event cashflow differs from canonical economic posting")

        transaction_ids = [active_id]
        reversal_raw = payload.get("reversal_transaction_id")
        if reversal_raw is not None:
            transaction_ids.insert(0, _text(reversal_raw, name="reversal_transaction_id"))
        for transaction_id in transaction_ids:
            if transaction_id in seen:
                continue
            transaction = by_id.get(transaction_id)
            if type(transaction) is not JournalTransaction:
                raise PerpetualFundingConflict("funding transaction is absent from economic authority")
            digest = transaction_digest(transaction)
            if cut_index.get(transaction_id) != digest:
                raise PerpetualFundingConflict("funding transaction is absent from frozen economic cut")
            cash_postings = tuple(
                posting
                for posting in transaction.postings
                if posting.ledger_account.startswith("CASH:")
            )
            if len(cash_postings) != 1:
                raise PerpetualFundingConflict("funding transaction lacks one canonical cash posting")
            posting = cash_postings[0]
            if posting.ledger_account != f"CASH:{posting.asset_or_currency}":
                raise PerpetualFundingConflict("funding cash account does not match value unit")
            amounts.setdefault(posting.asset_or_currency, []).append(posting.signed_amount)
            contributors.append((transaction_id, digest))
            seen.add(transaction_id)

    net: list[tuple[str, Decimal]] = []
    for unit in sorted(amounts):
        try:
            total = exact_sum(amounts[unit])
        except ExactDecimalError as error:
            raise PerpetualFundingConflict("funding net cashflow exceeds numeric resource authority") from error
        net.append((unit, total))

    material = _material(
        provider_id=economic.provider_id,
        account_id=economic.account_id,
        environment=economic.environment,
        funding_aggregate_version=expected_version,
        visibility_journal_sequence=visibility,
        funding_event_identities=event_identities,
        provider_economic_cut_digest=cut.cut_digest,
        provider_economic_book_digest=cut.resulting_book_digest,
        net_cashflow_by_unit=tuple(net),
        contributing_transactions=tuple(contributors),
    )
    return ResolvedAblationFundingCashflowEvidence(
        evidence_kind=_EVIDENCE_KIND,
        terminal_cost_component=False,
        cost_projection_ready=False,
        blocking_reason=_BLOCKER,
        provider_id=economic.provider_id,
        account_id=economic.account_id,
        environment=economic.environment,
        funding_aggregate_version=expected_version,
        visibility_journal_sequence=visibility,
        funding_event_identities=event_identities,
        provider_economic_cut_digest=cut.cut_digest,
        provider_economic_book_digest=cut.resulting_book_digest,
        net_cashflow_by_unit=tuple(net),
        contributing_transactions=tuple(contributors),
        evidence_digest=_hash(material),
    )


def reverify_ablation_funding_cashflow_evidence(
    owner: DurablePerpetualFundingAuthority,
    evidence: ResolvedAblationFundingCashflowEvidence,
) -> ResolvedAblationFundingCashflowEvidence:
    if type(evidence) is not ResolvedAblationFundingCashflowEvidence:
        raise TypeError("evidence must be exact ResolvedAblationFundingCashflowEvidence")
    ResolvedAblationFundingCashflowEvidence.verify_integrity(evidence)
    resolved = resolve_ablation_funding_cashflow_evidence(
        owner,
        expected_aggregate_version=evidence.funding_aggregate_version,
        visibility_journal_sequence=evidence.visibility_journal_sequence,
    )
    if resolved != evidence:
        raise PerpetualFundingConflict(
            "funding cashflow evidence does not match canonical owner replay"
        )
    return resolved
