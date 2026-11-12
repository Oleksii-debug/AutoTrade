"""Causal futures screen built from standard technical-analysis indicators.

This module turns the indicators covered by the CME technical-analysis course
into testable rules.  Indicators are computed from completed candles, signals
execute at the next candle open, and every fill pays the configured fee,
spread and slippage.  It is research-only and cannot place an order.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN, localcontext
from hashlib import sha256
import json

from .fast_screen import decimal, validate_bars


VERSION = "technical-confluence-futures-v1"
CANDIDATES = ("CASH", "HOLD_LONG", "HOLD_SHORT", "TECHNICAL_CONFLUENCE")


def digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


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


def simulate(bars: list[dict], *, candidate: str,
             costs: TechnicalCosts = TechnicalCosts(),
             configuration: TechnicalConfiguration = TechnicalConfiguration()) -> dict:
    """Run one partition with causal next-open perpetual-futures fills."""
    validate_bars(bars, require_ohlcv=True)
    costs.validate(); configuration.validate()
    if candidate not in CANDIDATES:
        raise ValueError("unknown technical candidate")
    with localcontext() as context:
        context.prec = 50
        balance = initial = peak = decimal(costs.initial_equity)
        fee_ratio = decimal(costs.fee_bps_per_fill) / Decimal(10000)
        adverse = (decimal(costs.spread_bps) / Decimal(2)
                   + decimal(costs.slippage_bps_per_fill)) / Decimal(10000)
        fraction = decimal(costs.capital_fraction)
        funding_day = decimal(costs.funding_bps_per_day) / Decimal(10000)
        quantity = entry_price = Decimal(0)
        pending = 1 if candidate == "HOLD_LONG" else -1 if candidate == "HOLD_SHORT" else None
        pending_reason = "CONTROL_ENTRY" if pending is not None else None
        history: list[dict] = []
        holding = closed_cycles = fill_count = signal_count = cooldown_remaining = 0
        fees = funding = max_drawdown = Decimal(0)
        long_cycles = short_cycles = 0
        exit_reasons: dict[str, int] = {}
        evidence = sha256()

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
            if direction is None:
                return
            if direction and quantity == 0:
                price = reference * (Decimal(1) + adverse if direction > 0 else Decimal(1) - adverse)
                notional = max(Decimal(0), balance) * fraction
                opened = (notional / price).quantize(Decimal("1e-18"), rounding=ROUND_DOWN)
                if opened == 0:
                    return
                quantity = opened if direction > 0 else -opened
                entry_price = price
                charge = notional * fee_ratio
                balance -= charge; fees += charge; fill_count += 1; holding = 0
            elif direction == 0 and quantity != 0:
                price = reference * (Decimal(1) - adverse if quantity > 0 else Decimal(1) + adverse)
                notional = abs(quantity) * price
                charge = notional * fee_ratio
                balance += quantity * (price - entry_price) - charge
                fees += charge; fill_count += 1; closed_cycles += 1
                long_cycles += int(quantity > 0); short_cycles += int(quantity < 0)
                quantity = entry_price = Decimal(0); holding = 0
                cooldown_remaining = configuration.cooldown_bars
                exit_reasons[reason or "UNSPECIFIED_EXIT"] = exit_reasons.get(reason or "UNSPECIFIED_EXIT", 0) + 1
            else:
                return
            evidence.update(json.dumps({"at": at, "direction": direction,
                "reason": reason, "balance": str(balance)}, sort_keys=True).encode())

        active_atr = Decimal(0)
        for index, bar in enumerate(bars):
            execute(pending, decimal(bar["open"]), pending_reason,
                    bar["opened_at"].isoformat())
            pending = None; pending_reason = None
            close = decimal(bar["close"])
            if quantity == 0 and cooldown_remaining > 0:
                cooldown_remaining -= 1
            mark(close)
            if quantity:
                holding += 1
                seconds = Decimal(str((bar["closed_at"] - bar["opened_at"]).total_seconds()))
                if not Decimal(0) < seconds <= Decimal(86400 * 7):
                    raise ValueError("bar duration is outside the funding-cost envelope")
                carry = abs(quantity) * close * funding_day * seconds / Decimal(86400)
                balance -= carry; funding += carry; mark(close)
            history.append(bar)
            features = _features(history, configuration)
            if candidate != "TECHNICAL_CONFLUENCE" or features is None:
                continue
            if quantity == 0:
                if cooldown_remaining:
                    continue
                direction = _entry(features, configuration)
                if direction:
                    pending = direction; pending_reason = "INDICATOR_CONFLUENCE_ENTRY"
                    active_atr = features["atr"]; signal_count += 1
                continue
            reason = None
            move = (close - entry_price) * (Decimal(1) if quantity > 0 else Decimal(-1))
            if holding >= configuration.maximum_hold_bars:
                reason = "MAX_HOLD_EXIT"
            elif holding >= configuration.minimum_hold_bars:
                if move <= -decimal(configuration.stop_atr) * active_atr:
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
        return {
            "candidate": candidate, "configuration": asdict(configuration),
            "costs": asdict(costs), "round_trip_cost_bps": str(costs.round_trip_bps),
            "initial_equity": str(initial), "final_equity": str(final),
            "net_pnl": str(final - initial), "return_fraction": str((final - initial) / initial),
            "maximum_drawdown_fraction": str(max_drawdown),
            "closed_cycles": closed_cycles, "long_cycles": long_cycles,
            "short_cycles": short_cycles, "fill_count": fill_count,
            "signal_count": signal_count, "fees": str(fees), "funding": str(funding),
            "exit_reasons": exit_reasons, "evidence_sha256": evidence.hexdigest(),
            "network_orders": False, "trading_authority": False,
        }
