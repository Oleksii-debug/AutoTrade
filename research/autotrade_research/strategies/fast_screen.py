"""Cost-aware screening with bounded signal history; no canonical ledger."""
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from .deterministic import (
    CausalObservation, ReturnThresholdBaseline, MeanReversionThresholdBaseline,
    _threshold_signal,
)

EVIDENCE = "ZERO_RESEARCH_SCREENING_ONLY_APPROXIMATE_EXECUTION"
VERSION = "fast-historical-screen-v3-cost-hurdle-three-way"
CANDIDATES = (
    "CASH", "MOMENTUM", "MEAN_REVERSION", "MOMENTUM_QUICK_REJECT",
    "MOMENTUM_COST_HURDLE",
)


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def decimal(value):
    if type(value) not in (str, Decimal, int):
        raise ValueError("decimal input must not be floating point")
    result = Decimal(value)
    if (not result.is_finite() or result.copy_abs() > Decimal("1e18")
            or abs(result.as_tuple().exponent) > 18 or len(result.as_tuple().digits) > 36):
        raise ValueError("decimal input outside finite screening budget")
    return result


@dataclass(frozen=True)
class Costs:
    initial_cash: str = "1000"
    fee_bps: str = "10"
    spread_bps: str = "10"
    slippage_bps: str = "5"
    capital_fraction: str = "0.25"
    lookback: int = 8
    threshold: str = "0.002"

    def validate(self):
        if decimal(self.initial_cash) <= 0:
            raise ValueError("initial cash must be positive")
        if any(not 0 <= decimal(v) < 1000 for v in
               (self.fee_bps, self.spread_bps, self.slippage_bps)):
            raise ValueError("costs must be between zero and 1000 bps")
        if not 0 < decimal(self.capital_fraction) <= 1:
            raise ValueError("capital fraction outside (0,1]")
        if type(self.lookback) is not int or not 2 <= self.lookback <= 1000:
            raise ValueError("lookback outside 2..1000")
        if not 0 <= decimal(self.threshold) <= 1:
            raise ValueError("threshold outside [0,1]")


@dataclass(frozen=True)
class QuickReject:
    """Causal entry-only gate intended to reject costly marginal turnover.

    Every feature uses the trailing completed OHLCV window.  The gate can
    abstain from a new BUY, but it never blocks an exit from an open position.
    """

    lookback: int = 24
    min_volatility_fraction: str = "0.001"
    max_volatility_fraction: str = "0.03"
    min_direction_agreement: str = "0.60"
    min_quote_volume: str = "1000000"

    def validate(self):
        if type(self.lookback) is not int or not 3 <= self.lookback <= 1000:
            raise ValueError("quick-reject lookback outside 3..1000")
        minimum = decimal(self.min_volatility_fraction)
        maximum = decimal(self.max_volatility_fraction)
        if not 0 <= minimum <= maximum <= 1:
            raise ValueError("quick-reject volatility bounds must satisfy 0 <= min <= max <= 1")
        agreement = decimal(self.min_direction_agreement)
        if not Decimal("0.5") <= agreement <= 1:
            raise ValueError("quick-reject direction agreement outside [0.5,1]")
        if not 0 <= decimal(self.min_quote_volume) <= Decimal("1e18"):
            raise ValueError("quick-reject quote-volume floor outside [0,1e18]")


@dataclass(frozen=True)
class CostHurdle:
    """Causal entry gate requiring trailing drift to clear explicit friction."""

    lookback: int = 24
    cost_multiplier: str = "1"
    cushion_bps: str = "5"

    def validate(self):
        if type(self.lookback) is not int or not 3 <= self.lookback <= 1000:
            raise ValueError("cost-hurdle lookback outside 3..1000")
        multiplier = decimal(self.cost_multiplier)
        if not Decimal("1") <= multiplier <= Decimal("10"):
            raise ValueError("cost-hurdle multiplier outside [1,10]")
        cushion = decimal(self.cushion_bps)
        if not 0 <= cushion <= 1000:
            raise ValueError("cost-hurdle cushion outside [0,1000] bps")


def validate_bars(bars, *, require_ohlcv=False):
    if len(bars) < 4:
        raise ValueError("at least four chronological bars required")
    previous = None
    for bar in bars:
        opened, closed = bar["opened_at"], bar["closed_at"]
        if (type(opened) is not datetime or type(closed) is not datetime
                or opened.utcoffset() is None or closed.utcoffset() is None
                or opened >= closed or (previous is not None and opened != previous)):
            raise ValueError("bars must form a continuous chronological timezone-aware cut")
        opened_price, close = decimal(bar["open"]), decimal(bar["close"])
        if opened_price <= 0 or close <= 0:
            raise ValueError("prices must be positive")
        available = tuple(name in bar for name in ("high", "low", "volume"))
        if any(available) and not all(available):
            raise ValueError("OHLCV bars must include high, low and volume together")
        if require_ohlcv and not all(available):
            raise ValueError("quick-reject candidate requires complete OHLCV bars")
        if all(available):
            high, low, volume = (decimal(bar[name]) for name in ("high", "low", "volume"))
            if low <= 0 or high < max(opened_price, close) or low > min(opened_price, close):
                raise ValueError("OHLCV price envelope is invalid")
            if volume < 0:
                raise ValueError("OHLCV volume must be non-negative")
        previous = closed


def _quick_reject(window, configuration):
    """Return deterministic rejection reasons from completed trailing bars."""
    if len(window) < configuration.lookback:
        return ("INSUFFICIENT_HISTORY",)
    closes = [decimal(bar["close"]) for bar in window]
    returns = [(current - previous) / previous
               for previous, current in zip(closes, closes[1:])]
    mean_absolute_return = sum((value.copy_abs() for value in returns), Decimal(0)) / len(returns)
    positive_fraction = sum(value > 0 for value in returns) / Decimal(len(returns))
    mean_quote_volume = sum(
        (decimal(bar["close"]) * decimal(bar["volume"]) for bar in window), Decimal(0)
    ) / len(window)
    reasons = []
    if mean_absolute_return < decimal(configuration.min_volatility_fraction):
        reasons.append("LOW_VOLATILITY")
    if mean_absolute_return > decimal(configuration.max_volatility_fraction):
        reasons.append("HIGH_VOLATILITY")
    if positive_fraction < decimal(configuration.min_direction_agreement):
        reasons.append("CHOPPY_OR_WRONG_REGIME")
    if mean_quote_volume < decimal(configuration.min_quote_volume):
        reasons.append("LOW_LIQUIDITY")
    return tuple(reasons)


def _round_trip_hurdle(costs, configuration):
    fee = decimal(costs.fee_bps) / 10000
    adverse = (decimal(costs.spread_bps) / 2 + decimal(costs.slippage_bps)) / 10000
    # Exact underlying-price appreciation required to recover both adverse
    # prices and both fees under the same conservative execution model.
    friction = ((1 + adverse) * (1 + fee)) / ((1 - adverse) * (1 - fee)) - 1
    return (friction * decimal(configuration.cost_multiplier)
            + decimal(configuration.cushion_bps) / 10000)


def _cost_hurdle_reject(window, costs, configuration):
    """Use only completed close-to-close returns available at decision time."""
    if len(window) < configuration.lookback:
        return ("INSUFFICIENT_COST_HISTORY",), None
    closes = [decimal(bar["close"]) for bar in window]
    returns = [(current - previous) / previous
               for previous, current in zip(closes, closes[1:])]
    estimated_edge = sum(returns, Decimal(0)) / len(returns)
    if estimated_edge <= _round_trip_hurdle(costs, configuration):
        return ("ESTIMATED_EDGE_BELOW_COST_HURDLE",), estimated_edge
    return (), estimated_edge


def simulate(bars, *, candidate, costs, quick_reject=QuickReject(),
             cost_hurdle=CostHurdle(), trace=False):
    """Signal at bar close; execute only at the following open.

    One long position, no borrowing or shorting. Partition-end liquidation is
    precommitted at its final close. Quotes are approximate adverse prices;
    capacity, latency, intrabar paths, queue and exchange lot rules are absent.
    """
    costs.validate()
    quick_reject.validate()
    cost_hurdle.validate()
    if candidate not in CANDIDATES:
        raise ValueError("unknown screening candidate")
    quick_gated = candidate == "MOMENTUM_QUICK_REJECT"
    cost_gated = candidate == "MOMENTUM_COST_HURDLE"
    validate_bars(bars, require_ohlcv=quick_gated)
    with localcontext() as context:
        context.prec = 50
        cash = decimal(costs.initial_cash)
        initial = peak = cash
        quantity = fees = drawdown = Decimal(0)
        fraction = decimal(costs.capital_fraction)
        fee = decimal(costs.fee_bps) / 10000
        adverse = (decimal(costs.spread_bps) / 2 + decimal(costs.slippage_bps)) / 10000
        pending = "HOLD"
        window = deque(maxlen=costs.lookback)
        gate_window = deque(maxlen=quick_reject.lookback)
        cost_window = deque(maxlen=cost_hurdle.lookback)
        fills = []
        count = cycles = entry_signals = entry_rejections = 0
        rejection_reasons = {}
        evidence = sha256()

        def mark(price):
            nonlocal peak, drawdown
            equity = cash + quantity * price * (1-adverse) * (1-fee)
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak-equity)/peak)

        def execute(action, reference, at, signal_at):
            nonlocal cash, quantity, fees, count, cycles
            if action == "BUY" and quantity == 0:
                price = reference * (1+adverse)
                budget = cash * fraction
                quantity = (budget / (price * (1+fee))).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                if quantity == 0:
                    return
                value = quantity * price
                charge = value * fee
                cash -= value + charge
            elif action == "SELL" and quantity > 0:
                price = reference * (1-adverse)
                value = quantity * price
                charge = value * fee
                cash += value - charge
                quantity = Decimal(0)
                cycles += 1
            else:
                return
            if cash < 0 or quantity < 0:
                raise ArithmeticError("screening capital conservation failed")
            fees += charge
            count += 1
            row = dict(action=action, at=at.isoformat(), signal_at=signal_at,
                       price=str(price), fee=str(charge), cash=str(cash), quantity=str(quantity))
            evidence.update(json.dumps(row, sort_keys=True).encode())
            if trace:
                fills.append(row)

        for index, bar in enumerate(bars):
            execute(pending, decimal(bar["open"]), bar["opened_at"],
                    bars[index-1]["closed_at"].isoformat() if index else None)
            mark(decimal(bar["open"]))
            close = decimal(bar["close"])
            mark(close)
            if candidate != "CASH":
                window.append(CausalObservation.create(event_id=str(index), symbol="SCREEN",
                    available_at=bar["closed_at"], price=close))
                if quick_gated:
                    gate_window.append(bar)
                if cost_gated:
                    cost_window.append(bar)
                if len(window) == costs.lookback:
                    family = (MeanReversionThresholdBaseline if candidate == "MEAN_REVERSION"
                              else ReturnThresholdBaseline)
                    pending = _threshold_signal(family, list(window), decimal(costs.threshold))[0]
                    # Repeated BUY signals while already long are not new
                    # turnover opportunities and therefore are not counted as
                    # gate decisions or rejections.
                    if pending == "BUY" and quantity == 0:
                        entry_signals += 1
                        reasons = ()
                        if quick_gated:
                            reasons = _quick_reject(gate_window, quick_reject)
                        elif cost_gated:
                            reasons, _ = _cost_hurdle_reject(
                                cost_window, costs, cost_hurdle)
                        if reasons:
                            entry_rejections += 1
                            pending = "HOLD"
                            for reason in reasons:
                                rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
        execute("SELL", decimal(bars[-1]["close"]), bars[-1]["closed_at"], "PRECOMMITTED_PARTITION_END")
        mark(decimal(bars[-1]["close"]))
        result = dict(candidate=candidate, observations=len(bars), final_cash=str(cash),
            net_pnl=str(cash-initial), return_fraction=str((cash-initial)/initial),
            max_drawdown_fraction=str(drawdown), fees=str(fees), fills=count,
            closed_cycles=cycles, execution_digest=evidence.hexdigest(),
            entry_signals=entry_signals, entry_rejections=entry_rejections,
            entry_rejection_reasons=dict(sorted(rejection_reasons.items())),
            round_trip_cost_hurdle_fraction=(str(_round_trip_hurdle(costs, cost_hurdle))
                if cost_gated else None),
            first_open=bars[0]["opened_at"].isoformat(), last_close=bars[-1]["closed_at"].isoformat())
        if trace:
            result["trace"] = fills
        return result


def screen(bars, *, train_count, dataset_sha256, costs=Costs(),
           quick_reject=QuickReject(), cost_hurdle=CostHurdle(),
           development_count=None, min_development_cycles=5, checkpoint=None):
    """Freeze on TRAIN, quick-reject on DEVELOPMENT, then touch FINAL once.

    checkpoint(key, producer) may persist completed stages and reuse them after
    restart. This never resumes an unfinished stage or supplies ledger evidence.
    Omitting development_count retains the legacy two-way screening path.
    """
    validate_bars(bars, require_ohlcv=True)
    costs.validate()
    quick_reject.validate()
    cost_hurdle.validate()
    if type(min_development_cycles) is not int or not 1 <= min_development_cycles <= 10000:
        raise ValueError("minimum development cycles outside 1..10000")
    if type(train_count) is not int or not 4 <= train_count <= len(bars)-4:
        raise ValueError("TRAIN and later evaluation must each contain four bars")
    if development_count is not None:
        if (type(development_count) is not int or development_count < 4
                or train_count + development_count > len(bars)-4):
            raise ValueError("DEVELOPMENT and final holdout must each contain four bars")
    if (type(dataset_sha256) is not str or len(dataset_sha256) != 64
            or any(c not in "0123456789abcdef" for c in dataset_sha256)):
        raise ValueError("dataset identity must be a SHA-256")
    content = sha256()
    for bar in bars:
        content.update(json.dumps([bar["opened_at"].isoformat(), bar["closed_at"].isoformat(),
                                   *[str(decimal(bar[name])) for name in
                                     ("open", "high", "low", "close", "volume")]]).encode())
    config = dict(version=VERSION, costs=asdict(costs), candidates=CANDIDATES,
                  quick_reject=asdict(quick_reject),
                  cost_hurdle=asdict(cost_hurdle),
                  train_count=train_count, observations=len(bars), dataset_sha256=dataset_sha256,
                  development_count=development_count,
                  final_holdout_count=(len(bars) - train_count - development_count
                                       if development_count is not None else None),
                  min_development_cycles=min_development_cycles,
                  consumed_bars_sha256=content.hexdigest(),
                  end_policy="FORCED_CLOSE_EACH_CONSUMED_PARTITION",
                  evaluation_policy=("TRAIN_SELECT_DEVELOPMENT_QUICK_REJECT_FINAL_UNTOUCHED_UNTIL_PASS"
                      if development_count is not None else
                      "FRESH_CASH_COLD_SIGNALS_ALL_CANDIDATES_TRAIN_SELECTION_ONLY"))
    config_sha = digest(config)
    run = checkpoint or (lambda key, producer: producer())
    training = [run(f"{config_sha}-train-{name}", lambda name=name:
                   simulate(bars[:train_count], candidate=name, costs=costs,
                            quick_reject=quick_reject, cost_hurdle=cost_hurdle))
                for name in CANDIDATES]
    # Prefer the cash control on equal PnL; never mine evaluation to choose.
    selected = max(training, key=lambda row: (Decimal(row["net_pnl"]),
                   Decimal(row["max_drawdown_fraction"]).copy_negate(), -CANDIDATES.index(row["candidate"])))
    if development_count is not None:
        development_bars = bars[train_count:train_count + development_count]
        development_candidates = tuple(dict.fromkeys(("CASH", selected["candidate"])))
        development = [run(f"{config_sha}-development-{name}", lambda name=name:
                       simulate(development_bars, candidate=name, costs=costs,
                                quick_reject=quick_reject, cost_hurdle=cost_hurdle))
                       for name in development_candidates]
        selected_development = next(row for row in development
                                    if row["candidate"] == selected["candidate"])
        cash_development = next(row for row in development if row["candidate"] == "CASH")
        rejection_reasons = []
        if selected["candidate"] == "CASH":
            rejection_reasons.append("TRAIN_SELECTED_CASH_CONTROL")
        else:
            if selected_development["closed_cycles"] < min_development_cycles:
                rejection_reasons.append("MINIMUM_DEVELOPMENT_CYCLES_NOT_MET")
            if Decimal(selected_development["net_pnl"]) <= Decimal(cash_development["net_pnl"]):
                rejection_reasons.append("DEVELOPMENT_DID_NOT_BEAT_CASH")
        if rejection_reasons:
            evaluation_matrix = []
            evaluation = None
            final_selected = "CASH"
            holdout_consumed = False
        else:
            final_selected = selected["candidate"]
            final_bars = bars[train_count + development_count:]
            final_candidates = tuple(dict.fromkeys(("CASH", final_selected)))
            evaluation_matrix = [run(f"{config_sha}-final-{name}", lambda name=name:
                                 simulate(final_bars, candidate=name, costs=costs,
                                          quick_reject=quick_reject,
                                          cost_hurdle=cost_hurdle))
                                 for name in final_candidates]
            evaluation = next(row for row in evaluation_matrix
                              if row["candidate"] == final_selected)
            holdout_consumed = True
        result = dict(schema=VERSION, evidence_class=EVIDENCE, config=config,
            config_sha256=config_sha, training=training,
            training_selected_candidate=selected["candidate"], development=development,
            development_rejection_reasons=rejection_reasons,
            selected_candidate=final_selected, evaluation=evaluation,
            evaluation_matrix=evaluation_matrix, holdout_consumed=holdout_consumed,
            network_orders=False, paid_model_calls=0, canonical_qualification=False,
            limitations=["NO_INTRABAR_DRAWDOWN", "NO_CAPACITY_OR_QUEUE_MODEL",
                         "NO_EXCHANGE_LOT_RULES", "RETROSPECTIVE_BAR_AVAILABILITY",
                         "CACHED_PERIODS_USED_BY_PRIOR_UNRELATED_RESEARCH",
                         "CANONICAL_FINALIST_VALIDATION_REQUIRED"])
        return {**result, "result_sha256": digest(result)}
    evaluation_matrix = [run(f"{config_sha}-eval-{name}", lambda name=name:
                         simulate(bars[train_count:], candidate=name, costs=costs,
                                  quick_reject=quick_reject,
                                  cost_hurdle=cost_hurdle)) for name in CANDIDATES]
    evaluation = next(row for row in evaluation_matrix
                      if row["candidate"] == selected["candidate"])
    result = dict(schema=VERSION, evidence_class=EVIDENCE, config=config, config_sha256=config_sha,
        training=training, selected_candidate=selected["candidate"],
        evaluation=evaluation, evaluation_matrix=evaluation_matrix,
        network_orders=False, paid_model_calls=0, canonical_qualification=False,
        limitations=["NO_INTRABAR_DRAWDOWN", "NO_CAPACITY_OR_QUEUE_MODEL", "NO_EXCHANGE_LOT_RULES",
                     "RETROSPECTIVE_BAR_AVAILABILITY", "CANONICAL_FINALIST_VALIDATION_REQUIRED"])
    return {**result, "result_sha256": digest(result)}
