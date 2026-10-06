"""Neutral Bybit public margin-market facts from exact provider response bytes.

This module is deliberately below production financial authority.  The parser
binds exact PUBLIC_DATA response/query identity and source facts, but a caller
can still mint a neutral ProviderResponseObservation from supplied bytes.
PAPER/LIVE margin admission therefore requires a later qualified wire-origin
composition before these facts can issue PerpetualMarginEvidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
import re
from types import MappingProxyType

from .exact_decimal import ExactDecimalError, parse_bounded_exact_decimal
from .provider_core import (
    ProviderCoreError,
    ProviderResponseObservation,
    Surface,
    provider_response_observation_require_scope,
)


BYBIT_MARGIN_TICKER_ENDPOINT = "/v5/market/tickers"
BYBIT_MARGIN_RISK_LIMIT_ENDPOINT = "/v5/market/risk-limit"
BYBIT_MARGIN_MARKET_PARSER_IDENTITY = "BYBIT_V5_MARGIN_PUBLIC_FACTS"
BYBIT_MARGIN_MARKET_PARSER_VERSION = "1"

_PARSER_CONTRACT = {
    "provider_id": "BYBIT",
    "surface": Surface.PUBLIC_DATA.value,
    "permission_scope": "MARKET.READ",
    "ticker": {
        "endpoint": BYBIT_MARGIN_TICKER_ENDPOINT,
        "query": ["category", "symbol"],
        "facts": ["symbol", "markPrice", "indexPrice"],
    },
    "risk_limit": {
        "endpoint": BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
        "query": ["category", "symbol"],
        "complete_page_required": True,
        "facts": [
            "id",
            "symbol",
            "riskLimitValue",
            "maintenanceMargin",
            "initialMargin",
            "isLowestRisk",
            "maxLeverage",
            "mmDeduction",
        ],
    },
}
BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST = "sha256:" + sha256(
    json.dumps(
        _PARSER_CONTRACT,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
).hexdigest()

_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")
_MAX_RISK_TIERS = 512


class BybitMarginMarketError(ProviderCoreError):
    """Raised when public Bybit margin facts violate the source contract."""


def _mapping(value: object, *, name: str) -> MappingProxyType:
    if type(value) is not MappingProxyType:
        raise BybitMarginMarketError(f"{name} must be an exact frozen provider object")
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BybitMarginMarketError(f"{name} must be exact canonical text")
    return value


def _symbol(value: object, *, name: str) -> str:
    result = _text(value, name=name)
    if _SYMBOL_RE.fullmatch(result) is None:
        raise BybitMarginMarketError(f"{name} must be uppercase canonical provider text")
    return result


def _decimal_text(
    value: object,
    *,
    name: str,
    positive: bool = False,
    allow_empty: bool = False,
) -> Decimal | None:
    if type(value) is not str:
        raise BybitMarginMarketError(f"{name} must be provider decimal text")
    if value == "":
        if allow_empty:
            return None
        raise BybitMarginMarketError(f"{name} must not be empty")
    if len(value) > 160 or _DECIMAL_RE.fullmatch(value) is None:
        raise BybitMarginMarketError(f"{name} must be canonical provider decimal text")
    try:
        result = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise BybitMarginMarketError(f"{name} exceeds the exact numeric envelope") from error
    if result < 0 or (positive and result <= 0):
        comparator = "positive" if positive else "non-negative"
        raise BybitMarginMarketError(f"{name} must be {comparator}")
    return result


def _decimal_number_or_text(
    value: object,
    *,
    name: str,
    positive: bool = False,
) -> Decimal:
    """Accept the two representations used by the documented Bybit contract."""

    if type(value) is str:
        result = _decimal_text(value, name=name, positive=positive)
        assert result is not None
        return result
    if type(value) is int:
        raw = str(value)
    elif type(value) is Decimal:
        if not value.is_finite():
            raise BybitMarginMarketError(f"{name} must be finite")
        raw = str(value)
    else:
        raise BybitMarginMarketError(
            f"{name} must be exact provider decimal text or JSON number"
        )
    try:
        result = parse_bounded_exact_decimal(raw)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise BybitMarginMarketError(f"{name} exceeds the exact numeric envelope") from error
    if result < 0 or (positive and result <= 0):
        comparator = "positive" if positive else "non-negative"
        raise BybitMarginMarketError(f"{name} must be {comparator}")
    return result


def _integer(value: object, *, name: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise BybitMarginMarketError(
            f"{name} must be an exact integer >= {minimum}"
        )
    return value


def _public_projection(
    observation: ProviderResponseObservation,
    *,
    endpoint: str,
) -> MappingProxyType:
    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be exact ProviderResponseObservation")
    projection = provider_response_observation_require_scope(
        observation,
        provider_id="BYBIT",
        surface=Surface.PUBLIC_DATA,
        endpoint=endpoint,
    )
    if projection["permission_scope"] != "MARKET.READ":
        raise BybitMarginMarketError(
            "Bybit public margin facts require MARKET.READ permission"
        )
    return projection


def _single_symbol_query(
    projection: MappingProxyType,
) -> tuple[str, str]:
    query = _mapping(projection["query"], name="public margin query")
    if set(query) != {"category", "symbol"}:
        raise BybitMarginMarketError(
            "Bybit public margin query requires exact category+symbol"
        )
    category = _text(query["category"], name="query category")
    if category not in {"linear", "inverse"}:
        raise BybitMarginMarketError(
            "Bybit public margin category must be linear or inverse"
        )
    symbol = _symbol(query["symbol"], name="query symbol")
    return category, symbol


def _result(
    projection: MappingProxyType,
    *,
    category: str,
) -> tuple[MappingProxyType, int]:
    envelope = _mapping(projection["payload"], name="Bybit public response")
    if type(envelope.get("retCode")) is not int or envelope.get("retCode") != 0:
        raise BybitMarginMarketError("Bybit public response requires exact integer retCode=0")
    provider_time_ms = _integer(
        envelope.get("time"),
        name="Bybit public response time",
    )
    result = _mapping(envelope.get("result"), name="Bybit public response result")
    if result.get("category") != category:
        raise BybitMarginMarketError(
            "Bybit public response category must match exact query category"
        )
    return result, provider_time_ms


@dataclass(frozen=True, slots=True)
class BybitMarginTickerFact:
    symbol: str
    category: str
    mark_price: Decimal
    index_price: Decimal
    provider_time_ms: int
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    capability_snapshot_id: str
    instrument_version: str
    query_digest: str
    evidence_ref: str
    response_sha256: str
    observed_at: str
    parser_identity: str = BYBIT_MARGIN_MARKET_PARSER_IDENTITY
    parser_version: str = BYBIT_MARGIN_MARKET_PARSER_VERSION
    parser_contract_digest: str = BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST


@dataclass(frozen=True, slots=True)
class BybitMarginRiskTierFact:
    risk_id: int
    symbol: str
    risk_limit_value: Decimal
    maintenance_margin: Decimal
    initial_margin: Decimal
    is_lowest_risk: bool
    max_leverage: Decimal
    maintenance_margin_deduction: Decimal | None


@dataclass(frozen=True, slots=True)
class BybitMarginRiskLimitPage:
    tiers: tuple[BybitMarginRiskTierFact, ...]
    symbol: str
    category: str
    provider_time_ms: int
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    capability_snapshot_id: str
    instrument_version: str
    query_digest: str
    evidence_ref: str
    response_sha256: str
    observed_at: str
    parser_identity: str = BYBIT_MARGIN_MARKET_PARSER_IDENTITY
    parser_version: str = BYBIT_MARGIN_MARKET_PARSER_VERSION
    parser_contract_digest: str = BYBIT_MARGIN_MARKET_PARSER_CONTRACT_DIGEST


def parse_bybit_margin_ticker(
    observation: ProviderResponseObservation,
) -> BybitMarginTickerFact:
    """Parse exact mark/index facts from one neutral single-symbol ticker response."""

    projection = _public_projection(
        observation,
        endpoint=BYBIT_MARGIN_TICKER_ENDPOINT,
    )
    category, symbol = _single_symbol_query(projection)
    result, provider_time_ms = _result(projection, category=category)
    rows = result.get("list")
    if type(rows) is not tuple or len(rows) != 1:
        raise BybitMarginMarketError(
            "Bybit single-symbol ticker response must contain exactly one row"
        )
    row = _mapping(rows[0], name="ticker row")
    if _symbol(row.get("symbol"), name="ticker symbol") != symbol:
        raise BybitMarginMarketError(
            "Bybit ticker row symbol must match exact query symbol"
        )
    mark_price = _decimal_text(
        row.get("markPrice"),
        name="ticker markPrice",
        positive=True,
    )
    index_price = _decimal_text(
        row.get("indexPrice"),
        name="ticker indexPrice",
        positive=True,
    )
    assert mark_price is not None and index_price is not None
    return BybitMarginTickerFact(
        symbol=symbol,
        category=category,
        mark_price=mark_price,
        index_price=index_price,
        provider_time_ms=provider_time_ms,
        provider_id=projection["provider_id"],
        account_id=projection["account_id"],
        entity_id=projection["entity_id"],
        environment=projection["environment"],
        capability_snapshot_id=projection["capability_snapshot_id"],
        instrument_version=projection["instrument_version"],
        query_digest=projection["query_digest"],
        evidence_ref=projection["evidence_ref"],
        response_sha256=projection["response_sha256"],
        observed_at=projection["observed_at"],
    )


def parse_bybit_margin_risk_limits(
    observation: ProviderResponseObservation,
) -> BybitMarginRiskLimitPage:
    """Parse one complete single-symbol risk-tier table without unit conversion."""

    projection = _public_projection(
        observation,
        endpoint=BYBIT_MARGIN_RISK_LIMIT_ENDPOINT,
    )
    category, symbol = _single_symbol_query(projection)
    result, provider_time_ms = _result(projection, category=category)
    cursor = result.get("nextPageCursor")
    if type(cursor) is not str or cursor != "":
        raise BybitMarginMarketError(
            "Bybit single-symbol risk-limit evidence must be a complete page"
        )
    rows = result.get("list")
    if type(rows) is not tuple or not rows or len(rows) > _MAX_RISK_TIERS:
        raise BybitMarginMarketError(
            "Bybit risk-limit response must contain a bounded non-empty tier table"
        )

    tiers: list[BybitMarginRiskTierFact] = []
    seen_ids: set[int] = set()
    seen_limits: set[Decimal] = set()
    lowest_count = 0
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, name=f"risk-limit row[{index}]")
        row_symbol = _symbol(
            row.get("symbol"),
            name=f"risk-limit row[{index}].symbol",
        )
        if row_symbol != symbol:
            raise BybitMarginMarketError(
                "Bybit risk-limit row symbol must match exact query symbol"
            )
        risk_id = _integer(
            row.get("id"),
            name=f"risk-limit row[{index}].id",
            minimum=1,
        )
        if risk_id in seen_ids:
            raise BybitMarginMarketError("Bybit risk-limit table contains duplicate risk id")
        seen_ids.add(risk_id)
        risk_limit = _decimal_text(
            row.get("riskLimitValue"),
            name=f"risk-limit row[{index}].riskLimitValue",
            positive=True,
        )
        assert risk_limit is not None
        if risk_limit in seen_limits:
            raise BybitMarginMarketError(
                "Bybit risk-limit table contains duplicate position limit"
            )
        seen_limits.add(risk_limit)

        lowest = _integer(
            row.get("isLowestRisk"),
            name=f"risk-limit row[{index}].isLowestRisk",
        )
        if lowest not in {0, 1}:
            raise BybitMarginMarketError("isLowestRisk must be exact integer 0 or 1")
        lowest_count += lowest

        tiers.append(
            BybitMarginRiskTierFact(
                risk_id=risk_id,
                symbol=row_symbol,
                risk_limit_value=risk_limit,
                maintenance_margin=_decimal_number_or_text(
                    row.get("maintenanceMargin"),
                    name=f"risk-limit row[{index}].maintenanceMargin",
                    positive=True,
                ),
                initial_margin=_decimal_number_or_text(
                    row.get("initialMargin"),
                    name=f"risk-limit row[{index}].initialMargin",
                    positive=True,
                ),
                is_lowest_risk=bool(lowest),
                max_leverage=_decimal_text(
                    row.get("maxLeverage"),
                    name=f"risk-limit row[{index}].maxLeverage",
                    positive=True,
                ),
                maintenance_margin_deduction=_decimal_text(
                    row.get("mmDeduction"),
                    name=f"risk-limit row[{index}].mmDeduction",
                    allow_empty=True,
                ),
            )
        )

    if lowest_count != 1:
        raise BybitMarginMarketError(
            "Bybit risk-limit table must identify exactly one lowest-risk tier"
        )

    return BybitMarginRiskLimitPage(
        tiers=tuple(tiers),
        symbol=symbol,
        category=category,
        provider_time_ms=provider_time_ms,
        provider_id=projection["provider_id"],
        account_id=projection["account_id"],
        entity_id=projection["entity_id"],
        environment=projection["environment"],
        capability_snapshot_id=projection["capability_snapshot_id"],
        instrument_version=projection["instrument_version"],
        query_digest=projection["query_digest"],
        evidence_ref=projection["evidence_ref"],
        response_sha256=projection["response_sha256"],
        observed_at=projection["observed_at"],
    )
