"""Keep a prospective weekly forecast and its later verified outcome on separate clocks."""
from contextlib import closing
from datetime import date, datetime, time, UTC
from hashlib import sha256
import json
import sqlite3
from data import CODE, ROOT, WhiteBitPublicClient, _write_immutable, DAY as SECONDS
from swing import BUY, SELL, D, DAY, authenticated_bars, buy_qty
from weekly_agent import WEEK, features, train
import numpy as np

OUT = ROOT/"weekly-forward"
RULE = (CODE/"weekly-forward-rule.json").read_bytes()
SPEC = json.loads(RULE)
MARKETS = SPEC["universe"]
FIRST = date.fromisoformat(SPEC["first_monday_utc"])
LAST = FIRST + SPEC["weeks"]*WEEK

def utc_midnight(day):
    return datetime.combine(day,time.min,UTC)

def refresh(db, today, client):
    panel={}
    for market in MARKETS:
        px={}
        source=ROOT/"daily"/"spot"/market/"1d"/"1640995200-1794787200.manifest.json"
        rows,_,info=authenticated_bars(source)
        if info["market"]!=market:
            raise ValueError("Archive market mismatch")
        for bar in rows:
            if bar["opened_at"].date()<today:
                px[bar["opened_at"].date()]={k:D(bar[k]) for k in ("open","close")}
        for d,op,cl in db.execute("SELECT d,op,cl FROM bars WHERE market=? ORDER BY d",(market,)):
            if date.fromisoformat(d)<today:
                px[date.fromisoformat(d)]={"open":D(op),"close":D(cl)}
        first=max(px)+DAY
        while first<today:
            count=min(1000,(today-first).days)
            epoch=int(utc_midnight(first).timestamp())
            page=client.klines(market,interval="1d",limit=count,start=epoch,end=epoch+count*SECONDS,training=True)
            digest=sha256(page.raw).hexdigest()
            _write_immutable(OUT/"raw"/(digest+".json"),page.raw)
            by_date={b.open_time.date():b for b in page.candles if first<=b.open_time.date()<first+count*DAY}
            if set(by_date)!={first+i*DAY for i in range(count)}:
                raise ValueError("Missing complete daily candle")
            for d,b in sorted(by_date.items()):
                if D(b.base_volume)<=0: raise ValueError("No trading volume for settlement")
                px[d]={"open":D(b.open),"close":D(b.close)}
                db.execute("INSERT INTO bars VALUES(?,?,?,?,?,?)",
                           (market,d.isoformat(),str(b.open),str(b.close),digest,page.received_at.isoformat()))
            first+=count*DAY
        panel[market]=px
    return panel

def episode(panel, decision, day, rules):
    moves={}
    for market in MARKETS:
        opened=panel[market][day]["open"]
        closed=panel[market][day+WEEK]["open"]
        moves[market]=dict(open=str(opened),close=str(closed),actual=float(closed/opened-1),
                           estimated=decision["scores"][market])
    def pnl(markets):
        total=D(0)
        for m in markets:
            op,cl=D(moves[m]["open"]),D(moves[m]["close"])
            q=buy_qty(D(500)/len(markets),op,rules[m])
            total+=q*(cl*SELL-op*BUY)
        return str(total)
    target=decision["target"]
    return dict(moves=moves,chosen_net_usdt=pnl([target] if target else []),
                clock_basket_net_usdt=pnl(MARKETS if target else []),cash_net_usdt="0")

def run():
    OUT.mkdir(parents=True,exist_ok=True)
    now=datetime.now(UTC)
    with closing(sqlite3.connect(OUT/"learning.sqlite3",timeout=30)) as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS bars(market TEXT,d TEXT,op TEXT,cl TEXT,sha TEXT,received TEXT,
                                            PRIMARY KEY(market,d));
            CREATE TABLE IF NOT EXISTS predictions(d TEXT PRIMARY KEY,recorded TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS outcomes(d TEXT PRIMARY KEY,recorded TEXT,body TEXT);
            CREATE TABLE IF NOT EXISTS identity(id INTEGER PRIMARY KEY, digest TEXT);
        """)
        db.execute("BEGIN IMMEDIATE")
        rule_hash=sha256(RULE+(CODE/"weekly-agent-rule.json").read_bytes()+
                         (CODE/"weekly_agent.py").read_bytes().replace(b"\r\n",b"\n")).hexdigest()
        saved=db.execute("SELECT digest FROM identity WHERE id=1").fetchone()
        if saved and saved[0]!=rule_hash: raise ValueError("Weekly model identity changed")
        if not saved: db.execute("INSERT INTO identity VALUES(1,?)",(rule_hash,))
        if now.date()>LAST and not db.execute(
            "SELECT 1 FROM predictions p LEFT JOIN outcomes o USING(d) WHERE o.d IS NULL LIMIT 1"
        ).fetchone():
            db.commit()
            print("Weekly forecast recording window finished.")
            return
        panel=refresh(db,min(now.date(),LAST+DAY),WhiteBitPublicClient())
        rules=json.loads((ROOT/"market-rules.json").read_bytes())
        # Take the clock after network reads, so a slow refresh cannot backdate a decision.
        now=datetime.now(UTC)
        for i in range(SPEC["weeks"]):
            day=FIRST+i*WEEK
            key=day.isoformat()
            pred=db.execute("SELECT body FROM predictions WHERE d=?",(key,)).fetchone()
            if not pred and utc_midnight(day-DAY)<=now<utc_midnight(day):
                samples={(d,m):np.array(features(panel,m,d)) for d in
                         [day]+[day-j*WEEK for j in range(2,80)] for m in MARKETS}
                model=train(day,panel,samples)
                at=datetime.now(UTC)
                if at>=utc_midnight(day): continue
                db.execute("INSERT INTO predictions VALUES(?,?,?)",(key,at.isoformat(),json.dumps(model)))
            if pred and day+WEEK<now.date() and not db.execute("SELECT 1 FROM outcomes WHERE d=?",(key,)).fetchone():
                outcome=episode(panel,json.loads(pred[0]),day,rules)
                db.execute("INSERT INTO outcomes VALUES(?,?,?)",(key,now.isoformat(),json.dumps(outcome)))
        db.commit()
        print("Public daily candles added after archive:",db.execute("SELECT count(*) FROM bars").fetchone()[0],
              "prospective predictions:",db.execute("SELECT count(*) FROM predictions").fetchone()[0],
              "settled:",db.execute("SELECT count(*) FROM outcomes").fetchone()[0])

if __name__=="__main__": run()
