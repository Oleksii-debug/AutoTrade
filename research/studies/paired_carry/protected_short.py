"""Daily downside channel with explicit adverse stop fills and settlement uncertainty."""
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from data import CODE, ROOT, DAY, HOUR, WEEK, stamp
from pairs import charge
from short_control import sample, run as short_reference


def load(market, first, last):
    bars, rates = {}, {}
    cursor = first - 30*DAY
    while cursor <= last:
        end = min(cursor + 999*DAY, last)
        batch, funding = sample(market, cursor, end, tag=f"{cursor}-{end}")
        if set(batch) & set(bars):
            raise ValueError("Overlapping daily prices")
        bars.update(batch)
        rates.update(funding)
        cursor = end + DAY
    return bars, rates


def signal(bars, day):
    k = day - 2*DAY
    if bars[k][4] >= min(bars[k-i*DAY][1] for i in range(1,21)):
        return None
    ranges = []
    for i in range(14):
        t = k-i*DAY
        low, high = bars[t][1:3]
        prev = bars[t-DAY][4]
        ranges.append(max(high-low,abs(high-prev),abs(low-prev)))
    atr = sum(ranges)/14
    return 2*atr if atr > 0 else None


def phase(panel,rules,left,right):
    wallet = peak = last_week = D(1000)
    costs = funding = drawdown = D(0)
    positions, trades, weeks = {}, [], []
    maintenance_failure = False
    def value(prices):
        return wallet + sum(p["q"]*(p["entry"]-prices[m])-charge(p["q"],prices[m])
                            for m,p in positions.items())
    def settle(m,price,day,reason):
        nonlocal wallet,costs
        p = positions.pop(m)
        fee = charge(p["q"],price)
        wallet += p["q"]*(p["entry"]-price)-fee
        costs += fee
        trades.append(dict(market=m,opened=p["date"],closed=day,reason=reason,
                           net=p["q"]*(p["entry"]-price)-fee-p["entry_cost"]+p["funding"]))
    for t in range(left,right+1,DAY):
        opens={m:v[0][t][0] for m,v in panel.items()}
        for m,p in positions.items():
            payment=p["q"]*panel[m][1][t]
            wallet+=payment
            funding+=payment
            p["funding"]+=payment
        nav=value(opens)
        peak=max(peak,nav)
        drawdown=max(drawdown,1-nav/peak)
        if t>left and (t-left)%WEEK==0:
            weeks.append(nav/last_week-1)
            last_week=nav
        if t==right:
            for m in list(positions): settle(m,opens[m],t,"terminal")
            break
        carried=set(positions)
        for m in carried:
            p=positions[m]
            bars=panel[m][0]
            p["stop"]=min(p["stop"],max(bars[t-i*DAY][2] for i in range(1,11)))
            if opens[m]>=p["stop"]: settle(m,opens[m],t,"opening gap")
        budget=value(opens)
        for m,(bars,_) in panel.items():
            if m in carried: continue
            distance=signal(bars,t)
            if distance is None: continue
            px=opens[m]
            stop=px+distance
            unit_loss=distance+charge(D(-1),px)+charge(D(1),stop)
            reserve=value(opens)-sum(p["q"]*opens[k] for k,p in positions.items())
            max_qty=min(budget*D(".005")/unit_loss,budget*D(".2")/(px*D("1.002")),
                        max(reserve,D(0))/(px*D("1.002")))
            rule=rules[m+"_PERP"]
            step=D(rule["step"])
            q=(max_qty/step).to_integral_value(rounding=ROUND_DOWN)*step
            if not q or q<D(rule["min_qty"]) or q*px<D(rule["min_value"]): continue
            fee=charge(-q,px)
            wallet-=fee
            costs+=fee
            positions[m]=dict(date=t,q=q,entry=px,stop=stop,entry_cost=fee,funding=D(0))
        # Stress every open leg to the whole candle's high; ignore any protective-stop benefit here.
        extreme={m:panel[m][0][t][2] for m in panel}
        funding_debits=D(0)
        for m,p in positions.items():
            funding_debits+=sum(min(D(0),p["q"]*panel[m][1][t+h*HOUR]) for h in (8,16))
        maintenance_failure |= value(extreme)+funding_debits<=sum(p["q"]*extreme[m]*D(".10") for m,p in positions.items())
        for m in list(positions):
            p=positions[m]
            stopped=extreme[m]>=p["stop"]
            for h in (8,16):
                payment=p["q"]*panel[m][1][t+h*HOUR]
                if stopped: payment=min(D(0),payment)
                wallet+=payment
                funding+=payment
                p["funding"]+=payment
            if stopped: settle(m,p["stop"],t,"protective buy stop")
        closing=value({m:v[0][t][4] for m,v in panel.items()})
        peak=max(peak,closing)
        drawdown=max(drawdown,1-closing/peak)
    assert not positions and len(weeks)==26
    if abs(sum((r["net"] for r in trades),D(0))-(wallet-1000))>D("1e-15"):
        raise AssertionError("Protected short ledger did not reconcile")
    return dict(net=wallet-1000,costs=costs,funding=funding,drawdown=drawdown,margin_failure=maintenance_failure,
                weeks=weeks,weeks_at_target=sum(w>=D(".02") for w in weeks),trades=trades)


def main():
    raw=(CODE/"protected-short-rule.json").read_bytes()
    cfg=json.loads(raw)
    cuts=list(map(stamp,cfg["cuts_utc_midnight"]))
    panel={m:load(m,cuts[0],cuts[-1]) for m in cfg["markets"]}
    rules=json.loads((ROOT/"market-rules.json").read_bytes())
    phases=[]
    for left,right in zip(cuts,cuts[1:]):
        control=short_reference(panel,rules,left,right,-1)
        covered=control["covered"] and all(
            all(t in bars and bars[t][3]>0 for t in range(left-22*DAY,left,DAY))
            for bars,_ in panel.values())
        if not covered:
            phases.append(dict(covered=False,control=control))
            print(left,"incomplete")
            continue
        candidate=phase(panel,rules,left,right)
        passes=(not candidate["margin_failure"] and candidate["net"]>max(D(0),control["net"])
                and candidate["drawdown"]<control["drawdown_daily"] and len(candidate["trades"])>=8)
        phases.append(dict(covered=True,control=control,candidate=candidate,passes=passes))
        print(left,str(candidate["net"]),str(control["net"]),candidate["weeks_at_target"],len(candidate["trades"]),passes)
    eligible=all(p.get("passes",False) for p in (phases[0],phases[-1]))
    (ROOT/"protected-short-results.json").write_text(json.dumps(dict(rule_sha256=sha256(raw).hexdigest(),
                    eligible=eligible,phases=phases),default=str,indent=2))

if __name__=="__main__": main()
