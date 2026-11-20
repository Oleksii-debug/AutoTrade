"""Four-week selection, continuous hedge accounting, and independently reset chronological cuts."""
from bisect import bisect_left, bisect_right
from datetime import UTC,datetime
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from statistics import median
from data import CODE,ROOT,CUTS,HOUR,DAY,WEEK,inputs

ZERO=D(0)
ONE=D(1)
SF=D(".001")
PF=D(".0006")
MAX_GAP=8*HOUR

def utc(t):
    return datetime.fromtimestamp(t,UTC).isoformat()

def covered(times,lo,hi):
    # Exact continuous coverage needs both ends plus all intervening settlements.
    i=bisect_right(times,lo)-1
    j=bisect_left(times,hi)
    if i<0 or j>=len(times): return False
    return all(v-u<=MAX_GAP for u,v in zip(times[i:j],times[i+1:j+1]))

def forecasts(funds,times,t):
    cutoff=t-HOUR
    out={}
    for a,h in funds.items():
        ts=times[a]
        # Past-only coverage: never consult a settlement at or after the cutoff.
        stop=bisect_left(ts,cutoff)
        if not stop: continue
        last=ts[stop-1]
        if cutoff-last>MAX_GAP or not covered(ts[:stop],cutoff-4*WEEK,last): continue
        def net(days):
            return sum((h[k][0] for k in ts[bisect_left(ts,cutoff-days*DAY):stop]),ZERO)
        out[a]=min(net(28),4*net(7))-2*(SF+PF)-4*D(".001")-D(".0025")
    return out

def choice(scores, incumbent):
    positive={k:v for k,v in scores.items() if v>0}
    if not positive: return None
    best=min(positive,key=lambda k:(-positive[k],k))
    if incumbent in positive and positive[best]-positive[incumbent]<=D(".0025"): return incumbent
    return best

class Account:
    def __init__(self,cash,slip):
        self.cash=cash
        self.slip=slip
        self.asset=None
        self.q=self.short_price=self.spot_price=ZERO
        self.fees=self.funding=self.basis=self.impact=ZERO

    def enter(self,asset,spot,perp,rules):
        sr,pr=rules[asset+"_USDT"],rules[asset+"_PERP"]
        step=max(D(sr["step"]),D(pr["step"]))
        q=(self.cash*D(".4")/(max(spot,perp)*(1+self.slip))/step).to_integral_value(rounding=ROUND_DOWN)*step
        if q<max(D(sr["min_qty"]),D(pr["min_qty"])) or q*min(spot,perp)<max(D(sr["min_value"]),D(pr["min_value"])):
            return False
        assert q%D(sr["step"])==0 and q%D(pr["step"])==0
        self.asset,self.q=asset,q
        self.spot_price,self.short_price=spot,perp
        self.short_fill=perp*(1-self.slip)
        buy=spot*(1+self.slip)
        fees=q*(buy*SF+self.short_fill*PF)
        self.cash-=q*buy+fees
        self.fees+=fees
        self.impact+=q*(spot+perp)*self.slip
        return True

    def value(self,spot,perp):
        sale,cover=spot*(1-self.slip),perp*(1+self.slip)
        return self.cash+self.q*(sale+self.short_fill-cover-sale*SF-cover*PF)

    def close(self,spot,perp):
        self.cash=self.value(spot,perp)
        self.basis+=self.q*(spot-self.spot_price+self.short_price-perp)
        self.fees+=self.q*(spot*(1-self.slip)*SF+perp*(1+self.slip)*PF)
        self.impact+=self.q*(spot+perp)*self.slip
        self.q=ZERO
        self.asset=None

def phase(panel,rules,start,end,policy,slip):
    funds={a:rows[0] for a,rows in panel.items()}
    times={a:sorted(h) for a,h in funds.items()}
    wallet=Account(D(1000),slip)
    peak=D(1000)
    drawdown=ZERO
    weeks,events,choices=[],[],[]
    last_week=peak
    covered_ok=margin_ok=True
    held_hours=0
    missing=set()
    for t in range(start,end+1,HOUR):
        a=wallet.asset
        if a:
            h,spot,perp=panel[a]
            ts=times[a]
            cash_before_funding=wallet.cash
            for f in ts[bisect_right(ts,t-HOUR):bisect_right(ts,t)]:
                rate,settled=h[f]
                cashflow=wallet.q*rate*settled
                wallet.cash+=cashflow
                wallet.funding+=cashflow
            if t not in spot or t not in perp:
                raise ValueError("Missing held-market synchronized mark: "+a+" "+utc(t))
            # Last completed hour's maximum tests short-side collateral independently of spot gains.
            prev=perp[t-HOUR][1]*(1+slip)
            margin_ok &= min(cash_before_funding,wallet.cash)+wallet.q*(wallet.short_fill-prev)>wallet.q*prev*(D(".10")+PF)
            held_hours+=1
            if not covered(ts,t-HOUR,t): covered_ok=False
            equity=wallet.value(spot[t][0],perp[t][0])
        else: equity=wallet.cash
        peak=max(peak,equity)
        drawdown=max(drawdown,1-equity/peak)
        if t>start and (t-start)%WEEK==0:
            weeks.append(equity/last_week-1)
            last_week=equity
        if t==end: target=None
        elif (t-start)%(4*WEEK)==0:
            scores=forecasts(funds,times,t)
            target="BTC" if policy=="btc" else choice(scores,a)
            choices.append(dict(time=utc(t),target=target,scores=scores))
        else: continue
        if target==a: continue
        if a:
            if min(panel[a][1][t][2],panel[a][2][t][2])<=0: missing.add((a,utc(t)))
            wallet.close(panel[a][1][t][0],panel[a][2][t][0])
            events.append(dict(time=utc(t),close=a,cash=wallet.cash))
        if target:
            _,spot,perp=panel[target]
            if t not in spot or t not in perp or min(spot[t][2],perp[t][2])<=0:
                missing.add((target,utc(t)))
            elif wallet.enter(target,spot[t][0],perp[t][0],rules):
                events.append(dict(time=utc(t),enter=target,qty=wallet.q,spot=spot[t][0],perp=perp[t][0]))
    assert not wallet.asset and len(weeks)==26
    assert abs(wallet.cash-1000-wallet.basis-wallet.funding+wallet.fees+wallet.impact)<D("1e-15")
    return dict(net_usdt=wallet.cash-1000,geometric_weekly=(wallet.cash/1000)**(ONE/len(weeks))-1,
                median_weekly=median(weeks),weeks_at_2pct=sum(w>=D(".02") for w in weeks),
                positive_weeks=sum(w>0 for w in weeks),drawdown=drawdown,
                funding=wallet.funding,basis=wallet.basis,fees=wallet.fees,impact=wallet.impact,
                covered=covered_ok and not missing,margin_ok=margin_ok,missing=sorted(missing),
                held_hours=held_hours,weeks=weeks,events=events,decisions=choices)

def main():
    panel,rules=inputs()
    out={"protocol_sha256":sha256((CODE/"protocol.json").read_bytes()).hexdigest(),"results":{}}
    for policy in ("rotation","btc"):
        groups=out["results"][policy]={}
        for name,slip in (("base",D(".0003")),("stress",D(".001"))):
            phases=groups[name]=[phase(panel,rules,i,j,policy,slip) for i,j in zip(CUTS,CUTS[1:])]
            print(policy,name,[tuple(str(p[k])[:10] for k in ("net_usdt","funding","basis","fees","impact","median_weekly","weeks_at_2pct","covered","margin_ok")) for p in phases])
        groups["eligible"]=all(p["net_usdt"]>0 and p["covered"] and p["margin_ok"] for p in groups["stress"])
    (ROOT/"results.json").write_text(json.dumps(out,default=str,indent=2),encoding="utf8")

if __name__=="__main__": main()
