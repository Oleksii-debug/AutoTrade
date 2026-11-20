"""Chronological replay of the registered BTC/ETH relative-value rule."""
from collections import deque
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from math import log, sqrt
from data import CODE, ROOT, HOUR, WEEK, inputs, stamp

ASSETS = ("BTC", "ETH")
FEE = D(".0006")
SLIP = D(".001")

def signals(panel):
    eth = panel["ETH"][2]
    btc = panel["BTC"][2]
    times = sorted(set(eth) & set(btc))
    if any(b - a != HOUR for a, b in zip(times, times[1:])):
        raise ValueError("Missing synchronized price")
    history = deque()
    total = squares = 0.
    result = {}
    for t in times:
        x = log(float(eth[t][0] / btc[t][0]))
        if len(history) == 672:
            mean = total / 672
            sd = sqrt(max(0., (squares - total * mean) / 671))
            if sd:
                result[t + HOUR] = (x - mean) / sd
            old = history.popleft()
            total -= old
            squares -= old * old
        history.append(x)
        total += x
        squares += x * x
    return result

def charge(trade, price):
    return abs(trade) * price * (SLIP + FEE * (1 + SLIP if trade > 0 else 1 - SLIP))

def phase(panel, rules, z, start, end):
    cash = peak = last_week = D(1000)
    dd = charges = funding = D(0)
    weekly, closed = [], []
    qty, entry = {}, {}
    crossed = False
    for t in range(start, end + 1, HOUR):
        prices = {a: panel[a][2][t][0] for a in ASSETS}
        if qty and not t % (8 * HOUR):
            for a in ASSETS:
                rate, mark = panel[a][0][t]
                payment = -qty[a] * rate * mark
                cash += payment
                funding += payment
        marked = cash + sum(q * (prices[a] - entry[a]) for a, q in qty.items())
        gross = sum(abs(q) * prices[a] for a, q in qty.items())
        reserve_breach = bool(qty) and marked <= gross * D(".1")
        crossed |= reserve_breach
        liquidation = marked - sum(charge(-q, prices[a]) for a, q in qty.items())
        if qty:
            score = side * z[t]
            if score <= .25 or score >= 3.5 or t - opened >= WEEK or t == end or reserve_breach:
                charges += marked - liquidation
                cash = liquidation
                closed.append({"entry": opened, "exit": t, "net": str(cash - previous)})
                qty.clear()
        elif t < end and t in z and (t - start) % (4 * HOUR) == 0 and abs(z[t]) >= 2:
            side = 1 if z[t] > 0 else -1
            previous, opened = cash, t
            sizes = {}
            for a, sign in zip(ASSETS, (side, -side)):
                rule = rules[a + "_PERP"]
                step = D(rule["step"])
                size = (cash * D(".4") / (prices[a] * (1 + SLIP)) / step).to_integral_value(rounding=ROUND_DOWN) * step
                if not size or size < D(rule["min_qty"]) or size * prices[a] < D(rule["min_value"]):
                    break
                sizes[a] = sign * size
            if len(sizes) == 2:
                qty, entry = sizes, prices
                opening = sum(charge(q, prices[a]) for a, q in qty.items())
                charges += opening
                cash -= opening
                liquidation = cash - sum(charge(-q, prices[a]) for a, q in qty.items())
        peak = max(peak, liquidation)
        dd = max(dd, 1 - liquidation / peak)
        if t > start and (t - start) % WEEK == 0:
            weekly.append(liquidation / last_week - 1)
            last_week = liquidation
    assert not qty and abs(sum((D(r["net"]) for r in closed), D(0)) - (cash-1000)) < D("1e-15")
    return dict(net=cash-1000, fees_and_slippage=charges, funding=funding,
                weekly=weekly, weeks_at_target=sum(x>=D(".02") for x in weekly),
                drawdown=dd, mark_collateral_breach=crossed, trades=closed)

if __name__ == "__main__":
    protocol = (CODE / "relative-pairs.json").read_bytes()
    cuts = list(map(stamp, json.loads(protocol)["cuts_utc"]))
    panel, rules = inputs()
    scores = signals(panel)
    runs = [phase(panel, rules, scores, a, b) for a, b in zip(cuts, cuts[1:])]
    result = dict(protocol_sha256=sha256(protocol).hexdigest(), runs=runs,
                  eligible=all(r["net"] > 0 and not r["mark_collateral_breach"] for r in runs))
    (ROOT / "pairs-results.json").write_text(json.dumps(result, default=str, indent=2), encoding="utf8")
    print([(str(x["net"]), len(x["trades"]), x["weeks_at_target"]) for x in runs])
