"""Learn relative weekly returns from six spot markets and settled perpetual funding."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from hashlib import sha256
import json
from math import log
from statistics import mean,stdev

import numpy as np

from data import CODE,ROOT,CUTS,DAY,HOUR,WEEK,inputs,market_data,stamp
from pairs import FEE,SLIP
from relative_funding import episode
from trend_sides import hourly_lows

CONFIG=(CODE/"market-ranking-rule.json").read_bytes()
SPEC=json.loads(CONFIG)
MARKETS=sorted(SPEC["markets"])
STATE=ROOT/"market-ranking"
IDENTITY=sha256(CONFIG+(CODE/"market_ranking.py").read_bytes().replace(b"\r\n",b"\n")).hexdigest()


def features(spot,clock):
    end=clock-DAY
    rows=[]
    for m in MARKETS:
        history=spot[m]
        ticks=range(end-WEEK,end+1,HOUR)
        prices=[float(history[t][0]) for t in ticks]
        volumes=[float(history[t][2]) for t in range(end-WEEK,end,HOUR)]
        if min(prices)<=0 or min(volumes)<0 or min(sum(volumes[-24:]),sum(volumes[:-24]))<=0:
            raise ValueError("Incomplete spot observations")
        moves=[log(b/a) for a,b in zip(prices,prices[1:])]
        rows.append([log(prices[-1]/prices[0]),log(prices[-1]/prices[-25]),stdev(moves),
                     log(mean(volumes[-24:])/mean(volumes[:-24]))])
    matrix=np.array(rows)
    return (matrix-matrix.mean(axis=0)).tolist()


def label(panel,clock):
    values=[]
    for m in MARKETS:
        funding,_,bars=panel[m]
        ticks=range((clock//(8*HOUR)+1)*8*HOUR,clock+WEEK+1,8*HOUR)
        present={t for t in funding if clock<t<=clock+WEEK}
        if present!=set(ticks) or min(bars[t][2] for t in (clock,clock+WEEK))<=0:
            raise ValueError("Unscorable weekly market or funding interval")
        financing=sum((funding[t][0]*funding[t][1] for t in ticks),D(0))
        entry=bars[clock][0]
        values.append(float((bars[clock+WEEK][0]-entry-financing)/entry))
    return [v-mean(values) for v in values]


def fit_models(records,clock):
    epochs=[clock-n*WEEK for n in range(49,1,-1)]
    models=[]
    for training in (epochs[:24],epochs[24:]):
        x=np.concatenate([records[k]["x"] for k in training])
        y=np.concatenate([records[k]["y"] for k in training])
        center=x.mean(axis=0)
        scale=x.std(axis=0)
        scale[scale<1.e-12]=1
        x=(x-center)/scale
        weights=np.linalg.solve(x.T@x+np.eye(x.shape[1])*.1*len(x),x.T@(y-y.mean()))
        models.append(dict(first=training[0],last_outcome=training[-1]+WEEK,center=center.tolist(),
                           scale=scale.tolist(),weights=weights.tolist(),intercept=float(y.mean())))
    return models


def predict(records,clock,current):
    models=fit_models(records,clock)
    for model in models:
        x=(np.array(current)-model["center"])/model["scale"]
        model["scores"]=(x@model["weights"]+model["intercept"]).tolist()
    scores=np.mean([m["scores"] for m in models],axis=0)
    ordered=sorted(range(len(MARKETS)),key=lambda i:(scores[i],MARKETS[i]))
    lo,hi=ordered[0],ordered[-1]
    trade=all(m["scores"][hi]-m["scores"][lo]>float(4*(FEE+SLIP)) for m in models)
    return dict(clock=clock,cutoff=clock-DAY,long=MARKETS[hi],short=MARKETS[lo],trade=trade,
                features=current,models=models,identity=IDENTITY)


def replay():
    STATE.mkdir(exist_ok=True,parents=True)
    early=list(map(stamp,json.loads((CODE/"momentum-transfer-rule.json").read_bytes())["prior_year"]))
    old=ROOT/"momentum-older"
    def load(m):
        return market_data(m,first_epoch=early[0]-WEEK-DAY,last_epoch=early[-1],archive=old)
    with ThreadPoolExecutor(max_workers=2) as pool:
        panel=dict(zip(MARKETS,pool.map(load,MARKETS),strict=True))
    spot={m:panel[m][1] for m in MARKETS}
    records={t:dict(x=features(spot,t),y=label(panel,t)) for t in range(early[0],early[-1],WEEK)}
    # The earliest decisions of the recent year still use a week of preceding spot data.
    tail={m:{k:v for k,v in spot[m].items() if k>=early[-1]-WEEK-DAY} for m in MARKETS}
    del panel,spot
    panel,rules=inputs()
    spot={m:tail[m]|panel[m][1] for m in MARKETS}
    lows={m:hourly_lows(m) for m in MARKETS}
    controls=json.loads((ROOT/"momentum-pair-results.json").read_bytes())["blocks"]
    blocks=[]
    for idx,(first,last) in enumerate(zip(CUTS,CUTS[1:])):
        previous=D(1000)
        weekly=[]
        peak=previous
        dd=D(0)
        for clock in range(first,last,WEEK):
            x=features(spot,clock)
            decision=predict(records,clock,x)
            path=[previous]
            economic={}
            if decision["trade"]:
                path,economic=episode(panel,lows,rules,clock,decision["long"],decision["short"],previous)
                if not economic["margin_ok"]:
                    raise ValueError("Insufficient collateral")
            for equity in path:
                peak=max(peak,equity)
                dd=max(dd,1-equity/peak)
            records[clock]=dict(x=x,y=label(panel,clock))
            weekly.append(dict(decision=decision,economic=economic,weekly=str(path[-1]/previous-1),capital=str(path[-1])))
            previous=path[-1]
        rates=[D(w["weekly"]) for w in weekly]
        competitors=controls[idx]["capital"]
        geo=(previous/1000)**(D(1)/len(rates))-1
        passes=geo>=D(".02") and previous>max(D(1000),D(competitors["reverse"]),D(competitors["rotation"])) and previous/(1+max(rates))>1000
        blocks.append(dict(first=first,ending=str(previous),weekly_geometric=str(geo),drawdown=str(dd),
                           target_hits=sum(r>=D(".02") for r in rates),trades=sum(w["decision"]["trade"] for w in weekly),
                           eligible=passes,weeks=weekly))
    for name,data in (("initial-experience",dict(identity=IDENTITY,records=records)),("historical",dict(identity=IDENTITY,blocks=blocks))):
        path=STATE/(name+".json")
        temp=path.with_suffix(".tmp")
        temp.write_text(json.dumps(data,separators=(",",":")))
        temp.replace(path)
    print([{k:v for k,v in b.items() if k!="weeks"} for b in blocks])


if __name__=="__main__": replay()
