"""Causal futures screen built from standard technical-analysis indicators.

This module turns the indicators covered by the CME technical-analysis course
into testable rules.  Indicators are computed from completed candles, signals
execute at the next candle open, and every fill pays the configured fee,
spread and slippage.  It is research-only and cannot place an order.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json
from statistics import median

from .fast_screen import decimal, validate_bars
from .pretrade_evidence import PreTradeThesis, evaluate_pretrade


VERSION = "technical-confluence-futures-v1"
CANDIDATES = ("CASH", "HOLD_LONG", "HOLD_SHORT", "TECHNICAL_CONFLUENCE")
DAILY_RETURN_TARGETS = (Decimal("0.05"), Decimal("0.10"), Decimal("0.20"))
# Owner target, 2026-11-12: net account-equity gain per complete UTC hour.
HOURLY_TARGET = Decimal("0.01")
HOURLY_WINDOWS = (1, 4, 8)


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def summarize_daily_returns(returns: list[Decimal]) -> dict:
    """Summarize full UTC days, including days without a position."""
    return {
        "complete_utc_days": len(returns),
        "positive_days": sum(value > 0 for value in returns),
        "negative_days": sum(value < 0 for value in returns),
        "best_day_fraction": str(max(returns)) if returns else None,
        "worst_day_fraction": str(min(returns)) if returns else None,
        "average_day_fraction": str(sum(returns) / len(returns)) if returns else None,
        "target_hit_days": {
            str(target): sum(value >= target for value in returns)
            for target in DAILY_RETURN_TARGETS
        },
    }


def summarize_hour_windows(returns: list[Decimal], hours: int) -> dict:
    target = hours * HOURLY_TARGET
    return {
        "complete_windows": len(returns),
        "target_fraction": str(target),
        "target_hits": sum(value >= target for value in returns),
        "positive_windows": sum(value > 0 for value in returns),
        "flat_windows": sum(value == 0 for value in returns),
        "negative_windows": sum(value < 0 for value in returns),
        "average_fraction": str(sum(returns) / len(returns)) if returns else None,
        "median_fraction": str(median(returns)) if returns else None,
        "best_fraction": str(max(returns)) if returns else None,
        "worst_fraction": str(min(returns)) if returns else None,
    }


class HourlyEquity:
    """Collect liquidation equity on complete UTC hours for a causal replay."""

    def __init__(self, opened_at: datetime, initial_equity: Decimal) -> None:
        self.samples: dict[int, Decimal] = {}
        self.record(opened_at, initial_equity)

    def record(self, at: datetime, equity: Decimal) -> None:
        at = at.astimezone(UTC)
        if not (at.minute or at.second or at.microsecond):
            self.samples[int(at.timestamp())] = equity

    def mark(self, at: datetime, *, balance: Decimal, quantity: Decimal, entry: Decimal,
             close: Decimal, adverse: Decimal, fee_ratio: Decimal) -> None:
        at = at.astimezone(UTC)
        if at.minute or at.second or at.microsecond:
            return
        if quantity:
            exit_price = close * (1 - adverse if quantity > 0 else 1 + adverse)
            balance += quantity * (exit_price - entry) - abs(quantity) * exit_price * fee_ratio
        self.record(at, balance)

    def report(self) -> dict:
        windows = {}
        for hours in HOURLY_WINDOWS:
            span = hours * 3600
            changes = []
            for end, equity in self.samples.items():
                baseline = self.samples.get(end - span)
                if end % span == 0 and baseline is not None and baseline > 0:
                    changes.append(equity / baseline - 1)
            windows[str(hours)] = summarize_hour_windows(changes, hours)
        return windows


@dataclass(frozen=True)
class TechnicalCosts:
    initial_equity: str = "1000"
    fee_bps_per_fill: str = "5.5"
    spread_bps: str = "2"
    slippage_bps_per_fill: str = "1"
    capital_fraction: str = "0.25"
    funding_bps_per_day: str = "1"

    def validate(self) -> None:
        if decimal(self.initial_equity) <= 0:
            raise ValueError("initial equity must be positive")
        for name in ("fee_bps_per_fill", "spread_bps",
                     "slippage_bps_per_fill", "funding_bps_per_day"):
            if not 0 <= decimal(getattr(self, name)) < 1000:
                raise ValueError(f"{name} outside [0,1000) bps")
        if not 0 < decimal(self.capital_fraction) <= 1:
            raise ValueError("capital fraction outside (0,1]")

    @property
    def round_trip_bps(self) -> Decimal:
        return (Decimal(2) * decimal(self.fee_bps_per_fill)
                + decimal(self.spread_bps)
                + Decimal(2) * decimal(self.slippage_bps_per_fill))


@dataclass(frozen=True)
class TechnicalConfiguration:
    family: str = "EMA_RSI_TREND"
    fast_ema: int = 12
    slow_ema: int = 48
    rsi_period: int = 14
    rsi_long_min: str = "52"
    rsi_long_max: str = "72"
    rsi_short_min: str = "28"
    rsi_short_max: str = "48"
    bollinger_period: int = 20
    bollinger_width: str = "2"
    atr_period: int = 14
    stop_atr: str = "2"
    take_profit_atr: str = "3"
    minimum_ema_gap_fraction: str = "0.0015"
    minimum_expected_move_fraction: str = "0.003"
    cooldown_bars: int = 8
    minimum_hold_bars: int = 3
    maximum_hold_bars: int = 120
    maximum_entries_per_utc_day: int = 4

    def validate(self) -> None:
        if self.family not in {"EMA_RSI_TREND", "BOLLINGER_RSI_REVERSION"}:
            raise ValueError("unsupported technical family")
        if not 2 <= self.fast_ema < self.slow_ema <= 1000:
            raise ValueError("EMA periods are invalid")
        if not 2 <= self.rsi_period <= 200:
            raise ValueError("RSI period is invalid")
        if not 3 <= self.bollinger_period <= 1000:
            raise ValueError("Bollinger period is invalid")
        if not 2 <= self.atr_period <= 200:
            raise ValueError("ATR period is invalid")
        for name in ("rsi_long_min", "rsi_long_max",
                     "rsi_short_min", "rsi_short_max"):
            if not 0 <= decimal(getattr(self, name)) <= 100:
                raise ValueError(f"{name} outside [0,100]")
        if decimal(self.rsi_long_min) > decimal(self.rsi_long_max):
            raise ValueError("long RSI bounds are inverted")
        if decimal(self.rsi_short_min) > decimal(self.rsi_short_max):
            raise ValueError("short RSI bounds are inverted")
        if not Decimal("0.1") <= decimal(self.bollinger_width) <= 10:
            raise ValueError("Bollinger width is invalid")
        if not Decimal("0.1") <= decimal(self.stop_atr) <= 20:
            raise ValueError("ATR stop is invalid")
        if not Decimal("0.1") <= decimal(self.take_profit_atr) <= 50:
            raise ValueError("ATR take-profit is invalid")
        if not 0 <= decimal(self.minimum_ema_gap_fraction) <= Decimal("0.10"):
            raise ValueError("minimum EMA gap is invalid")
        if not 0 <= decimal(self.minimum_expected_move_fraction) <= Decimal("0.25"):
            raise ValueError("minimum expected move is invalid")
        if type(self.cooldown_bars) is not int or not 0 <= self.cooldown_bars <= 10000:
            raise ValueError("cooldown bars are invalid")
        if not 1 <= self.minimum_hold_bars <= self.maximum_hold_bars <= 10000:
            raise ValueError("holding bounds are invalid")
        if (type(self.maximum_entries_per_utc_day) is not int
                or not 1 <= self.maximum_entries_per_utc_day <= 1000):
            raise ValueError("daily entry bound is invalid")


def _ema(values: list[Decimal], period: int) -> Decimal:
    alpha = Decimal(2) / Decimal(period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal(1) - alpha) * result
    return result


def _rsi(values: list[Decimal], period: int) -> Decimal:
    changes = [values[index] - values[index - 1]
               for index in range(len(values) - period, len(values))]
    gains = sum((max(change, Decimal(0)) for change in changes), Decimal(0))
    losses = sum((max(-change, Decimal(0)) for change in changes), Decimal(0))
    if losses == 0:
        return Decimal(100) if gains else Decimal(50)
    relative = gains / losses
    return Decimal(100) - Decimal(100) / (Decimal(1) + relative)


def _atr(bars: list[dict], period: int) -> Decimal:
    ranges = []
    for index in range(len(bars) - period, len(bars)):
        bar = bars[index]
        previous = decimal(bars[index - 1]["close"])
        high, low = decimal(bar["high"]), decimal(bar["low"])
        ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
    return sum(ranges, Decimal(0)) / Decimal(period)


def _features(history: list[dict], config: TechnicalConfiguration) -> dict | None:
    need = max(config.slow_ema * 3, config.rsi_period + 1,
               config.bollinger_period, config.atr_period + 1)
    if len(history) < need:
        return None
    closes = [decimal(row["close"]) for row in history[-need:]]
    band = closes[-config.bollinger_period:]
    mean = sum(band, Decimal(0)) / Decimal(len(band))
    variance = sum(((value - mean) ** 2 for value in band), Decimal(0)) / Decimal(len(band))
    deviation = variance.sqrt()
    return {
        "close": closes[-1],
        "fast_ema": _ema(closes[-config.fast_ema * 3:], config.fast_ema),
        "slow_ema": _ema(closes[-config.slow_ema * 3:], config.slow_ema),
        "rsi": _rsi(closes, config.rsi_period),
        "band_mid": mean,
        "band_upper": mean + decimal(config.bollinger_width) * deviation,
        "band_lower": mean - decimal(config.bollinger_width) * deviation,
        "atr": _atr(history, config.atr_period),
    }


def _entry(features: dict, config: TechnicalConfiguration) -> int:
    if features["atr"] / features["close"] < decimal(config.minimum_expected_move_fraction):
        return 0
    if config.family == "EMA_RSI_TREND":
        gap = abs(features["fast_ema"] - features["slow_ema"]) / features["close"]
        if gap < decimal(config.minimum_ema_gap_fraction):
            return 0
        if (features["fast_ema"] > features["slow_ema"]
                and decimal(config.rsi_long_min) <= features["rsi"] <= decimal(config.rsi_long_max)):
            return 1
        if (features["fast_ema"] < features["slow_ema"]
                and decimal(config.rsi_short_min) <= features["rsi"] <= decimal(config.rsi_short_max)):
            return -1
    else:
        distance = abs(features["close"] - features["band_mid"]) / features["close"]
        if distance < decimal(config.minimum_expected_move_fraction):
            return 0
        if features["close"] < features["band_lower"] and features["rsi"] <= decimal(config.rsi_long_min):
            return 1
        if features["close"] > features["band_upper"] and features["rsi"] >= decimal(config.rsi_short_max):
            return -1
    return 0


def technical_signal(history: list[dict],
                     configuration: TechnicalConfiguration) -> dict:
    """Return a causal, completed-candle technical decision with evidence.

    This is the single live/shadow entry surface for the frozen strategy.  It
    deliberately exposes no price forecast and never treats a news headline or
    model response as an entry signal.  Callers must still apply the pre-trade,
    news and microstructure gates before acting on a non-zero direction.
    """
    validate_bars(history, require_ohlcv=True)
    configuration.validate()
    features = _features(history, configuration)
    if features is None:
        return {"direction": 0, "reason": "INSUFFICIENT_TECHNICAL_HISTORY",
                "features": None}
    direction = _entry(features, configuration)
    return {
        "direction": direction,
        "reason": ("TECHNICAL_SETUP_LONG" if direction > 0 else
                   "TECHNICAL_SETUP_SHORT" if direction < 0 else
                   "NO_TECHNICAL_SETUP"),
        "features": {key: format(value, "f") for key, value in features.items()},
        "configuration_sha256": digest(asdict(configuration)),
        "last_completed_bar": history[-1]["closed_at"].isoformat(),
    }


INTRABAR_EXECUTION_MODEL = "NEXT_OPEN_WITH_OHLC_PROTECTIVE_STOP"


def simulate(bars: list[dict], *, candidate: str,
             costs: TechnicalCosts = TechnicalCosts(),
             configuration: TechnicalConfiguration = TechnicalConfiguration(),
             report_daily_returns: bool = False,
             report_hourly_returns: bool = False,
             require_hourly_trend_alignment: bool = False,
             protective_stop_intrabar: bool = False) -> dict:
    """Run causal next-open fills with an optional OHLC protective stop."""
    validate_bars(bars, require_ohlcv=True)
    costs.validate(); configuration.validate()
    if candidate not in CANDIDATES:
        raise ValueError("unknown technical candidate")
    if protective_stop_intrabar and candidate != "TECHNICAL_CONFLUENCE":
        raise ValueError("intrabar protection requires a technical candidate")
    if require_hourly_trend_alignment and (
            candidate != "TECHNICAL_CONFLUENCE"
            or any((bar["closed_at"] - bar["opened_at"]) != timedelta(minutes=15)
                   or bar["opened_at"].timestamp() % 900 != 0
                   for bar in bars)):
        raise ValueError("hourly alignment requires a technical candidate on M15 bars")
    with localcontext() as context:
        context.prec = 50
        balance = initial = peak = decimal(costs.initial_equity)
        fee_ratio = decimal(costs.fee_bps_per_fill) / Decimal(10000)
        half_spread = decimal(costs.spread_bps) / Decimal(20000)
        adverse = half_spread + decimal(costs.slippage_bps_per_fill) / Decimal(10000)
        fraction = decimal(costs.capital_fraction)
        funding_day = decimal(costs.funding_bps_per_day) / Decimal(10000)
        quantity = entry_price = Decimal(0)
        pending = 1 if candidate == "HOLD_LONG" else -1 if candidate == "HOLD_SHORT" else None
        pending_reason = "CONTROL_ENTRY" if pending is not None else None
        history: list[dict] = []
        holding = closed_cycles = fill_count = signal_count = cooldown_remaining = 0
        pretrade_holds = 0
        pretrade_hold_reasons: dict[str, int] = {}
        fees = funding = max_drawdown = Decimal(0)
        long_cycles = short_cycles = 0
        exit_reasons: dict[str, int] = {}
        entries_by_day: dict[str, int] = {}
        evidence = sha256()
        hourly_closes = deque(maxlen=configuration.slow_ema * 3)
        cycle_start = None
        by_direction: dict[str, dict] = {}
        by_exit: dict[str, dict] = {}
        window_hours = ((HOURLY_WINDOWS if report_hourly_returns else ())
                        + ((24,) if report_daily_returns else ()))
        window_returns: dict[int, list[Decimal]] = {hours: [] for hours in window_hours}
        exposed_hour_returns: list[Decimal] = []
        hour_had_exposure = False
        start = bars[0]["opened_at"].astimezone(UTC)
        baselines = {}
        if not (start.minute or start.second or start.microsecond):
            baselines = {hours: (start, initial) for hours in window_hours if start.hour % hours == 0}

        def record_boundary(at, reference: Decimal) -> None:
            nonlocal hour_had_exposure
            at = at.astimezone(UTC)
            if at.minute or at.second or at.microsecond:
                return
            equity = balance
            if quantity:
                exit_price = reference * (1 - adverse if quantity > 0 else 1 + adverse)
                equity += quantity * (exit_price - entry_price)
                equity -= abs(quantity) * exit_price * fee_ratio
            for hours in window_hours:
                if at.hour % hours:
                    continue
                prior = baselines.get(hours)
                if prior is not None:
                    previous_at, previous_equity = prior
                    if at - previous_at == timedelta(hours=hours) and previous_equity > 0:
                        change = (equity - previous_equity) / previous_equity
                        window_returns[hours].append(change)
                        if hours == 1 and hour_had_exposure:
                            exposed_hour_returns.append(change)
                baselines[hours] = (at, equity)
            hour_had_exposure = False

        def mark(price: Decimal) -> Decimal:
            nonlocal peak, max_drawdown
            equity = balance + quantity * (price - entry_price)
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak)
            return equity

        def execute(direction: int | None, reference: Decimal,
                    reason: str | None, at: str) -> None:
            nonlocal balance, quantity, entry_price, fees, fill_count
            nonlocal closed_cycles, long_cycles, short_cycles, holding, cooldown_remaining
            nonlocal pretrade_holds, cycle_start
            if direction is None:
                return
            if direction and quantity == 0:
                price = reference * (Decimal(1) + adverse if direction > 0 else Decimal(1) - adverse)
                if reason == "INDICATOR_CONFLUENCE_ENTRY":
                    entry_day = at[:10]
                    if entries_by_day.get(entry_day, 0) >= configuration.maximum_entries_per_utc_day:
                        pretrade_holds += 1
                        pretrade_hold_reasons["DAILY_ENTRY_LIMIT"] = (
                            pretrade_hold_reasons.get("DAILY_ENTRY_LIMIT", 0) + 1)
                        return
                    stop_distance = decimal(configuration.stop_atr) * active_atr
                    target_distance = decimal(configuration.take_profit_atr) * active_atr
                    gate = evaluate_pretrade(PreTradeThesis(
                        direction="LONG" if direction > 0 else "SHORT",
                        regime=("TREND" if configuration.family == "EMA_RSI_TREND"
                                else "RANGE"),
                        causal_data_complete=True, regime_confirmed=True,
                        location_confirmed=True, trigger_confirmed=True,
                        entry_price=price,
                        invalidation_price=(price - stop_distance if direction > 0
                                            else price + stop_distance),
                        target_price=(price + target_distance if direction > 0
                                      else price - target_distance),
                        round_trip_cost_bps=costs.round_trip_bps,
                        minimum_reward_risk="2",
                        microstructure_status="NOT_AVAILABLE_HISTORICAL"))
                    if gate["decision"] != "ENTER":
                        pretrade_holds += 1
                        for failure in gate["failure_reasons"]:
                            pretrade_hold_reasons[failure] = (
                                pretrade_hold_reasons.get(failure, 0) + 1)
                        return
                notional = max(Decimal(0), balance) * fraction
                opened = (notional / price).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                if opened == 0:
                    return
                if report_hourly_returns:
                    cycle_start = (balance, fees, funding, reference)
                quantity = opened if direction > 0 else -opened
                entry_price = price
                charge = notional * fee_ratio
                balance -= charge; fees += charge; fill_count += 1; holding = 0
                if reason == "INDICATOR_CONFLUENCE_ENTRY":
                    entries_by_day[at[:10]] = entries_by_day.get(at[:10], 0) + 1
            elif direction == 0 and quantity != 0:
                price = reference * (Decimal(1) - adverse if quantity > 0 else Decimal(1) + adverse)
                notional = abs(quantity) * price
                charge = notional * fee_ratio
                executed_pnl = quantity * (price - entry_price)
                balance += executed_pnl - charge
                fees += charge; fill_count += 1; closed_cycles += 1
                long_cycles += int(quantity > 0); short_cycles += int(quantity < 0)
                exit_reason = reason or "UNSPECIFIED_EXIT"
                if cycle_start is not None:
                    start_balance, start_fees, start_funding, entry_reference = cycle_start
                    reference_pnl = quantity * (reference - entry_reference)
                    cycle = {"gross_pnl_at_reference": reference_pnl,
                             "spread_slippage_cost": reference_pnl - executed_pnl,
                             "fees": fees - start_fees,
                             "funding": funding - start_funding,
                             "net_pnl": balance - start_balance}
                    for group, key in ((by_direction, "LONG" if quantity > 0 else "SHORT"),
                                       (by_exit, exit_reason)):
                        totals = group.setdefault(key, {"cycles": 0})
                        totals["cycles"] += 1
                        for field, value in cycle.items():
                            totals[field] = totals.get(field, Decimal(0)) + value
                    cycle_start = None
                quantity = entry_price = Decimal(0); holding = 0
                cooldown_remaining = configuration.cooldown_bars
                exit_reasons[exit_reason] = exit_reasons.get(exit_reason, 0) + 1
            else:
                return
            evidence.update(json.dumps({"at": at, "direction": direction,
                "reason": reason, "balance": str(balance)}, sort_keys=True).encode())

        active_atr = Decimal(0)
        for bar in bars:
            opened = decimal(bar["open"])
            execute(pending, opened, pending_reason, bar["opened_at"].isoformat())
            pending = None; pending_reason = None
            close = decimal(bar["close"])
            if quantity == 0 and cooldown_remaining > 0:
                cooldown_remaining -= 1
            protective_reason = None
            reference = close
            if quantity and protective_stop_intrabar:
                side = 1 if quantity > 0 else -1
                stop = entry_price - side * decimal(configuration.stop_atr) * active_atr
                # OHLC is a reference price. Translate the bid/ask stop before
                # applying the execution spread and slippage in execute().
                threshold = stop / (1 - side * half_spread)
                extreme = decimal(bar["low"] if side > 0 else bar["high"])
                if side * (opened - threshold) <= 0:
                    protective_reason = "ATR_PROTECTIVE_GAP"
                    hour_had_exposure = True
                    execute(0, opened, protective_reason, bar["opened_at"].isoformat())
                elif side * (extreme - threshold) <= 0:
                    protective_reason = "ATR_PROTECTIVE_TOUCH"
                    reference = threshold
            mark(reference)
            if quantity:
                hour_had_exposure = True
                holding += 1
                seconds = Decimal(str((bar["closed_at"] - bar["opened_at"]).total_seconds()))
                if not Decimal(0) < seconds <= Decimal(86400 * 7):
                    raise ValueError("bar duration is outside the funding-cost envelope")
                # A touch has unknown timing. Charge the full bar at the stop
                # reference, and never mark the subsequent close as exposed.
                carry = abs(quantity) * reference * funding_day * seconds / Decimal(86400)
                balance -= carry; funding += carry; mark(reference)
                if protective_reason:
                    execute(0, reference, protective_reason, bar["closed_at"].isoformat())
                    mark(reference)
            if window_hours:
                record_boundary(bar["closed_at"], close)
            if require_hourly_trend_alignment:
                utc_close = bar["closed_at"].astimezone(UTC)
                if not (utc_close.minute or utc_close.second or utc_close.microsecond):
                    hourly_closes.append(close)
            history.append(bar)
            if protective_reason:
                continue
            features = _features(history, configuration)
            if candidate != "TECHNICAL_CONFLUENCE" or features is None:
                continue
            if quantity == 0:
                if cooldown_remaining:
                    continue
                direction = _entry(features, configuration)
                if direction and require_hourly_trend_alignment:
                    if len(hourly_closes) != hourly_closes.maxlen:
                        failure = "HOURLY_TREND_WARMUP"
                    else:
                        completed = list(hourly_closes)
                        fast = _ema(completed[-configuration.fast_ema * 3:], configuration.fast_ema)
                        slow = _ema(completed, configuration.slow_ema)
                        failure = None if direction * (fast - slow) > 0 else "HOURLY_TREND_CONFLICT"
                    if failure:
                        pretrade_holds += 1
                        pretrade_hold_reasons[failure] = pretrade_hold_reasons.get(failure, 0) + 1
                        continue
                if direction:
                    pending = direction; pending_reason = "INDICATOR_CONFLUENCE_ENTRY"
                    active_atr = features["atr"]; signal_count += 1
                continue
            reason = None
            move = (close - entry_price) * (Decimal(1) if quantity > 0 else Decimal(-1))
            if holding >= configuration.maximum_hold_bars:
                reason = "MAX_HOLD_EXIT"
            elif holding >= configuration.minimum_hold_bars:
                if not protective_stop_intrabar and move <= -decimal(configuration.stop_atr) * active_atr:
                    reason = "ATR_STOP_EXIT"
                elif move >= decimal(configuration.take_profit_atr) * active_atr:
                    reason = "ATR_TAKE_PROFIT_EXIT"
                elif configuration.family == "EMA_RSI_TREND":
                    if ((quantity > 0 and features["fast_ema"] < features["slow_ema"])
                            or (quantity < 0 and features["fast_ema"] > features["slow_ema"])):
                        reason = "EMA_REVERSAL_EXIT"
                elif ((quantity > 0 and close >= features["band_mid"])
                      or (quantity < 0 and close <= features["band_mid"])):
                    reason = "BOLLINGER_MEAN_EXIT"
            if reason:
                pending = 0; pending_reason = reason; signal_count += 1
        if quantity:
            execute(0, decimal(bars[-1]["close"]), "PARTITION_END_EXIT",
                    bars[-1]["closed_at"].isoformat())
        final = balance
        result = {
            "candidate": candidate, "configuration": asdict(configuration),
            "costs": asdict(costs), "round_trip_cost_bps": str(costs.round_trip_bps),
            "initial_equity": str(initial), "final_equity": str(final),
            "net_pnl": str(final - initial), "return_fraction": str((final - initial) / initial),
            "maximum_drawdown_fraction": str(max_drawdown),
            "closed_cycles": closed_cycles, "long_cycles": long_cycles,
            "short_cycles": short_cycles, "fill_count": fill_count,
            "signal_count": signal_count, "fees": str(fees), "funding": str(funding),
            "pretrade_holds": pretrade_holds,
            "pretrade_hold_reasons": pretrade_hold_reasons,
            "exit_reasons": exit_reasons, "evidence_sha256": evidence.hexdigest(),
            "network_orders": False, "trading_authority": False,
        }
        if protective_stop_intrabar:
            result["execution_model"] = INTRABAR_EXECUTION_MODEL
        if report_daily_returns:
            result["daily_returns"] = summarize_daily_returns(window_returns[24])
        if require_hourly_trend_alignment:
            result["entry_context"] = "COMPLETED_H1_EMA_ALIGNMENT"
        if report_hourly_returns:
            result["clock_window_returns"] = {
                str(hours): summarize_hour_windows(window_returns[hours], hours)
                for hours in HOURLY_WINDOWS
            }
            result["one_hour_with_exposure"] = summarize_hour_windows(exposed_hour_returns, 1)
            result["execution_attribution"] = {
                name: {key: {field: str(value) if isinstance(value, Decimal) else value
                             for field, value in totals.items()}
                       for key, totals in groups.items()}
                for name, groups in (("by_direction", by_direction), ("by_exit_reason", by_exit))
            }
        return result
