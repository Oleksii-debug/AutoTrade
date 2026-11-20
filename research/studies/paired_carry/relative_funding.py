"""Select two perpetual sides using realized funding available before entry."""
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json

from data import CODE, ROOT, CUTS, DAY, HOUR, WEEK, inputs
from pairs import FEE, SLIP, charge
from trend_sides import hourly_lows


def rank(panel, markets, clock):
    cutoff = clock - DAY
    latest = cutoff - cutoff % (8*HOUR)
    clocks = range(latest-62*8*HOUR, latest+1, 8*HOUR)
    weekly = {}
    for market in markets:
        history = panel[market][0]
        weekly[market] = sum(history[t][0] for t in clocks) / 3
    ordered = sorted(markets, key=lambda m: (weekly[m], m))
    spread = weekly[ordered[-1]] - weekly[ordered[0]]
    # Two round trips for equal-size legs, priced per unit of one leg.
    return ordered[0], ordered[-1], spread, spread > 4*(SLIP+FEE)


def episode(panel, lows, rules, start, long, short, account):
    positions = {}
    initial_cost = D(0)
    for market, side in ((long, 1), (short, -1)):
        entry = panel[market][2][start][0]
        rule = rules[market+"_PERP"]
        step = D(rule["step"])
        qty = (account/4/(entry*(1+SLIP))/step).to_integral_value(rounding=ROUND_DOWN)*step
        if qty <= 0 or qty < D(rule["min_qty"]) or qty*entry < D(rule["min_value"]):
            raise ValueError("Insufficient trade size")
        positions[market] = (side*qty, entry)
        initial_cost += charge(side*qty, entry)
    funding = D(0)
    equity_path = [account]
    margin_ok = True
    for t in range(start+HOUR, start+WEEK+1, HOUR):
        prior_funding = funding
        worst_pnl = pnl = maintenance = exit_cost = D(0)
        for market,(q,entry) in positions.items():
            payments,_,bars = panel[market]
            if t % (8*HOUR) == 0:
                rate, mark = payments[t]
                funding -= q*rate*mark
            px = bars[t][0]
            high = bars[t-HOUR][1]
            extreme = lows[market][t-HOUR] if q > 0 else high
            worst_pnl += q*(extreme-entry) - charge(-q, extreme)
            maintenance += abs(q)*high*D(".1")
            pnl += q*(px-entry)
            exit_cost += charge(-q, px)
        margin_ok &= account-initial_cost+min(prior_funding,funding)+worst_pnl > maintenance
        equity_path.append(account-initial_cost+pnl+funding-exit_cost)
    return equity_path, dict(funding=str(funding), price_pnl=str(pnl),
                             costs=str(initial_cost+exit_cost), margin_ok=margin_ok)


def phase(panel, lows, rules, markets, first, last):
    balances = {"selected":D(1000), "reversed":D(1000)}
    peaks = dict(balances)
    drawdown = dict.fromkeys(balances,D(0))
    records = []
    for t in range(first,last,WEEK):
        long,short,edge,active = rank(panel,markets,t)
        record = dict(clock=t, long=long, short=short, spread=str(edge), active=active)
        for arm,(buy,sell) in (("selected",(long,short)),("reversed",(short,long))):
            before = balances[arm]
            metrics = dict(funding="0",price_pnl="0",costs="0",margin_ok=True)
            if active:
                path,metrics = episode(panel,lows,rules,t,buy,sell,before)
                for value in path:
                    peaks[arm] = max(peaks[arm],value)
                    drawdown[arm] = max(drawdown[arm],1-value/peaks[arm])
                balances[arm] = path[-1]
            record[arm] = dict(**metrics, net=str(balances[arm]-before),
                               weekly=str(balances[arm]/before-1))
        records.append(record)
    weekly = [D(r["selected"]["weekly"]) for r in records]
    excluding_best = balances["selected"]/(1+max(weekly))-1000
    return dict(records=records, balance={k:str(v) for k,v in balances.items()},
                drawdown={k:str(v) for k,v in drawdown.items()}, excluding_best=str(excluding_best),
                hits=sum(v>=D(".02") for v in weekly), active=sum(r["active"] for r in records),
                passes=balances["selected"]>max(1000,balances["reversed"]) and excluding_best>0
                       and all(r["selected"]["margin_ok"] for r in records))


if __name__ == "__main__":
    raw = (CODE/"relative-funding-rule.json").read_bytes()
    markets = json.loads(raw)["markets"]
    panel,rules = inputs()
    lows = {m:hourly_lows(m) for m in markets}
    blocks = [phase(panel,lows,rules,markets,a,b) for a,b in zip(CUTS,CUTS[1:])]
    report = dict(protocol_sha256=sha256(raw).hexdigest(), blocks=blocks,
                  eligible=all(b["passes"] for b in blocks))
    (ROOT/"relative-funding-results.json").write_text(json.dumps(report,indent=2))
    print([(b["active"],b["balance"],b["hits"]) for b in blocks])
