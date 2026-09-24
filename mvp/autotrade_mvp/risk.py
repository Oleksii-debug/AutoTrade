"""Independent deterministic risk admission for the AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


RISK_ACTIONS = frozenset({"TRADE", "REDUCE", "HEDGE", "FLATTEN", "EXERCISE"})
RISK_INSTRUMENT_TYPES = frozenset(
    {"GENERIC", "SPOT", "EQUITY", "FUTURE", "PERPETUAL", "OPTION"}
)


def _decimal(value, *, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise TypeError(f"{name} must use Decimal, string or integer input")
    try:
        result = value if isinstance(value, Decimal) else Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError(f"{name} must be a finite decimal") from error
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive(value, *, name: str, allow_zero: bool = False) -> Decimal:
    result = _decimal(value, name=name)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return result


def _identity_key(value, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} keys must be non-empty strings")
    return value.strip()


def _normalize_mapping(values, *, name: str, parser) -> dict[str, Decimal]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, Decimal] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = parser(raw_value, key)
    return normalized


def _normalize_text_mapping(values, *, name: str) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, str] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = _identity_key(raw_value, name=f"{name} value")
    return normalized


def _normalize_actions(values, *, name: str) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be a sequence of action names")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} values must be non-empty strings")
        action = value.strip().upper()
        if action not in RISK_ACTIONS:
            raise ValueError(f"Unsupported risk action: {action}")
        if action in normalized:
            raise ValueError(f"{name} values must be unique")
        normalized.append(action)
    if not normalized:
        raise ValueError(f"{name} must contain at least one action")
    return tuple(normalized)


def _normalize_nested_mapping(values, *, name: str) -> dict[str, dict[str, Decimal]]:
    if not isinstance(values, Mapping):
        raise TypeError(f"{name} must be a mapping")
    normalized: dict[str, dict[str, Decimal]] = {}
    for raw_key, raw_value in values.items():
        key = _identity_key(raw_key, name=name)
        if key in normalized:
            raise ValueError(f"{name} keys must be unique after normalization")
        normalized[key] = _normalize_mapping(
            raw_value,
            name=f"{name}[{key}]",
            parser=lambda value, factor: _decimal(
                value,
                name=f"{name}[{key}][{factor}]",
            ),
        )
    return normalized


@dataclass(frozen=True)
class RiskIntent:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    expected_state_version: int
    reduce_only: bool = False
    action: str = "TRADE"
    instrument_type: str = "GENERIC"

    @classmethod
    def create(
        cls,
        *,
        symbol: str,
        side: str,
        quantity,
        price,
        expected_state_version: int,
        reduce_only: bool = False,
        action: str = "TRADE",
        instrument_type: str = "GENERIC",
    ) -> "RiskIntent":
        if not isinstance(symbol, str) or not symbol.strip():
            raise ValueError("symbol is required")
        normalized_side = side.upper() if isinstance(side, str) else ""
        if normalized_side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        if not isinstance(expected_state_version, int) or isinstance(expected_state_version, bool) or expected_state_version < 0:
            raise ValueError("expected_state_version must be a non-negative integer")
        if not isinstance(reduce_only, bool):
            raise TypeError("reduce_only must be a boolean")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("action is required")
        normalized_action = action.strip().upper()
        if normalized_action not in RISK_ACTIONS:
            raise ValueError(f"Unsupported risk action: {normalized_action}")
        if normalized_action in {"REDUCE", "FLATTEN"} and not reduce_only:
            raise ValueError(f"{normalized_action} action requires reduce_only")
        if not isinstance(instrument_type, str) or not instrument_type.strip():
            raise ValueError("instrument_type is required")
        normalized_instrument_type = instrument_type.strip().upper()
        if normalized_instrument_type not in RISK_INSTRUMENT_TYPES:
            raise ValueError(
                f"Unsupported risk instrument type: {normalized_instrument_type}"
            )
        if normalized_action == "EXERCISE" and normalized_instrument_type != "OPTION":
            raise ValueError("EXERCISE action requires OPTION instrument_type")
        return cls(
            symbol=symbol.strip(),
            side=normalized_side,
            quantity=_positive(quantity, name="quantity"),
            price=_positive(price, name="price"),
            expected_state_version=expected_state_version,
            reduce_only=reduce_only,
            action=normalized_action,
            instrument_type=normalized_instrument_type,
        )


@dataclass(frozen=True)
class RiskPolicy:
    max_abs_position: Decimal
    max_single_notional: Decimal
    max_gross_leverage: Decimal
    max_net_leverage: Decimal
    max_daily_loss: Decimal
    max_drawdown_fraction: Decimal
    max_data_age_seconds: Decimal
    max_fx_age_seconds: Decimal
    min_margin_headroom: Decimal
    max_stress_loss: Decimal
    max_asset_concentration_fraction: Decimal | None = None
    max_venue_concentration_fraction: Decimal | None = None
    max_order_participation_fraction: Decimal | None = None
    max_abs_factor_exposure: Decimal | None = None
    max_spread_fraction: Decimal | None = None
    max_slippage_fraction: Decimal | None = None
    max_clock_age_seconds: Decimal | None = None
    allowed_actions: tuple[str, ...] | None = None
    require_settlement_evidence: bool = False
    require_option_exercise_evidence: bool = False
    min_futures_delivery_headroom_seconds: Decimal | None = None

    @classmethod
    def create(
        cls,
        *,
        max_abs_position,
        max_single_notional,
        max_gross_leverage,
        max_net_leverage,
        max_daily_loss,
        max_drawdown_fraction,
        max_data_age_seconds,
        max_fx_age_seconds,
        min_margin_headroom,
        max_stress_loss,
        max_asset_concentration_fraction=None,
        max_venue_concentration_fraction=None,
        max_order_participation_fraction=None,
        max_abs_factor_exposure=None,
        max_spread_fraction=None,
        max_slippage_fraction=None,
        max_clock_age_seconds=None,
        allowed_actions: Sequence[str] | None = None,
        require_settlement_evidence: bool = False,
        require_option_exercise_evidence: bool = False,
        min_futures_delivery_headroom_seconds=None,
    ) -> "RiskPolicy":
        values = {
            "max_abs_position": _positive(max_abs_position, name="max_abs_position"),
            "max_single_notional": _positive(max_single_notional, name="max_single_notional"),
            "max_gross_leverage": _positive(max_gross_leverage, name="max_gross_leverage"),
            "max_net_leverage": _positive(max_net_leverage, name="max_net_leverage"),
            "max_daily_loss": _positive(max_daily_loss, name="max_daily_loss", allow_zero=True),
            "max_drawdown_fraction": _positive(max_drawdown_fraction, name="max_drawdown_fraction", allow_zero=True),
            "max_data_age_seconds": _positive(max_data_age_seconds, name="max_data_age_seconds", allow_zero=True),
            "max_fx_age_seconds": _positive(max_fx_age_seconds, name="max_fx_age_seconds", allow_zero=True),
            "min_margin_headroom": _positive(min_margin_headroom, name="min_margin_headroom", allow_zero=True),
            "max_stress_loss": _positive(max_stress_loss, name="max_stress_loss", allow_zero=True),
        }
        if values["max_drawdown_fraction"] > 1:
            raise ValueError("max_drawdown_fraction cannot exceed 1")

        optional_limits: dict[str, Decimal | None] = {}
        for name, raw_value in (
            ("max_asset_concentration_fraction", max_asset_concentration_fraction),
            ("max_venue_concentration_fraction", max_venue_concentration_fraction),
            ("max_order_participation_fraction", max_order_participation_fraction),
            ("max_spread_fraction", max_spread_fraction),
            ("max_slippage_fraction", max_slippage_fraction),
        ):
            if raw_value is None:
                optional_limits[name] = None
                continue
            fraction = _positive(raw_value, name=name, allow_zero=True)
            if fraction > 1:
                raise ValueError(f"{name} cannot exceed 1")
            optional_limits[name] = fraction

        factor_limit = (
            None
            if max_abs_factor_exposure is None
            else _positive(
                max_abs_factor_exposure,
                name="max_abs_factor_exposure",
                allow_zero=True,
            )
        )
        clock_limit = (
            None
            if max_clock_age_seconds is None
            else _positive(
                max_clock_age_seconds,
                name="max_clock_age_seconds",
                allow_zero=True,
            )
        )
        normalized_allowed_actions = (
            None
            if allowed_actions is None
            else _normalize_actions(allowed_actions, name="allowed_actions")
        )
        if not isinstance(require_settlement_evidence, bool):
            raise TypeError("require_settlement_evidence must be a boolean")
        if not isinstance(require_option_exercise_evidence, bool):
            raise TypeError("require_option_exercise_evidence must be a boolean")
        delivery_headroom = (
            None
            if min_futures_delivery_headroom_seconds is None
            else _positive(
                min_futures_delivery_headroom_seconds,
                name="min_futures_delivery_headroom_seconds",
                allow_zero=True,
            )
        )
        return cls(
            **values,
            **optional_limits,
            max_abs_factor_exposure=factor_limit,
            max_clock_age_seconds=clock_limit,
            allowed_actions=normalized_allowed_actions,
            require_settlement_evidence=require_settlement_evidence,
            require_option_exercise_evidence=require_option_exercise_evidence,
            min_futures_delivery_headroom_seconds=delivery_headroom,
        )


@dataclass(frozen=True)
class RiskContext:
    state_version: int
    equity: Decimal
    positions: Mapping[str, Decimal]
    marks: Mapping[str, Decimal]
    reserved_position_delta: Mapping[str, Decimal]
    daily_pnl: Decimal
    drawdown_fraction: Decimal
    market_data_age_seconds: Decimal
    fx_age_seconds: Mapping[str, Decimal]
    margin_headroom: Decimal
    capability_allowed: bool
    borrow_available: bool | None
    stress_scenarios: Sequence[Mapping[str, Decimal]]
    asset_buckets: Mapping[str, str] | None = None
    venues: Mapping[str, str] | None = None
    liquidity_capacity: Mapping[str, Decimal] | None = None
    factor_loadings: Mapping[str, Mapping[str, Decimal]] | None = None
    spread_fraction: Mapping[str, Decimal] | None = None
    slippage_fraction: Mapping[str, Decimal] | None = None
    clock_age_seconds: Decimal | None = None
    settlement_allowed: bool | None = None
    option_deliverable_verified: bool | None = None
    option_exercise_cash_required: Decimal | None = None
    option_exercise_cash_available: Decimal | None = None
    futures_delivery_headroom_seconds: Mapping[str, Decimal] | None = None

    @classmethod
    def create(
        cls,
        *,
        state_version: int,
        equity,
        positions: Mapping[str, object],
        marks: Mapping[str, object],
        reserved_position_delta: Mapping[str, object] | None = None,
        daily_pnl=0,
        drawdown_fraction=0,
        market_data_age_seconds=0,
        fx_age_seconds: Mapping[str, object] | None = None,
        margin_headroom=1,
        capability_allowed: bool = True,
        borrow_available: bool | None = True,
        stress_scenarios: Sequence[Mapping[str, object]] = (),
        asset_buckets: Mapping[str, str] | None = None,
        venues: Mapping[str, str] | None = None,
        liquidity_capacity: Mapping[str, object] | None = None,
        factor_loadings: Mapping[str, Mapping[str, object]] | None = None,
        spread_fraction: Mapping[str, object] | None = None,
        slippage_fraction: Mapping[str, object] | None = None,
        clock_age_seconds=None,
        settlement_allowed: bool | None = None,
        option_deliverable_verified: bool | None = None,
        option_exercise_cash_required=None,
        option_exercise_cash_available=None,
        futures_delivery_headroom_seconds: Mapping[str, object] | None = None,
    ) -> "RiskContext":
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 0:
            raise ValueError("state_version must be a non-negative integer")
        normalized_positions = _normalize_mapping(
            positions,
            name="positions",
            parser=lambda value, key: _decimal(value, name=f"position {key}"),
        )
        normalized_marks = _normalize_mapping(
            marks,
            name="marks",
            parser=lambda value, key: _positive(value, name=f"mark {key}"),
        )
        normalized_reserved = _normalize_mapping(
            reserved_position_delta or {},
            name="reserved_position_delta",
            parser=lambda value, key: _decimal(
                value,
                name=f"reserved position {key}",
            ),
        )
        normalized_fx = _normalize_mapping(
            fx_age_seconds or {},
            name="fx_age_seconds",
            parser=lambda value, key: _positive(
                value,
                name=f"FX age {key}",
                allow_zero=True,
            ),
        )
        normalized_asset_buckets = _normalize_text_mapping(
            asset_buckets or {},
            name="asset_buckets",
        )
        normalized_venues = _normalize_text_mapping(
            venues or {},
            name="venues",
        )
        normalized_liquidity = _normalize_mapping(
            liquidity_capacity or {},
            name="liquidity_capacity",
            parser=lambda value, key: _positive(
                value,
                name=f"liquidity capacity {key}",
                allow_zero=True,
            ),
        )
        normalized_factor_loadings = _normalize_nested_mapping(
            factor_loadings or {},
            name="factor_loadings",
        )
        normalized_spread = _normalize_mapping(
            spread_fraction or {},
            name="spread_fraction",
            parser=lambda value, key: _positive(
                value,
                name=f"spread fraction {key}",
                allow_zero=True,
            ),
        )
        normalized_slippage = _normalize_mapping(
            slippage_fraction or {},
            name="slippage_fraction",
            parser=lambda value, key: _positive(
                value,
                name=f"slippage fraction {key}",
                allow_zero=True,
            ),
        )
        normalized_clock_age = (
            None
            if clock_age_seconds is None
            else _positive(
                clock_age_seconds,
                name="clock_age_seconds",
                allow_zero=True,
            )
        )
        normalized_exercise_required = (
            None
            if option_exercise_cash_required is None
            else _positive(
                option_exercise_cash_required,
                name="option_exercise_cash_required",
                allow_zero=True,
            )
        )
        normalized_exercise_available = (
            None
            if option_exercise_cash_available is None
            else _positive(
                option_exercise_cash_available,
                name="option_exercise_cash_available",
                allow_zero=True,
            )
        )
        normalized_delivery_headroom = _normalize_mapping(
            futures_delivery_headroom_seconds or {},
            name="futures_delivery_headroom_seconds",
            parser=lambda value, key: _decimal(
                value,
                name=f"futures delivery headroom {key}",
            ),
        )
        if not isinstance(stress_scenarios, Sequence) or isinstance(
            stress_scenarios,
            (str, bytes),
        ):
            raise TypeError("stress_scenarios must be a sequence of mappings")
        scenarios = tuple(
            _normalize_mapping(
                scenario,
                name=f"stress_scenarios[{index}]",
                parser=lambda value, key: _decimal(
                    value,
                    name=f"stress shock {key}",
                ),
            )
            for index, scenario in enumerate(stress_scenarios)
        )
        normalized_drawdown = _positive(
            drawdown_fraction,
            name="drawdown_fraction",
            allow_zero=True,
        )
        if normalized_drawdown > 1:
            raise ValueError("drawdown_fraction cannot exceed 1")
        if not isinstance(capability_allowed, bool):
            raise TypeError("capability_allowed must be a boolean")
        if borrow_available is not None and not isinstance(borrow_available, bool):
            raise TypeError("borrow_available must be a boolean or None")
        if settlement_allowed is not None and not isinstance(settlement_allowed, bool):
            raise TypeError("settlement_allowed must be a boolean or None")
        if (
            option_deliverable_verified is not None
            and not isinstance(option_deliverable_verified, bool)
        ):
            raise TypeError("option_deliverable_verified must be a boolean or None")
        return cls(
            state_version=state_version,
            equity=_positive(equity, name="equity"),
            positions=normalized_positions,
            marks=normalized_marks,
            reserved_position_delta=normalized_reserved,
            daily_pnl=_decimal(daily_pnl, name="daily_pnl"),
            drawdown_fraction=normalized_drawdown,
            market_data_age_seconds=_positive(market_data_age_seconds, name="market_data_age_seconds", allow_zero=True),
            fx_age_seconds=normalized_fx,
            margin_headroom=_positive(margin_headroom, name="margin_headroom", allow_zero=True),
            capability_allowed=capability_allowed,
            borrow_available=borrow_available,
            stress_scenarios=scenarios,
            asset_buckets=normalized_asset_buckets,
            venues=normalized_venues,
            liquidity_capacity=normalized_liquidity,
            factor_loadings=normalized_factor_loadings,
            spread_fraction=normalized_spread,
            slippage_fraction=normalized_slippage,
            clock_age_seconds=normalized_clock_age,
            settlement_allowed=settlement_allowed,
            option_deliverable_verified=option_deliverable_verified,
            option_exercise_cash_required=normalized_exercise_required,
            option_exercise_cash_available=normalized_exercise_available,
            futures_delivery_headroom_seconds=normalized_delivery_headroom,
        )


@dataclass(frozen=True)
class RiskRuleResult:
    rule: str
    passed: bool
    observed: str
    limit: str
    reason: str


@dataclass(frozen=True)
class RiskDecision:
    admitted: bool
    resulting_position: Decimal
    gross_leverage: Decimal
    net_leverage: Decimal
    worst_stress_loss: Decimal
    rules: tuple[RiskRuleResult, ...]


def evaluate_risk(intent: RiskIntent, context: RiskContext, policy: RiskPolicy) -> RiskDecision:
    if intent.symbol not in context.marks:
        raise ValueError(f"Missing mark for {intent.symbol}")
    signed = intent.quantity if intent.side == "BUY" else -intent.quantity
    current = context.positions.get(intent.symbol, Decimal("0"))
    reserved = context.reserved_position_delta.get(intent.symbol, Decimal("0"))
    base_position = current + reserved
    resulting = base_position + signed

    base_positions = dict(context.positions)
    for symbol, delta in context.reserved_position_delta.items():
        base_positions[symbol] = base_positions.get(symbol, Decimal("0")) + delta
    projected_positions = dict(base_positions)
    projected_positions[intent.symbol] = projected_positions.get(intent.symbol, Decimal("0")) + signed

    missing_marks = [symbol for symbol, qty in projected_positions.items() if qty != 0 and symbol not in context.marks]
    if missing_marks:
        raise ValueError(f"Missing marks for positions: {', '.join(sorted(missing_marks))}")

    base_notionals = {
        symbol: qty * context.marks[symbol]
        for symbol, qty in base_positions.items()
        if qty != 0
    }
    notionals = {
        symbol: qty * context.marks[symbol]
        for symbol, qty in projected_positions.items()
        if qty != 0
    }
    base_gross = sum((abs(value) for value in base_notionals.values()), Decimal("0"))
    base_net = abs(sum(base_notionals.values(), Decimal("0")))
    gross = sum((abs(value) for value in notionals.values()), Decimal("0"))
    net = abs(sum(notionals.values(), Decimal("0")))
    gross_leverage = gross / context.equity
    net_leverage = net / context.equity
    mark_notional = abs(resulting * context.marks[intent.symbol])
    intent_notional = intent.quantity * intent.price
    single_notional = max(mark_notional, intent_notional)

    asset_concentration = Decimal("0")
    asset_concentration_complete = True
    missing_asset_buckets: set[str] = set()
    if policy.max_asset_concentration_fraction is not None and gross > 0:
        asset_groups: dict[str, Decimal] = {}
        asset_map = context.asset_buckets or {}
        for symbol, notional in notionals.items():
            bucket = asset_map.get(symbol)
            if bucket is None:
                missing_asset_buckets.add(symbol)
                continue
            asset_groups[bucket] = asset_groups.get(bucket, Decimal("0")) + abs(notional)
        asset_concentration_complete = not missing_asset_buckets
        if asset_concentration_complete and asset_groups:
            asset_concentration = max(asset_groups.values()) / gross

    venue_concentration = Decimal("0")
    venue_concentration_complete = True
    missing_venues: set[str] = set()
    if policy.max_venue_concentration_fraction is not None and gross > 0:
        venue_groups: dict[str, Decimal] = {}
        venue_map = context.venues or {}
        for symbol, notional in notionals.items():
            venue = venue_map.get(symbol)
            if venue is None:
                missing_venues.add(symbol)
                continue
            venue_groups[venue] = venue_groups.get(venue, Decimal("0")) + abs(notional)
        venue_concentration_complete = not missing_venues
        if venue_concentration_complete and venue_groups:
            venue_concentration = max(venue_groups.values()) / gross

    participation = Decimal("0")
    participation_evidenced = True
    if policy.max_order_participation_fraction is not None:
        liquidity_map = context.liquidity_capacity or {}
        capacity = liquidity_map.get(intent.symbol)
        if capacity is None or capacity <= 0:
            participation_evidenced = False
        else:
            participation = intent.quantity / capacity

    spread_observation = (context.spread_fraction or {}).get(intent.symbol)
    slippage_observation = (context.slippage_fraction or {}).get(intent.symbol)

    factor_exposure = Decimal("0")
    base_factor_exposure = Decimal("0")
    factor_exposure_complete = True
    missing_factor_loadings: set[str] = set()
    if policy.max_abs_factor_exposure is not None:
        loading_map = context.factor_loadings or {}
        projected_factors: dict[str, Decimal] = {}
        base_factors: dict[str, Decimal] = {}
        for symbol, notional in notionals.items():
            symbol_loadings = loading_map.get(symbol)
            if not symbol_loadings:
                missing_factor_loadings.add(symbol)
                continue
            for factor, loading in symbol_loadings.items():
                projected_factors[factor] = (
                    projected_factors.get(factor, Decimal("0"))
                    + notional * loading
                )
        factor_exposure_complete = not missing_factor_loadings
        if factor_exposure_complete:
            factor_exposure = max(
                (abs(value) for value in projected_factors.values()),
                default=Decimal("0"),
            )
        for symbol, notional in base_notionals.items():
            symbol_loadings = loading_map.get(symbol)
            if not symbol_loadings:
                continue
            for factor, loading in symbol_loadings.items():
                base_factors[factor] = (
                    base_factors.get(factor, Decimal("0"))
                    + notional * loading
                )
        base_factor_exposure = max(
            (abs(value) for value in base_factors.values()),
            default=Decimal("0"),
        )

    stress_symbols = set(notionals)
    stress_coverage_complete = bool(context.stress_scenarios) or not stress_symbols
    missing_stress_symbols: set[str] = set()
    for scenario in context.stress_scenarios:
        missing_stress_symbols.update(stress_symbols - set(scenario))
    if missing_stress_symbols:
        stress_coverage_complete = False

    base_worst_stress_loss = Decimal("0")
    worst_stress_loss = Decimal("0")
    if stress_coverage_complete:
        for scenario in context.stress_scenarios:
            base_pnl = sum(
                (
                    notional * scenario.get(symbol, Decimal("0"))
                    for symbol, notional in base_notionals.items()
                ),
                Decimal("0"),
            )
            pnl = sum(
                (
                    notional * scenario[symbol]
                    for symbol, notional in notionals.items()
                ),
                Decimal("0"),
            )
            base_worst_stress_loss = max(base_worst_stress_loss, -base_pnl)
            worst_stress_loss = max(worst_stress_loss, -pnl)

    reduces_absolute_exposure = (
        abs(resulting) < abs(base_position)
        and base_position * resulting >= 0
    )
    protective_reduction = (
        intent.reduce_only
        and reduces_absolute_exposure
        and gross < base_gross
        and net <= base_net
        and worst_stress_loss <= base_worst_stress_loss
    )

    rules: list[RiskRuleResult] = []

    def add(rule: str, passed: bool, observed, limit, reason: str) -> None:
        rules.append(RiskRuleResult(rule, passed, str(observed), str(limit), reason))

    add(
        "state_version",
        intent.expected_state_version == context.state_version,
        context.state_version,
        intent.expected_state_version,
        "authoritative state version must match the intent",
    )
    add(
        "capability",
        context.capability_allowed,
        context.capability_allowed,
        True,
        "account/instrument capability must be currently evidenced",
    )
    if policy.allowed_actions is not None:
        add(
            "allowed_action",
            intent.action in policy.allowed_actions,
            intent.action,
            ",".join(policy.allowed_actions),
            "intent action class must be explicitly permitted by risk policy",
        )
    if policy.require_settlement_evidence:
        add(
            "settlement",
            context.settlement_allowed is True,
            (
                context.settlement_allowed
                if context.settlement_allowed is not None
                else "UNKNOWN"
            ),
            True,
            "settlement state must affirmatively permit the requested action",
        )
    if (
        policy.require_option_exercise_evidence
        and intent.action == "EXERCISE"
    ):
        add(
            "option_deliverable",
            context.option_deliverable_verified is True,
            (
                context.option_deliverable_verified
                if context.option_deliverable_verified is not None
                else "UNKNOWN"
            ),
            True,
            "option exercise requires a verified current deliverable",
        )
        exercise_funding_known = (
            context.option_exercise_cash_required is not None
            and context.option_exercise_cash_available is not None
        )
        add(
            "option_exercise_funding",
            exercise_funding_known
            and context.option_exercise_cash_available
            >= context.option_exercise_cash_required,
            (
                context.option_exercise_cash_available
                if exercise_funding_known
                else "UNKNOWN"
            ),
            (
                context.option_exercise_cash_required
                if context.option_exercise_cash_required is not None
                else "UNKNOWN"
            ),
            "option exercise requires evidenced buying power for its obligation",
        )
    add(
        "market_freshness",
        context.market_data_age_seconds <= policy.max_data_age_seconds,
        context.market_data_age_seconds,
        policy.max_data_age_seconds,
        "market data must be fresh enough for admission",
    )
    if policy.max_clock_age_seconds is not None:
        add(
            "clock_freshness",
            context.clock_age_seconds is not None
            and context.clock_age_seconds <= policy.max_clock_age_seconds,
            context.clock_age_seconds if context.clock_age_seconds is not None else "UNKNOWN",
            policy.max_clock_age_seconds,
            "clock synchronization evidence must be fresh enough for admission",
        )
    stale_fx = max(context.fx_age_seconds.values(), default=Decimal("0"))
    add(
        "fx_freshness",
        stale_fx <= policy.max_fx_age_seconds,
        stale_fx,
        policy.max_fx_age_seconds,
        "FX inputs must be fresh enough for valuation",
    )
    add(
        "position_limit",
        abs(resulting) <= policy.max_abs_position or protective_reduction,
        abs(resulting),
        policy.max_abs_position,
        "resulting absolute position must stay within policy",
    )
    add(
        "single_notional",
        single_notional <= policy.max_single_notional or protective_reduction,
        single_notional,
        policy.max_single_notional,
        "single-instrument notional must stay within policy",
    )
    add(
        "gross_leverage",
        gross_leverage <= policy.max_gross_leverage or protective_reduction,
        gross_leverage,
        policy.max_gross_leverage,
        "gross leverage must stay within policy",
    )
    add(
        "net_leverage",
        net_leverage <= policy.max_net_leverage or protective_reduction,
        net_leverage,
        policy.max_net_leverage,
        "net leverage must stay within policy",
    )
    if policy.max_asset_concentration_fraction is not None:
        add(
            "asset_concentration",
            asset_concentration_complete
            and asset_concentration <= policy.max_asset_concentration_fraction,
            (
                asset_concentration
                if asset_concentration_complete
                else "MISSING:" + ",".join(sorted(missing_asset_buckets))
            ),
            policy.max_asset_concentration_fraction,
            "projected gross exposure by asset bucket must stay within policy",
        )
    if policy.max_venue_concentration_fraction is not None:
        add(
            "venue_concentration",
            venue_concentration_complete
            and venue_concentration <= policy.max_venue_concentration_fraction,
            (
                venue_concentration
                if venue_concentration_complete
                else "MISSING:" + ",".join(sorted(missing_venues))
            ),
            policy.max_venue_concentration_fraction,
            "projected gross exposure by venue must stay within policy",
        )
    if policy.max_order_participation_fraction is not None:
        add(
            "liquidity_participation",
            participation_evidenced
            and participation <= policy.max_order_participation_fraction,
            participation if participation_evidenced else "UNKNOWN",
            policy.max_order_participation_fraction,
            "order quantity must stay within evidenced liquidity participation policy",
        )
    if policy.max_spread_fraction is not None:
        add(
            "spread",
            spread_observation is not None
            and spread_observation <= policy.max_spread_fraction,
            spread_observation if spread_observation is not None else "UNKNOWN",
            policy.max_spread_fraction,
            "evidenced execution spread must stay within policy",
        )
    if policy.max_slippage_fraction is not None:
        add(
            "slippage",
            slippage_observation is not None
            and slippage_observation <= policy.max_slippage_fraction,
            slippage_observation if slippage_observation is not None else "UNKNOWN",
            policy.max_slippage_fraction,
            "evidenced execution slippage must stay within policy",
        )
    if policy.max_abs_factor_exposure is not None:
        add(
            "factor_exposure",
            factor_exposure_complete
            and (
                factor_exposure <= policy.max_abs_factor_exposure
                or (
                    protective_reduction
                    and factor_exposure < base_factor_exposure
                )
            ),
            (
                factor_exposure
                if factor_exposure_complete
                else "MISSING:" + ",".join(sorted(missing_factor_loadings))
            ),
            policy.max_abs_factor_exposure,
            "correlated factor exposure must stay within the independent policy bound",
        )
    daily_loss = max(-context.daily_pnl, Decimal("0"))
    add(
        "daily_loss",
        daily_loss <= policy.max_daily_loss or protective_reduction,
        daily_loss,
        policy.max_daily_loss,
        "daily loss must stay within policy",
    )
    add(
        "drawdown",
        context.drawdown_fraction <= policy.max_drawdown_fraction or protective_reduction,
        context.drawdown_fraction,
        policy.max_drawdown_fraction,
        "drawdown must stay within policy",
    )
    add(
        "margin_headroom",
        context.margin_headroom >= policy.min_margin_headroom or protective_reduction,
        context.margin_headroom,
        policy.min_margin_headroom,
        "margin headroom must meet policy floor",
    )
    add(
        "stress_coverage",
        stress_coverage_complete,
        ",".join(sorted(missing_stress_symbols)) if missing_stress_symbols else len(context.stress_scenarios),
        "complete non-empty stress evidence for every non-zero projected position",
        "stress admission must fail closed when scenarios are missing or incomplete",
    )
    add(
        "stress_loss",
        stress_coverage_complete
        and (worst_stress_loss <= policy.max_stress_loss or protective_reduction),
        worst_stress_loss if stress_coverage_complete else "UNKNOWN",
        policy.max_stress_loss,
        "worst configured stress loss must stay within policy",
    )

    opens_short = resulting < 0 and resulting < base_position
    borrow_ok = not opens_short or context.borrow_available is True
    add(
        "short_borrow",
        borrow_ok,
        context.borrow_available,
        True,
        "new or increased short exposure requires affirmative borrow evidence",
    )

    if (
        policy.min_futures_delivery_headroom_seconds is not None
        and intent.instrument_type == "FUTURE"
    ):
        delivery_headroom = (context.futures_delivery_headroom_seconds or {}).get(
            intent.symbol
        )
        delivery_ok = protective_reduction or (
            delivery_headroom is not None
            and delivery_headroom >= policy.min_futures_delivery_headroom_seconds
        )
        add(
            "futures_delivery_cutoff",
            delivery_ok,
            (
                "RISK_REDUCTION"
                if protective_reduction
                else (
                    delivery_headroom
                    if delivery_headroom is not None
                    else "UNKNOWN"
                )
            ),
            policy.min_futures_delivery_headroom_seconds,
            "new futures risk requires sufficient evidenced delivery headroom",
        )

    reduce_only_ok = not intent.reduce_only or reduces_absolute_exposure
    add(
        "reduce_only",
        reduce_only_ok,
        abs(resulting),
        abs(base_position),
        "reduce-only intent must not increase actual portfolio exposure",
    )

    return RiskDecision(
        admitted=all(rule.passed for rule in rules),
        resulting_position=resulting,
        gross_leverage=gross_leverage,
        net_leverage=net_leverage,
        worst_stress_loss=worst_stress_loss,
        rules=tuple(rules),
    )
