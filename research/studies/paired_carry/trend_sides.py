"""Matched long and short directional experiment with observed funding settlements."""
from datetime import datetime, UTC
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
from urllib.parse import urlsplit,parse_qs
import json
from data import CODE, ROOT, HOUR, WEEK, inputs, parse_klines, stamp
from pairs import charge
from swing import DAY, authenticated_bars

def hourly_lows(asset):
    lows={}
    for path in (ROOT/"data"/asset).glob(asset+"_PERP-*.json"):
        packet=json.loads(path.read_bytes())
        raw=packet["raw"].encode()
        if sha256(raw).hexdigest()!=packet["sha256"]: raise ValueError("Changed candle packet")
        uri=packet["uri"]
        args=parse_qs(urlsplit(uri).query)
        page=parse_klines(raw,asset+"_PERP","1h",datetime.fromisoformat(packet["received"]),
                          source_uri=uri,start=int(args["start"][0]),end=int(args["end"][0]),
                          requested_limit=int(args["limit"][0]),training=True)
        for b in page.candles: lows[int(b.open_time.timestamp())]=D(b.low)
    return lows

def direction(history, t):
    saturday=datetime.fromtimestamp(t,UTC).date()-2*DAY
    v=history[saturday]
    first,second=history[saturday-28*DAY],history[saturday-84*DAY]
    return int(v>max(first,second))-int(v<min(first,second))

def phase(panel,lows,daily,rules,start,end,arm):
    markets=list(daily)
    wallet=peak=prior_week=D(1000)
    fees=funded=dd=D(0)
    positions,entries={},{}
    weeks,activity=[],[]
    breached=False
    sides={-1:0,1:0}
    for t in range(start,end+1,HOUR):
        p={m:panel[m][2][t][0] for m in markets}
        if positions:
            adverse={m:lows[m][t-HOUR] if q>0 else panel[m][2][t-HOUR][1] for m,q in positions.items()}
            cash_start=wallet
            for m,q in positions.items():
                sides[1 if q>0 else -1]+=1
                if not t%(8*HOUR):
                    rate,mark=panel[m][0][t]
                    payment=-q*rate*mark
                    wallet+=payment
                    funded+=payment
            worst=min(wallet,cash_start)+sum(q*(adverse[m]-entries[m])-charge(-q,adverse[m]) for m,q in positions.items())
            # Upper-bound maintenance on total positions using the high for both signs.
            maintenance=sum(abs(q)*panel[m][2][t-HOUR][1]*D(".10") for m,q in positions.items())
            breached |= worst<=maintenance
        def value():
            return wallet+sum(q*(p[m]-entries[m])-charge(-q,p[m]) for m,q in positions.items())
        nav=value()
        peak=max(peak,nav)
        dd=max(dd,1-nav/peak)
        if (t-start)%WEEK: continue
        if t>start:
            weeks.append(nav/prior_week-1)
            prior_week=nav
        desired={}
        for m in markets:
            trend=direction(daily[m],t)
            desired[m]=0 if t==end else {"trend":trend,"inverted":-trend,"long":1,"short":-1}[arm]
        for m,q in list(positions.items()):
            if desired[m]*q>0: continue
            cost=charge(-q,p[m])
            wallet+=q*(p[m]-entries[m])-cost
            fees+=cost
            del positions[m]
            activity.append((t,m,"close",str(q),str(p[m])))
        budget=value()
        for m in markets:
            if m in positions or not desired[m]: continue
            available=value()-sum(abs(q)*p[k] for k,q in positions.items())
            notional=min(budget*D(".2"),available)
            rule=rules[m+"_PERP"]
            step=D(rule["step"])
            units=(max(D(0),notional)/(p[m]*D("1.002"))/step).to_integral_value(rounding=ROUND_DOWN)*step
            if not units or units<D(rule["min_qty"]) or units*p[m]<D(rule["min_value"]): continue
            q=desired[m]*units
            entries[m],positions[m]=p[m],q
            cost=charge(q,p[m])
            wallet-=cost
            fees+=cost
            activity.append((t,m,"enter",str(q),str(p[m])))
        dd=max(dd,1-value()/peak)
    assert not positions and len(weeks)==26
    return dict(net=wallet-1000,weeks=weeks,weeks_at_target=sum(w>=D(".02") for w in weeks),
                drawdown=dd,modeled_cost=fees,funding=funded,margin_failure=breached,position_hours=sides,fills=activity)

def main():
    raw=(CODE/"trend-sides-rule.json").read_bytes()
    cfg=json.loads(raw)
    panel,rules=inputs()
    daily,lows,ids={},{},{}
    for m in cfg["universe"]:
        bars,ids[m],_=authenticated_bars(ROOT/"daily"/"spot"/(m+"_USDT")/"1d"/"1640995200-1794787200.manifest.json")
        daily[m]={b["opened_at"].date():D(b["close"]) for b in bars}
        lows[m]=hourly_lows(m)
        if set(lows[m])!=set(panel[m][2]): raise ValueError("Missing high/low")
    cuts=list(map(stamp,cfg["cuts_utc"]))
    out=dict(rule_sha256=sha256(raw).hexdigest(),daily_ids=ids,periods=[])
    for a,b in zip(cuts,cuts[1:]):
        rows={arm:phase(panel,lows,daily,rules,a,b,arm) for arm in ("trend","inverted","long","short")}
        candidate=rows["trend"]
        rows["eligible"]=not candidate["margin_failure"] and candidate["net"]>max(D(0),*(rows[k]["net"] for k in ("long","short","inverted")))
        print({k:str(rows[k]["net"]) for k in ("trend","inverted","long","short")},candidate["weeks_at_target"],rows["eligible"])
        out["periods"].append(rows)
    (ROOT/"trend-sides-results.json").write_text(json.dumps(out,default=str,indent=2))

if __name__=="__main__": main()
