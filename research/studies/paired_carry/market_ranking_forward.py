"""Update the local research model on completed weeks and record future forecasts before the clock."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC,datetime
from decimal import Decimal as D
from hashlib import sha256
from itertools import permutations
import json
import sqlite3
import sys
import time

from data import DAY,HOUR,WEEK,ROOT,WhiteBitPublicClient,inputs,market_data,pack,parse_klines
from market_ranking import IDENTITY,MARKETS,SPEC,STATE,features,fit_models,label,predict
from relative_funding import episode
from trend_sides import hourly_lows

FIRST=int(datetime.fromisoformat(SPEC["start_utc"]).timestamp())
END=FIRST+SPEC["weeks"]*WEEK


def current_features(clock):
    end=clock-DAY+HOUR
    start=end-WEEK-HOUR
    count=(end-start)//HOUR
    def observations(m):
        market=m+"_USDT"
        path=STATE/"future-data"/f"ranking-{clock}-{market}.json"
        raw,received,uri=pack(path,lambda:WhiteBitPublicClient().klines(
            market,interval="1h",limit=count,start=start,end=end,training=True))
        page=parse_klines(raw,market,"1h",received,source_uri=uri,requested_limit=count,start=start,end=end,training=True)
        values={int(b.open_time.timestamp()):(D(b.open),D(b.high),D(b.base_volume)) for b in page.candles}
        if set(values)!=set(range(start,end,HOUR)):
            raise ValueError("Incomplete decision inputs")
        return values,sha256(raw).hexdigest()
    with ThreadPoolExecutor(max_workers=2) as pool:
        markets=dict(zip(MARKETS,pool.map(observations,MARKETS),strict=True))
    return features({m:markets[m][0] for m in MARKETS},clock),{m:markets[m][1] for m in MARKETS}


def alternatives(panel,lows,rules,clock):
    results={}
    for buy,sell in permutations(MARKETS,2):
        if any(panel[m][2][t][2]<=0 for m in (buy,sell) for t in (clock,clock+WEEK)):
            continue
        path,costs=episode(panel,lows,rules,clock,buy,sell,D(1000))
        if costs["margin_ok"]:
            results[buy+"/"+sell]=dict(net=str(path[-1]-1000),**costs)
    return results


def completed(clock):
    cache=STATE/"future-data"/str(clock)
    def observations(m):
        return market_data(m,first_epoch=clock-WEEK-DAY,last_epoch=clock+WEEK,archive=cache)
    with ThreadPoolExecutor(max_workers=2) as pool:
        panel=dict(zip(MARKETS,pool.map(observations,MARKETS),strict=True))
    lows={m:hourly_lows(m,cache) for m in MARKETS}
    rules=json.loads((ROOT/"market-rules.json").read_bytes())
    return dict(x=features({m:panel[m][1] for m in MARKETS},clock),y=label(panel,clock),
                alternatives_per_1000=alternatives(panel,lows,rules,clock))


def economic_review(row,prediction):
    feasible={"cash":D(0)}
    feasible.update((name,D(r["net"])) for name,r in row["alternatives_per_1000"].items())
    selected=prediction["long"]+"/"+prediction["short"] if prediction["trade"] else "cash"
    best=max(feasible,key=feasible.get)
    chosen=feasible.get(selected)
    return dict(hindsight_best=best,hindsight_net=str(feasible[best]),selected=selected,
                selected_net=str(chosen) if chosen is not None else None,
                target_attainable=feasible[best]>=20,selection_shortfall=str(feasible[best]-chosen) if chosen is not None else None)


def historical_opportunity():
    panel,rules=inputs()
    lows={m:hourly_lows(m) for m in MARKETS}
    study=json.loads((STATE/"historical.json").read_bytes())
    blocks=[]
    for block in study["blocks"]:
        reviews=[]
        for week in block["weeks"]:
            prediction=week["decision"]
            clock=prediction["clock"]
            choices=alternatives(panel,lows,rules,clock)
            result=economic_review({"alternatives_per_1000":choices},prediction)
            reviews.append(dict(clock=clock,review=result,alternatives=choices))
        print(block["first"],sum(r["review"]["target_attainable"] for r in reviews),"of",len(reviews))
        blocks.append(reviews)
    artifact=dict(historical=True,basis="1000 virtual USDT anew each week, hypothetical all-pair selection after the fact",blocks=blocks)
    (STATE/"opportunity.json").write_text(json.dumps(artifact,separators=(",",":")))


def dispatch(db,initial,now):
    records=dict(initial)
    records.update((k,json.loads(v)) for k,v in db.execute("SELECT clock,body FROM examples"))
    added=0
    for t in range(FIRST-WEEK,END,WEEK):
        if t+WEEK+HOUR>now:
            break
        if t in records:
            continue
        db.execute("BEGIN IMMEDIATE")
        saved=db.execute("SELECT body FROM examples WHERE clock=?",(t,)).fetchone()
        if saved:
            records[t]=json.loads(saved[0])
            db.commit()
            continue
        row=completed(t)
        pred=db.execute("SELECT body FROM forecasts WHERE clock=?",(t,)).fetchone()
        outcome=None
        if pred:
            p=json.loads(pred[0])
            buy,sell=MARKETS.index(p["long"]),MARKETS.index(p["short"])
            outcome=dict(actual_relative_gap=row["y"][buy]-row["y"][sell],long=p["long"],short=p["short"],
                         research_only=True,would_trade=p["trade"],features_revised=row["x"]!=p["features"],
                         marked_per_1000=economic_review(row,p))
        db.execute("INSERT INTO examples VALUES(?,?,?,?)",(t,time.time(),json.dumps(row),json.dumps(outcome)))
        db.commit()
        records[t]=row
        added+=1
        if added==2:
            break
    clock=FIRST if now<FIRST else FIRST+(int((now-FIRST)//WEEK)+1)*WEEK
    if clock>=END:
        return
    if any(clock-n*WEEK not in records for n in range(49,1,-1)):
        return
    db.execute("BEGIN IMMEDIATE")
    if not db.execute("SELECT 1 FROM fits WHERE clock=?",(clock,)).fetchone():
        models=fit_models(records,clock)
        db.execute("INSERT INTO fits VALUES(?,?,?)",(clock,time.time(),json.dumps(models)))
    if not clock-DAY+HOUR<=now<clock or db.execute("SELECT 1 FROM forecasts WHERE clock=?",(clock,)).fetchone():
        db.commit()
        return
    x,sources=current_features(clock)
    prediction=predict(records,clock,x)
    stamp=time.time()
    if stamp<clock:
        prediction["source_sha256"]=sources
        db.execute("INSERT INTO forecasts VALUES(?,?,?)",(clock,stamp,json.dumps(prediction)))
    db.commit()


def run():
    raw=(STATE/"initial-experience.json").read_bytes()
    seed=json.loads(raw)
    if seed["identity"]!=IDENTITY:
        raise ValueError("Initial training experience has a different model identity")
    initial={int(k):v for k,v in seed["records"].items()}
    with closing(sqlite3.connect(STATE/"learning.sqlite3",timeout=20)) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY,model TEXT,seed TEXT);
        CREATE TABLE IF NOT EXISTS examples(clock INTEGER PRIMARY KEY,recorded REAL,body TEXT,outcome TEXT);
        CREATE TABLE IF NOT EXISTS fits(clock INTEGER PRIMARY KEY,recorded REAL,body TEXT);
        CREATE TABLE IF NOT EXISTS forecasts(clock INTEGER PRIMARY KEY,recorded REAL,body TEXT);
        """)
        identity=(IDENTITY,sha256(raw).hexdigest())
        db.execute("INSERT OR IGNORE INTO identity VALUES(1,?,?)",identity)
        if db.execute("SELECT model,seed FROM identity").fetchone()!=identity:
            raise ValueError("Stored training identity does not match")
        db.commit()
        dispatch(db,initial,time.time())
        print("Training weeks, fitted clocks, prospective forecasts:",len(initial)+db.execute("SELECT count(*) FROM examples").fetchone()[0],
              db.execute("SELECT count(*) FROM fits").fetchone()[0],db.execute("SELECT count(*) FROM forecasts").fetchone()[0])


if __name__=="__main__":
    if sys.argv[1:]==["--historical-opportunity"]:
        historical_opportunity()
    elif not sys.argv[1:]:
        run()
    else:
        raise SystemExit("Use without arguments for the research worker, or --historical-opportunity")
