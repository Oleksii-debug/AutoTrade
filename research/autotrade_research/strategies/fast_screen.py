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

EVIDENCE = "SCREENING_ONLY_APPROXIMATE_EXECUTION"
VERSION = "fast-historical-screen-v1"
CANDIDATES = ("CASH", "MOMENTUM", "MEAN_REVERSION")


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


def validate_bars(bars):
    if len(bars) < 4:
        raise ValueError("at least four chronological bars required")
    previous = None
    for bar in bars:
        opened, closed = bar["opened_at"], bar["closed_at"]
        if (type(opened) is not datetime or type(closed) is not datetime
                or opened.utcoffset() is None or closed.utcoffset() is None
                or opened >= closed or (previous is not None and opened != previous)):
            raise ValueError("bars must form a continuous chronological timezone-aware cut")
        if decimal(bar["open"]) <= 0 or decimal(bar["close"]) <= 0:
            raise ValueError("prices must be positive")
        previous = closed


def simulate(bars, *, candidate, costs, trace=False):
    """Signal at bar close; execute only at the following open.

    One long position, no borrowing or shorting. Partition-end liquidation is
    precommitted at its final close. Quotes are approximate adverse prices;
    capacity, latency, intrabar paths, queue and exchange lot rules are absent.
    """
    costs.validate()
    validate_bars(bars)
    if candidate not in CANDIDATES:
        raise ValueError("unknown screening candidate")
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
        fills = []
        count = cycles = 0
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
                if len(window) == costs.lookback:
                    family = ReturnThresholdBaseline if candidate == "MOMENTUM" else MeanReversionThresholdBaseline
                    pending = _threshold_signal(family, list(window), decimal(costs.threshold))[0]
        execute("SELL", decimal(bars[-1]["close"]), bars[-1]["closed_at"], "PRECOMMITTED_PARTITION_END")
        mark(decimal(bars[-1]["close"]))
        result = dict(candidate=candidate, observations=len(bars), final_cash=str(cash),
            net_pnl=str(cash-initial), return_fraction=str((cash-initial)/initial),
            max_drawdown_fraction=str(drawdown), fees=str(fees), fills=count,
            closed_cycles=cycles, execution_digest=evidence.hexdigest(),
            first_open=bars[0]["opened_at"].isoformat(), last_close=bars[-1]["closed_at"].isoformat())
        if trace:
            result["trace"] = fills
        return result


def screen(bars, *, train_count, dataset_sha256, costs=Costs(), checkpoint=None):
    """Freeze selection from TRAIN only, then evaluate on the later partition.

    checkpoint(key, producer) may persist completed stages and reuse them after
    restart. This never resumes an unfinished stage or supplies ledger evidence.
    """
    validate_bars(bars)
    costs.validate()
    if type(train_count) is not int or not 4 <= train_count <= len(bars)-4:
        raise ValueError("TRAIN and later evaluation must each contain four bars")
    if (type(dataset_sha256) is not str or len(dataset_sha256) != 64
            or any(c not in "0123456789abcdef" for c in dataset_sha256)):
        raise ValueError("dataset identity must be a SHA-256")
    content = sha256()
    for bar in bars:
        content.update(json.dumps([bar["opened_at"].isoformat(), bar["closed_at"].isoformat(),
                                   str(decimal(bar["open"])), str(decimal(bar["close"]))]).encode())
    config = dict(version=VERSION, costs=asdict(costs), candidates=CANDIDATES,
                  train_count=train_count, observations=len(bars), dataset_sha256=dataset_sha256,
                  consumed_bars_sha256=content.hexdigest(),
                  end_policy="FORCED_CLOSE_BOTH_PARTITIONS", evaluation_policy="FRESH_CASH_COLD_SIGNALS")
    config_sha = digest(config)
    run = checkpoint or (lambda key, producer: producer())
    training = [run(f"{config_sha}-train-{name}", lambda name=name:
                   simulate(bars[:train_count], candidate=name, costs=costs)) for name in CANDIDATES]
    # Prefer the cash control on equal PnL; never mine evaluation to choose.
    selected = max(training, key=lambda row: (Decimal(row["net_pnl"]),
                   Decimal(row["max_drawdown_fraction"]).copy_negate(), -CANDIDATES.index(row["candidate"])))
    evaluation = run(f"{config_sha}-eval-{selected['candidate']}", lambda:
                     simulate(bars[train_count:], candidate=selected["candidate"], costs=costs))
    result = dict(schema=VERSION, evidence_class=EVIDENCE, config=config, config_sha256=config_sha,
        training=training, selected_candidate=selected["candidate"], evaluation=evaluation,
        network_orders=False, paid_model_calls=0, canonical_qualification=False,
        limitations=["NO_INTRABAR_DRAWDOWN", "NO_CAPACITY_OR_QUEUE_MODEL", "NO_EXCHANGE_LOT_RULES",
                     "RETROSPECTIVE_BAR_AVAILABILITY", "CANONICAL_FINALIST_VALIDATION_REQUIRED"])
    return {**result, "result_sha256": digest(result)}
