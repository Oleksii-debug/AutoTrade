"""Provider-origin funding-income facts from qualified Bybit transaction logs.

This module is deliberately *not* a funding booking authority.  It projects one
narrow independently authoritative fact from the shared WP-18 provider-origin
boundary: provider-reported funding cash movement and provider transaction
identity.  Funding rate, mark/index valuation, canonical position/cut and
instrument convention remain separate authorities and must be composed by
WP-29 before PAPER/LIVE accounting can mutate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import re
from typing import Mapping

from .exact_decimal import ExactDecimalError, parse_bounded_exact_decimal
from .persistence import canonical_json
from .provider_core import ProviderResponseObservation, Surface
from .provider_origin import (
    ProviderOriginError,
    ProviderOriginObservation,
    require_provider_origin_response_binding_authority,
)
from .provider_route_reads import ProviderRouteReadError


class ProviderFundingIncomeError(ValueError):
    """Raised when provider-origin funding-income evidence is not exact."""


_BYBIT_TRANSACTION_LOG_ENDPOINT = "/v5/account/transaction-log"
_BYBIT_FUNDING_PARSER_IDENTITY = "BYBIT_V5_TRANSACTION_LOG_FUNDING_V1"
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ORIGIN_RE = re.compile(r"^provider-origin:sha256:[0-9a-f]{64}$")
_QUALIFIED_RE = re.compile(r"^qualified-provider-read:sha256:[0-9a-f]{64}$")


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderFundingIncomeError(f"{name} must be exact non-empty text")
    return value


def _decimal(value: object, *, name: str) -> Decimal:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderFundingIncomeError(f"{name} must be exact decimal text")
    try:
        return parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise ProviderFundingIncomeError(f"{name} must be bounded exact decimal text") from error


def _transaction_time(value: object) -> datetime:
    text = _text(value, name="transactionTime")
    if not text.isascii() or not text.isdigit():
        raise ProviderFundingIncomeError("transactionTime must be exact epoch-millisecond text")
    millis = int(text)
    if millis <= 0:
        raise ProviderFundingIncomeError("transactionTime must be positive")
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ProviderFundingIncomeError("transactionTime is outside supported UTC range") from error


@dataclass(frozen=True, slots=True)
class ProviderFundingIncomeObservation:
    """One provider-reported funding cash movement, retaining exact origin/Q."""

    provider_id: str
    account_id: str
    runtime_environment: str
    provider_environment: str
    instrument_id: str
    product_category: str
    settlement_currency: str
    provider_transaction_id: str
    occurred_at: datetime
    side: str
    position_size: Decimal
    funding_amount: Decimal
    origin_ref: str
    qualified_evidence_ref: str
    response_sha256: str
    qualification_id: str
    qualified_query_digest: str
    qualified_route_rule_digest: str
    parser_identity: str
    origin_journal_sequence: int
    evidence_ref: str

    def __post_init__(self) -> None:
        if self.provider_id != "BYBIT":
            raise ProviderFundingIncomeError("funding income observation requires BYBIT")
        for name in (
            "account_id",
            "runtime_environment",
            "provider_environment",
            "instrument_id",
            "product_category",
            "settlement_currency",
            "provider_transaction_id",
            "side",
            "qualification_id",
            "parser_identity",
        ):
            _text(getattr(self, name), name=name)
        if self.runtime_environment not in {"PAPER", "LIVE"}:
            raise ProviderFundingIncomeError("runtime environment must be PAPER or LIVE")
        if self.provider_environment not in {"TESTNET", "DEMO", "MAINNET"}:
            raise ProviderFundingIncomeError("provider environment is not canonical Bybit")
        if self.product_category not in {"linear", "inverse"}:
            raise ProviderFundingIncomeError("funding income requires linear or inverse category")
        if self.side not in {"Buy", "Sell", "None"}:
            raise ProviderFundingIncomeError("funding income side is not canonical Bybit")
        if type(self.occurred_at) is not datetime or self.occurred_at.tzinfo is None:
            raise ProviderFundingIncomeError("funding income occurrence must be timezone-aware")
        if type(self.position_size) is not Decimal or type(self.funding_amount) is not Decimal:
            raise ProviderFundingIncomeError("funding economics must use exact Decimal values")
        if _ORIGIN_RE.fullmatch(self.origin_ref) is None:
            raise ProviderFundingIncomeError("origin_ref is not canonical")
        if _QUALIFIED_RE.fullmatch(self.qualified_evidence_ref) is None:
            raise ProviderFundingIncomeError("qualified_evidence_ref is not canonical")
        for name in ("response_sha256", "qualified_query_digest", "qualified_route_rule_digest"):
            if _SHA256_RE.fullmatch(getattr(self, name)) is None:
                raise ProviderFundingIncomeError(f"{name} is not canonical")
        if self.parser_identity != _BYBIT_FUNDING_PARSER_IDENTITY:
            raise ProviderFundingIncomeError("funding parser identity is not qualified")
        if type(self.origin_journal_sequence) is not int or self.origin_journal_sequence < 1:
            raise ProviderFundingIncomeError("origin journal sequence is invalid")
        if _SHA256_RE.fullmatch(self.evidence_ref) is None:
            raise ProviderFundingIncomeError("funding income evidence_ref is not canonical")


def _validated_origin(
    source: ProviderOriginObservation,
) -> tuple[object, ProviderResponseObservation, str]:
    if type(source) is not ProviderOriginObservation:
        raise ProviderFundingIncomeError(
            "funding income requires exact ProviderOriginObservation"
        )
    binding = source.response_binding
    try:
        require_provider_origin_response_binding_authority(binding)
        qualified = source.qualified_observation
        qualified_ref = qualified.evidence_ref
        qualified_query_digest = qualified.query_binding.query_digest
    except (ProviderOriginError, ProviderRouteReadError) as error:
        raise ProviderFundingIncomeError(
            "funding income provider-origin authority is unavailable"
        ) from error

    neutral = qualified.observation
    if type(neutral) is not ProviderResponseObservation:
        raise ProviderFundingIncomeError("qualified funding content is not canonical")
    base = qualified.query_binding.query_binding
    if (
        source.origin_ref != binding.origin_ref
        or source.qualified_evidence_ref != qualified_ref
        or binding.provider_id != qualified.provider_id
        or binding.account_id != qualified.account_id
        or binding.environment != qualified.environment
        or binding.capability_snapshot_id != base.capability_snapshot_id
        or binding.qualification_id != qualified.qualification_id
        or binding.endpoint != base.endpoint
        or binding.qualified_query_digest != qualified_query_digest
        or binding.endpoint_rule_digest != qualified.endpoint_rule_digest
        or binding.qualified_route_rule_digest != qualified.qualified_route_rule_digest
        or binding.data_entitlement != qualified.data_entitlement
        or binding.parser_identity != qualified.parser_identity
        or binding.response_sha256 != neutral.response_sha256
        or binding.observed_at != neutral.observed_at
    ):
        raise ProviderFundingIncomeError(
            "funding income provider-origin wrapper differs from sealed Q/response"
        )
    if (
        binding.provider_id != "BYBIT"
        or binding.endpoint != _BYBIT_TRANSACTION_LOG_ENDPOINT
        or binding.data_entitlement != "ACTIVITIES"
        or binding.parser_identity != _BYBIT_FUNDING_PARSER_IDENTITY
        or base.surface is not Surface.AUTHENTICATED_READ
        or base.permission_scope != "ACCOUNT.READ"
    ):
        raise ProviderFundingIncomeError(
            "provider origin is not qualified for Bybit funding transaction-log semantics"
        )
    query = base.query
    if (
        query.get("accountType") != "UNIFIED"
        or query.get("type") != "SETTLEMENT"
        or query.get("category") not in {"linear", "inverse"}
    ):
        raise ProviderFundingIncomeError(
            "funding transaction-log query is not narrowed to one derivative settlement domain"
        )
    try:
        neutral.require_scope(
            provider_id="BYBIT",
            surface=Surface.AUTHENTICATED_READ,
            endpoint=_BYBIT_TRANSACTION_LOG_ENDPOINT,
            account_id=binding.account_id,
            environment=binding.environment,
        )
    except Exception as error:
        raise ProviderFundingIncomeError(
            "funding transaction-log neutral response scope is invalid"
        ) from error
    return binding, neutral, qualified_ref


def bybit_funding_income_observations(
    source: ProviderOriginObservation,
) -> tuple[ProviderFundingIncomeObservation, ...]:
    """Project funding cash movements; grant no rate/price/position authority."""

    binding, neutral, qualified_ref = _validated_origin(source)
    payload = neutral.payload
    if type(payload) is not dict:
        raise ProviderFundingIncomeError("Bybit transaction-log payload must be an object")
    if payload.get("retCode") != 0 or type(payload.get("retMsg")) is not str:
        raise ProviderFundingIncomeError("Bybit transaction-log response is not successful")
    result = payload.get("result")
    if type(result) is not dict or type(result.get("list")) is not list:
        raise ProviderFundingIncomeError("Bybit transaction-log result shape is invalid")

    expected_category = binding.qualified_observation.query_binding.query_binding.query.get(
        "category"
    ) if False else None
    # Read through the already-authorized qualified observation rather than the
    # response-binding payload; the response binding intentionally stores only
    # immutable route/origin identity.
    query = source.qualified_observation.query_binding.query_binding.query
    expected_category = query["category"]
    observations: list[ProviderFundingIncomeObservation] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(result["list"]):
        if type(item) is not dict:
            raise ProviderFundingIncomeError(
                f"Bybit transaction-log row {index} must be an object"
            )
        row_type = item.get("type")
        category = item.get("category")
        funding = item.get("funding")
        if row_type != "SETTLEMENT" or category not in {"linear", "inverse"}:
            continue
        if category != expected_category:
            raise ProviderFundingIncomeError(
                "Bybit funding row escaped the qualified derivative category"
            )
        if funding == "":
            continue
        provider_transaction_id = _text(item.get("id"), name="id")
        if provider_transaction_id in seen_ids:
            raise ProviderFundingIncomeError(
                "Bybit funding transaction id is duplicated in one provider response"
            )
        seen_ids.add(provider_transaction_id)
        symbol = _text(item.get("symbol"), name="symbol")
        currency = _text(item.get("currency"), name="currency").upper()
        side = _text(item.get("side"), name="side")
        occurred_at = _transaction_time(item.get("transactionTime"))
        position_size = _decimal(item.get("size"), name="size")
        funding_amount = _decimal(funding, name="funding")
        material = {
            "schema_version": "1.0.0",
            "origin_ref": binding.origin_ref,
            "qualified_evidence_ref": qualified_ref,
            "provider_transaction_id": provider_transaction_id,
            "instrument_id": symbol,
            "product_category": category,
            "settlement_currency": currency,
            "transaction_time_ms": item.get("transactionTime"),
            "side": side,
            "position_size": format(position_size, "f"),
            "funding_amount": format(funding_amount, "f"),
            "qualification_id": binding.qualification_id,
            "qualified_query_digest": binding.qualified_query_digest,
            "qualified_route_rule_digest": binding.qualified_route_rule_digest,
            "parser_identity": binding.parser_identity,
        }
        evidence_ref = "sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()
        observations.append(
            ProviderFundingIncomeObservation(
                provider_id="BYBIT",
                account_id=binding.account_id,
                runtime_environment=binding.environment,
                provider_environment=binding.provider_environment,
                instrument_id=symbol,
                product_category=category,
                settlement_currency=currency,
                provider_transaction_id=provider_transaction_id,
                occurred_at=occurred_at,
                side=side,
                position_size=position_size,
                funding_amount=funding_amount,
                origin_ref=binding.origin_ref,
                qualified_evidence_ref=qualified_ref,
                response_sha256=binding.response_sha256,
                qualification_id=binding.qualification_id,
                qualified_query_digest=binding.qualified_query_digest,
                qualified_route_rule_digest=binding.qualified_route_rule_digest,
                parser_identity=binding.parser_identity,
                origin_journal_sequence=binding.journal_sequence,
                evidence_ref=evidence_ref,
            )
        )
    return tuple(observations)


__all__ = [
    "ProviderFundingIncomeError",
    "ProviderFundingIncomeObservation",
    "bybit_funding_income_observations",
]
