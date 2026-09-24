"""Independent deterministic risk admission for the AutoTrade foundation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


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


@dataclass(frozen=True)
class RiskIntent:
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    expected_state_version: int
    reduce_only: bool = False

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
        return cls(
            symbol=symbol.strip(),
            side=normalized_side,
            quantity=_positive(quantity, name="quantity"),
            price=_positive(price, name="price"),
            expected_state_version=expected_state_version,
            reduce_only=reduce_only,
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
        return cls(**values)


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
    add(
        "market_freshness",
        context.market_data_age_seconds <= policy.max_data_age_seconds,
        context.market_data_age_seconds,
        policy.max_data_age_seconds,
        "market data must be fresh enough for admission",
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
