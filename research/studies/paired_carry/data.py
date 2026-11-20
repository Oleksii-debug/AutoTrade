from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal as D
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

CODE = Path(__file__).resolve().parent
ROOT = Path(os.getenv("AUTOTRADE_CARRY_STATE_DIR", CODE))
checkout = next((p for p in (Path(os.getenv("AUTOTRADE_CHECKOUT", CODE)), *CODE.parents,
                             CODE.parent/"20261120-weekly-integration")
                 if (p/"tools"/"whitebit_historical_klines.py").is_file()), None)
if checkout is None:
    raise RuntimeError("Set AUTOTRADE_CHECKOUT to the AutoTrade source directory")
sys.path.insert(0,str(checkout))
from tools.whitebit_historical_klines import WhiteBitPublicClient, _write_immutable
from research.autotrade_research.integrations.whitebit_public import parse_klines, parse_funding_history

HOUR=3600
DAY=24*HOUR
WEEK=7*DAY
SPEC=json.loads((CODE/"protocol.json").read_bytes())
def stamp(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=UTC).timestamp())
CUTS=list(map(stamp,SPEC["cuts"]))

def pack(path,fetch):
    if not path.exists():
        item=fetch()
        packet=dict(raw=item.raw.decode(),uri=item.source_uri,received=item.received_at.isoformat(),
                    sha256=sha256(item.raw).hexdigest())
        _write_immutable(path,json.dumps(packet,sort_keys=True).encode())
    p=json.loads(path.read_bytes())
    raw=p["raw"].encode()
    if sha256(raw).hexdigest()!=p["sha256"]: raise ValueError("Archive changed")
    return raw,datetime.fromisoformat(p["received"]),p["uri"]

def market_data(asset):
    c=WhiteBitPublicClient()
    base=ROOT/"data"/asset
    future=asset+"_PERP"
    funding={}
    for offset in range(0,4000,100):
        raw,at,uri=pack(base/f"funding-{offset}.json",lambda:c.funding_history(future,limit=100,offset=offset))
        page=parse_funding_history(raw,future,at,limit=100,offset=offset)
        if page.source_uri!=uri: raise ValueError("Funding source mismatch")
        for r in page.records:
            t=int(r.funding_time.timestamp())
            value=(D(r.funding_rate),D(r.settlement_price))
            if t in funding and funding[t]!=value: raise ValueError("Conflicting settlement")
            funding[t]=value
        if not page.records or min(funding)<=CUTS[0]-30*DAY or len(page.records)<100: break
    prices={}
    for symbol in (asset+"_USDT",future):
        series=prices[symbol]={}
        for first in range(CUTS[0],CUTS[-1]+1,1000*HOUR):
            n=min(1000,(CUTS[-1]-first)//HOUR+1)
            end=first+n*HOUR
            args=dict(start=first,end=end,training=True)
            raw,at,uri=pack(base/f"{symbol}-{first}.json",lambda:c.klines(symbol,interval="1h",limit=n,**args))
            batch=parse_klines(raw,symbol,"1h",at,source_uri=uri,requested_limit=n,**args)
            for bar in batch.candles:
                t=int(bar.open_time.timestamp())
                if not first<=t<end or t%HOUR or t in series: raise ValueError("Invalid candle grid")
                series[t]=(D(bar.open),D(bar.high),D(bar.base_volume))
    return funding,prices[asset+"_USDT"],prices[future]

def inputs():
    profile=ROOT/"market-rules.json"
    if not profile.exists():
        items=WhiteBitPublicClient().instruments()
        selected={i.market:dict(step=i.step_size,min_qty=i.minimum_quantity,min_value=i.minimum_notional,
                                taker=i.taker_fee_rate) for i in items if i.market in
                  {a+suffix for a in SPEC["universe"] for suffix in ("_USDT","_PERP")}}
        if len(selected)!=2*len(SPEC["universe"]): raise ValueError("Incomplete instrument universe")
        _write_immutable(profile,json.dumps(selected,sort_keys=True).encode())
    with ThreadPoolExecutor(max_workers=2) as pool:
        panel=dict(zip(SPEC["universe"],pool.map(market_data,SPEC["universe"]),strict=True))
    return panel,json.loads(profile.read_bytes())

if __name__=="__main__":
    panel,_=inputs()
    print({a:list(map(len,rows)) for a,rows in panel.items()})
