"""Check price reachability for the seven pre-existing course signals on one-minute public candles."""
from datetime import datetime
from decimal import Decimal as D
from hashlib import sha256
import json
from pathlib import Path
import sys
from data import ROOT, WhiteBitPublicClient, pack, parse_klines


def check(candidate,client,dest):
    t=int(datetime.fromisoformat(candidate["confirmation_close_utc"]).timestamp())
    market=candidate["market"]
    long=candidate["direction"]=="LONG"
    limit=D(candidate["passive_execution_threshold"])
    stop,target=map(D,(candidate["fixed_stop"],candidate["fixed_target"]))
    raw,seen,uri=pack(dest/f"{market}-{t}.json",
                      lambda:client.klines(market,interval="1m",limit=17,start=t,end=t+17*60,training=True))
    page=parse_klines(raw,market,"1m",seen,source_uri=uri,requested_limit=17,start=t,end=t+17*60,training=True)
    bars={int(b.open_time.timestamp()):b for b in page.candles}
    result=dict(market=market,t=t,side=candidate["direction"],source=uri,raw_sha256=sha256(raw).hexdigest(),
                limit=str(limit),stop=str(stop),target=str(target))
    if set(bars)!=set(range(t,t+17*60,60)) or any(D(b.base_volume)<=0 for b in bars.values()):
        return dict(**result,status="missing_activity")
    def invalid(b):
        return (D(b.low)<=stop or D(b.high)>=target) if long else (D(b.high)>=stop or D(b.low)<=target)
    if any(invalid(bars[k]) for k in range(t,t+15*60,60)):
        return dict(**result,status="invalidated_before_window")
    for k in range(t+15*60,t+17*60,60):
        b=bars[k]
        touch=D(b.low)<=limit if long else D(b.high)>=limit
        if touch:
            return dict(**result,status="ambiguous_order" if invalid(b) else "price_reached",
                        observed_minute=k)
        if invalid(b): return dict(**result,status="invalidated_in_window")
    return dict(**result,status="never_reached")


def main(source):
    raw=source.read_bytes()
    audit=json.loads(raw)
    if audit["result_sha256"]!="aef08d76af68dfe902518370d46216fd953a80faea08c880db09c86fd6c7b37e":
        raise ValueError("Unexpected original course audit")
    dest=ROOT/"six-hour"/"course-price-evidence"
    dest.mkdir(parents=True,exist_ok=True)
    client=WhiteBitPublicClient()
    rows=[check(c,client,dest) for c in audit["candidates"]]
    result=dict(source_sha256=sha256(raw).hexdigest(),historical=True,orders=0,
                note="A printed price can exclude an entry. It cannot establish queue priority or profit.",
                observations=rows)
    (dest/"result.json").write_text(json.dumps(result,indent=2))
    print([r["status"] for r in rows])

if __name__=="__main__": main(Path(sys.argv[1]))
