"""Causal long-only trend-regime screen with trailing risk exits.

The experiment is intentionally small and deterministic.  Every decision uses
completed closes only and executes at the following bar open.  CASH and HOLD
remain explicit controls, and FINAL is not read until TRAIN and DEVELOPMENT
both beat those controls after costs.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from research.autotrade_research.strategies.fast_screen import Costs, decimal, digest, validate_bars


VERSION = "trend-regime-trailing-screen-v3-three-way"
EVIDENCE = "ZERO_RESEARCH_SCREENING_ONLY_APPROXIMATE_EXECUTION"
CANDIDATES = ("CASH", "HOLD", "TREND_REGIME_TRAILING")


@dataclass(frozen=True)
class TrendRegime:
    fast_lookback: int = 24
    slow_lookback: int = 168
    entry_threshold: str = "0.004"
    slow_threshold: str = "0.01"
    exit_threshold: str = "0.004"
    trailing_stop_fraction: str = "0.03"
    min_hold_bars: int = 6
    max_hold_bars: int = 336
    allow_short: bool = False
    short_entry_threshold: str = "0.03"
    short_slow_threshold: str = "0.08"
    short_carry_bps_per_day: str = "2"

    def validate(self):
        if (type(self.fast_lookback) is not int or type(self.slow_lookback) is not int
                or not 2 <= self.fast_lookback <= self.slow_lookback <= 2000):
            raise ValueError("trend lookbacks outside 2..2000 or not ordered")
        for name in ("entry_threshold", "slow_threshold", "exit_threshold",
                     "short_entry_threshold", "short_slow_threshold"):
            value = decimal(getattr(self, name))
            if not 0 <= value <= Decimal("0.5"):
                raise ValueError(f"{name} outside [0,0.5]")
        trailing = decimal(self.trailing_stop_fraction)
        if not 0 < trailing <= Decimal("0.5"):
            raise ValueError("trailing stop outside (0,0.5]")
        if (type(self.min_hold_bars) is not int or type(self.max_hold_bars) is not int
                or not 1 <= self.min_hold_bars <= self.max_hold_bars <= 10000):
            raise ValueError("holding bounds invalid")
        if type(self.allow_short) is not bool:
            raise ValueError("allow_short must be boolean")
        carry = decimal(self.short_carry_bps_per_day)
        if not 0 <= carry <= Decimal(1000):
            raise ValueError("short carry must be in [0,1000] bps per day")


def simulate(bars, *, candidate, costs=Costs(), configuration=TrendRegime(), trace=False):
    """Simulate one causal candidate with next-open execution and full costs."""
    costs.validate()
    configuration.validate()
    if candidate not in CANDIDATES:
        raise ValueError("unknown trend-regime candidate")
    validate_bars(bars, require_ohlcv=True)
    with localcontext() as context:
        context.prec = 50
        cash = decimal(costs.initial_cash)
        initial = peak = cash
        quantity = fees = financing = drawdown = Decimal(0)
        fraction = decimal(costs.capital_fraction)
        fee = decimal(costs.fee_bps) / 10000
        adverse = (decimal(costs.spread_bps) / 2 + decimal(costs.slippage_bps)) / 10000
        pending = "BUY" if candidate == "HOLD" else "HOLD"
        pending_reason = "PRECOMMITTED_PARTITION_START" if candidate == "HOLD" else None
        closes = deque(maxlen=configuration.slow_lookback + 1)
        fills = []
        evidence = sha256()
        count = cycles = entry_signals = exit_signals = holding_bars = 0
        favorable_water = None
        long_cycles = short_cycles = 0
        exit_reasons = {}

        def mark(price):
            nonlocal peak, drawdown
            if quantity >= 0:
                equity = cash + quantity * price * (1-adverse) * (1-fee)
            else:
                equity = cash + quantity * price * (1+adverse) * (1+fee)
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak-equity)/peak)

        def execute(action, reference, at, signal_at, reason=None):
            nonlocal cash, quantity, fees, count, cycles, favorable_water
            nonlocal long_cycles, short_cycles
            if action == "BUY" and quantity == 0:
                price = reference * (1+adverse)
                budget = cash * fraction
                quantity = (budget / (price * (1+fee))).quantize(
                    Decimal("1e-18"), rounding=ROUND_DOWN)
                if quantity == 0:
                    return None
                value = quantity * price
                charge = value * fee
                cash -= value + charge
                favorable_water = price
            elif action == "SELL" and quantity > 0:
                price = reference * (1-adverse)
                value = quantity * price
                charge = value * fee
                cash += value - charge
                quantity = Decimal(0)
                cycles += 1
                long_cycles += 1
                favorable_water = None
            elif action == "SHORT" and quantity == 0:
                price = reference * (1-adverse)
                budget = cash * fraction
                opened = (budget / price).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                if opened == 0:
                    return None
                value = opened * price
                charge = value * fee
                cash += value - charge
                quantity = -opened
                favorable_water = price
            elif action == "COVER" and quantity < 0:
                price = reference * (1+adverse)
                value = -quantity * price
                charge = value * fee
                cash -= value + charge
                quantity = Decimal(0)
                cycles += 1
                short_cycles += 1
                favorable_water = None
            else:
                return None
            if cash < 0:
                raise ArithmeticError("trend-regime capital conservation failed")
            fees += charge
            count += 1
            row = dict(action=action, at=at.isoformat(), signal_at=signal_at,
                       reason=reason, price=str(price), fee=str(charge),
                       cash=str(cash), quantity=str(quantity))
            evidence.update(json.dumps(row, sort_keys=True).encode())
            if trace:
                fills.append(row)
            return action

        for index, bar in enumerate(bars):
            action = execute(
                pending, decimal(bar["open"]), bar["opened_at"],
                (bars[index-1]["closed_at"].isoformat() if index else None),
                pending_reason,
            )
            if action in ("BUY", "SELL"):
                holding_bars = 0
            elif action in ("SHORT", "COVER"):
                holding_bars = 0
            pending = "HOLD"
            pending_reason = None
            mark(decimal(bar["open"]))
            close = decimal(bar["close"])
            mark(close)
            if quantity != 0:
                holding_bars += 1
                favorable_water = (max(favorable_water, close) if quantity > 0
                                   else min(favorable_water, close))
            if quantity < 0:
                carrying = (-quantity * close
                            * decimal(configuration.short_carry_bps_per_day)
                            / Decimal(10000) / Decimal(24))
                cash -= carrying
                financing += carrying
                if cash < 0:
                    raise ArithmeticError("short carrying cost exhausted capital")
                mark(close)
            closes.append(close)
            if candidate in ("CASH", "HOLD") or len(closes) < closes.maxlen:
                continue
            values = list(closes)
            fast_return = (values[-1] - values[-1-configuration.fast_lookback]) / values[-1-configuration.fast_lookback]
            slow_return = (values[-1] - values[0]) / values[0]
            if quantity == 0:
                if (fast_return >= decimal(configuration.entry_threshold)
                        and slow_return >= decimal(configuration.slow_threshold)):
                    entry_signals += 1
                    pending = "BUY"
                    pending_reason = "FAST_AND_SLOW_TREND_ENTRY"
                elif (configuration.allow_short
                      and fast_return <= -decimal(configuration.short_entry_threshold)
                      and slow_return <= -decimal(configuration.short_slow_threshold)):
                    entry_signals += 1
                    pending = "SHORT"
                    pending_reason = "FAST_AND_SLOW_DOWNTREND_ENTRY"
                continue
            reason = None
            if holding_bars >= configuration.max_hold_bars:
                reason = "MAX_HOLD_EXIT"
            elif holding_bars >= configuration.min_hold_bars:
                if quantity > 0:
                    if close <= favorable_water * (1-decimal(configuration.trailing_stop_fraction)):
                        reason = "TRAILING_STOP_EXIT"
                    elif fast_return <= -decimal(configuration.exit_threshold):
                        reason = "FAST_TREND_EXIT"
                else:
                    if close >= favorable_water * (1+decimal(configuration.trailing_stop_fraction)):
                        reason = "SHORT_TRAILING_STOP_EXIT"
                    elif fast_return >= decimal(configuration.exit_threshold):
                        reason = "FAST_DOWNTREND_EXIT"
            if reason:
                exit_signals += 1
                exit_reasons[reason] = exit_reasons.get(reason, 0) + 1
                pending = "SELL" if quantity > 0 else "COVER"
                pending_reason = reason

        if quantity != 0:
            exit_reasons["PARTITION_END_EXIT"] = exit_reasons.get("PARTITION_END_EXIT", 0) + 1
        execute(("SELL" if quantity > 0 else "COVER"),
                decimal(bars[-1]["close"]), bars[-1]["closed_at"],
                "PRECOMMITTED_PARTITION_END", "PARTITION_END_EXIT")
        mark(decimal(bars[-1]["close"]))
        result = dict(candidate=candidate, observations=len(bars), final_cash=str(cash),
            net_pnl=str(cash-initial), return_fraction=str((cash-initial)/initial),
            max_drawdown_fraction=str(drawdown), fees=str(fees),
            financing_costs=str(financing), fills=count,
            closed_cycles=cycles, execution_digest=evidence.hexdigest(),
            long_cycles=long_cycles, short_cycles=short_cycles,
            entry_signals=entry_signals, exit_signals=exit_signals,
            exit_reasons=dict(sorted(exit_reasons.items())),
            first_open=bars[0]["opened_at"].isoformat(),
            last_close=bars[-1]["closed_at"].isoformat())
        if trace:
            result["trace"] = fills
        return result


def screen(bars, *, train_count, development_count, dataset_sha256,
           costs=Costs(), configuration=TrendRegime(), min_cycles=3,
           max_drawdown_fraction="0.20", checkpoint=None):
    """Gate TRAIN then DEVELOPMENT before consuming FINAL exactly once."""
    costs.validate()
    configuration.validate()
    if (type(train_count) is not int or type(development_count) is not int
            or train_count < 4 or development_count < 4
            or train_count + development_count > len(bars)-4):
        raise ValueError("TRAIN, DEVELOPMENT and FINAL must each contain four bars")
    if type(min_cycles) is not int or not 1 <= min_cycles <= 10000:
        raise ValueError("minimum cycles outside 1..10000")
    maximum_drawdown = decimal(max_drawdown_fraction)
    if not 0 <= maximum_drawdown <= 1:
        raise ValueError("maximum drawdown outside [0,1]")
    if (type(dataset_sha256) is not str or len(dataset_sha256) != 64
            or any(c not in "0123456789abcdef" for c in dataset_sha256)):
        raise ValueError("dataset identity must be a SHA-256")
    training_bars = bars[:train_count]
    validate_bars(training_bars, require_ohlcv=True)
    config = dict(version=VERSION, costs=asdict(costs), configuration=asdict(configuration),
        candidates=CANDIDATES, train_count=train_count, development_count=development_count,
        final_count=len(bars)-train_count-development_count, min_cycles=min_cycles,
        max_drawdown_fraction=str(maximum_drawdown), observations=len(bars),
        dataset_sha256=dataset_sha256,
        evaluation_policy="TRAIN_THEN_DEVELOPMENT_GATES_FINAL_UNTOUCHED_UNTIL_PASS",
        end_policy="FORCED_CLOSE_EACH_CONSUMED_PARTITION")
    config_sha = digest(config)
    run = checkpoint or (lambda key, producer: producer())

    def evaluate(partition, stage):
        return [run(f"{config_sha}-{stage}-{name}", lambda name=name:
                    simulate(partition, candidate=name, costs=costs,
                             configuration=configuration)) for name in CANDIDATES]

    training = evaluate(training_bars, "train")
    candidate = next(row for row in training if row["candidate"] == CANDIDATES[-1])
    controls = [row for row in training if row["candidate"] != CANDIDATES[-1]]
    train_rejections = []
    if candidate["closed_cycles"] < min_cycles:
        train_rejections.append("MINIMUM_TRAIN_CYCLES_NOT_MET")
    if Decimal(candidate["net_pnl"]) <= max(Decimal(row["net_pnl"]) for row in controls):
        train_rejections.append("TRAIN_DID_NOT_BEAT_CASH_AND_HOLD")
    if Decimal(candidate["max_drawdown_fraction"]) > maximum_drawdown:
        train_rejections.append("TRAIN_DRAWDOWN_LIMIT_EXCEEDED")
    development = []
    development_rejections = []
    evaluation_matrix = []
    evaluation = None
    holdout_consumed = False
    final_rejections = []
    if not train_rejections:
        development_bars = bars[train_count:train_count+development_count]
        validate_bars(development_bars, require_ohlcv=True)
        if development_bars[0]["opened_at"] != training_bars[-1]["closed_at"]:
            raise ValueError("TRAIN and DEVELOPMENT must be continuous")
        development = evaluate(development_bars, "development")
        candidate = next(row for row in development if row["candidate"] == CANDIDATES[-1])
        controls = [row for row in development if row["candidate"] != CANDIDATES[-1]]
        if candidate["closed_cycles"] < min_cycles:
            development_rejections.append("MINIMUM_DEVELOPMENT_CYCLES_NOT_MET")
        if Decimal(candidate["net_pnl"]) <= max(Decimal(row["net_pnl"]) for row in controls):
            development_rejections.append("DEVELOPMENT_DID_NOT_BEAT_CASH_AND_HOLD")
        if Decimal(candidate["max_drawdown_fraction"]) > maximum_drawdown:
            development_rejections.append("DEVELOPMENT_DRAWDOWN_LIMIT_EXCEEDED")
        if not development_rejections:
            final_bars = bars[train_count+development_count:]
            validate_bars(final_bars, require_ohlcv=True)
            if final_bars[0]["opened_at"] != development_bars[-1]["closed_at"]:
                raise ValueError("DEVELOPMENT and FINAL must be continuous")
            evaluation_matrix = evaluate(final_bars, "final")
            evaluation = next(row for row in evaluation_matrix if row["candidate"] == CANDIDATES[-1])
            holdout_consumed = True
            controls = [row for row in evaluation_matrix if row["candidate"] != CANDIDATES[-1]]
            if evaluation["closed_cycles"] < min_cycles:
                final_rejections.append("MINIMUM_FINAL_CYCLES_NOT_MET")
            if Decimal(evaluation["net_pnl"]) <= max(
                    Decimal(row["net_pnl"]) for row in controls):
                final_rejections.append("FINAL_DID_NOT_BEAT_CASH_AND_HOLD")
            if Decimal(evaluation["max_drawdown_fraction"]) > maximum_drawdown:
                final_rejections.append("FINAL_DRAWDOWN_LIMIT_EXCEEDED")
    prospective_qualification = holdout_consumed and not final_rejections
    result = dict(schema=VERSION, evidence_class=EVIDENCE, config=config,
        config_sha256=config_sha, training=training,
        training_rejection_reasons=train_rejections, development=development,
        development_rejection_reasons=development_rejections,
        final_rejection_reasons=final_rejections,
        prospective_qualification=prospective_qualification,
        selected_candidate=(CANDIDATES[-1] if prospective_qualification else "CASH"),
        evaluation=evaluation, evaluation_matrix=evaluation_matrix,
        holdout_consumed=holdout_consumed, network_orders=False, paid_model_calls=0,
        canonical_qualification=False,
        limitations=["NO_INTRABAR_DRAWDOWN", "NO_CAPACITY_OR_QUEUE_MODEL",
                     "NO_EXCHANGE_LOT_RULES", "FIXED_SLIPPAGE_DURING_HIGH_VOLATILITY",
                     "SPOT_PRICE_PROXY_FOR_SHORT_WITH_FIXED_CARRY_NOT_PERPETUAL_FUNDING",
                     "NO_LIQUIDATION_OR_MARGIN_CALL_MODEL",
                     "RETROSPECTIVE_BAR_AVAILABILITY", "NO_DEPLOYMENT_OR_PROFIT_CLAIM"])
    return {**result, "result_sha256": digest(result)}
