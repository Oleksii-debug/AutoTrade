"""Settle the registered UTC clock study against retained public quotes."""

from argparse import ArgumentParser
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
from itertools import product
import json
from pathlib import Path
import re
from statistics import median

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.strategies.two_agent_weekly import prepare
from tools.screen_whitebit_trend_regime import authenticated_bars


MARKETS = ("BTC_PERP", "ETH_PERP", "SOL_PERP")
BAR = timedelta(minutes=15)
ZERO = Decimal(0)
INITIAL = Decimal(1000)
FEE = Decimal("0.00055")
RATES = {"base": (Decimal("0.0001"), Decimal("0.0003"), Decimal("0.0002")),
         "stress": (Decimal("0.0004"), Decimal("0.0006"), Decimal("0.00095"))}
ARMS = ("evening_stop", "evening_fixed", "evening_reverse_stop", "morning_stop")
MINIMUM_EVENING_BASKETS = 16
MINIMUM_SIGNAL_COVERAGE = Decimal("0.90")
PROTOCOL_SHA = "249946ab881da9a7bedaad4dcec4cd9eb18b0747d8b5c56197c92b8d7154439b"
PHASES = {"morning": (6, 10), "evening": (17, 21)}
SHA256 = re.compile(r"(?:sha256:)?[a-f0-9]{64}\Z")


def digest(document):
    return sha256(json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def timestamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no offset")
    return parsed.astimezone(UTC)


def synchronized_quotes(rows, boundary):
    """Choose by time alone. Capacity and P&L never decide which quote is read."""
    groups = {market: [] for market in MARKETS}
    seen = set()
    for row in rows:
        previous = (row["market"], row["bucket_epoch"])
        at = timestamp(row["received_at"])
        if (row["market"] not in groups or previous in seen or previous[1] != int(at.timestamp())
                or type(row["generation"]) is not int or row["generation"] < 0
                or type(row["update_id"]) is not int or row["update_id"] <= 0
                or not SHA256.fullmatch(row["source_sha256"])
                or not boundary <= at < boundary + timedelta(seconds=5)):
            raise ValueError("Quote clock or market differs from the registered window")
        seen.add(previous)
        groups[row["market"]].append((at, row))
    best = None
    for group in product(*(groups[market] for market in MARKETS)):
        clocks = tuple(at for at, _ in group)
        latest = max(clocks)
        if latest - min(clocks) > timedelta(seconds=1):
            continue
        key = (latest, clocks)
        if best is None or key < best[0]:
            best = key, tuple(row for _, row in group)
    return None if best is None else best[1]


def observation(market, index):
    if any(bar in market.zero_volume_indices for bar in range(index, index + 3)):
        return None
    return market.closes[index + 2] / market.opens[index] - 1


def impulse(markets, index):
    movement = tuple(observation(market, index) for market in markets)
    if None in movement:
        return None
    scales = []
    for market in markets:
        prior = [observation(market, index - day * 96) for day in range(1, 29)]
        valid = [abs(value) for value in prior if value is not None]
        if len(valid) < 24 or (scale := median(valid)) <= 0:
            return None
        scales.append(scale)
    direction = 1 if min(movement) > 0 else -1 if max(movement) < 0 else 0
    strength = median(abs(change) / scale for change, scale in zip(movement, scales, strict=True))
    return direction if strength >= 2 else 0


def exit_index(markets, entry, direction, use_stop, adverse, carry):
    """Keep the frozen close-based stop trigger; historical close is only a trigger."""
    expiration = entry + 16
    if not use_stop:
        return expiration
    decision_entry = tuple(market.opens[entry] for market in markets)
    each = INITIAL / 6 / (1 + FEE)
    quantities = tuple(each / (price * (1 + direction * adverse)) for price in decision_entry)
    entry_fee = FEE * each * 3
    for clock in range(entry + 2, expiration):
        last = clock - 2
        if any(last in market.zero_volume_indices for market in markets):
            continue
        closing = tuple(market.closes[last] * (1 - direction * adverse) for market in markets)
        gross = sum(quantity * direction * (price - reference * (1 + direction * adverse))
                    for quantity, price, reference in zip(quantities, closing, decision_entry, strict=True))
        fees = entry_fee + FEE * sum(qty * px for qty, px in zip(quantities, closing, strict=True))
        funding = each * 3 * carry * (last + 1 - entry) / 96
        if gross - fees - funding <= -INITIAL * Decimal("0.0075"):
            return clock
    return expiration


def prices(row, direction):
    bid, ask = Decimal(row["bid_price"]), Decimal(row["ask_price"])
    bid_size, ask_size = Decimal(row["bid_quantity"]), Decimal(row["ask_quantity"])
    if not all(x.is_finite() and x > 0 for x in (bid, ask, bid_size, ask_size)) or bid > ask:
        raise ValueError("Invalid public top-of-book evidence")
    return (ask, ask_size) if direction > 0 else (bid, bid_size)


def quote_return(entry, exit_, direction, slipped, carry, held_bars):
    """Model one 500-USDT basket using the observed side and displayed size."""
    if entry is None or exit_ is None:
        return None, "MISSING_SYNCHRONIZED_QUOTES"
    each = INITIAL / 6 / (1 + FEE)
    pnl = ZERO
    for before, after in zip(entry, exit_, strict=True):
        if (before["generation"] != after["generation"]
                or before["update_id"] > after["update_id"]):
            return None, "SOURCE_DISCONTINUITY"
        in_px, available_in = prices(before, direction)
        out_px, available_out = prices(after, -direction)
        in_px *= 1 + direction * slipped
        out_px *= 1 - direction * slipped
        quantity = each / in_px
        if min(available_in, available_out) < quantity:
            return None, "INSUFFICIENT_DISPLAYED_SIZE"
        gross = quantity * direction * (out_px - in_px)
        pnl += gross - FEE * quantity * (in_px + out_px) - each * carry * held_bars / 96
    return pnl / INITIAL, "OBSERVED_QUOTE_PROXY"


def market_panel(manifest_paths, first, last):
    all_bars, identities = {}, {}
    warmup = first - timedelta(days=28)
    for path in manifest_paths:
        bars, identity, metadata = authenticated_bars(path)
        market = metadata["market"]
        if market in all_bars or market not in MARKETS or metadata["interval"] != "15m":
            raise ValueError("Expected three distinct perpetual M15 cuts")
        if bars[0]["opened_at"] != warmup or bars[-1]["closed_at"] != last:
            raise ValueError("Historical bars differ from the exact prospective boundaries")
        all_bars[market] = bars
        identities[market] = identity
    return prepare(all_bars, step=15), identities


def phase_clocks(day, phase):
    start, stop = PHASES[phase]
    first = datetime.combine(day, time(start), tzinfo=UTC)
    return tuple(first + offset * BAR for offset in range((stop - start) * 4 + 1))


def load_phase(root, protocol, day, phase):
    clocks = phase_clocks(day, phase)
    names = {clock.isoformat() for clock in clocks}
    file = root / "captures" / day.isoformat() / (phase + ".json")
    if not file.exists():
        return {}, ""
    if file.is_symlink() or not file.is_file() or file.stat().st_size > 512_000:
        raise ValueError("Invalid custody file")
    packet = json.loads(file.read_bytes())
    signature = packet.pop("document_sha256")
    windows, states = packet["windows"], packet["window_status"]
    if (digest(packet) != signature or packet["protocol_sha256"] != PROTOCOL_SHA
            or packet["schema"] != "autotrade-calendar-forward-bbo-window-v1"
            or packet["study_id"] != protocol["study_id"] or packet["day_utc"] != day.isoformat()
            or packet["phase"] != phase or packet["orders"] != 0 or packet["network_requests"] != 0
            or packet["source"]["table"] != "execution_bbo"
            or timestamp(packet["sealed_at_utc"]) < clocks[-1] + timedelta(seconds=120)
            or set(windows) != names or set(states) != names):
        raise ValueError("Public quote receipt identity differs")
    for key, rows in windows.items():
        if (not isinstance(rows, list) or len(rows) > 15
                or states[key] not in ("OBSERVED", "SOURCE_GAP", "EXPIRED")
                or ({row["market"] for row in rows} == set(MARKETS)) != (states[key] == "OBSERVED")):
            raise ValueError("Invalid or incomplete clock coverage")
    return {clock: synchronized_quotes(windows[clock.isoformat()], clock) for clock in clocks}, signature


def evaluate_day(markets, origin, day, quotes, rate):
    slipped, carry, trigger_adverse = rate
    evening, morning = (phase_clocks(day, phase)[0] for phase in ("evening", "morning"))
    results = {}
    for name in ARMS:
        entry_time = morning if name == "morning_stop" else evening
        entry = (entry_time - origin) // BAR
        signal = impulse(markets, entry - 4)
        if signal is None:
            results[name] = None, "MISSING_SIGNAL_DATA"
        elif not signal:
            results[name] = ZERO, "HOLD"
        else:
            direction = -signal if name == "evening_reverse_stop" else signal
            stop = exit_index(markets, entry, direction, name != "evening_fixed", trigger_adverse, carry)
            exit_time = origin + stop * BAR
            results[name] = quote_return(quotes.get(entry_time), quotes.get(exit_time), direction,
                                        slipped, carry, stop - entry)
    return results


def aggregate(days, first, last):
    equity = {arm: INITIAL for arm in ARMS}
    peak = equity.copy()
    drawdown = dict.fromkeys(ARMS, ZERO)
    known = {}
    matched_days = attempts = valid_evening_signals = 0
    pending_reasons = {}
    for day, results in days.items():
        reasons = [reason for value, reason in results.values() if value is None]
        known[day] = not reasons
        if results["evening_stop"][1] not in ("MISSING_SIGNAL_DATA", "HOLD"):
            attempts += 1
            valid_evening_signals += not reasons
        if reasons:
            for reason in set(reasons):
                pending_reasons[reason] = pending_reasons.get(reason, 0) + 1
            continue
        matched_days += 1
        for arm, (value, _) in results.items():
            equity[arm] *= 1 + value
            peak[arm] = max(peak[arm], equity[arm])
            drawdown[arm] = max(drawdown[arm], 1 - equity[arm] / peak[arm])
    first_monday = first + timedelta(days=(-first.weekday()) % 7)
    weekly_returns = []
    for day_index in range((last - first_monday).days // 7):
        week = first_monday + timedelta(weeks=day_index)
        dates = [week + timedelta(days=i) for i in range(7)]
        if not all(known[day] for day in dates):
            continue
        ret = Decimal(1)
        for day in dates:
            ret *= 1 + days[day]["evening_stop"][0]
        weekly_returns.append(ret - 1)
    coverage = Decimal(valid_evening_signals) / attempts if attempts else ZERO
    candidate = equity["evening_stop"]
    reasons = []
    if valid_evening_signals < MINIMUM_EVENING_BASKETS:
        reasons.append("INSUFFICIENT_EVENING_BASKETS")
    if coverage < MINIMUM_SIGNAL_COVERAGE or Decimal(matched_days) / len(days) < MINIMUM_SIGNAL_COVERAGE:
        reasons.append("INSUFFICIENT_COVERAGE")
    if candidate <= INITIAL:
        reasons.append("NONPOSITIVE_NET")
    if any(candidate <= equity[arm] for arm in ARMS[1:]):
        reasons.append("NO_EDGE_OVER_MATCHED_CONTROLS")
    if drawdown["evening_stop"] > Decimal("0.12"):
        reasons.append("SETTLED_DRAWDOWN_LIMIT")
    return {
        "matched_days": matched_days, "unavailable_days_by_reason": pending_reasons,
        "evening_signal_attempts": attempts, "qualified_evening_baskets": valid_evening_signals,
        "matched_evening_signal_fraction": str(coverage),
        "returns": {arm: str(value / INITIAL - 1) for arm, value in equity.items()},
        "settled_day_drawdowns": {arm: str(value) for arm, value in drawdown.items()},
        "weekly": {"scored_complete_weeks": len(weekly_returns),
                   "median": str(median(weekly_returns)) if weekly_returns else None,
                   "3_percent_hits": sum(ret >= Decimal("0.03") for ret in weekly_returns),
                   "5_percent_hits": sum(ret >= Decimal("0.05") for ret in weekly_returns)},
        "reasons": reasons,
    }


def evaluate(root, protocol, manifest_paths, first, last):
    (origin, markets), identities = market_panel(manifest_paths, first, last)
    days = {scenario: {} for scenario in RATES}
    receipt_hashes = {}
    for offset in range((last - first).days):
        day = first.date() + timedelta(days=offset)
        quotes = {}
        for phase in PHASES:
            evidence, identity = load_phase(root, protocol, day, phase)
            quotes.update(evidence)
            receipt_hashes[f"{day.isoformat()}/{phase}"] = identity
        for scenario, rates in RATES.items():
            days[scenario][day] = evaluate_day(markets, origin, day, quotes, rates)
    outcomes = {name: aggregate(rows, first.date(), last.date()) for name, rows in days.items()}
    return {
        "schema": "autotrade-calendar-forward-score-v1", "protocol_sha256": PROTOCOL_SHA,
        "source_ohlcv": identities, "capture_document_hashes": receipt_hashes, "cash_return": "0",
        "outcomes": outcomes, "execution_class": "OBSERVED_TOP_OF_BOOK_SIMULATION_ONLY",
        "qualified_for_research": not any(row["reasons"] for row in outcomes.values()),
        "automatic_promotion": False, "network_requests": 0, "orders": 0, "paid_model_calls": 0,
    }


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--study-dir", required=True, type=Path)
    parser.add_argument("--manifest", nargs=3, type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    raw = args.study_dir.joinpath("protocol.json").read_bytes()
    if sha256(raw).hexdigest() != PROTOCOL_SHA:
        raise ValueError("Registered study protocol differs")
    protocol = json.loads(raw)
    first, last = (timestamp(protocol[key]) for key in ("start_utc", "end_exclusive_utc"))
    if datetime.now(UTC) < last:
        print(json.dumps({"status": "WAITING_FOR_TERMINAL_SAMPLE", "end_exclusive_utc": last.isoformat(),
                          "scored": False, "network_requests": 0, "orders": 0}))
        return
    if not args.manifest or not args.output_dir:
        parser.error("terminal scoring requires three exact market manifest paths and --output-dir")
    with localcontext() as ctx:
        ctx.prec = 36
        result = evaluate(args.study_dir, protocol, args.manifest, first, last)
    result["result_sha256"] = digest(result)
    output = args.output_dir / (result["result_sha256"] + ".json")
    atomic_write_json(output, result)
    print(json.dumps({"output": str(output), "result_sha256": result["result_sha256"]}))


if __name__ == "__main__":
    main()
