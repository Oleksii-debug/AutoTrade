"""Replay the pre-entry policy against retained, hash-verified quotes; no prospective writes."""
from contextlib import closing
from datetime import datetime, UTC
import importlib.util
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import json
import sys
from data import ROOT, HOUR
from six_hour import D, OUT, MARKET, STEP, FEE, fit, quantity, base_data
from six_hour_forward import cost_sample, FIRST


def stage(tape, directory):
    spec=importlib.util.spec_from_file_location("exact_retention",directory/"retain.py")
    archive=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(archive)
    plan,signature=archive.read_protocol()
    count=0
    tape.execute("""CREATE TABLE execution_bbo(market,bucket_epoch INTEGER,received_at,bid_price,ask_price,
                 bid_quantity,ask_quantity,generation,update_id,source_sha256,PRIMARY KEY(market,bucket_epoch)) WITHOUT ROWID""")
    for path in sorted((directory/"shards").glob("*.json.gz")):
        if int(path.name.split(".")[0])>=FIRST: continue
        data,_=archive.verify_file(path,plan,signature)
        if data["book_columns"]!=list(archive.BOOK_COLUMNS): raise ValueError("Book layout changed")
        tape.executemany("INSERT INTO execution_bbo VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (row for row in data["books"] if row[0]==MARKET))
        count+=1
    tape.commit()
    return count


def read_quote(tape,t):
    row=tape.execute("""SELECT bid_price,ask_price,bid_quantity,ask_quantity,bucket_epoch,received_at,source_sha256
                         FROM execution_bbo WHERE market=? AND bucket_epoch BETWEEN ? AND ?
                         AND received_at>=? ORDER BY bucket_epoch LIMIT 1""",
                     (MARKET,t+22,t+240,datetime.fromtimestamp(t+22,UTC).isoformat())).fetchone()
    if not row: return None
    seen=datetime.fromisoformat(row[5]).timestamp()
    if not t<=seen<t+300: return None
    return tuple(map(D,row[:4]))+(seen,row[6])


def archived_inputs():
    bars,funding=base_data()
    with closing(sqlite3.connect((OUT/"agent.sqlite3").resolve().as_uri()+"?mode=ro",uri=True)) as live:
        for k,body in live.execute("SELECT epoch,body FROM candles"):
            bars[k]=tuple(map(D,json.loads(body)))
        funding.update({k:D(v) for k,v in live.execute("SELECT epoch,value FROM funding")})
    return bars,funding


def replay(directory):
    bars,funding=archived_inputs()
    rule=json.loads((ROOT/"market-rules.json").read_bytes())[MARKET]
    with TemporaryDirectory() as tmp,closing(sqlite3.connect(Path(tmp)/"books.db")) as tape:
        count=stage(tape,directory)
        lo,hi=tape.execute("SELECT min(bucket_epoch),max(bucket_epoch) FROM execution_bbo").fetchone()
        t=FIRST-((FIRST-lo)//STEP)*STEP
        capital=D(1000)
        records=[]
        residuals=[]
        cache={}
        while t+STEP+300<=hi:
            opened,closed=read_quote(tape,t),read_quote(tape,t+STEP)
            cutoff=t-HOUR
            model=fit(bars,funding,t,cache)
            calib=cost_sample(t-25*60,Path(tmp)/"books.db")
            errors=[max(-.02,min(.02,y)) for at,y in residuals if at<=cutoff][-28:]
            bias=sum(errors)/(len(errors)+24) if len(errors)>=12 else 0
            mean=model["expected"]+bias
            side=(1 if mean>0 else -1) if calib["ready"] and abs(mean)>calib["estimated_round_trip"]+model["standard_error"] else 0
            rec=dict(clock=t,side=side,fee_hurdle=calib["estimated_round_trip"],calibrated=calib["ready"])
            if opened and closed:
                b,a,bq,aq,at,_=opened
                xb,xa,xbq,xaq,xt,_=closed
                q=quantity(capital,a,rule)
                if q<=min(bq,aq,xbq,xaq):
                    carry=sum((funding[k] for k in range((int(at)//(8*HOUR)+1)*8*HOUR,int(xt)+1,8*HOUR)),D(0))
                    long=q*(xb-a-carry-(a+xb)*FEE)
                    short=q*(b-xa+carry-(b+xa)*FEE)
                    net={1:long,-1:short,0:D(0)}[side]
                    capital+=net
                    actual=(xb+xa-b-a-2*carry)/(a+b)
                    residuals.append((xt,float(actual)-model["expected"]))
                    rec.update(entry=opened[-1],exit=closed[-1],long_net=str(long),short_net=str(short),
                               chosen_net=str(net),capital=str(capital))
            records.append(rec)
            t+=STEP
    out=dict(historical=True,shards=count,ending_capital=str(capital),episodes=records)
    (OUT/"retained-book-replay.json").write_text(json.dumps(out,indent=2))
    print("Historical clocks, executable pairs, chosen trades, ending equity:",len(records),
          sum("chosen_net" in r for r in records),sum(bool(r["side"]) and "chosen_net" in r for r in records),capital)

if __name__=="__main__":
    replay(Path(sys.argv[1]).resolve())
