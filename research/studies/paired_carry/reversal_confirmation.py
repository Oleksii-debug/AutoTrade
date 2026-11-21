"""Test a confirmed relative recovery, preserving the terminal weekly clock."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from hashlib import sha256
import json
import sys
from data import CODE,ROOT,CUTS,DAY,HOUR,WEEK,inputs,market_data,pack,parse_klines,stamp
from trend_sides import hourly_lows
from relative_funding import episode
from momentum_pair import ranking_price,WhiteBitPublicClient

MARKETS=json.loads((CODE/"momentum-pair-rule.json").read_bytes())["markets"]


def confirm(panel,clock,long,short,window=2*DAY):
    ratios=[]
    for t in range(clock-DAY,clock+window,HOUR):
        a,b=panel[long][1][t],panel[short][1][t]
        if min(a[2],b[2])<=0:
            return None
        ratio=a[0]/b[0]
        if len(ratios)>=24 and ratio>max(ratios[-24:]):
            return t+HOUR
        ratios.append(ratio)
    return None


def phase(panel,lows,rules,first,last):
    cash=dict(confirmed=D(1000),fixed=D(1000))
    peak=dict(cash)
    dd=dict.fromkeys(cash,D(0))
    records=[]
    client=WhiteBitPublicClient()
    for t in range(first,last,WEEK):
        ranks={m:ranking_price(m,t-DAY,panel[m][1],client)/ranking_price(m,t-WEEK-DAY,panel[m][1],client) for m in MARKETS}
        order=sorted(MARKETS,key=lambda m:(ranks[m],m))
        buy,sell=order[0],order[-1]
        entered=confirm(panel,t,buy,sell)
        row=dict(clock=t,confirmed_entry=entered,long=buy,short=sell)
        for name,start in (("confirmed",entered),("fixed",t)):
            before=cash[name]
            if start is not None:
                if any(panel[m][2][at][2]<=0 for m in (buy,sell) for at in (start,t+WEEK)):
                    raise ValueError("Execution clock lacks observed activity")
                path,metrics=episode(panel,lows,rules,start,buy,sell,before,end_clock=t+WEEK)
                if not metrics["margin_ok"]:
                    raise ValueError("Insufficient collateral")
                for v in path:
                    peak[name]=max(peak[name],v)
                    dd[name]=max(dd[name],1-v/peak[name])
                cash[name]=path[-1]
                row[name+"_economics"]=metrics
            row[name]=str(cash[name]/before-1)
        records.append(row)
    geo=(cash["confirmed"]/1000)**(D(1)/len(records))-1
    return dict(weeks=records,ending=cash,weekly_geometric=geo,drawdown=dd,trades=sum(r["confirmed_entry"] is not None for r in records),
                target_hits=sum(D(r["confirmed"])>=D(".02") for r in records),eligible=geo>=D(".02") and cash["confirmed"]>cash["fixed"])


def load_era(era):
    archive=None
    if era=="recent":
        cuts=CUTS
        panel,rules=inputs()
        start=cuts[0]-DAY
        for m in MARKETS:
            symbol=m+"_USDT"
            raw,seen,uri=pack(ROOT/"momentum-warmup"/f"{symbol}-prior-day-{start}.json",
                lambda:WhiteBitPublicClient().klines(symbol,interval="1h",limit=24,start=start,end=cuts[0],training=True))
            page=parse_klines(raw,symbol,"1h",seen,source_uri=uri,requested_limit=24,start=start,end=cuts[0],training=True)
            for bar in page.candles:
                panel[m][1][int(bar.open_time.timestamp())]=(D(bar.open),D(bar.high),D(bar.base_volume))
    else:
        cuts=list(map(stamp,["2023-11-20T01:00:00","2024-05-20T01:00:00","2024-11-18T01:00:00"])) if era=="additional" else list(map(stamp,json.loads((CODE/"momentum-transfer-rule.json").read_bytes())["prior_year"]))
        archive=ROOT/("momentum-additional" if era=="additional" else "momentum-older")
        def load(m):
            return market_data(m,first_epoch=cuts[0]-WEEK-DAY,last_epoch=cuts[-1],archive=archive,funding_archive=ROOT/"momentum-older")
        with ThreadPoolExecutor(max_workers=2) as pool:
            panel=dict(zip(MARKETS,pool.map(load,MARKETS),strict=True))
        rules=json.loads((ROOT/"market-rules.json").read_bytes())
    lows={m:hourly_lows(m,archive) for m in MARKETS}
    return cuts,panel,rules,lows


def evaluate(era):
    cuts,panel,rules,lows=load_era(era)
    blocks=[]
    for a,b in zip(cuts,cuts[1:]):
        try:
            block=phase(panel,lows,rules,a,b)
        except (KeyError,ValueError) as exc:
            block=dict(eligible=False,unscorable=str(exc))
        blocks.append(dict(first=a,last=b,**block))
    rule=sha256((CODE/"reversal-confirm-rule.json").read_bytes()).hexdigest()
    (ROOT/f"reversal-confirm-{era}.json").write_text(json.dumps(dict(rule=rule,blocks=blocks),default=str,indent=2))
    print(era,[{k:v for k,v in block.items() if k!="weeks"} for block in blocks])


if __name__=="__main__":
    if len(sys.argv)!=2 or sys.argv[1] not in ("recent","prior","additional"):
        raise SystemExit("Choose recent, prior or additional")
    evaluate(sys.argv[1])
