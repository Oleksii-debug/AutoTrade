"""Exact settled/unsettled cash projection for AutoTrade economic evidence.

This module is provider-neutral and performs no networking or order authorization.
It models contractual cash obligations separately from spendable settled cash so
future proceeds cannot be silently reused before their settlement date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping

from .accounting import EconomicBook, JournalTransaction, ScopedEconomicBook, _canonical_equity_fill_terms
from .exact_decimal import (
    ExactDecimalError,
    canonical_decimal_text,
    exact_add,
    exact_multiply,
    exact_subtract,
    exact_sum,
    parse_bounded_exact_decimal,
)
from .persistence import payload_digest


class SettlementConflict(ValueError):
    """Raised when immutable settlement identity or lifecycle invariants conflict."""


def _decimal(value: Decimal | str | int, *, name: str) -> Decimal:
    if type(value) not in (Decimal, str, int):
        raise TypeError(f"{name} must use exact Decimal, string or integer input")
    try:
        return parse_bounded_exact_decimal(value)
    except ExactDecimalError as error:
        raise ValueError(f"{name} must be a bounded finite decimal") from error


def _text(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _utc(value: datetime, *, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class SettlementAccountScope:
    provider_id: str
    account_id: str
    environment: str
    provider_environment: str | None = None

    def __post_init__(self) -> None:
        provider = _text(self.provider_id, name="provider_id").upper()
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "account_id", _text(self.account_id, name="account_id"))
        environment = _text(self.environment, name="environment").upper()
        if environment not in {"REPLAY", "SIMULATION", "PAPER", "LIVE"}:
            raise ValueError("unsupported environment")
        provider_environment = (
            environment
            if self.provider_environment is None
            else _text(self.provider_environment, name="provider_environment").upper()
        )
        if provider == "BYBIT":
            if self.provider_environment is None:
                raise ValueError(
                    "BYBIT settlement scope requires explicit provider_environment"
                )
            if provider_environment not in {"MAINNET", "TESTNET", "DEMO"}:
                raise ValueError(
                    "BYBIT provider_environment must be MAINNET, TESTNET or DEMO"
                )
            if (
                environment == "LIVE" and provider_environment != "MAINNET"
            ) or (
                environment == "PAPER"
                and provider_environment not in {"TESTNET", "DEMO"}
            ):
                raise ValueError(
                    "BYBIT provider_environment does not match runtime environment"
                )
        elif provider_environment != environment:
            raise ValueError(
                "provider_environment must equal runtime environment until an exact "
                "provider-domain policy is qualified"
            )
        object.__setattr__(self, "environment", environment)
        object.__setattr__(self, "provider_environment", provider_environment)


@dataclass(frozen=True, slots=True)
class SettlementRuleBinding:
    """Versioned instrument/account/provider evidence for one settlement rule."""

    rule_id: str
    rule_version: str
    scope: SettlementAccountScope
    instrument_version: str
    settlement_currency: str
    effective_from: date
    effective_to: date | None
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "rule_id", _text(self.rule_id, name="rule_id"))
        object.__setattr__(
            self, "rule_version", _text(self.rule_version, name="rule_version")
        )
        if not isinstance(self.scope, SettlementAccountScope):
            raise TypeError("scope must be SettlementAccountScope")
        object.__setattr__(
            self,
            "instrument_version",
            _text(self.instrument_version, name="instrument_version"),
        )
        object.__setattr__(
            self,
            "settlement_currency",
            _text(self.settlement_currency, name="settlement_currency").upper(),
        )
        if type(self.effective_from) is not date:
            raise TypeError("effective_from must be a date value")
        if self.effective_to is not None:
            if type(self.effective_to) is not date:
                raise TypeError("effective_to must be a date value")
            if self.effective_to <= self.effective_from:
                raise ValueError("effective_to must be after effective_from")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("evidence_refs must be a non-empty tuple")
        refs = tuple(_text(item, name="evidence_ref") for item in self.evidence_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must be unique")
        object.__setattr__(self, "evidence_refs", refs)

    def applies_on(self, trade_date: date) -> bool:
        if type(trade_date) is not date:
            raise TypeError("trade_date must be a date value")
        return self.effective_from <= trade_date and (
            self.effective_to is None or trade_date < self.effective_to
        )

    @property
    def digest(self) -> str:
        material = {
            "schema_version": "1.0.0",
            "rule_id": self.rule_id,
            "rule_version": self.rule_version,
            "provider_id": self.scope.provider_id,
            "account_id": self.scope.account_id,
            "environment": self.scope.environment,
            "instrument_version": self.instrument_version,
            "settlement_currency": self.settlement_currency,
            "effective_from": self.effective_from.isoformat(),
            "effective_to": (
                None if self.effective_to is None else self.effective_to.isoformat()
            ),
            "evidence_refs": list(self.evidence_refs),
        }
        if self.scope.provider_environment != self.scope.environment:
            material["provider_environment"] = self.scope.provider_environment
        return payload_digest(material)


@dataclass(frozen=True, slots=True)
class SettlementObligation:
    obligation_id: str
    cause_event_id: str
    currency: str
    amount: Decimal
    trade_date: date
    settlement_date: date
    component_id: str = "PRIMARY"
    source_transaction_id: str | None = None
    rule_binding: SettlementRuleBinding | None = None

    def __post_init__(self) -> None:
        obligation_id = _text(self.obligation_id, name="obligation_id")
        cause_event_id = _text(self.cause_event_id, name="cause_event_id")
        component_id = _text(self.component_id, name="component_id").upper()
        currency = _text(self.currency, name="currency").upper()
        amount = _decimal(self.amount, name="amount")
        if amount == 0:
            raise ValueError("amount must be non-zero")
        if type(self.trade_date) is not date or type(self.settlement_date) is not date:
            raise TypeError("trade_date and settlement_date must be date values")
        if self.settlement_date < self.trade_date:
            raise ValueError("settlement_date cannot precede trade_date")
        object.__setattr__(self, "obligation_id", obligation_id)
        object.__setattr__(self, "cause_event_id", cause_event_id)
        object.__setattr__(self, "component_id", component_id)
        object.__setattr__(self, "currency", currency)
        object.__setattr__(self, "amount", amount)
        if self.source_transaction_id is not None:
            object.__setattr__(
                self,
                "source_transaction_id",
                _text(self.source_transaction_id, name="source_transaction_id"),
            )
        if self.rule_binding is not None:
            if not isinstance(self.rule_binding, SettlementRuleBinding):
                raise TypeError("rule_binding must be SettlementRuleBinding")
            if self.rule_binding.settlement_currency != currency:
                raise SettlementConflict(
                    "settlement rule currency does not match obligation currency"
                )
            if not self.rule_binding.applies_on(self.trade_date):
                raise SettlementConflict(
                    "settlement rule was not effective on the trade date"
                )


@dataclass(frozen=True, slots=True)
class SettlementEvidence:
    """Immutable evidence that a settlement fact became available locally."""

    obligation_id: str
    evidence_ref: str
    observed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "obligation_id",
            _text(self.obligation_id, name="evidence obligation_id"),
        )
        object.__setattr__(
            self,
            "evidence_ref",
            _text(self.evidence_ref, name="settlement_evidence_ref"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _utc(self.observed_at, name="evidence observed_at"),
        )


@dataclass(frozen=True, slots=True)
class SettlementCheckpoint:
    """Explicit restart boundary: cash plus the exact evidence prefix it includes."""

    checkpoint_id: str
    settled_cash: tuple[tuple[str, Decimal], ...]
    settled_obligation_evidence: tuple[SettlementEvidence, ...]

    @classmethod
    def create(
        cls,
        *,
        checkpoint_id: str,
        settled_cash: Mapping[str, Decimal | str | int],
        settled_obligation_evidence: Mapping[str, SettlementEvidence],
    ) -> "SettlementCheckpoint":
        cash: dict[str, Decimal] = {}
        for raw_currency, raw_amount in settled_cash.items():
            currency = _text(raw_currency, name="checkpoint currency").upper()
            if currency in cash:
                raise SettlementConflict(
                    "checkpoint contains duplicate normalized currency codes"
                )
            cash[currency] = _decimal(raw_amount, name="checkpoint settled_cash")

        evidence: dict[str, SettlementEvidence] = {}
        for raw_id, record in settled_obligation_evidence.items():
            obligation_id = _text(raw_id, name="checkpoint obligation_id")
            if not isinstance(record, SettlementEvidence):
                raise TypeError("checkpoint settlement evidence must be SettlementEvidence")
            if record.obligation_id != obligation_id:
                raise SettlementConflict(
                    "checkpoint evidence key does not match evidence obligation_id"
                )
            if obligation_id in evidence:
                raise SettlementConflict(
                    "checkpoint contains duplicate normalized obligation ids"
                )
            evidence[obligation_id] = record

        return cls(
            checkpoint_id=_text(checkpoint_id, name="checkpoint_id"),
            settled_cash=tuple(sorted(cash.items())),
            settled_obligation_evidence=tuple(evidence[k] for k in sorted(evidence)),
        )

    def cash_dict(self) -> dict[str, Decimal]:
        return dict(self.settled_cash)

    def evidence_dict(self) -> dict[str, SettlementEvidence]:
        return {record.obligation_id: record for record in self.settled_obligation_evidence}


@dataclass(frozen=True, slots=True)
class SettlementSnapshot:
    currency: str
    settled_cash: Decimal
    unsettled_receivable: Decimal
    unsettled_payable: Decimal

    @property
    def net_unsettled(self) -> Decimal:
        return exact_subtract(self.unsettled_receivable, self.unsettled_payable)

    @property
    def economic_cash(self) -> Decimal:
        return exact_add(self.settled_cash, self.net_unsettled)


@dataclass(frozen=True, slots=True)
class BuyingPowerEvidence:
    """Provider-granted credit kept separate from legal cash settlement."""

    evidence_id: str
    scope: SettlementAccountScope
    currency: str
    additional_credit: Decimal
    observed_at: datetime
    valid_until: datetime
    evidence_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.evidence_id) is not str:
            raise TypeError("evidence_id must be exact text")
        if type(self.scope) is not SettlementAccountScope:
            raise TypeError("scope must be exact SettlementAccountScope")
        if type(self.currency) is not str:
            raise TypeError("currency must be exact text")
        if type(self.observed_at) is not datetime or type(self.valid_until) is not datetime:
            raise TypeError("buying-power timestamps must be exact datetime values")
        if (
            type(self.evidence_refs) is not tuple
            or not self.evidence_refs
            or any(type(item) is not str for item in self.evidence_refs)
        ):
            raise TypeError("evidence_refs must be a non-empty exact-text tuple")

        object.__setattr__(
            self, "evidence_id", _text(self.evidence_id, name="evidence_id")
        )
        object.__setattr__(
            self, "currency", _text(self.currency, name="currency").upper()
        )
        credit = _decimal(self.additional_credit, name="additional_credit")
        if credit < 0:
            raise ValueError("additional_credit cannot be negative")
        object.__setattr__(self, "additional_credit", credit)
        observed = _utc(self.observed_at, name="observed_at")
        valid_until = _utc(self.valid_until, name="valid_until")
        if valid_until <= observed:
            raise ValueError("valid_until must be after observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "valid_until", valid_until)
        refs = tuple(_text(item, name="evidence_ref") for item in self.evidence_refs)
        if len(refs) != len(set(refs)):
            raise ValueError("evidence_refs must be unique")
        object.__setattr__(self, "evidence_refs", refs)

    @property
    def resource_key(self) -> str:
        """Canonical reservation resource for separately evidenced margin credit."""

        return f"MARGIN_CREDIT:{self.currency}"

    def resource_detail(self) -> dict[str, str]:
        """Serialize one typed provider buying-power observation for reconciliation."""

        try:
            credit = canonical_decimal_text(self.additional_credit)
        except ExactDecimalError as error:
            raise SettlementConflict(
                "buying-power credit exceeds exact rendering authority"
            ) from error
        detail = {
            "schema_version": "margin-buying-power-resource.v1",
            "resource_type": "MARGIN_BUYING_POWER",
            "credit_semantics": "ADDITIONAL_TO_SETTLED_CASH",
            "evidence_id": self.evidence_id,
            "provider_id": self.scope.provider_id,
            "account_id": self.scope.account_id,
            "environment": self.scope.environment,
            "provider_environment": self.scope.provider_environment,
            "currency": self.currency,
            "additional_credit": credit,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "valid_until": self.valid_until.isoformat().replace("+00:00", "Z"),
            "evidence_ref_count": str(len(self.evidence_refs)),
        }
        detail.update(
            {
                f"evidence_ref_{index}": reference
                for index, reference in enumerate(self.evidence_refs)
            }
        )
        return detail

    @classmethod
    def from_resource_detail(
        cls,
        detail: Mapping[str, object],
    ) -> "BuyingPowerEvidence":
        """Rebuild typed margin credit from the exact durable resource detail."""

        if type(detail) is not dict:
            raise TypeError("margin-credit resource detail must use an exact dict")
        if detail.get("schema_version") != "margin-buying-power-resource.v1":
            raise ValueError("margin-credit resource detail schema is unsupported")
        if detail.get("resource_type") != "MARGIN_BUYING_POWER":
            raise ValueError("margin-credit resource detail has invalid resource_type")
        if detail.get("credit_semantics") != "ADDITIONAL_TO_SETTLED_CASH":
            raise ValueError(
                "margin-credit evidence must be additional to settled cash"
            )

        raw_count = detail.get("evidence_ref_count")
        if type(raw_count) is not str or not raw_count.isdigit():
            raise ValueError("margin-credit evidence_ref_count is invalid")
        count = int(raw_count)
        if count < 1 or count > 64 or raw_count != str(count):
            raise ValueError("margin-credit evidence_ref_count is invalid")

        fixed = {
            "schema_version",
            "resource_type",
            "credit_semantics",
            "evidence_id",
            "provider_id",
            "account_id",
            "environment",
            "provider_environment",
            "currency",
            "additional_credit",
            "observed_at",
            "valid_until",
            "evidence_ref_count",
        }
        expected = fixed | {f"evidence_ref_{index}" for index in range(count)}
        if set(detail) != expected:
            raise ValueError("margin-credit resource detail fields are invalid")

        def exact_text(field: str) -> str:
            value = detail.get(field)
            if type(value) is not str or not value.strip():
                raise TypeError(f"margin-credit {field} must be exact text")
            return value.strip()

        def exact_instant(field: str) -> datetime:
            text = exact_text(field)
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError(
                    f"margin-credit {field} must be an ISO timestamp"
                ) from error
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError(
                    f"margin-credit {field} must include a timezone"
                )
            return parsed.astimezone(timezone.utc)

        scope = SettlementAccountScope(
            provider_id=exact_text("provider_id"),
            account_id=exact_text("account_id"),
            environment=exact_text("environment"),
            provider_environment=exact_text("provider_environment"),
        )
        return cls(
            evidence_id=exact_text("evidence_id"),
            scope=scope,
            currency=exact_text("currency"),
            additional_credit=exact_text("additional_credit"),
            observed_at=exact_instant("observed_at"),
            valid_until=exact_instant("valid_until"),
            evidence_refs=tuple(
                exact_text(f"evidence_ref_{index}") for index in range(count)
            ),
        )


@dataclass(frozen=True, slots=True)
class CapitalAvailabilityProjection:
    scope: SettlementAccountScope
    currency: str
    as_of: datetime
    settled_cash: Decimal
    unsettled_receivable: Decimal
    unsettled_payable: Decimal
    available_cash: Decimal
    additional_buying_power: Decimal
    available_capital: Decimal
    overdue_obligation_ids: tuple[str, ...]
    blocks_new_risk: bool

    def reservation_resources(self) -> Mapping[str, Decimal]:
        return {
            f"CASH:{self.currency}": max(self.available_cash, Decimal("0")),
            f"MARGIN_CREDIT:{self.currency}": max(
                self.additional_buying_power, Decimal("0")
            ),
        }


class SettlementBook:
    """Immutable obligations plus exact spendable-cash projection.

    Positive obligations are receivables, negative obligations are payables.
    They remain outside settled cash until settled explicitly on or after the
    contractual settlement date. Identical retries are idempotent.
    """

    def __init__(
        self,
        *,
        settled_cash: dict[str, Decimal | str | int] | None = None,
        obligations: Iterable[SettlementObligation] = (),
        settled_obligation_evidence: Mapping[str, SettlementEvidence] | None = None,
    ) -> None:
        self._settled_cash: dict[str, Decimal] = {}
        for currency, amount in (settled_cash or {}).items():
            unit = _text(currency, name="currency").upper()
            if unit in self._settled_cash:
                raise SettlementConflict(
                    "settled_cash contains duplicate normalized currency codes"
                )
            self._settled_cash[unit] = _decimal(amount, name="settled_cash")
        self._obligations: dict[str, SettlementObligation] = {}
        self._by_cause_component: dict[tuple[str, str], SettlementObligation] = {}
        self._settled_ids: set[str] = set()
        self._settlement_evidence: dict[str, SettlementEvidence] = {}
        for obligation in obligations:
            self.add(obligation)
        for obligation_id, record in (settled_obligation_evidence or {}).items():
            key = _text(obligation_id, name="settled_obligation_id")
            if not isinstance(record, SettlementEvidence):
                raise TypeError("settled obligation evidence must be SettlementEvidence")
            if record.obligation_id != key:
                raise SettlementConflict(
                    "settlement evidence key does not match evidence obligation_id"
                )
            if key not in self._obligations:
                raise SettlementConflict(
                    "settled obligation evidence cannot reference an unknown obligation"
                )
            if key in self._settlement_evidence:
                raise SettlementConflict(
                    "settled obligation evidence contains duplicate normalized obligation ids"
                )
            obligation = self._obligations[key]
            if record.observed_at.date() < obligation.settlement_date:
                raise SettlementConflict(
                    "settlement evidence predates contractual settlement date"
                )
            self._settled_ids.add(key)
            self._settlement_evidence[key] = record

    @property
    def obligations(self) -> tuple[SettlementObligation, ...]:
        return tuple(self._obligations.values())

    @property
    def settled_obligation_evidence(self) -> dict[str, SettlementEvidence]:
        """Copy of durable settlement evidence bound to settled obligations."""

        return dict(self._settlement_evidence)

    def checkpoint(self, checkpoint_id: str) -> SettlementCheckpoint:
        """Capture the exact restart boundary without re-applying its history."""

        return SettlementCheckpoint.create(
            checkpoint_id=checkpoint_id,
            settled_cash=self._settled_cash,
            settled_obligation_evidence=self._settlement_evidence,
        )

    @classmethod
    def from_history(
        cls,
        *,
        checkpoint: SettlementCheckpoint,
        obligations: Iterable[SettlementObligation] = (),
        settled_obligation_evidence: Mapping[str, SettlementEvidence] | None = None,
    ) -> "SettlementBook":
        """Restore checkpoint state and replay only the strict evidence suffix.

        The checkpoint carries the exact settled-history prefix already reflected
        in its cash. Full retained history is accepted only when that prefix
        matches exactly; this prevents current cash + full history from being
        applied twice after restart.
        """

        if not isinstance(checkpoint, SettlementCheckpoint):
            raise TypeError("checkpoint must be SettlementCheckpoint")

        obligations_tuple = tuple(obligations)
        validation = cls(
            settled_cash=checkpoint.cash_dict(),
            obligations=obligations_tuple,
            settled_obligation_evidence=checkpoint.evidence_dict(),
        )
        normalized: dict[str, SettlementEvidence] = {}
        for raw_id, record in (settled_obligation_evidence or {}).items():
            obligation_id = _text(raw_id, name="settled_obligation_id")
            if not isinstance(record, SettlementEvidence):
                raise TypeError("settled obligation evidence must be SettlementEvidence")
            if record.obligation_id != obligation_id:
                raise SettlementConflict(
                    "settlement evidence key does not match evidence obligation_id"
                )
            if obligation_id in normalized:
                raise SettlementConflict(
                    "settled obligation evidence contains duplicate normalized obligation ids"
                )
            if obligation_id not in validation._obligations:
                raise SettlementConflict(
                    "settled obligation evidence cannot reference an unknown obligation"
                )
            normalized[obligation_id] = record

        ordered_ids = sorted(
            normalized,
            key=lambda key: (
                validation._obligations[key].settlement_date,
                key,
            ),
        )
        checkpoint_evidence = checkpoint.evidence_dict()
        prefix_ids = ordered_ids[: len(checkpoint_evidence)]
        if set(prefix_ids) != set(checkpoint_evidence):
            raise SettlementConflict(
                "checkpoint settled history is not the exact retained-history prefix"
            )
        for obligation_id in prefix_ids:
            if normalized[obligation_id] != checkpoint_evidence[obligation_id]:
                raise SettlementConflict(
                    "checkpoint settlement evidence conflicts with retained history"
                )

        book = validation
        for obligation_id in ordered_ids[len(prefix_ids) :]:
            obligation = book._obligations[obligation_id]
            record = normalized[obligation_id]
            book.settle(
                obligation_id,
                as_of=max(obligation.settlement_date, record.observed_at.date()),
                settlement_evidence=record,
            )
        return book

    def add(self, obligation: SettlementObligation) -> bool:
        if not isinstance(obligation, SettlementObligation):
            raise TypeError("obligation must be SettlementObligation")
        existing = self._obligations.get(obligation.obligation_id)
        if existing is not None:
            if existing != obligation:
                raise SettlementConflict(
                    "obligation_id already exists with different economic content"
                )
            return False
        cause_key = (obligation.cause_event_id, obligation.component_id)
        cause_existing = self._by_cause_component.get(cause_key)
        if cause_existing is not None:
            raise SettlementConflict(
                "cause_event_id and component_id were already represented by a different settlement obligation"
            )
        self._obligations[obligation.obligation_id] = obligation
        self._by_cause_component[cause_key] = obligation
        return True

    def is_settled(self, obligation_id: str) -> bool:
        return _text(obligation_id, name="obligation_id") in self._settled_ids

    def settle(
        self,
        obligation_id: str,
        *,
        as_of: date,
        settlement_evidence: SettlementEvidence,
    ) -> bool:
        key = _text(obligation_id, name="obligation_id")
        if type(as_of) is not date:
            raise TypeError("as_of must be a date value")
        if not isinstance(settlement_evidence, SettlementEvidence):
            raise TypeError("settlement_evidence must be SettlementEvidence")
        if settlement_evidence.obligation_id != key:
            raise SettlementConflict(
                "settlement evidence obligation_id does not match requested obligation"
            )
        obligation = self._obligations.get(key)
        if obligation is None:
            raise SettlementConflict("Cannot settle an unknown obligation")
        if key in self._settled_ids:
            if self._settlement_evidence[key] != settlement_evidence:
                raise SettlementConflict(
                    "settlement retry used different settlement evidence"
                )
            return False
        if as_of < obligation.settlement_date:
            raise SettlementConflict("Cannot settle before contractual settlement date")
        if settlement_evidence.observed_at.date() < obligation.settlement_date:
            raise SettlementConflict(
                "settlement evidence predates contractual settlement date"
            )
        if settlement_evidence.observed_at.date() > as_of:
            raise SettlementConflict(
                "settlement evidence was not yet available as of projection date"
            )
        current = self._settled_cash.get(obligation.currency, Decimal("0"))
        self._settled_cash[obligation.currency] = exact_add(
            current,
            obligation.amount,
        )
        self._settled_ids.add(key)
        self._settlement_evidence[key] = settlement_evidence
        return True

    def settle_due(
        self,
        *,
        as_of: date,
        settlement_evidence: Mapping[str, SettlementEvidence],
    ) -> tuple[str, ...]:
        """Settle only due obligations whose evidence was already observed by as_of."""

        if type(as_of) is not date:
            raise TypeError("as_of must be a date value")
        if not isinstance(settlement_evidence, Mapping):
            raise TypeError("settlement_evidence must be a mapping")
        normalized_evidence: dict[str, SettlementEvidence] = {}
        for raw_id, record in settlement_evidence.items():
            obligation_id = _text(raw_id, name="settlement_evidence obligation_id")
            if not isinstance(record, SettlementEvidence):
                raise TypeError("settlement evidence values must be SettlementEvidence")
            if record.obligation_id != obligation_id:
                raise SettlementConflict(
                    "settlement evidence key does not match evidence obligation_id"
                )
            if obligation_id not in self._obligations:
                raise SettlementConflict(
                    "settlement evidence references an unknown obligation"
                )
            if obligation_id in normalized_evidence:
                raise SettlementConflict(
                    "settlement evidence contains duplicate normalized obligation ids"
                )
            normalized_evidence[obligation_id] = record

        settled: list[str] = []
        for obligation in sorted(
            self._obligations.values(),
            key=lambda item: (item.settlement_date, item.obligation_id),
        ):
            record = normalized_evidence.get(obligation.obligation_id)
            if (
                obligation.obligation_id not in self._settled_ids
                and obligation.settlement_date <= as_of
                and record is not None
                and record.observed_at.date() <= as_of
            ):
                self.settle(
                    obligation.obligation_id,
                    as_of=as_of,
                    settlement_evidence=record,
                )
                settled.append(obligation.obligation_id)
        return tuple(settled)

    def snapshot(self, currency: str) -> SettlementSnapshot:
        unit = _text(currency, name="currency").upper()
        receivables: list[Decimal] = []
        payables: list[Decimal] = []
        for obligation in self._obligations.values():
            if obligation.currency != unit or obligation.obligation_id in self._settled_ids:
                continue
            if obligation.amount > 0:
                receivables.append(obligation.amount)
            else:
                payables.append(exact_subtract(Decimal("0"), obligation.amount))
        receivable = exact_sum(receivables)
        payable = exact_sum(payables)
        return SettlementSnapshot(
            currency=unit,
            settled_cash=self._settled_cash.get(unit, Decimal("0")),
            unsettled_receivable=receivable,
            unsettled_payable=payable,
        )

    def available_to_spend(self, currency: str, *, reserve: Decimal | str | int = 0) -> Decimal:
        snapshot = self.snapshot(currency)
        locked = _decimal(reserve, name="reserve")
        if locked < 0:
            raise ValueError("reserve cannot be negative")
        # Contractual payables consume spendable cash as soon as the economic
        # obligation exists. Receivables remain unavailable until settlement.
        # reserve is an additional caller-owned hold and must not duplicate
        # the same settlement obligation.
        available = exact_subtract(
            exact_subtract(snapshot.settled_cash, snapshot.unsettled_payable),
            locked,
        )
        return available if available > 0 else Decimal("0")

    @classmethod
    def from_economic_book(
        cls,
        *,
        economic_book: EconomicBook,
        obligations: Iterable[SettlementObligation],
        settled_obligation_evidence: Mapping[str, SettlementEvidence] | None = None,
    ) -> "SettlementBook":
        """Rebuild settled/unsettled cash from canonical economic transactions.

        Generic EconomicBook cash is trade-date economic cash.  This constructor
        removes every active bound settlement obligation to recover the settled
        opening balance, then reapplies only immutable provider settlement
        evidence.  Reversed/busted source transactions are excluded, so their
        old settlement facts cannot release capital twice.
        """

        owner_scope = None
        if type(economic_book) is EconomicBook:
            canonical_book = economic_book
        elif type(economic_book) is ScopedEconomicBook:
            from .accounting import _require_scoped_economic_book_owner, AccountingConflict
            try:
                environment, account_id, canonical_book = _require_scoped_economic_book_owner(economic_book)
            except AccountingConflict as error:
                raise SettlementConflict(str(error)) from error
            owner_scope = (None, account_id, environment)
        else:
            # The durable financial book has its own original provider/account/
            # store generation seal. Validate it before obtaining the projection.
            from .provider_activity_accounting import (
                DurableProviderEconomicBook, _require_durable_provider_economic_book_authority,
            )
            if type(economic_book) is not DurableProviderEconomicBook:
                raise TypeError("economic_book must be an exact canonical economic authority")
            authority = _require_durable_provider_economic_book_authority(economic_book)
            owner_scope = (authority.provider_id, authority.account_id, authority.environment)
            canonical_book = object.__getattribute__(economic_book, "_book")
            if type(canonical_book) is not EconomicBook:
                raise TypeError("durable economic authority must own an exact EconomicBook")

        # Take one canonical class-owned transaction cut and use it throughout
        # the projection.  This keeps caller-polymorphic properties/methods and
        # per-instance method shadows outside financial authority.
        transactions = EconomicBook.transactions.__get__(
            canonical_book,
            EconomicBook,
        )

        items = tuple(obligations)
        if owner_scope is not None:
            provider, account, environment = owner_scope
            for item in items:
                scope = None if item.rule_binding is None else item.rule_binding.scope
                if scope is None or (
                    scope.account_id != account or scope.environment != environment
                    or (provider is not None and scope.provider_id != provider)
                ):
                    raise SettlementConflict("scoped economic book differs from settlement scope")
        by_transaction = {
            transaction.transaction_id: transaction
            for transaction in transactions
        }
        reversed_ids = {
            transaction.reverses_transaction_id
            for transaction in transactions
            if transaction.reverses_transaction_id is not None
        }

        all_ids = {item.obligation_id for item in items}
        evidence = dict(settled_obligation_evidence or {})
        unknown_evidence = set(evidence) - all_ids
        if unknown_evidence:
            raise SettlementConflict(
                "settlement evidence references an unknown obligation"
            )

        bound_source_currencies = {
            (item.source_transaction_id, item.currency)
            for item in items
            if item.source_transaction_id is not None
        }
        settlement_bound_sources = {
            source_transaction_id
            for source_transaction_id, _currency in bound_source_currencies
        }
        for transaction in transactions:
            corrected_id = transaction.corrects_transaction_id
            if (
                corrected_id is None
                or transaction.transaction_id in reversed_ids
                or corrected_id not in settlement_bound_sources
            ):
                continue
            replacement_cash: dict[str, Decimal] = {}
            for posting in transaction.postings:
                if posting.ledger_account == f"CASH:{posting.asset_or_currency}":
                    replacement_cash[posting.asset_or_currency] = exact_add(
                        replacement_cash.get(
                            posting.asset_or_currency,
                            Decimal("0"),
                        ),
                        posting.signed_amount,
                    )
            for currency, amount in replacement_cash.items():
                if (
                    amount != 0
                    and (transaction.transaction_id, currency)
                    not in bound_source_currencies
                ):
                    raise SettlementConflict(
                        "active correction replacement cash leg lacks settlement obligation"
                    )

        active: list[SettlementObligation] = []
        active_source_currency: set[tuple[str, str]] = set()
        currencies: set[str] = set()
        for transaction in transactions:
            for posting in transaction.postings:
                if (
                    posting.ledger_account.startswith("CASH:")
                    and posting.ledger_account == f"CASH:{posting.asset_or_currency}"
                ):
                    currencies.add(posting.asset_or_currency)

        for obligation in items:
            if obligation.source_transaction_id is None or obligation.rule_binding is None:
                raise SettlementConflict(
                    "economic-book settlement requires source transaction and rule binding"
                )
            transaction = by_transaction.get(obligation.source_transaction_id)
            if transaction is None:
                raise SettlementConflict(
                    "settlement obligation references an unknown economic transaction"
                )
            if transaction.cause_event_id != obligation.cause_event_id:
                raise SettlementConflict(
                    "settlement obligation cause does not match source transaction"
                )
            currencies.add(obligation.currency)
            if obligation.source_transaction_id in reversed_ids:
                continue
            source_currency = (
                obligation.source_transaction_id,
                obligation.currency,
            )
            if source_currency in active_source_currency:
                raise SettlementConflict(
                    "economic transaction/currency has multiple full settlement obligations"
                )
            cash_effect = exact_sum(
                posting.signed_amount
                for posting in transaction.postings
                if posting.ledger_account == f"CASH:{obligation.currency}"
                and posting.asset_or_currency == obligation.currency
            )
            if cash_effect != obligation.amount:
                raise SettlementConflict(
                    "settlement obligation amount does not match source economic cash effect"
                )
            active.append(obligation)
            active_source_currency.add(source_currency)

        # Never infer active trading cash as already-settled opening capital.
        # Explicit external cash flows are intentionally outside this fence;
        # equity/FX trading legs require source-transaction settlement coverage
        # before their cash can participate in a settlement projection.
        for transaction in transactions:
            if (
                transaction.transaction_id in reversed_ids
                or transaction.reverses_transaction_id is not None
            ):
                continue
            settlement_relevant = any(
                posting.ledger_account.startswith("POSITION:")
                or posting.ledger_account.startswith("FX_CLEARING:")
                for posting in transaction.postings
            )
            if not settlement_relevant:
                continue
            transaction_cash: dict[str, Decimal] = {}
            for posting in transaction.postings:
                currency = posting.asset_or_currency
                if posting.ledger_account == f"CASH:{currency}":
                    transaction_cash[currency] = exact_add(
                        transaction_cash.get(currency, Decimal("0")),
                        posting.signed_amount,
                    )
            for currency, amount in transaction_cash.items():
                if (
                    amount != 0
                    and (transaction.transaction_id, currency)
                    not in active_source_currency
                ):
                    raise SettlementConflict(
                        "active trading cash leg lacks settlement obligation"
                    )

        active_ids = {item.obligation_id for item in active}
        active_evidence = {
            obligation_id: record
            for obligation_id, record in evidence.items()
            if obligation_id in active_ids
        }
        opening_cash = {
            currency: exact_subtract(
                exact_sum(
                    posting.signed_amount
                    for transaction in transactions
                    for posting in transaction.postings
                    if posting.ledger_account == f"CASH:{currency}"
                    and posting.asset_or_currency == currency
                ),
                exact_sum(
                    item.amount
                    for item in active
                    if item.currency == currency
                ),
            )
            for currency in currencies
        }
        return cls.from_history(
            checkpoint=SettlementCheckpoint.create(
                checkpoint_id="economic-book-settlement-opening",
                settled_cash=opening_cash,
                settled_obligation_evidence={},
            ),
            obligations=active,
            settled_obligation_evidence=active_evidence,
        )

    def available_capital(
        self,
        *,
        scope: SettlementAccountScope,
        currency: str,
        as_of: datetime,
        buying_power_evidence: BuyingPowerEvidence | None = None,
        require_buying_power_evidence: bool = False,
    ) -> CapitalAvailabilityProjection:
        """Project spendable capital without treating receivables as cash."""

        if type(scope) is not SettlementAccountScope:
            raise TypeError("scope must be exact SettlementAccountScope")
        if type(currency) is not str:
            raise TypeError("currency must be exact text")
        if type(as_of) is not datetime:
            raise TypeError("as_of must be an exact datetime")
        unit = _text(currency, name="currency").upper()
        point = _utc(as_of, name="as_of")
        if type(require_buying_power_evidence) is not bool:
            raise TypeError("require_buying_power_evidence must be boolean")

        overdue: list[str] = []
        for obligation in self._obligations.values():
            if obligation.currency != unit:
                continue
            if obligation.source_transaction_id is None or obligation.rule_binding is None:
                raise SettlementConflict(
                    "available capital requires source-bound settlement obligations"
                )
            if obligation.rule_binding.scope != scope:
                raise SettlementConflict(
                    "settlement obligation account/provider scope differs from capital scope"
                )
            if (
                obligation.obligation_id not in self._settled_ids
                and point.date() > obligation.settlement_date
            ):
                overdue.append(obligation.obligation_id)

        additional_credit = Decimal("0")
        credit_unknown = False
        if buying_power_evidence is not None:
            if type(buying_power_evidence) is not BuyingPowerEvidence:
                raise TypeError(
                    "buying_power_evidence must be exact BuyingPowerEvidence"
                )
            if (
                buying_power_evidence.scope != scope
                or buying_power_evidence.currency != unit
            ):
                raise SettlementConflict(
                    "buying-power evidence scope/currency mismatch"
                )
            if (
                buying_power_evidence.observed_at
                <= point
                < buying_power_evidence.valid_until
            ):
                additional_credit = buying_power_evidence.additional_credit
            elif require_buying_power_evidence:
                credit_unknown = True
        elif require_buying_power_evidence:
            credit_unknown = True

        snapshot = self.snapshot(unit)
        available_cash = self.available_to_spend(unit)
        blocks = bool(overdue) or credit_unknown
        return CapitalAvailabilityProjection(
            scope=scope,
            currency=unit,
            as_of=point,
            settled_cash=snapshot.settled_cash,
            unsettled_receivable=snapshot.unsettled_receivable,
            unsettled_payable=snapshot.unsettled_payable,
            available_cash=available_cash,
            additional_buying_power=additional_credit,
            available_capital=exact_add(available_cash, additional_credit),
            overdue_obligation_ids=tuple(sorted(overdue)),
            blocks_new_risk=blocks,
        )


def cash_settlement_obligation_from_transaction(
    transaction: JournalTransaction,
    *,
    obligation_id: str,
    currency: str,
    trade_date: date,
    settlement_date: date,
    component_id: str,
    instrument_version: str,
    rule_binding: SettlementRuleBinding,
) -> SettlementObligation:
    """Bind one exact cash-currency leg of any economic transaction to settlement.

    This is the shared path for FX, derivative settlement, corporate cash
    actions and other journaled economic events.  It does not infer a global
    settlement cycle; callers must supply versioned rule evidence and the
    contractual settlement date.
    """

    if not isinstance(transaction, JournalTransaction):
        raise TypeError("transaction must be a JournalTransaction")
    if type(trade_date) is not date or type(settlement_date) is not date:
        raise TypeError("trade_date and settlement_date must be date values")
    if not isinstance(rule_binding, SettlementRuleBinding):
        raise TypeError("rule_binding must be SettlementRuleBinding")
    unit = _text(currency, name="currency").upper()
    bound_instrument = _text(
        instrument_version, name="instrument_version"
    )
    if rule_binding.instrument_version != bound_instrument:
        raise SettlementConflict(
            "settlement rule instrument version does not match economic source"
        )
    if rule_binding.settlement_currency != unit:
        raise SettlementConflict(
            "settlement rule currency does not match economic cash leg"
        )
    if not rule_binding.applies_on(trade_date):
        raise SettlementConflict(
            "settlement rule was not effective on economic event date"
        )
    amount = exact_sum(
        posting.signed_amount
        for posting in transaction.postings
        if posting.ledger_account == f"CASH:{unit}"
        and posting.asset_or_currency == unit
    )
    if amount == 0:
        raise SettlementConflict(
            "source transaction has no cash effect in settlement currency"
        )
    return SettlementObligation(
        obligation_id=_text(obligation_id, name="obligation_id"),
        cause_event_id=transaction.cause_event_id,
        currency=unit,
        amount=amount,
        trade_date=trade_date,
        settlement_date=settlement_date,
        component_id=_text(component_id, name="component_id").upper(),
        source_transaction_id=transaction.transaction_id,
        rule_binding=rule_binding,
    )


def equity_cash_obligation_from_transaction(
    transaction: JournalTransaction,
    *,
    obligation_id: str,
    instrument: str,
    settlement_currency: str,
    settlement_date: date,
    rule_binding: SettlementRuleBinding,
) -> SettlementObligation:
    """Create exact settlement obligation from canonical booked fill economics."""

    if not isinstance(transaction, JournalTransaction):
        raise TypeError("transaction must be a JournalTransaction")
    if transaction.economic_effective_at is None:
        raise SettlementConflict(
            "settlement-bound fill requires economic_effective_at"
        )
    terms = _canonical_equity_fill_terms(
        transaction,
        instrument=instrument,
        settlement_currency=settlement_currency,
    )
    if terms is None:
        raise SettlementConflict(
            "settlement obligation requires canonical equity-fill postings"
        )
    unit = _text(settlement_currency, name="settlement_currency").upper()
    try:
        trade_instant = datetime.fromisoformat(
            transaction.economic_effective_at.replace("Z", "+00:00")
        )
    except ValueError as error:
        raise SettlementConflict(
            "source transaction economic_effective_at is invalid"
        ) from error
    if trade_instant.tzinfo is None or trade_instant.utcoffset() is None:
        raise SettlementConflict(
            "source transaction economic_effective_at must be timezone-aware"
        )
    trade_date = trade_instant.astimezone(timezone.utc).date()
    if not isinstance(rule_binding, SettlementRuleBinding):
        raise TypeError("rule_binding must be SettlementRuleBinding")
    if rule_binding.settlement_currency != unit:
        raise SettlementConflict(
            "settlement rule currency does not match source fill"
        )
    if not rule_binding.applies_on(trade_date):
        raise SettlementConflict(
            "settlement rule was not effective on source fill date"
        )

    return cash_settlement_obligation_from_transaction(
        transaction,
        obligation_id=obligation_id,
        currency=unit,
        trade_date=trade_date,
        settlement_date=settlement_date,
        component_id="PRINCIPAL_AND_SAME_CURRENCY_FEE",
        instrument_version=instrument,
        rule_binding=rule_binding,
    )


def equity_cash_obligation(
    *,
    obligation_id: str,
    cause_event_id: str,
    settlement_currency: str,
    side: str,
    quantity: Decimal | str | int,
    price: Decimal | str | int,
    fee: Decimal | str | int = 0,
    trade_date: date,
    settlement_date: date,
) -> SettlementObligation:
    unit = _text(settlement_currency, name="settlement_currency").upper()
    normalized_side = _text(side, name="side").upper()
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    qty = _decimal(quantity, name="quantity")
    px = _decimal(price, name="price")
    fee_amount = _decimal(fee, name="fee")
    if qty <= 0 or px <= 0 or fee_amount < 0:
        raise ValueError("quantity and price must be positive and fee non-negative")
    gross = exact_multiply(qty, px)
    amount = (
        exact_subtract(
            Decimal("0"),
            exact_add(gross, fee_amount),
        )
        if normalized_side == "BUY"
        else exact_subtract(gross, fee_amount)
    )
    if amount == 0:
        raise ValueError("net settlement amount cannot be zero")
    return SettlementObligation(
        obligation_id=_text(obligation_id, name="obligation_id"),
        cause_event_id=_text(cause_event_id, name="cause_event_id"),
        currency=unit,
        amount=amount,
        trade_date=trade_date,
        settlement_date=settlement_date,
        component_id="PRINCIPAL_AND_SAME_CURRENCY_FEE",
    )
