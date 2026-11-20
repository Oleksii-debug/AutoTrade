"""Persist a model decision, then open, close, review and resume a single paper account."""
from bisect import bisect_left
from contextlib import closing
from datetime import datetime, UTC
from decimal import Decimal as D
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import time
import numpy as np
from data import CODE, ROOT, HOUR, WEEK, WhiteBitPublicClient, _write_immutable
from forward import average, settlements
from six_hour import OUT, MARKET, RULE, FEE, STEP, base_data, fit, quantity

POLICY=(CODE/"six-hour-forward-rule.json").read_bytes()
SPEC=json.loads(POLICY)
IDENTITY=sha256(POLICY+RULE.encode()+(CODE/"six_hour.py").read_bytes().replace(b"\r\n",b"\n")).hexdigest()
FIRST=int(datetime.fromisoformat(SPEC["first_entry_utc"]).timestamp())
END=FIRST+SPEC["duration_weeks"]*WEEK
TAPE=Path(os.getenv("AUTOTRADE_DEPTH_TAPE",r"D:\AutoTradeData\whitebit-research\runtime\whitebit-depth-tape-v1-20261112\depth-tape.sqlite3"))


def keep(public):
    key=sha256(public.raw).hexdigest()
    _write_immutable(OUT/"raw"/(key+".json"),public.raw)
    return dict(sha256=key,uri=public.source_uri,received=public.received_at.isoformat())


def cost_sample(at, tape_path=None):
    path=TAPE if tape_path is None else tape_path
    if not path.is_file():
        return dict(ready=False,count=0,last_quote=None,estimated_round_trip=None,
                    samples_sha256=None,reason="no_collector_database")
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=8)) as tape:
        cursor=tape.execute("""SELECT bucket_epoch,bid_price,ask_price,bid_quantity,ask_quantity,generation,
                               received_at FROM execution_bbo WHERE market=? AND bucket_epoch BETWEEN ? AND ?
                               ORDER BY bucket_epoch""",(MARKET,int(at)-86400,int(at)))
        rows=[(int(k),float(b),float(a),float(bq),float(aq),g,datetime.fromisoformat(ts).timestamp())
              for k,b,a,bq,aq,g,ts in cursor]
    epochs=[v[0] for v in rows]
    last_bin=None
    costs=[]
    instants=[]
    for k,b,a,bq,aq,g,seen in rows:
        if k//300==last_bin or not 0<b<=a or seen>at or min(b*bq,a*aq)<400:
            continue
        i=bisect_left(epochs,k+1)
        if i==len(rows): continue
        nxt,bid,ask,bsize,asize,generation,received=rows[i]
        if (nxt-k>3 or generation!=g or not seen<received<=at or received-seen>3
                or not 0<bid<=ask or min(bid*bsize,ask*asize)<400):
            continue
        mid=(a+b)/2
        costs.append(max(0.,(ask-mid)/mid,(mid-bid)/mid))
        instants.append(k)
        last_bin=k//300
    last_seen=max((r[6] for r in rows if r[6]<=at),default=None)
    ready=bool(last_seen and 0<=at-last_seen<120 and len(costs)>=144 and sum(t>=at-4*HOUR for t in instants)>=24)
    return dict(ready=ready,count=len(costs),last_quote=last_seen,
                estimated_round_trip=2*(float(np.quantile(costs,.95))+float(FEE)) if costs else None,
                samples_sha256=sha256(json.dumps(list(zip(instants,costs))).encode()).hexdigest())


def history_to(db, cutoff):
    bars,funding=base_data()
    for k,body in db.execute("SELECT epoch,body FROM candles"):
        bars[k]=tuple(map(D,json.loads(body)))
    for k,value in db.execute("SELECT epoch,value FROM funding"):
        funding[k]=D(value)
    cursor=max(bars)+HOUR
    client=WhiteBitPublicClient()
    while cursor<cutoff:
        count=min((cutoff-cursor)//HOUR,1000)
        page=client.klines(MARKET,interval="1h",limit=count,start=cursor,end=cursor+count*HOUR,training=True)
        source=keep(page)
        observed={}
        for b in page.candles:
            k=int(b.open_time.timestamp())
            if k in observed or D(b.base_volume)<=0: raise ValueError("Invalid training candle")
            observed[k]=tuple(map(D,(b.open,b.close,b.low,b.high)))
        if set(observed)!=set(range(cursor,cursor+count*HOUR,HOUR)): raise ValueError("Missing price history")
        for k,row in observed.items():
            db.execute("INSERT INTO candles VALUES(?,?,?)",(k,json.dumps(list(map(str,row))),source["sha256"]))
        bars.update(observed)
        cursor+=count*HOUR
    after=max(funding)
    if cutoff//(8*HOUR)>after//(8*HOUR):
        events,sources=settlements(after,cutoff,MARKET,keep)
        for k,(rate,px) in events.items():
            funding[k]=rate*px
            db.execute("INSERT INTO funding VALUES(?,?,?)",(k,str(funding[k]),json.dumps(sources)))
    return bars,funding


def direction(db,t,now):
    bars,funding=history_to(db,t-HOUR)
    model=fit(bars,funding,t,{})
    calibration=cost_sample(now)
    errors=[max(-.02,min(.02,e)) for (e,) in db.execute(
        """SELECT residual FROM reviews WHERE timely=1 AND closed<=?
           AND json_extract(body,'$.reviewed_at')<=? ORDER BY closed DESC LIMIT 28""",(t-HOUR,t-HOUR))]
    correction=sum(errors)/(24+len(errors)) if len(errors)>=12 else 0.
    estimate=model["expected"]+correction
    side=0
    if calibration["ready"] and abs(estimate)>calibration["estimated_round_trip"]+model["standard_error"]:
        side=1 if estimate>0 else -1
    return dict(side=side,model=model,calibration=calibration,online_bias=correction,
                prior_reviews=len(errors),rule=IDENTITY,epoch=t)


def quote():
    book=WhiteBitPublicClient().instrument_orderbook(MARKET)
    at=datetime.now(UTC)
    book.assert_fresh(at)
    return book,at.timestamp()


def mid(book):
    return (D(book.asks[0][0])+D(book.bids[0][0]))/2


def take_fill(book,q):
    return average(book.asks if q>0 else book.bids,abs(q)) if q else mid(book)


def reference_fill(book, quantity):
    if not quantity:
        return None
    levels = book.asks if quantity > 0 else book.bids
    if sum((D(volume) for _, volume in levels), D(0)) < abs(quantity):
        return None
    return str(average(levels, abs(quantity)))


def balance(db):
    row=db.execute("SELECT capital FROM reviews ORDER BY clock DESC LIMIT 1").fetchone()
    return D(row[0]) if row else D(1000)


def advance(db, now):
    active=db.execute("""SELECT e.clock,e.body,d.body FROM entries e JOIN decisions d USING(clock)
                         LEFT JOIN reviews r USING(clock) WHERE r.clock IS NULL""").fetchall()
    if len(active)>1: raise ValueError("Overlapping active positions")
    for t,body,pred in active:
        if now<t+STEP: break
        entry,model=json.loads(body),json.loads(pred)
        q=D(entry["quantity"])
        size=D(entry["reference_quantity"])
        captured=db.execute("SELECT body FROM exit_quotes WHERE clock=?",(t,)).fetchone()
        if captured is None:
            book,at=quote()
            if book.source_time.timestamp()<t+STEP: raise ValueError("Awaiting post-exit quote")
            receipt=dict(at=at,source=keep(book),fill=str(take_fill(book,-q)),mid=str(mid(book)),
                         sell=reference_fill(book,-size),buy=reference_fill(book,size))
            db.execute("INSERT INTO exit_quotes VALUES(?,?)",(t,json.dumps(receipt)))
            # Freeze the executable price before contacting the separate funding service.
            db.commit()
            db.execute("BEGIN IMMEDIATE")
        else:
            receipt=json.loads(captured[0])
        if db.execute("SELECT 1 FROM reviews WHERE clock=?",(t,)).fetchone():
            continue
        at,source,exit_px=receipt["at"],receipt["source"],D(receipt["fill"])
        entry_px=D(entry["fill"])
        flow_per_unit=D(0)
        evidence=[]
        if int(at)//(8*HOUR)>int(entry["at"])//(8*HOUR):
            events,evidence=settlements(entry["at"],at,MARKET,keep)
            flow_per_unit=sum((rate*px for rate,px in events.values()),D(0))
        opening_mid=D(entry["mid"])
        actual=(D(receipt["mid"])-opening_mid-flow_per_unit)/opening_mid
        fee=abs(q)*(entry_px+exit_px)*FEE
        pnl=q*(exit_px-entry_px-flow_per_unit)-fee
        capital=D(entry["capital"])+pnl
        controls={}
        for name,signed,opening in (("long",size,entry["buy"]),("short",-size,entry["sell"])):
            closing_price=receipt["sell" if signed>0 else "buy"]
            if opening is None or closing_price is None:
                controls[name]=None
                continue
            opened,closed=D(opening),D(closing_price)
            controls[name]=str(signed*(closed-opened-flow_per_unit)-size*(opened+closed)*FEE)
        if q and D(controls["long" if q>0 else "short"])!=pnl:
            raise ValueError("Actual action and matched settlement disagree")
        timely=at<t+STEP+300
        review=dict(chosen=model["side"],actual_signed_long=str(actual),fees=str(fee),funding=str(-q*flow_per_unit),
                    fill=str(exit_px),net=str(pnl),opening_capital=entry["capital"],capital=str(capital),
                    quote=source,funding_sources=evidence,closed=at,reviewed_at=time.time(),timely=timely,
                    matched_alternatives=controls,cash_net="0")
        db.execute("INSERT INTO reviews VALUES(?,?,?,?,?,?)",
                   (t,at,int(timely),float(actual)-model["model"]["expected"],str(capital),json.dumps(review)))
        # A failure getting the next entry must never undo an observed exit.
        db.commit()
        db.execute("BEGIN IMMEDIATE")
    now=time.time()
    if now>=FIRST:
        t=FIRST+int((now-FIRST)//STEP)*STEP
        pred=db.execute("SELECT recorded,body FROM decisions WHERE clock=?",(t,)).fetchone()
        if t<END and now<t+300 and pred and not db.execute("SELECT 1 FROM entries WHERE clock=?",(t,)).fetchone():
            book,at=quote()
            if at>=t+300: return
            if book.source_time.timestamp()<max(t,pred[0]): raise ValueError("Awaiting post-entry quote")
            spec=json.loads(pred[1])
            rule=json.loads((ROOT/"market-rules.json").read_bytes())[MARKET]
            capital=balance(db)
            size=quantity(max(D(0),capital),D(book.asks[0][0]),rule)
            amount=spec["side"]*size
            price=take_fill(book,amount)
            if abs(amount)*price>capital*D(".4"): raise ValueError("Book exceeds position budget")
            entry=dict(at=at,fill=str(price),mid=str(mid(book)),quantity=str(amount),reference_quantity=str(size),
                       buy=reference_fill(book,size),sell=reference_fill(book,-size),
                       capital=str(capital),quote=keep(book))
            db.execute("INSERT INTO entries VALUES(?,?)",(t,json.dumps(entry)))
    next_t=FIRST if now<FIRST else FIRST+(int((now-FIRST)//STEP)+1)*STEP
    if next_t-HOUR<=now<next_t and next_t<END and not db.execute("SELECT 1 FROM decisions WHERE clock=?",(next_t,)).fetchone():
        decision=direction(db,next_t,now)
        stamp=time.time()
        if stamp<next_t: db.execute("INSERT INTO decisions VALUES(?,?,?)",(next_t,stamp,json.dumps(decision)))


def update_progress(db,now):
    objective=json.loads((CODE/"earnings-goal.json").read_bytes())
    target=D(objective["target_net_fraction_per_week"])
    weekly=[]
    for start in range(FIRST,END,WEEK):
        if now<start+WEEK: break
        rows=dict(db.execute("SELECT clock,body FROM reviews WHERE clock>=? AND clock<? ORDER BY clock",
                             (start,start+WEEK)))
        missing=sorted(set(range(start,start+WEEK,STEP))-rows.keys())
        reviews=[json.loads(row) for row in rows.values()]
        valid=not missing and all(r["timely"] for r in reviews)
        rate=D(reviews[-1]["capital"])/D(reviews[0]["opening_capital"])-1 if valid else None
        weekly.append(dict(start=start,missing=missing,timely=valid,
                           net_fraction=str(rate) if valid else None,at_target=valid and rate>=target))
    passed=len(weekly)==SPEC["duration_weeks"] and all(w["at_target"] for w in weekly)
    progress=dict(next_due=FIRST if now<FIRST else min(END,FIRST+(int((now-FIRST)//STEP)+1)*STEP),
                  capital=str(balance(db)),weeks=weekly,target_verified=passed,
                  status="collecting" if now<END else ("verified" if passed else "not_verified"))
    db.execute("INSERT OR REPLACE INTO progress VALUES(1,?)",(json.dumps(progress),))


def write_account_view(db):
    row=db.execute("SELECT clock,recorded,body FROM decisions ORDER BY clock DESC LIMIT 1").fetchone()
    reviewed=db.execute("SELECT body FROM reviews ORDER BY clock DESC LIMIT 1").fetchone()
    count,positions=db.execute(
        "SELECT count(*), coalesce(sum(CAST(json_extract(body,'$.quantity') AS REAL)!=0),0) FROM entries"
    ).fetchone()
    progress=json.loads(db.execute("SELECT body FROM progress WHERE id=1").fetchone()[0])
    def local_clock(epoch):
        return datetime.fromtimestamp(epoch,UTC).astimezone().strftime("%d.%m.%Y %H:%M:%S %z")
    lines=["AutoTrade. Навчальний рахунок BTC на котируваннях WhiteBIT.",
           f"Оновлено {local_clock(time.time())}.",
           f"Капітал після останнього розрахунку {D(progress['capital']):.2f} USDT із початкових 1000.",
           f"Почато періодів {count}, із позицією {positions}."]
    if row:
        decision=json.loads(row[2])
        side={-1:"коротка позиція",0:"залишитися у грошах",1:"довга позиція"}[decision["side"]]
        lines.append(f"Рішення на {local_clock(row[0])} — {side}. Записано {local_clock(row[1])}.")
    if reviewed:
        review=json.loads(reviewed[0])
        lines.append(f"Останній завершений період {local_clock(review['closed'])}. Чистий результат {D(review['net']):+.2f} USDT.")
    lines.append(f"Тижнів із повними спостереженнями {sum(w['timely'] for w in progress['weeks'])}.")
    temp=OUT/f"account-{os.getpid()}.tmp"
    temp.write_text("\n".join(lines)+"\n",encoding="utf-8-sig")
    os.replace(temp,OUT/"Рахунок BTC за шість годин.txt")


def run():
    OUT.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(OUT/"agent.sqlite3",timeout=20)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY,sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS candles(epoch INTEGER PRIMARY KEY,body TEXT NOT NULL,sha TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS funding(epoch INTEGER PRIMARY KEY,value TEXT NOT NULL,sources TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decisions(clock INTEGER PRIMARY KEY,recorded REAL NOT NULL,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS entries(clock INTEGER PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS exit_quotes(clock INTEGER PRIMARY KEY,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews(clock INTEGER PRIMARY KEY,closed REAL NOT NULL,timely INTEGER NOT NULL,
                residual REAL NOT NULL,capital TEXT NOT NULL,body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS progress(id INTEGER PRIMARY KEY,body TEXT NOT NULL);
        """)
        db.execute("BEGIN IMMEDIATE")
        db.execute("INSERT OR IGNORE INTO identity VALUES(1,?)",(IDENTITY,))
        if db.execute("SELECT sha FROM identity WHERE id=1").fetchone()[0]!=IDENTITY:
            raise ValueError("Frozen policy mismatch")
        advance(db,time.time())
        update_progress(db,time.time())
        db.commit()
        write_account_view(db)
        print("Decisions, opened episodes, settled reviews:",*[db.execute("SELECT count(*) FROM "+table).fetchone()[0]
                                                               for table in ("decisions","entries","reviews")])

if __name__=="__main__": run()
