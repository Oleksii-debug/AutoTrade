"""One observer, two mirrored virtual accounts, the same clocks and public liquidity."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, UTC
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from os import getpid, replace
import sqlite3
from data import CODE, ROOT, HOUR, DAY, WhiteBitPublicClient, _write_immutable
from forward import average, settlements, SLIP, PF

OUT=ROOT/"direction-forward"
RAW=(CODE/"direction-forward-rule.json").read_bytes()
SPEC=json.loads(RAW)
MARKETS=SPEC["markets"]
ANCHOR=datetime.fromisoformat(SPEC["first_entry_clock"]).timestamp()
RULE=sha256(RAW).hexdigest()

def keep(item):
    digest=sha256(item.raw).hexdigest()
    _write_immutable(OUT/"raw"/(digest+".json"),item.raw)
    return dict(sha256=digest,source=item.source_uri,received=item.received_at.isoformat())

def capture():
    with ThreadPoolExecutor(max_workers=len(MARKETS)) as ex:
        books=dict(zip(MARKETS,ex.map(lambda m:WhiteBitPublicClient().instrument_orderbook(m), MARKETS)))
    now=datetime.now(UTC)
    for book in books.values(): book.assert_fresh(now)
    stamps=[b.source_time for b in books.values()]
    if (max(stamps)-min(stamps)).total_seconds()>5: raise ValueError("Unsynchronized portfolio quotes")
    return books,now

def prices(book,q):
    return average(book.asks,q)*(1+SLIP), average(book.bids,q)*(1-SLIP)

def opening(books,rules):
    positions={}
    for m,b in books.items():
        r=rules[m]
        step=D(r["step"])
        q=(D(200)/(D(b.asks[0][0])*(1+SLIP))/step).to_integral_value(rounding=ROUND_DOWN)*step
        if q<=0: raise ValueError("Zero market quantity")
        buy,sell=prices(b,q)
        if q*buy>200 or q<D(r["min_qty"]) or q*sell<D(r["min_value"]):
            raise ValueError("Public book or lot rules prevent matched execution")
        positions[m]={"q":str(q),"long_entry":str(buy),"short_entry":str(sell)}
    return positions

def nav(positions,books,short_funding):
    short_nav,long_nav=D(1000)+short_funding,D(1000)-short_funding
    gross=D(0)
    for m,r in positions.items():
        q,buy,sell=map(D,(r["q"],r["long_entry"],r["short_entry"]))
        cover,liquidate=prices(books[m],q)
        long_nav+=q*(liquidate-buy-PF*(buy+liquidate))
        short_nav+=q*(sell-cover-PF*(sell+cover))
        gross+=q*cover
    return short_nav,long_nav,gross

def tick():
    OUT.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(OUT/"observer.sqlite3",timeout=30)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS marks(slot INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        row=db.execute("SELECT body FROM marks ORDER BY slot DESC LIMIT 1").fetchone()
        prev=json.loads(row[0]) if row else None
        now=datetime.now(UTC).timestamp()
        if prev and prev["rule"]!=RULE: raise ValueError("Registered observation changed")
        if prev and (prev["closed"] or int(now)//HOUR<=prev["slot"]): return prev
        if not prev and now<ANCHOR: return {"state":"waiting","at":SPEC["first_entry_clock"]}
        if not prev and now>=ANCHOR+60*SPEC["entry_window_minutes"]: return {"state":"missed"}
        books,clock=capture()
        at=clock.timestamp()
        if not prev and at>=ANCHOR+60*SPEC["entry_window_minutes"]: return {"state":"missed"}
        slot=int(at)//HOUR
        if prev and slot<=prev["slot"]: return prev
        positions=prev["positions"] if prev else opening(books,json.loads((ROOT/"market-rules.json").read_bytes()))
        cashflows={}
        evidence={}
        funded=D(prev["short_funding"]) if prev else D(0)
        if prev and int(at)//(8*HOUR)>int(prev["epoch"])//(8*HOUR):
            for m,pos in positions.items():
                events,evidence[m]=settlements(prev["epoch"],at,m,keep)
                cashflows[m]={str(t):str(D(pos["q"])*rate*price) for t,(rate,price) in events.items()}
                funded+=sum(map(D,cashflows[m].values()),D(0))
        short,long,gross=nav(positions,books,funded)
        entry=prev["entry_epoch"] if prev else at
        elapsed=slot-int(entry)//HOUR
        week_counts=dict(prev.get("week_counts",{})) if prev else {}
        weekly=None
        if elapsed>0 and elapsed%168==0:
            prior=db.execute("SELECT body FROM marks WHERE slot=?",(slot-168,)).fetchone()
            if prior:
                prior=json.loads(prior[0])
                # First week includes the full entry cost on the initial 1000 USDT.
                last_short=D(1000) if elapsed==168 else D(prior["short_nav"])
                last_long=D(1000) if elapsed==168 else D(prior["long_nav"])
                s,l=short/last_short-1,long/last_long-1
                weekly=dict(index=elapsed//168,short=str(s),long=str(l))
                for name,inc in (("measured",1),("short_hits",int(s>=D(".02"))),("long_hits",int(l>=D(".02")))):
                    week_counts[name]=week_counts.get(name,0)+inc
        closed=at>=entry+SPEC["duration_days"]*DAY or min(short,long)<=gross*D(".1")
        result=dict(state="observed",slot=slot,epoch=at,at=clock.isoformat(),entry_epoch=entry,rule=RULE,
                    positions=positions,books={m:keep(b) for m,b in books.items()},
                    short_nav=str(short),long_nav=str(long),short_funding=str(funded),
                    week_counts=week_counts,weekly=weekly,
                    funding_evidence=evidence,new_cashflows=cashflows,closed=closed,
                    unobserved_hours=(prev["unobserved_hours"]+max(0,slot-prev["slot"]-1) if prev else 0))
        db.execute("INSERT INTO marks VALUES(?,?)",(slot,json.dumps(result,sort_keys=True)))
        db.commit()
        return result

def show(r):
    if r["state"]=="observed":
        short,long=D(r["short_nav"]),D(r["long_nav"])
        weeks=r.get("week_counts",{})
        msg=(f"WhiteBIT. Два віртуальні рахунки по 1000 USDT. Оцінка {r['at']} UTC.\n"
             f"Короткі позиції. Вартість після витрат {short:.2f} USDT. Зміна {short-1000:+.2f} USDT.\n"
             f"Довгі позиції. Вартість після витрат {long:.2f} USDT. Зміна {long-1000:+.2f} USDT.\n"
             f"Готівка 1000,00 USDT. Пропущено годин спостереження {r['unobserved_hours']}.\n"
             f"Виміряно повних тижнів {weeks.get('measured',0)}. Ціль 2% досягнута: "
             f"короткі позиції {weeks.get('short_hits',0)}, довгі позиції {weeks.get('long_hits',0)}.\n")
    else:
        msg="Очікується перше спостереження.\n" if r["state"]=="waiting" else "Початкове вікно пропущене. Позиції не відкрито.\n"
    path=OUT/f"mirror.{getpid()}.tmp"
    path.write_text(msg,encoding="utf-8-sig")
    replace(path,OUT/"Два рахунки.txt")

if __name__=="__main__":
    result=tick()
    show(result)
    print(result["state"])
