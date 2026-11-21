"""Train a weekly reversal critic using only settled market weeks before each decision."""
from decimal import Decimal as D
from hashlib import sha256
import json
from statistics import mean, median, stdev

from data import CODE, ROOT, CUTS, DAY, WEEK, inputs
from relative_funding import episode
from trend_sides import hourly_lows


def features(row):
    scores=[D(v) for v in row["scores"].values()]
    return median(scores),max(scores)-min(scores)


def train(records,row):
    prior=[r for r in records if r["clock"]+WEEK<=row["cutoff"]][-52:]
    if len(prior)<26:
        return {"trade":False,"count":len(prior),"neighbors":[]}
    x=features(row)
    history=[features(r) for r in prior]
    scales=[stdev(column) or D(1) for column in zip(*history)]
    distances=[sum(((a-b)/s)**2 for a,b,s in zip(x,y,scales)) for y in history]
    indexes=sorted(range(len(prior)),key=lambda i:(distances[i],prior[i]["clock"]))[:12]
    selected=[prior[i] for i in indexes]
    labels=[D(r["arms"]["reverse"]["weekly"]) for r in selected]
    estimated=mean(labels)
    error=stdev(labels)/D(len(labels)).sqrt()
    return dict(trade=estimated>error,count=len(prior),neighbors=[r["clock"] for r in selected],
                features=x,scale=scales,mean=estimated,standard_error=error,labels=labels)


def evaluate():
    prior=json.loads((ROOT/"momentum-transfer-results.json").read_bytes())
    recent=json.loads((ROOT/"momentum-pair-results.json").read_bytes())
    records=[row for study in (prior,recent) for block in study["blocks"] for row in block["weeks"]]
    epochs=[r["clock"] for r in records]
    if len(epochs)!=104 or any(b-a!=WEEK for a,b in zip(epochs,epochs[1:])):
        raise ValueError("Expected 104 contiguous market weeks")
    panel,rules=inputs()
    lows={m:hourly_lows(m) for m in panel}
    blocks=[]
    for first,last in zip(CUTS,CUTS[1:]):
        capital=ceiling=D(1000)
        rows=[]
        for row in records:
            clock=row["clock"]
            if not first<=clock<last:
                continue
            fit=train(records,row)
            candidate=row["arms"]["reverse"]
            buy,sell=candidate["long"],candidate["short"]
            before=capital
            if fit["trade"]:
                path,metrics=episode(panel,lows,rules,clock,buy,sell,capital)
                if not metrics["margin_ok"]:
                    raise ValueError("Collateral breach in candidate account")
                capital=path[-1]
            diagnostic=[ceiling]
            for long,short in [(buy,sell),(sell,buy)]:
                path,metrics=episode(panel,lows,rules,clock,long,short,ceiling)
                if metrics["margin_ok"]:
                    diagnostic.append(path[-1])
            previous=ceiling
            ceiling=max(diagnostic)
            rows.append(dict(clock=clock,forecast=fit,capital=capital,weekly=capital/before-1,
                             hindsight_weekly=ceiling/previous-1,always_reversal=candidate["capital"]))
        returns=[r["weekly"] for r in rows]
        rate=(capital/1000)**(D(1)/len(rows))-1
        ex_best=capital/(1+max(returns))-1000
        baseline=D(rows[-1]["always_reversal"])
        blocks.append(dict(first=first,capital=capital,weekly_geometric=rate,without_best=ex_best,
                           always_reversal=baseline,trades=sum(r["forecast"]["trade"] for r in rows),
                           at_target=sum(r>=D(".02") for r in returns),
                           hindsight_target_weeks=sum(r["hindsight_weekly"]>=D(".02") for r in rows),
                           eligible=capital>max(D(1000),baseline) and ex_best>0 and rate>=D(".02"),rows=rows))
    return blocks


if __name__=="__main__":
    blocks=evaluate()
    rule=sha256((CODE/"pair-selection-rule.json").read_bytes()).hexdigest()
    (ROOT/"pair-selection-results.json").write_text(json.dumps(dict(rule=rule,blocks=blocks),default=str,indent=2))
    for b in blocks:
        print({k:str(v) for k,v in b.items() if k!="rows"})
