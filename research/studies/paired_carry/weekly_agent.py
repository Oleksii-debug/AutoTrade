"""Weekly walk-forward fitting and replay, with checkpointed decisions and matched controls."""
from datetime import date
from hashlib import sha256
import json
from math import log
from statistics import stdev
import numpy as np
from data import CODE, ROOT
from swing import BUY, SELL, DAY, D, buy_qty, authenticated_bars

WEEK = 7 * DAY

def features(panel, market, day):
    latest = day - 2*DAY
    px = lambda k: float(panel[market][latest-k*DAY]["close"])
    ret = lambda n: log(px(0)/px(n))
    btc = panel["BTC_USDT"]
    return [ret(7), ret(28), ret(84), stdev(log(px(i)/px(i+1)) for i in range(28)),
            px(0)/max(px(i) for i in range(28))-1,
            ret(7)-log(float(btc[latest]["close"]/btc[latest-WEEK]["close"]))]

def train(day, panel, samples):
    markets = sorted(panel)
    days = [day-i*WEEK for i in range(79,1,-1)]
    x = np.array([samples[d,m] for d in days for m in markets])
    y = np.array([float(panel[m][d+WEEK]["open"]/panel[m][d]["open"]-1)
                  for d in days for m in markets]).clip(-.25,.25)
    center, scale = x.mean(0), x.std(0)
    scale[scale<1.e-12] = 1.
    x = (x-center)/scale
    intercept = y.mean()
    w = np.linalg.solve(x.T@x + np.eye(x.shape[1])*.1*len(y), x.T@(y-intercept))
    scores = {m:float(intercept + ((samples[day,m]-center)/scale)@w) for m in markets}
    top = max(markets, key=scores.get)
    predicted_net = .5*((1+scores[top])*float(SELL/BUY)-1)
    return dict(asof=str(day),first_train=str(days[0]),last_outcome=str(days[-1]+WEEK),
                count=len(y),intercept=float(intercept),coefficients=w.tolist(),center=center.tolist(),
                scale=scale.tolist(),scores=scores,expected_net=predicted_net,
                target=top if predicted_net>=.02 else None)

def account(first, last, panel, rules, decisions, mode):
    cash = peak = monday_nav = D(1000)
    dd = costs = D(0)
    held = {}
    weeks, fills, active = [], [], 0
    t = first
    while t<=last:
        prices = {m: panel[m][t]["open"] for m in panel}
        equity = cash+sum(q*prices[m]*SELL for m,q in held.items())
        if t.weekday()==0:
            if t>first:
                weeks.append(equity/monday_nav-1)
                monday_nav=equity
            target=decisions[t]["target"] if t<last else None
            desired=tuple(sorted(panel)) if t<last and (mode=="passive" or (target and mode=="clock")) else ((target,) if target else ())
            if set(desired)!=set(held):
                for m,q in held.items():
                    cash+=q*prices[m]*SELL
                    costs+=q*prices[m]*(1-SELL)
                    fills.append((str(t),"sell",m,str(q),str(prices[m])))
                held={}
                budget=cash/2
                for m in desired:
                    q=buy_qty(budget/len(desired), prices[m], rules[m])
                    if not q: raise ValueError("Below minimum eligible order")
                    cash-=q*prices[m]*BUY
                    costs+=q*prices[m]*(BUY-1)
                    held[m]=q
                    fills.append((str(t),"buy",m,str(q),str(prices[m])))
            active+=bool(held)
        equity=cash+sum(q*panel[m][t]["close"]*SELL for m,q in held.items())
        peak=max(equity,peak)
        dd=max(dd,1-equity/peak)
        t+=DAY
    assert not held and len(weeks)==52
    return dict(net=cash-1000,active=active,weeks=weeks,at_target=sum(v>=D(".02") for v in weeks),
                geometric_weekly=(cash/1000)**(D(1)/len(weeks))-1,
                without_best=cash/(1+max(weeks))-1000, modeled_cost=costs,drawdown=dd,fills=fills)

def main():
    protocol=(CODE/"weekly-agent-rule.json").read_bytes()
    cfg=json.loads(protocol)
    cuts=list(map(date.fromisoformat,cfg["cuts"]))
    panel, ident={},{}
    for m in cfg["universe"]:
        path=ROOT/"daily"/"spot"/m/"1d"/"1640995200-1794787200.manifest.json"
        bars,ident[m],meta=authenticated_bars(path)
        if meta["market"]!=m or any(D(b["volume"])<=0 for b in bars):
            raise ValueError("Incomplete market")
        panel[m]={b["opened_at"].date():{k:D(b[k]) for k in ("open","close")} for b in bars}
    rules=json.loads((ROOT/"market-rules.json").read_bytes())
    t=cuts[0]-79*WEEK
    samples={}
    while t<cuts[-1]:
        for m in panel: samples[t,m]=np.array(features(panel,m,t))
        t+=WEEK
    out=dict(protocol_sha256=sha256(protocol).hexdigest(),data_sha256=ident,periods=[],decisions=[])
    for first,last in zip(cuts,cuts[1:]):
        decisions={first+i*WEEK:train(first+i*WEEK,panel,samples) for i in range(52)}
        out["decisions"].extend(decisions.values())
        results={s:account(first,last,panel,rules,decisions,s) for s in ("model","passive","clock")}
        candidate=results["model"]
        results["qualified"]=candidate["net"]>max(D(0),results["passive"]["net"],results["clock"]["net"]) and candidate["active"]>=10 and candidate["without_best"]>0
        out["periods"].append(results)
        print(first,[(s,str(results[s]["net"]),results[s]["active"]) for s in ("model","passive","clock")],candidate["at_target"],results["qualified"])
    (ROOT/"weekly-agent-results.json").write_text(json.dumps(out,default=str,indent=2))

if __name__=="__main__": main()
