"""Evaluate the successful recent short control on earlier, fixed calendar windows."""
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from data import CODE, ROOT, DAY, HOUR, WEEK, stamp, pack, WhiteBitPublicClient, parse_funding_history, parse_klines
from pairs import charge

def sample(asset,begin,end):
    client=WhiteBitPublicClient()
    market=asset+"_PERP"
    count=(end-begin)//DAY+1
    args=dict(start=begin,end=end+DAY,training=True)
    raw,received,uri=pack(ROOT/"older"/(market+"-daily.json"),
                         lambda:client.klines(market,interval="1d",limit=count,**args))
    data=parse_klines(raw,market,"1d",received,requested_limit=count,source_uri=uri,**args)
    bars={int(b.open_time.timestamp()):(D(b.open),D(b.low),D(b.high),D(b.base_volume)) for b in data.candles}
    rates={}
    for off in range(0,5000,100):
        raw,received,uri=pack(ROOT/"data"/asset/f"funding-{off}.json",
                              lambda:client.funding_history(market,limit=100,offset=off))
        page=parse_funding_history(raw,market,received,limit=100,offset=off)
        if page.source_uri!=uri: raise ValueError("Wrong funding endpoint")
        for r in page.records:
            t=int(r.funding_time.timestamp())
            v=D(r.funding_rate)*D(r.settlement_price)
            if t in rates and rates[t]!=v: raise ValueError("Conflicting funding history")
            rates[t]=v
        if not page.records or min(rates)<=begin or len(page.records)<100: break
    return bars,rates

def run(panel,rules,left,right,side):
    marks=set(range(left,right+1,DAY))
    settlements=set(range(left+8*HOUR,right+1,8*HOUR))
    gaps={}
    for asset,(bars,rates) in panel.items():
        absent=settlements-{t for t in rates if left<t<=right}
        extra={t for t in rates if left<t<=right}-settlements
        missing=marks-{t for t,row in bars.items() if row[3]>0}
        if absent or extra or missing:
            gaps[asset]=dict(missing_funding=len(absent),changed_schedule=len(extra),missing_prices=len(missing))
    if gaps: return dict(covered=False,gaps=gaps)
    wallet=peak=prev=D(1000)
    dd=funding=fees=D(0)
    positions={}
    for m,(bars,_) in panel.items():
        px=bars[left][0]
        rule=rules[m+"_PERP"]
        step=D(rule["step"])
        q=(D(200)/(px*D("1.002"))/step).to_integral_value(rounding=ROUND_DOWN)*step
        if q<D(rule["min_qty"]) or q*px<D(rule["min_value"]): raise ValueError("Below minimum")
        q*=side
        positions[m]=(q,px)
        fees+=charge(q,px)
    wallet-=fees
    weeks=[]
    breached=False
    for t in sorted(marks):
        if t>left:
            before=wallet
            for mark in (t-16*HOUR,t-8*HOUR,t):
                payment=sum(-q*panel[m][1][mark] for m,(q,_) in positions.items())
                wallet+=payment
                funding+=payment
                before=min(before,wallet)
            worst=before
            maintenance=D(0)
            for m,(q,px) in positions.items():
                _,low,high,_=panel[m][0][t-DAY]
                adverse=low if q>0 else high
                worst+=q*(adverse-px)-charge(-q,adverse)
                maintenance+=abs(q)*high*D(".1")
            breached |= worst<=maintenance
        nav=wallet
        for m,(q,px) in positions.items():
            price=panel[m][0][t][0]
            nav+=q*(price-px)-charge(-q,price)
            if t==right: fees+=charge(-q,price)
        peak=max(peak,nav)
        dd=max(dd,1-nav/peak)
        if t>left and not (t-left)%WEEK:
            weeks.append(nav/prev-1)
            prev=nav
    assert len(weeks)==26
    return dict(covered=True,net=nav-1000,margin_failure=breached,modeled_cost=fees,funding=funding,
                drawdown_daily=dd,weeks=weeks,weeks_at_target=sum(w>=D(".02") for w in weeks))

if __name__=="__main__":
    raw=(CODE/"short-regime-rule.json").read_bytes()
    spec=json.loads(raw)
    cuts=list(map(stamp,spec["utc_midnight_cuts"]))
    panel={m:sample(m,cuts[0],cuts[-1]) for m in spec["universe"]}
    rules=json.loads((ROOT/"market-rules.json").read_bytes())
    out=[]
    for left,right in zip(cuts,cuts[1:]):
        pair={s:run(panel,rules,left,right,direction) for s,direction in (("short",-1),("long",1))}
        print({s:(str(v["net"]),v["margin_failure"],v["weeks_at_target"]) if v["covered"] else v["gaps"] for s,v in pair.items()})
        out.append(pair)
    (ROOT/"short-control-results.json").write_text(json.dumps(dict(rule_sha256=sha256(raw).hexdigest(),periods=out),default=str,indent=2))
