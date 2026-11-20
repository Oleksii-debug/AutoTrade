"""Point-in-time feasibility of a 2% weekly portfolio target, before opening any new pair."""
from dataclasses import asdict
from decimal import Decimal as D
from datetime import datetime,UTC
from pathlib import Path
import json
from data import ROOT,WhiteBitPublicClient,pack,parse_funding_history,DAY

def main():
    c=WhiteBitPublicClient()
    at=datetime.now(UTC)
    instruments={x.market:x for x in c.instruments()}
    pairs=[]
    for f in c.futures_markets():
        spot=f.base_asset+"_USDT"
        if f.quote_asset!="USDT" or D(f.quote_volume)<1000000 or spot not in instruments or f.market not in instruments: continue
        n=D(7*1440)/f.funding_interval_minutes
        weekly=D(".4")*D(f.funding_rate)*n
        pairs.append(dict(market=f.market,spot=spot,rate=f.funding_rate,minutes=f.funding_interval_minutes,
                          volume=f.quote_volume,weekly_account_if_rate_constant=weekly))
    pairs.sort(key=lambda x:-x["weekly_account_if_rate_constant"])
    checked=[]
    dest=ROOT/"screen"/at.strftime("%Y%m%dT%H%M%SZ")
    for p in pairs:
        if p["weekly_account_if_rate_constant"]<D(".02"): continue
        recent={}
        for offset in range(0,800,100):
            m=p["market"]
            raw,got,_=pack(dest/f"{m}-{offset}.json",lambda:c.funding_history(m,limit=100,offset=offset))
            r=parse_funding_history(raw,m,got,limit=100,offset=offset)
            for row in r.records:
                recent[int(row.funding_time.timestamp())]=D(row.funding_rate)
            if not r.records or min(recent)<=at.timestamp()-28*DAY or len(r.records)<100: break
        ts=sorted(k for k in recent if k<at.timestamp())
        def calc(days): return sum((recent[k] for k in ts if k>at.timestamp()-days*DAY),D(0))
        # Pre-entry evidence only. Reject gaps and histories too short for the lookback.
        ok=bool(ts and ts[0]<=at.timestamp()-28*DAY and at.timestamp()-ts[-1]<=8*3600
                and all(b-a<=8*3600 for a,b in zip(ts,ts[1:])))
        estimate=D(".4")*(min(calc(7),calc(28)/4)-D(".0072"))
        checked.append(dict(market=m,history_covered=ok,estimated_net=estimate,passes=ok and estimate>=D(".02")))
    dest.mkdir(parents=True,exist_ok=True)
    result=dict(at=at.isoformat(),universe=pairs,history=checked,candidates=[r["market"] for r in checked if r["passes"]])
    (dest/"snapshot.json").write_text(json.dumps(result,default=str,indent=2))
    print(len(pairs),"liquid paired markets;",len(checked),"clear constant-rate hurdle;",result["candidates"],"confirmed by trailing rates")
    print([(p["market"],str((p["weekly_account_if_rate_constant"]*100).quantize(D(".0001")))) for p in pairs[:5]])

if __name__=="__main__": main()
