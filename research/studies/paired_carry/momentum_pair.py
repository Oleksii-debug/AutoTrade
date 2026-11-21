"""Replay one fixed cross-sectional momentum pair on observed WhiteBIT funding and prices."""
from decimal import Decimal as D
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from itertools import permutations
import json
import sys
from statistics import median

from data import CODE, ROOT, CUTS, DAY, HOUR, WEEK, inputs, market_data, pack, parse_klines, stamp, WhiteBitPublicClient
from relative_funding import episode
from trend_sides import hourly_lows


def ranking_price(asset, epoch, prices, client):
    if epoch in prices:
        price,_,volume=prices[epoch]
    else:
        market=asset+"_USDT"
        path=ROOT/"momentum-warmup"/f"{market}-{epoch}.json"
        raw,received,uri=pack(path,lambda:client.klines(market,interval="1h",limit=1,start=epoch,end=epoch+HOUR,training=True))
        page=parse_klines(raw,market,"1h",received,source_uri=uri,requested_limit=1,start=epoch,end=epoch+HOUR,training=True)
        if len(page.candles)!=1 or int(page.candles[0].open_time.timestamp())!=epoch:
            raise ValueError("Missing ranking candle")
        bar=page.candles[0]
        price,volume=D(bar.open),D(bar.base_volume)
    if price<=0 or volume<=0:
        raise ValueError("Inactive ranking market")
    return price


def compare(panel,rules,lows,markets,first,last,client,primary,*,with_exits=False):
    arms=("momentum","reverse","rotation","reversal_with_exit") if with_exits else ("momentum","reverse","rotation")
    capital=dict.fromkeys(arms,D(1000))
    peaks=dict(capital)
    drawdown=dict.fromkeys(arms,D(0))
    pairs=list(permutations(sorted(markets),2))
    weeks=[]
    for clock in range(first,last,WEEK):
        cutoff=clock-DAY
        scores={}
        for m in markets:
            prices=panel[m][1]
            scores[m]=ranking_price(m,cutoff,prices,client)/ranking_price(m,cutoff-WEEK,prices,client)-1
        ranked=sorted(markets,key=lambda m:(scores[m],m))
        selection={"momentum":(ranked[-1],ranked[0]),"reverse":(ranked[0],ranked[-1]),
                   "rotation":pairs[((clock-CUTS[0])//WEEK)%len(pairs)]}
        if with_exits:
            selection["reversal_with_exit"]=selection["reverse"]
        row=dict(clock=clock,cutoff=cutoff,scores=scores,arms={})
        for arm,(buy,sell) in selection.items():
            for m in (buy,sell):
                if min(panel[m][2][t][2] for t in (clock,clock+WEEK))<=0:
                    raise ValueError(f"No observed activity at an execution clock for {m} at {clock}")
            before=capital[arm]
            path,metrics=episode(panel,lows,rules,clock,buy,sell,before,
                                 exit_band=D(".02") if arm=="reversal_with_exit" else None)
            if any(panel[m][2][metrics.get("exit_clock",clock+WEEK)][2]<=0 for m in (buy,sell)):
                raise ValueError("Missing activity at the simulated exit")
            if not metrics["margin_ok"]:
                raise ValueError(f"Collateral breach in {arm} at {clock}")
            capital[arm]=path[-1]
            for equity in path:
                peaks[arm]=max(peaks[arm],equity)
                drawdown[arm]=max(drawdown[arm],1-equity/peaks[arm])
            attributed=D(metrics["price_pnl"])+D(metrics["funding"])-D(metrics["costs"])
            if abs(capital[arm]-before-attributed)>D("1e-18"):
                raise ValueError("Weekly settlement failed to reconcile")
            row["arms"][arm]=dict(long=buy,short=sell,capital=capital[arm],weekly=capital[arm]/before-1,**metrics)
        weeks.append(row)
    returns=[r["arms"][primary]["weekly"] for r in weeks]
    geometric=(capital[primary]/1000)**(D(1)/len(weeks))-1
    ex_best=capital[primary]/(1+max(returns))-1000
    passes=geometric>=D(".02") and capital[primary]>max([1000]+[v for k,v in capital.items() if k!=primary]) and ex_best>0
    return dict(first=first,last=last,capital=capital,drawdown=drawdown,weeks=weeks,target_hits=sum(r>=D(".02") for r in returns),
                geometric_weekly=geometric,median_weekly=median(returns),excluding_best=ex_best,eligible=passes)


if __name__=="__main__":
    raw=(CODE/"momentum-pair-rule.json").read_bytes()
    markets=json.loads(raw)["markets"]
    cuts=CUTS
    archive=None
    primary="momentum"
    rule_name="momentum-pair"
    args=sys.argv[1:]
    if len(args)!=len(set(args)) or set(args)-{"--earlier-year","--with-exits"}:
        raise SystemExit("Supported options are --earlier-year and --with-exits")
    if "--earlier-year" in args:
        rule_name="momentum-transfer"
        transfer=(CODE/"momentum-transfer-rule.json").read_bytes()
        cuts=list(map(stamp,json.loads(transfer)["prior_year"]))
        raw+=transfer
        primary="reverse"
        archive=ROOT/"momentum-older"
        rules=json.loads((ROOT/"market-rules.json").read_bytes())
        def load_market(m):
            return market_data(m,first_epoch=cuts[0]-WEEK-DAY,last_epoch=cuts[-1],archive=archive)
        with ThreadPoolExecutor(max_workers=2) as pool:
            panel=dict(zip(markets,pool.map(load_market,markets),strict=True))
    else:
        panel,rules=inputs()
    with_exits="--with-exits" in args
    if with_exits:
        rule_name+="-exits"
        raw+=(CODE/"reversal-exit-rule.json").read_bytes()
        primary="reversal_with_exit"
    lows={m:hourly_lows(m,archive) for m in markets}
    client=WhiteBitPublicClient()
    blocks=[compare(panel,rules,lows,markets,a,b,client,primary,with_exits=with_exits) for a,b in zip(cuts,cuts[1:])]
    result=dict(rule_sha256=sha256(raw).hexdigest(),historical=True,primary=primary,blocks=blocks,eligible=all(b["eligible"] for b in blocks))
    (ROOT/(rule_name+"-results.json")).write_text(json.dumps(result,default=str,indent=2))
    print([(b["capital"],b["geometric_weekly"],b["target_hits"],b["eligible"]) for b in blocks])
