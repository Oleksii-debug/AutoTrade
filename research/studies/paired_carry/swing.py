"""Study a confirmed reversal at a prior low using one shared spot account."""
from datetime import date, timedelta
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from statistics import median
from data import CODE, ROOT, stamp
from tools.whitebit_historical_klines import collect_whitebit_history
from tools.screen_whitebit_trend_regime import authenticated_bars

DAY = timedelta(days=1)
COST = D(".001")
BUY = (1+COST)**2
SELL = (1-COST)**2

def setup(bars, t):
    signal = bars[t-2*DAY]
    past = [bars[t-i*DAY] for i in range(3, 17)]
    floor, target = min(p["low"] for p in past), max(p["high"] for p in past)
    volume = median(p["volume"] for p in past)
    if (volume > 0 and signal["low"] < floor < signal["close"]
            and 2*signal["close"] >= signal["high"]+signal["low"]
            and signal["volume"] > volume):
        return signal["volume"]/volume, signal["low"], target
    return None

def exit_fill(bar, stop, target):
    if bar["open"] <= stop:
        return bar["open"]
    if bar["open"] >= target:
        return target
    if bar["low"] <= stop:
        return stop
    if bar["high"] >= target:
        return target
    return None

def buy_qty(budget, price, rule):
    step = D(rule["step"])
    q = (budget / (price*BUY) / step).to_integral_value(rounding=ROUND_DOWN)*step
    return q if q >= D(rule["min_qty"]) and q*price >= D(rule["min_value"]) else D(0)

def phase(panel, rules, first, last):
    cash = peak = last_monday = D(1000)
    dd = D(0)
    holding = None
    trades, weeks = [], []
    t = first
    while t <= last:
        if holding:
            market, q, opened, before, stop, target = holding
            equity = cash + q*panel[market][t]["open"]*SELL
        else:
            equity = cash
        if t > first and t.weekday() == 0:
            weeks.append(equity/last_monday - 1)
            last_monday = equity
        if holding is None and t < last:
            candidates = [(s[0], m, s[1], s[2]) for m, bars in panel.items()
                          if (s := setup(bars, t))]
            if candidates:
                _, market, stop, target = max(candidates)
                price = panel[market][t]["open"]
                q = buy_qty(cash/2, price, rules[market]) if stop < price < target else D(0)
                if q:
                    before, opened = cash, t
                    cash -= q * price * BUY
                    holding = (market, q, opened, before, stop, target)
        if holding:
            bar = panel[market][t]
            sale = bar["open"] if t == last or t-opened >= 7*DAY else exit_fill(bar, stop, target)
            if sale is not None:
                cash += q * sale * SELL
                trades.append(dict(market=market, entry=opened, exit=t, net=cash-before))
                equity = cash
                holding = None
            else:
                equity = cash + q * bar["close"] * SELL
        peak = max(peak, equity)
        dd = max(dd, 1 - equity/peak)
        t += DAY
    passive = D(0)
    for market, bars in panel.items():
        q = buy_qty(D(500)/len(panel), bars[first]["open"], rules[market])
        passive += q*(bars[last]["open"]*SELL-bars[first]["open"]*BUY)
    net = cash - 1000
    ex_best = net - max((r["net"] for r in trades), default=D(0))
    if abs(sum((r["net"] for r in trades), D(0)) - net) > D("1e-16") or holding:
        raise AssertionError("Swing ledger did not reconcile")
    return dict(net=net, passive_net=passive, excluding_best=ex_best, trades=trades,
                eligible=net>0 and ex_best>0 and len(trades)>=10 and net>passive,
                weekly=weeks, weeks_at_target=sum(x>=D(".02") for x in weeks), drawdown_daily=dd)

def main():
    raw = (CODE/"swing-rule.json").read_bytes()
    cfg = json.loads(raw)
    cuts = list(map(date.fromisoformat, cfg["cuts"]))
    start, end = stamp(cfg["archive_first_day"]), stamp(cfg["cuts"][-1])
    panel, ids = {}, {}
    for market in cfg["universe"]:
        store = ROOT/"daily"
        manifest = store/"spot"/market/"1d"/f"{start}-{end}.manifest.json"
        if not manifest.exists():
            collect_whitebit_history(market=market, interval="1d", start=start, end=end,
                                     output=store, page_limit=1000, max_attempts=2)
        bars, ids[market], meta = authenticated_bars(manifest)
        if len(bars) != 1+(end-start)//86400 or meta["market"] != market:
            raise ValueError(f"Incomplete daily price series for {market}")
        panel[market] = {b["opened_at"].date(): {k:D(b[k]) for k in ("low","high","open","close","volume")}
                         for b in bars}
        if any(b["volume"] <= 0 for b in panel[market].values()):
            raise ValueError(f"Missing trading activity for {market}")
    rules = json.loads((ROOT/"market-rules.json").read_bytes())
    result = [phase(panel,rules,a,b) for a,b in zip(cuts,cuts[1:])]
    (ROOT/"swing-results.json").write_text(json.dumps(
        dict(protocol_sha256=sha256(raw).hexdigest(), data_sha256=ids, phases=result),default=str,indent=2))
    print([(str(r["net"]),str(r["passive_net"]),len(r["trades"]),r["weeks_at_target"],len(r["weekly"]),r["eligible"]) for r in result])

if __name__ == "__main__":
    main()
