"""Provider-origin funding-income facts from qualified Bybit transaction logs.

This module is deliberately *not* a funding booking authority. It projects one
narrow independently authoritative fact from the shared WP-18 provider-origin
boundary: provider-reported funding cash movement and provider transaction
identity. Funding rate, mark/index valuation, canonical position/cut and
instrument convention remain separate authorities and must be composed by
WP-29 before PAPER/LIVE accounting can mutate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import re
from types import MappingProxyType
import weakref

from .exact_decimal import ExactDecimalError, as_fraction, parse_bounded_exact_decimal
from .persistence import canonical_json
from .provider_core import ProviderResponseObservation, Surface
from .provider_origin import (
    AuthenticatedReadResponseBinding,
    ProviderOriginError,
    ProviderOriginObservation,
    require_provider_origin_response_binding_authority,
)
from .provider_route_reads import (
    ProviderRouteReadError,
    QualifiedProviderResponseObservation,
)
from .provider_transport import (
    direct_authenticated_read_network_policy_identity,
    direct_authenticated_read_transport_identity,
)


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


def _epoch_millis(value: object, *, name: str) -> int:
    text = _text(value, name=name)
    if not text.isascii() or not text.isdigit():
        raise ProviderFundingIncomeError(
            f"{name} must be exact epoch-millisecond text"
        )
    try:
        millis = int(text)
    except ValueError as error:
        raise ProviderFundingIncomeError(
            f"{name} must be exact epoch-millisecond text"
        ) from error
    if millis <= 0:
        raise ProviderFundingIncomeError(f"{name} must be positive")
    if str(millis) != text:
        raise ProviderFundingIncomeError(
            f"{name} must use canonical epoch-millisecond text"
        )
    return millis


def _datetime_from_epoch_millis(millis: int, *, name: str) -> datetime:
    seconds, remainder_millis = divmod(millis, 1000)
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(
            milliseconds=remainder_millis
        )
    except (OverflowError, OSError, ValueError) as error:
        raise ProviderFundingIncomeError(
            f"{name} is outside supported UTC range"
        ) from error


def _uppercase_ascii_text(value: object, *, name: str) -> str:
    text = _text(value, name=name)
    if not text.isascii() or text != text.upper():
        raise ProviderFundingIncomeError(f"{name} must be canonical uppercase ASCII text")
    return text


@dataclass(frozen=True, slots=True, weakref_slot=True, init=False)
class ProviderFundingIncomeObservation:
    """Closure-issued provider funding cash fact; insufficient alone to book."""

    provider_id: str
    account_id: str
    runtime_environment: str
    provider_environment: str
    instrument_id: str
    product_category: str
    settlement_currency: str
    provider_transaction_id: str
    provider_transaction_at: datetime
    side: str
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

    def __init__(self, *_args, **_kwargs) -> None:
        raise ProviderFundingIncomeError(
            "provider funding income must come from qualified provider-origin bytes"
        )


def _validate_income(value: ProviderFundingIncomeObservation) -> None:
    if type(value) is not ProviderFundingIncomeObservation:
        raise ProviderFundingIncomeError("exact provider funding income is required")
    if value.provider_id != "BYBIT":
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
        _text(getattr(value, name), name=name)
    if value.runtime_environment not in {"PAPER", "LIVE"}:
        raise ProviderFundingIncomeError("runtime environment must be PAPER or LIVE")
    if value.provider_environment not in {"TESTNET", "DEMO", "MAINNET"}:
        raise ProviderFundingIncomeError("provider environment is not canonical Bybit")
    if value.product_category not in {"linear", "inverse"}:
        raise ProviderFundingIncomeError("funding income requires linear or inverse category")
    if value.side not in {"Buy", "Sell", "None"}:
        raise ProviderFundingIncomeError("funding income side is not canonical Bybit")
    if (
        type(value.provider_transaction_at) is not datetime
        or value.provider_transaction_at.tzinfo is None
        or value.provider_transaction_at.utcoffset() is None
    ):
        raise ProviderFundingIncomeError("provider transaction time must be timezone-aware")
    if type(value.funding_amount) is not Decimal:
        raise ProviderFundingIncomeError("funding amount must use exact Decimal")
    if _ORIGIN_RE.fullmatch(value.origin_ref) is None:
        raise ProviderFundingIncomeError("origin_ref is not canonical")
    if _QUALIFIED_RE.fullmatch(value.qualified_evidence_ref) is None:
        raise ProviderFundingIncomeError("qualified_evidence_ref is not canonical")
    for name in ("response_sha256", "qualified_query_digest", "qualified_route_rule_digest"):
        if _SHA256_RE.fullmatch(getattr(value, name)) is None:
            raise ProviderFundingIncomeError(f"{name} is not canonical")
    if value.parser_identity != _BYBIT_FUNDING_PARSER_IDENTITY:
        raise ProviderFundingIncomeError("funding parser identity is not qualified")
    if type(value.origin_journal_sequence) is not int or value.origin_journal_sequence < 1:
        raise ProviderFundingIncomeError("origin journal sequence is invalid")
    if _SHA256_RE.fullmatch(value.evidence_ref) is None:
        raise ProviderFundingIncomeError("funding income evidence_ref is not canonical")


def _install_funding_income_authority():
    field_names = tuple(ProviderFundingIncomeObservation.__dataclass_fields__)
    states: dict[int, tuple[weakref.ReferenceType, tuple[object, ...]]] = {}

    def prune() -> None:
        for object_id, state in tuple(states.items()):
            if state[0]() is None:
                states.pop(object_id, None)

    def material(value: ProviderFundingIncomeObservation) -> tuple[object, ...]:
        _validate_income(value)
        return tuple(getattr(value, name) for name in field_names)

    def issue(**values: object) -> ProviderFundingIncomeObservation:
        if set(values) != set(field_names):
            raise ProviderFundingIncomeError("provider funding income issue schema is not exact")
        value = object.__new__(ProviderFundingIncomeObservation)
        for name in field_names:
            object.__setattr__(value, name, values[name])
        snapshot = material(value)
        prune()
        states[id(value)] = (weakref.ref(value), snapshot)
        return value

    def require(value: ProviderFundingIncomeObservation) -> ProviderFundingIncomeObservation:
        snapshot = material(value)
        prune()
        state = states.get(id(value))
        if state is None or state[0]() is not value:
            raise ProviderFundingIncomeError(
                "provider funding income construction authority is unavailable"
            )
        if state[1] != snapshot:
            raise ProviderFundingIncomeError(
                "provider funding income changed after qualified origin projection"
            )
        return value

    return issue, require


(
    _issue_provider_funding_income,
    require_provider_funding_income_authority,
) = _install_funding_income_authority()
del _install_funding_income_authority


def _validated_origin(
    source: ProviderOriginObservation,
) -> tuple[
    AuthenticatedReadResponseBinding,
    ProviderResponseObservation,
    QualifiedProviderResponseObservation,
    str,
]:
    if type(source) is not ProviderOriginObservation:
        raise ProviderFundingIncomeError(
            "funding income requires exact ProviderOriginObservation"
        )
    binding = source.response_binding
    qualified = source.qualified_observation
    if type(binding) is not AuthenticatedReadResponseBinding:
        raise ProviderFundingIncomeError("funding income origin binding is not canonical")
    if type(qualified) is not QualifiedProviderResponseObservation:
        raise ProviderFundingIncomeError("funding income qualified observation is not canonical")
    try:
        require_provider_origin_response_binding_authority(binding)
        qualified_ref = qualified.evidence_ref
        qualified_query_digest = qualified.query_binding.query_digest
    except (ProviderOriginError, ProviderRouteReadError, AttributeError) as error:
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
        binding.execution_class != "DIRECT_PROVIDER_WIRE"
        or binding.transport_identity != direct_authenticated_read_transport_identity()
        or binding.network_policy_identity
        != direct_authenticated_read_network_policy_identity()
    ):
        raise ProviderFundingIncomeError(
            "funding income requires canonical direct provider wire origin"
        )
    if (
        binding.provider_id != "BYBIT"
        or binding.endpoint != _BYBIT_TRANSACTION_LOG_ENDPOINT
        or binding.data_entitlement != "ACTIVITIES"
        or binding.parser_identity != _BYBIT_FUNDING_PARSER_IDENTITY
        or base.surface != Surface.AUTHENTICATED_READ
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
    return binding, neutral, qualified, qualified_ref


@dataclass(frozen=True, slots=True)
class _ParsedBybitFundingIncomeRow:
    """Normalized provider content only; this value carries no origin authority."""

    provider_transaction_id: str
    instrument_id: str
    product_category: str
    settlement_currency: str
    transaction_time_ms: str
    provider_transaction_at: datetime
    side: str
    funding_amount: Decimal


def _parse_bybit_funding_income_rows(
    payload: object,
    query: object,
    observed_at: str,
) -> tuple[_ParsedBybitFundingIncomeRow, ...]:
    """Parse exact Bybit funding rows without minting financial authority."""

    if type(observed_at) is not str or not observed_at.endswith("Z"):
        raise ProviderFundingIncomeError(
            "provider response observed_at must be canonical UTC text"
        )
    try:
        response_observed_at = datetime.fromisoformat(
            observed_at.replace("Z", "+00:00")
        ).astimezone(timezone.utc)
    except ValueError as error:
        raise ProviderFundingIncomeError(
            "provider response observed_at must be canonical UTC text"
        ) from error

    if type(payload) not in {dict, MappingProxyType}:
        raise ProviderFundingIncomeError("Bybit transaction-log payload must be an object")
    if type(query) is not dict:
        raise ProviderFundingIncomeError("qualified funding query must be an exact object")

    ret_code = payload.get("retCode")
    if (
        type(ret_code) is not int
        or ret_code != 0
        or type(payload.get("retMsg")) is not str
    ):
        raise ProviderFundingIncomeError("Bybit transaction-log response is not successful")
    result = payload.get("result")
    if (
        type(result) not in {dict, MappingProxyType}
        or type(result.get("list")) not in {list, tuple}
        or type(result.get("nextPageCursor")) is not str
    ):
        raise ProviderFundingIncomeError("Bybit transaction-log result shape is invalid")

    if (
        query.get("accountType") != "UNIFIED"
        or query.get("type") != "SETTLEMENT"
        or query.get("category") not in {"linear", "inverse"}
    ):
        raise ProviderFundingIncomeError(
            "funding transaction-log query is not narrowed to one derivative settlement domain"
        )
    expected_category = query["category"]
    expected_currency = query.get("currency")
    if expected_currency is not None:
        expected_currency = _uppercase_ascii_text(
            expected_currency,
            name="qualified query currency",
        )
    start_millis = (
        None
        if query.get("startTime") is None
        else _epoch_millis(
            query.get("startTime"),
            name="qualified query startTime",
        )
    )
    end_millis = (
        None
        if query.get("endTime") is None
        else _epoch_millis(
            query.get("endTime"),
            name="qualified query endTime",
        )
    )
    day_millis = 24 * 60 * 60 * 1000
    effective_start_millis = start_millis
    effective_end_millis = end_millis
    if start_millis is not None and end_millis is not None:
        if end_millis < start_millis:
            raise ProviderFundingIncomeError(
                "qualified funding query endTime precedes startTime"
            )
        if end_millis - start_millis > 7 * day_millis:
            raise ProviderFundingIncomeError(
                "qualified funding query exceeds Bybit seven-day range"
            )
    elif start_millis is not None:
        effective_end_millis = start_millis + day_millis
    elif end_millis is not None:
        effective_start_millis = max(1, end_millis - day_millis)

    rows: list[_ParsedBybitFundingIncomeRow] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(result["list"]):
        if type(item) not in {dict, MappingProxyType}:
            raise ProviderFundingIncomeError(
                f"Bybit transaction-log row {index} must be an object"
            )
        row_type = item.get("type")
        category = item.get("category")
        funding = item.get("funding")
        if row_type != "SETTLEMENT":
            raise ProviderFundingIncomeError(
                "Bybit transaction-log row escaped the qualified transaction type"
            )
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
        currency = _uppercase_ascii_text(item.get("currency"), name="currency")
        if expected_currency is not None and currency != expected_currency:
            raise ProviderFundingIncomeError(
                "Bybit funding row escaped the qualified currency scope"
            )
        side = _text(item.get("side"), name="side")
        if side not in {"Buy", "Sell", "None"}:
            raise ProviderFundingIncomeError(
                "Bybit funding row side is not canonical"
            )

        transaction_time_text = _text(
            item.get("transactionTime"),
            name="transactionTime",
        )
        transaction_millis = _epoch_millis(
            transaction_time_text,
            name="transactionTime",
        )
        if (
            effective_start_millis is not None
            and transaction_millis < effective_start_millis
        ):
            raise ProviderFundingIncomeError(
                "Bybit funding row predates the qualified startTime"
            )
        if (
            effective_end_millis is not None
            and transaction_millis > effective_end_millis
        ):
            raise ProviderFundingIncomeError(
                "Bybit funding row exceeds the qualified endTime"
            )
        provider_transaction_at = _datetime_from_epoch_millis(
            transaction_millis,
            name="transactionTime",
        )
        if provider_transaction_at > response_observed_at:
            raise ProviderFundingIncomeError(
                "Bybit funding transaction occurs after provider response observation"
            )

        funding_amount = _decimal(funding, name="funding")
        fee = _decimal(item.get("fee"), name="fee")
        cash_flow = _decimal(item.get("cashFlow"), name="cashFlow")
        change = _decimal(item.get("change"), name="change")
        if (
            as_fraction(change)
            != as_fraction(cash_flow) + as_fraction(funding_amount) - as_fraction(fee)
        ):
            raise ProviderFundingIncomeError(
                "Bybit funding row violates change = cashFlow + funding - fee"
            )

        rows.append(
            _ParsedBybitFundingIncomeRow(
                provider_transaction_id=provider_transaction_id,
                instrument_id=symbol,
                product_category=category,
                settlement_currency=currency,
                transaction_time_ms=transaction_time_text,
                provider_transaction_at=provider_transaction_at,
                side=side,
                funding_amount=funding_amount,
            )
        )
    return tuple(rows)


def _bybit_funding_income_observations_impl(
    source: ProviderOriginObservation,
    *,
    _issue_income,
    _validated_origin_fn,
    _parse_rows_fn,
) -> tuple[ProviderFundingIncomeObservation, ...]:
    binding, neutral, qualified, qualified_ref = _validated_origin_fn(source)
    base = qualified.query_binding.query_binding
    rows = _parse_rows_fn(
        neutral.payload,
        dict(base.query),
        neutral.observed_at,
    )

    observations: list[ProviderFundingIncomeObservation] = []
    for row in rows:
        material = {
            "schema_version": "1.0.0",
            "origin_ref": binding.origin_ref,
            "qualified_evidence_ref": qualified_ref,
            "provider_transaction_id": row.provider_transaction_id,
            "instrument_id": row.instrument_id,
            "product_category": row.product_category,
            "settlement_currency": row.settlement_currency,
            "transaction_time_ms": row.transaction_time_ms,
            "side": row.side,
            "funding_amount": format(row.funding_amount, "f"),
            "qualification_id": binding.qualification_id,
            "qualified_query_digest": binding.qualified_query_digest,
            "qualified_route_rule_digest": binding.qualified_route_rule_digest,
            "parser_identity": binding.parser_identity,
        }
        evidence_ref = "sha256:" + sha256(
            canonical_json(material).encode("utf-8")
        ).hexdigest()
        observations.append(
            _issue_income(
                provider_id="BYBIT",
                account_id=binding.account_id,
                runtime_environment=binding.environment,
                provider_environment=binding.provider_environment,
                instrument_id=row.instrument_id,
                product_category=row.product_category,
                settlement_currency=row.settlement_currency,
                provider_transaction_id=row.provider_transaction_id,
                provider_transaction_at=row.provider_transaction_at,
                side=row.side,
                funding_amount=row.funding_amount,
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

def _bind_bybit_funding_income_observations(
    impl,
    issue_income,
    validated_origin,
    parse_rows,
):
    def bybit_funding_income_observations(
        source: ProviderOriginObservation,
    ) -> tuple[ProviderFundingIncomeObservation, ...]:
        """Project provider funding cash; grant no rate/price/position authority."""

        return impl(
            source,
            _issue_income=issue_income,
            _validated_origin_fn=validated_origin,
            _parse_rows_fn=parse_rows,
        )

    return bybit_funding_income_observations


bybit_funding_income_observations = _bind_bybit_funding_income_observations(
    _bybit_funding_income_observations_impl,
    _issue_provider_funding_income,
    _validated_origin,
    _parse_bybit_funding_income_rows,
)
del _bind_bybit_funding_income_observations
del _bybit_funding_income_observations_impl
del _issue_provider_funding_income
del _validated_origin


__all__ = [
    "ProviderFundingIncomeError",
    "ProviderFundingIncomeObservation",
    "bybit_funding_income_observations",
    "require_provider_funding_income_authority",
]
