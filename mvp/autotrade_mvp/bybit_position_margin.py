"""Source-owned Bybit V5 position/margin fact parser.

This module intentionally stops below perpetual-margin decision authority.
It preserves exact provider position facts from one authenticated read, but
does not convert riskLimitValue into a tier table, infer margin mode, or grant
PAPER/LIVE risk authority.
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


BYBIT_POSITION_MARGIN_PARSER_IDENTITY = "BYBIT_POSITION_MARGIN_V5_JSON_V1"
BYBIT_POSITION_MARGIN_PARSER_VERSION = "1.0.1"
BYBIT_POSITION_MARGIN_PARSER_CONTRACT_DIGEST = (
    "sha256:"
    + sha256(
        json.dumps(
            {
                "parser_identity": BYBIT_POSITION_MARGIN_PARSER_IDENTITY,
                "parser_version": BYBIT_POSITION_MARGIN_PARSER_VERSION,
                "source_type": "EXACT_ProviderResponseObservation",
                "scope": {
                    "provider_id": "BYBIT",
                    "surface": "AUTHENTICATED_READ",
                    "endpoint": "/v5/position/list",
                    "permission_scope": "POSITION.READ",
                    "query_category": ["linear", "inverse"],
                    "query_symbol": "EXACT_UPPERCASE_SINGLE_SYMBOL",
                    "pagination": "ONE_COMPLETE_SYMBOL_PAGE_ONLY",
                },
                "row_identity": {
                    "positionIdx": [0, 1, 2],
                    "positionIdx_side": {
                        "0": ["", "Buy", "Sell"],
                        "1": ["", "Buy"],
                        "2": ["", "Sell"],
                    },
                    "position_mode_topology": "ONE_WAY_IDX_0_XOR_HEDGE_IDX_1_2",
                    "symbol": "EXACT_QUERY_SYMBOL",
                    "side": ["", "Buy", "Sell"],
                    "seq": "EXACT_JSON_INTEGER_GTE_NEGATIVE_ONE",
                },
                "economic_fields": {
                    "riskId": "EXACT_JSON_INTEGER_GTE_ZERO",
                    "riskLimitValue": "BOUNDED_NONNEGATIVE_DECIMAL_TEXT_OR_EMPTY_ONLY_WHEN_RISK_ID_ZERO",
                    "size": "BOUNDED_NONNEGATIVE_DECIMAL_TEXT",
                    "markPrice": "BOUNDED_POSITIVE_DECIMAL_TEXT",
                    "liqPrice": "EMPTY_OR_BOUNDED_POSITIVE_DECIMAL_TEXT",
                    "positionIM": "EMPTY_OR_BOUNDED_NONNEGATIVE_DECIMAL_TEXT",
                    "positionMM": "EMPTY_OR_BOUNDED_NONNEGATIVE_DECIMAL_TEXT",
                    "leverage": "EMPTY_OR_BOUNDED_POSITIVE_DECIMAL_TEXT",
                    "updatedTime": "CANONICAL_NONNEGATIVE_INTEGER_TEXT",
                    "isReduceOnly": "EXACT_JSON_BOOLEAN",
                    "positionStatus": ["Normal", "Liq", "Adl"],
                },
                "authority_limits": {
                    "riskLimitValue": "CURRENT_POSITION_PROVIDER_FACT_NOT_COMPLETE_RISK_TIER_TABLE",
                    "riskId_zero": "RISK_LIMIT_RULES_INVALID_PORTFOLIO_MARGIN",
                    "liqPrice": "PROVIDER_FACT_MAY_BE_EMPTY_OR_ESTIMATED",
                    "margin_mode": "NOT_ISSUED_BY_THIS_PARSER",
                    "tier_rates": "REQUIRE_SEPARATE_QUALIFIED_RISK_LIMIT_SOURCE",
                    "paper_live_risk": "NOT_GRANTED",
                },
                "output": "BybitPositionMarginPage_PROVIDER_FACTS_ONLY",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
)

_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
_SYMBOL_RE = re.compile(r"^[A-Z0-9]+(?:-[A-Z0-9]+)*$")


class BybitPositionMarginError(ProviderCoreError):
    """Raised when Bybit position facts do not match the source-owned contract."""


def _mapping(value: object, *, name: str) -> MappingProxyType:
    if type(value) is not MappingProxyType:
        raise BybitPositionMarginError(f"{name} must be an exact frozen provider object")
    return value


def _text(value: object, *, name: str) -> str:
    if type(value) is not str or value != value.strip():
        raise BybitPositionMarginError(f"{name} must be exact canonical text")
    return value


def _decimal(
    value: object,
    *,
    name: str,
    allow_empty: bool = False,
    positive: bool = False,
) -> Decimal | None:
    if type(value) is not str:
        raise BybitPositionMarginError(f"{name} must be provider decimal text")
    if value == "":
        if allow_empty:
            return None
        raise BybitPositionMarginError(f"{name} must not be empty")
    if len(value) > 160 or _DECIMAL_RE.fullmatch(value) is None:
        raise BybitPositionMarginError(f"{name} must be canonical provider decimal text")
    try:
        parsed = parse_bounded_exact_decimal(value)
    except (ExactDecimalError, TypeError, ValueError) as error:
        raise BybitPositionMarginError(f"{name} exceeds the exact numeric envelope") from error
    if parsed < 0 or (positive and parsed <= 0):
        comparator = "positive" if positive else "non-negative"
        raise BybitPositionMarginError(f"{name} must be {comparator}")
    return parsed


def _integer(value: object, *, name: str, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise BybitPositionMarginError(f"{name} must be an exact integer >= {minimum}")
    return value


def _millisecond_text(value: object, *, name: str) -> int:
    if (
        type(value) is not str
        or not value
        or len(value) > 20
        or not value.isascii()
        or not value.isdigit()
    ):
        raise BybitPositionMarginError(
            f"{name} must be canonical non-negative millisecond text"
        )
    parsed = int(value, 10)
    if str(parsed) != value:
        raise BybitPositionMarginError(
            f"{name} must be canonical non-negative millisecond text"
        )
    return parsed


@dataclass(frozen=True, slots=True)
class BybitPositionMarginFact:
    position_idx: int
    risk_id: int
    risk_limit_value: Decimal | None
    symbol: str
    side: str
    size: Decimal
    mark_price: Decimal
    liquidation_price: Decimal | None
    position_initial_margin: Decimal | None
    position_maintenance_margin: Decimal | None
    leverage: Decimal | None
    position_status: str
    updated_time_ms: int
    sequence: int
    is_reduce_only: bool


@dataclass(frozen=True, slots=True)
class BybitPositionMarginPage:
    positions: tuple[BybitPositionMarginFact, ...]
    category: str
    provider_id: str
    account_id: str
    entity_id: str
    environment: str
    capability_snapshot_id: str
    instrument_version: str
    permission_scope: str
    query_digest: str
    evidence_ref: str
    response_sha256: str
    observed_at: str
    parser_identity: str = BYBIT_POSITION_MARGIN_PARSER_IDENTITY
    parser_version: str = BYBIT_POSITION_MARGIN_PARSER_VERSION
    parser_contract_digest: str = BYBIT_POSITION_MARGIN_PARSER_CONTRACT_DIGEST


def parse_bybit_position_margin_page(
    observation: ProviderResponseObservation,
) -> BybitPositionMarginPage:
    """Parse one bounded single-symbol position page into provider facts only."""

    if type(observation) is not ProviderResponseObservation:
        raise TypeError("observation must be exact ProviderResponseObservation")
    projection = provider_response_observation_require_scope(
        observation,
        provider_id="BYBIT",
        surface=Surface.AUTHENTICATED_READ,
        endpoint="/v5/position/list",
    )
    if projection["permission_scope"] != "POSITION.READ":
        raise BybitPositionMarginError(
            "Bybit position margin facts require POSITION.READ permission"
        )
    query = _mapping(projection["query"], name="position query")
    if set(query) != {"category", "symbol"}:
        raise BybitPositionMarginError(
            "Bybit position margin parser requires exact category+symbol query"
        )
    category = _text(query["category"], name="query category")
    if category not in {"linear", "inverse"}:
        raise BybitPositionMarginError(
            "Bybit position margin category must be linear or inverse"
        )
    symbol = _text(query["symbol"], name="query symbol")
    if not symbol or _SYMBOL_RE.fullmatch(symbol) is None:
        raise BybitPositionMarginError(
            "Bybit position margin symbol must be uppercase canonical provider text"
        )

    envelope = _mapping(projection["payload"], name="position response")
    ret_code = envelope.get("retCode")
    if type(ret_code) is not int or ret_code != 0:
        raise BybitPositionMarginError(
            "Bybit position response requires exact integer retCode=0"
        )
    result = _mapping(envelope.get("result"), name="position response result")
    if result.get("category") != category:
        raise BybitPositionMarginError(
            "Bybit position response category must match exact query category"
        )
    next_cursor = result.get("nextPageCursor")
    if type(next_cursor) is not str or next_cursor != "":
        raise BybitPositionMarginError(
            "Bybit single-symbol position evidence must be a complete page"
        )
    rows = result.get("list")
    if type(rows) is not tuple or not rows or len(rows) > 2:
        raise BybitPositionMarginError(
            "Bybit single-symbol position evidence must contain one or two legs"
        )

    positions: list[BybitPositionMarginFact] = []
    position_indices: set[int] = set()
    for index, raw_row in enumerate(rows):
        row = _mapping(raw_row, name=f"result.list[{index}]")
        position_idx = _integer(
            row.get("positionIdx"),
            name=f"result.list[{index}].positionIdx",
            minimum=0,
        )
        if position_idx not in {0, 1, 2}:
            raise BybitPositionMarginError("positionIdx must be 0, 1 or 2")
        if position_idx in position_indices:
            raise BybitPositionMarginError(
                "Bybit position page contains duplicate positionIdx"
            )
        position_indices.add(position_idx)

        row_symbol = _text(row.get("symbol"), name=f"result.list[{index}].symbol")
        if row_symbol != symbol:
            raise BybitPositionMarginError(
                "Bybit position row symbol must match exact query symbol"
            )
        side = _text(row.get("side"), name=f"result.list[{index}].side")
        if side not in {"", "Buy", "Sell"}:
            raise BybitPositionMarginError(
                "Bybit position side must be empty, Buy or Sell"
            )
        if position_idx == 1 and side not in {"", "Buy"}:
            raise BybitPositionMarginError(
                "hedge positionIdx=1 requires empty or Buy side"
            )
        if position_idx == 2 and side not in {"", "Sell"}:
            raise BybitPositionMarginError(
                "hedge positionIdx=2 requires empty or Sell side"
            )
        risk_id = _integer(
            row.get("riskId"),
            name=f"result.list[{index}].riskId",
            minimum=0,
        )
        risk_limit = _decimal(
            row.get("riskLimitValue"),
            name=f"result.list[{index}].riskLimitValue",
            allow_empty=True,
        )
        if risk_id == 0:
            if risk_limit not in {None, Decimal("0")}:
                raise BybitPositionMarginError(
                    "riskId=0 requires empty or zero riskLimitValue"
                )
        elif risk_limit is None or risk_limit <= 0:
            raise BybitPositionMarginError(
                "positive riskId requires positive riskLimitValue"
            )

        position_status = _text(
            row.get("positionStatus"),
            name=f"result.list[{index}].positionStatus",
        )
        if position_status not in {"Normal", "Liq", "Adl"}:
            raise BybitPositionMarginError(
                "positionStatus must be Normal, Liq or Adl"
            )
        is_reduce_only = row.get("isReduceOnly")
        if type(is_reduce_only) is not bool:
            raise BybitPositionMarginError("isReduceOnly must be an exact boolean")

        positions.append(
            BybitPositionMarginFact(
                position_idx=position_idx,
                risk_id=risk_id,
                risk_limit_value=risk_limit,
                symbol=row_symbol,
                side=side,
                size=_decimal(row.get("size"), name=f"result.list[{index}].size"),
                mark_price=_decimal(
                    row.get("markPrice"),
                    name=f"result.list[{index}].markPrice",
                    positive=True,
                ),
                liquidation_price=_decimal(
                    row.get("liqPrice"),
                    name=f"result.list[{index}].liqPrice",
                    allow_empty=True,
                    positive=True,
                ),
                position_initial_margin=_decimal(
                    row.get("positionIM"),
                    name=f"result.list[{index}].positionIM",
                    allow_empty=True,
                ),
                position_maintenance_margin=_decimal(
                    row.get("positionMM"),
                    name=f"result.list[{index}].positionMM",
                    allow_empty=True,
                ),
                leverage=_decimal(
                    row.get("leverage"),
                    name=f"result.list[{index}].leverage",
                    allow_empty=True,
                    positive=True,
                ),
                position_status=position_status,
                updated_time_ms=_millisecond_text(
                    row.get("updatedTime"),
                    name=f"result.list[{index}].updatedTime",
                ),
                sequence=_integer(
                    row.get("seq"),
                    name=f"result.list[{index}].seq",
                    minimum=-1,
                ),
                is_reduce_only=is_reduce_only,
            )
        )

    if 0 in position_indices and len(position_indices) != 1:
        raise BybitPositionMarginError(
            "Bybit position page cannot mix one-way and hedge-mode positionIdx values"
        )

    return BybitPositionMarginPage(
        positions=tuple(positions),
        category=category,
        provider_id=projection["provider_id"],
        account_id=projection["account_id"],
        entity_id=projection["entity_id"],
        environment=projection["environment"],
        capability_snapshot_id=projection["capability_snapshot_id"],
        instrument_version=projection["instrument_version"],
        permission_scope=projection["permission_scope"],
        query_digest=projection["query_digest"],
        evidence_ref=projection["evidence_ref"],
        response_sha256=projection["response_sha256"],
        observed_at=projection["observed_at"],
    )
