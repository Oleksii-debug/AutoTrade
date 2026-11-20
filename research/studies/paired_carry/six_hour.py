"""One BTC perpetual paper learner, trained on strictly earlier six-hour outcomes."""
from datetime import datetime, UTC
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from urllib.parse import parse_qs, urlsplit
import numpy as np
from data import CODE, ROOT, HOUR, WEEK, CUTS, parse_klines, parse_funding_history
from pairs import FEE, SLIP, charge

MARKET = "BTC_PERP"
STEP = 6*HOUR
OUT = ROOT/"six-hour"
RULE = sha256((CODE/"six-hour-rule.json").read_bytes()).hexdigest()

def base_data():
    bars, funding = {}, {}
    for path in sorted((ROOT/"data"/"BTC").glob("*.json")):
        if not (path.name.startswith(MARKET+"-") or path.name.startswith("funding-")):
            continue
        p = json.loads(path.read_bytes())
        raw = p["raw"].encode()
        if sha256(raw).hexdigest() != p["sha256"]:
            raise ValueError("Public archive hash mismatch")
        received = datetime.fromisoformat(p["received"])
        if path.name.startswith("funding-"):
            offset = int(path.stem.split("-")[1])
            page = parse_funding_history(raw, MARKET, received, limit=100, offset=offset)
            if page.source_uri != p["uri"]:
                raise ValueError("Funding source mismatch")
            for r in page.records:
                t = int(r.funding_time.timestamp())
                value = D(r.funding_rate)*D(r.settlement_price)
                if t in funding and value != funding[t]:
                    raise ValueError("Conflicting funding history")
                funding[t] = value
        else:
            params = parse_qs(urlsplit(p["uri"]).query)
            page = parse_klines(raw, MARKET, "1h", received, source_uri=p["uri"],
                                requested_limit=int(params["limit"][0]),start=int(params["start"][0]),
                                end=int(params["end"][0]),training=True)
            for b in page.candles:
                t = int(b.open_time.timestamp())
                if t in bars or D(b.base_volume)<=0:
                    raise ValueError("Invalid hourly history")
                bars[t] = (D(b.open),D(b.close),D(b.low),D(b.high))
    return bars, funding

def features(bars, entry):
    last = entry-2*HOUR  # close of this candle is known at entry minus one hour
    last_close = float(bars[last][1])
    momentum = [np.log(last_close/float(bars[last-h*HOUR][1])) for h in (6,24,72)]
    prices = [float(bars[last-i*HOUR][1]) for i in range(25)]
    return np.array([*momentum, np.std(np.diff(np.log(prices)),ddof=1)])

def flow(funding,start,end):
    grid = 8*HOUR
    return sum((funding[t] for t in range((start//grid+1)*grid,end+1,grid)),D(0))

def fit(bars,funding,t,cache):
    def sample(k):
        if k not in cache: cache[k]=features(bars,k)
        return cache[k]
    epochs = list(range(t-361*STEP,t-STEP,STEP))
    x = np.stack([sample(k) for k in epochs])
    std = x.std(0)
    std[std<1.e-12] = 1
    distance = np.sum(((x-sample(t))/std)**2,axis=1)
    closest = sorted(range(len(epochs)),key=lambda i:(distance[i], epochs[i]))[:36]
    neighbors = [epochs[i] for i in closest]
    labels = [float((bars[k+STEP][0]-bars[k][0]-flow(funding,k,k+STEP))/bars[k][0]) for k in neighbors]
    mean = float(np.mean(labels))
    se = float(np.std(labels,ddof=1)/np.sqrt(len(labels)))
    cost = 2*float(FEE+SLIP)
    side = (1 if mean>0 else -1) if abs(mean)>cost+se else 0
    return dict(side=side, expected=mean,standard_error=se,round_trip=cost,neighbors=neighbors,
                labels=labels,features=sample(t).tolist(),scale=std.tolist(),
                last_training_endpoint=epochs[-1]+STEP,cutoff=t-HOUR)

def quantity(balance,price,rule):
    step=D(rule["step"])
    q=(balance*D(".4")/(price*(1+SLIP))/step).to_integral_value(rounding=ROUND_DOWN)*step
    if q<D(rule["min_qty"]) or q*price<D(rule["min_value"]): return D(0)
    return q

def history():
    bars,funding=base_data()
    rule=json.loads((ROOT/"market-rules.json").read_bytes())[MARKET]
    end=CUTS[-1]
    cache={}
    blocks=[]
    for start in (end-28*WEEK,end-14*WEEK):
        equity={"agent":D(1000),"reverse":D(1000)}
        marks=[D(1000)]
        episodes=[]
        for t in range(start,start+14*WEEK,STEP):
            m=fit(bars,funding,t,cache)
            entry,exit_px=bars[t][0],bars[t+STEP][0]
            carry=flow(funding,t,t+STEP)
            episode={"clock":t,"model":m}
            for arm,sign in (("agent",m["side"]),("reverse",-m["side"])):
                q=sign*quantity(equity[arm],entry,rule)
                price_pnl=q*(exit_px-entry)
                payments=-q*carry
                cost=charge(q,entry)+charge(-q,exit_px)
                net=price_pnl+payments-cost
                equity[arm]+=net
                episode[arm]=dict(q=str(q),net=str(net),cost=str(cost),funding=str(payments),price_pnl=str(price_pnl))
            if (t+STEP-start)%WEEK==0: marks.append(equity["agent"])
            episodes.append(episode)
        weekly=[b/a-1 for a,b in zip(marks,marks[1:])]
        blocks.append(dict(start=start,equity={k:str(v) for k,v in equity.items()},episodes=episodes,
                           weekly=list(map(str,weekly)),hits=sum(r>=D(".02") for r in weekly)))
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/"history.json").write_text(json.dumps(dict(rule=RULE,blocks=blocks),separators=(",",":")))
    print([(b["equity"],len(b["episodes"]),b["hits"]) for b in blocks])

if __name__=="__main__": history()
