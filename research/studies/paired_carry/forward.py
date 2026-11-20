"""Persistent public-book simulation of one fully collateral-budgeted BTC hedge."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, UTC
from decimal import Decimal as D, ROUND_DOWN
from hashlib import sha256
import json
from os import replace, getpid
import sqlite3
from time import sleep
from data import CODE, ROOT, WhiteBitPublicClient, _write_immutable, HOUR, DAY
from study import SF,PF

OUT=ROOT/"forward"
SLIP=D(".001")
MARKETS=("BTC_USDT","BTC_PERP")

def average(levels,q):
    rest=q
    total=D(0)
    for p,v in levels:
        size=min(rest,D(v))
        total+=size*D(p)
        rest-=size
        if not rest: return total/q
    raise ValueError("Insufficient book depth on one leg")

def archive(public):
    h=sha256(public.raw).hexdigest()
    _write_immutable(OUT/"raw"/(h+".json"),public.raw)
    return dict(sha256=h,source=public.source_uri,received=public.received_at.isoformat())

def paired_books():
    with ThreadPoolExecutor(max_workers=2) as pool:
        books=list(pool.map(lambda m:WhiteBitPublicClient().instrument_orderbook(m),MARKETS))
    now=datetime.now(UTC)
    for b in books: b.assert_fresh(now)
    if max(abs((books[0].source_time-books[1].source_time).total_seconds()),
           abs((books[0].received_at-books[1].received_at).total_seconds()))>2:
        raise ValueError("Public books outside paired time tolerance")
    return books

def settlements(after,at,market="BTC_PERP",save=archive):
    rows={}
    evidence=[]
    c=WhiteBitPublicClient()
    for off in range(0,1000,100):
        page=c.funding_history(market,limit=100,offset=off)
        evidence.append(save(page))
        for r in page.records:
            t=int(r.funding_time.timestamp())
            if after<t<=at: rows[t]=(D(r.funding_rate),D(r.settlement_price))
        if page.records and page.records[-1].funding_time.timestamp()<=after: break
        if len(page.records)<100: raise ValueError("Incomplete funding archive")
    else: raise ValueError("Funding pagination incomplete")
    # All 1,200 archived BTC observations follow the eight-hour UTC grid.
    expected=set(range((int(after)//(8*HOUR)+1)*8*HOUR,int(at)+1,8*HOUR))
    if set(rows)!=expected: raise ValueError("Funding schedule changed or pending settlement")
    return rows,evidence

def inspect(db):
    row=db.execute("SELECT body FROM marks ORDER BY slot DESC LIMIT 1").fetchone()
    return json.loads(row[0]) if row else None

def save_summary(mark):
    nav = D(mark["nav_usdt"])
    state = "закрита" if mark["closed"] else "відкрита"
    at = datetime.fromtimestamp(mark["epoch"],UTC).astimezone().strftime("%Y-%m-%d %H:%M %z")
    message = (f"AutoTrade. Симуляція на котируваннях WhiteBIT. {at}\n"
               f"Позиція {state}. Спот і короткий ф’ючерс по {mark['entry']['q']} BTC.\n"
               f"Початковий капітал 1000 USDT. Вартість після витрат на закриття {nav:.2f} USDT.\n"
               f"Зміна вартості {nav-1000:+.2f} USDT. Накопичені виплати {D(mark['funding_usdt']):+.4f} USDT.\n")
    temp = OUT / f"status.{getpid()}.tmp"
    temp.write_text(message,encoding="utf-8-sig")
    replace(temp,OUT/"Рахунок BTC.txt")

def tick():
    OUT.mkdir(exist_ok=True,parents=True)
    with closing(sqlite3.connect(OUT/"ledger.sqlite3",timeout=25)) as db:
        db.execute("CREATE TABLE IF NOT EXISTS marks(slot INTEGER PRIMARY KEY, body TEXT NOT NULL)")
        db.execute("BEGIN IMMEDIATE")
        old=inspect(db)
        if old and (old["closed"] or old["slot"]==int(datetime.now(UTC).timestamp())//HOUR):
            return old
        study=json.loads((ROOT/"results.json").read_bytes())
        rule=sha256((CODE/"protocol.json").read_bytes()).hexdigest()
        if not study["results"]["btc"]["eligible"] or rule!=study["protocol_sha256"]:
            raise ValueError("No qualifying registered BTC comparator")
        spot,perp=paired_books()
        at=max(spot.received_at,perp.received_at)
        epoch=at.timestamp()
        slot=int(epoch)//HOUR
        if old and slot<=old["slot"]: return old
        refs=[archive(spot),archive(perp)]
        if old:
            entry=old["entry"]
            if entry["protocol"]!=rule: raise ValueError("Protocol changed")
        else:
            rules=json.loads((ROOT/"market-rules.json").read_bytes())
            step=max(D(rules[m]["step"]) for m in MARKETS)
            q=(D(400)/(max(D(spot.asks[0][0]),D(perp.bids[0][0]))*(1+SLIP))/step).to_integral_value(rounding=ROUND_DOWN)*step
            if q<=0 or any(q%D(rules[m]["step"]) for m in MARKETS): raise ValueError("Invalid order size")
            buy=average(spot.asks,q)*(1+SLIP)
            short=average(perp.bids,q)*(1-SLIP)
            if q*max(buy,short)>400: raise ValueError("Book impact exceeded budget")
            if any(q<D(rules[m]["min_qty"]) or q*p<D(rules[m]["min_value"]) for m,p in zip(MARKETS,(buy,short))):
                raise ValueError("Order below public market minimum")
            entry=dict(at=epoch,protocol=rule,q=str(q),buy=str(buy),short=str(short),
                       fees=str(q*(buy*SF+short*PF)),books=refs)
        q=D(entry["q"])
        buy,short,entry_fees=map(D,(entry["buy"],entry["short"],entry["fees"]))
        new,evidence=settlements(old["epoch"],epoch) if old else ({},[])
        payment=sum((q*rate*price for rate,price in new.values()),D(0))
        funded=payment+(D(old["funding_usdt"]) if old else 0)
        sell=average(spot.bids,q)*(1-SLIP)
        cover=average(perp.asks,q)*(1+SLIP)
        close_fees=q*(sell*SF+cover*PF)
        nav=D(1000)+q*(sell-buy+short-cover)+funded-entry_fees-close_fees
        collateral=D(1000)-q*buy-entry_fees+funded+q*(short-cover)
        close=epoch>=entry["at"]+182*DAY or collateral<=q*cover*(D(".1")+PF)
        doc=dict(slot=slot,epoch=epoch,at=at.isoformat(),entry=entry,books=refs,nav_usdt=str(nav),
                 funding_usdt=str(funded),payments={str(k):[str(x) for x in v] for k,v in new.items()},
                 funding_sources=evidence,collateral=str(collateral),closed=close,
                 realized_trade_pnl=str(nav-D(1000)) if close else None,
                 reference_spot_exit=str(sell),reference_perp_cover=str(cover),
                 exchange_orders=0,private_account=False,paid_model_calls=0)
        db.execute("INSERT INTO marks VALUES(?,?)",(slot,json.dumps(doc,sort_keys=True)))
        db.commit()
        return doc

if __name__=="__main__":
    for attempt in range(3):
        try:
            result=tick()
            save_summary(result)
            print(result["at"],result["nav_usdt"],result["funding_usdt"],result["closed"])
            break
        except (OSError,RuntimeError,ValueError):
            if attempt==2: raise
            sleep(2)
