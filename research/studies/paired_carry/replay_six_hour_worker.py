"""Drive the installed worker's account and learning code with archived clock observations."""
from contextlib import ExitStack, closing, redirect_stdout
from datetime import UTC, datetime
from io import StringIO
import json
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import six_hour_forward as agent
from six_hour import D, HOUR, MARKET, OUT, STEP
from six_hour_book_replay import archived_inputs, stage


class NoArchivedQuote(Exception):
    pass


def simulate(directory):
    bars,funding=archived_inputs()
    with TemporaryDirectory() as tmp, ExitStack() as stack:
        root=Path(tmp)
        tape_path=root/"books.sqlite3"
        tape=stack.enter_context(closing(sqlite3.connect(tape_path)))
        shard_count=stage(tape,directory)
        low,high=tape.execute("SELECT min(bucket_epoch),max(bucket_epoch) FROM execution_bbo").fetchone()
        first=agent.FIRST-((agent.FIRST-low)//STEP)*STEP
        last=first+((high-300-first)//STEP)*STEP
        if last<=first:
            raise ValueError("No complete six-hour intervals")
        now=first-1500
        def book():
            rows=tape.execute("""SELECT bucket_epoch,bid_price,bid_quantity,ask_price,ask_quantity,received_at,source_sha256
                    FROM execution_bbo WHERE market=? AND bucket_epoch BETWEEN ? AND ? ORDER BY bucket_epoch DESC""",
                    (MARKET,int(now)-10,int(now)))
            for bucket,bid,bq,ask,aq,received,digest in rows:
                dt=datetime.fromisoformat(received)
                if 0<=now-dt.timestamp()<10:
                    return SimpleNamespace(bids=[(bid,bq)],asks=[(ask,aq)],source_time=dt,
                                           evidence=dict(bucket=bucket,received=received,sha256=digest)),now
            raise NoArchivedQuote()
        def history(_db,cutoff):
            # Inputs physically stop at the forecast cutoff, including on clock retries.
            return ({k:v for k,v in bars.items() if k+HOUR<=cutoff},
                    {k:v for k,v in funding.items() if k<=cutoff})
        def financing(after,until):
            ticks=range((int(after)//(8*HOUR)+1)*8*HOUR,int(until)+1,8*HOUR)
            return sum((funding[t] for t in ticks),D(0)),[dict(epoch=t,payment_per_btc=str(funding[t])) for t in ticks]
        state=root/"state"
        dependencies=dict(FIRST=first,END=last,OUT=state,TAPE=tape_path,quote=book,keep=lambda b:b.evidence,
                          history_to=history,funding_per_unit=financing)
        for name,value in dependencies.items():
            stack.enter_context(patch.object(agent,name,value))
        stack.enter_context(patch.object(agent.time,"time",lambda:now))
        stack.enter_context(redirect_stdout(StringIO()))
        gaps=[]
        for clock in range(first,last+1,STEP):
            for delay in (-1500,22):
                for retry in range(3):
                    now=clock+delay+60*retry
                    try:
                        agent.run()
                        break
                    except NoArchivedQuote:
                        gaps.append(now)
        with closing(sqlite3.connect(state/"agent.sqlite3")) as db:
            forecasts=[json.loads(b) for b, in db.execute("SELECT body FROM decisions ORDER BY clock")]
            reviews=[json.loads(b) for b, in db.execute("SELECT body FROM reviews ORDER BY clock")]
            progress=json.loads(db.execute("SELECT body FROM progress").fetchone()[0])
            if db.execute("PRAGMA integrity_check").fetchone()!=("ok",):
                raise ValueError("Archived account failed integrity validation")
        report=dict(historical=True,source="retained BTC best bid/ask with recorded receipt timestamps",
                    shards=shard_count,first=first,last=last,forecasts=len(forecasts),reviews=len(reviews),
                    timely=sum(r["timely"] for r in reviews),corrected=sum(f["prior_reviews"]>=12 for f in forecasts),
                    largest_absolute_bias=max((abs(f["online_bias"]) for f in forecasts),default=0),
                    directional_choices=sum(bool(f["side"]) for f in forecasts),
                    attempted_clocks_without_quotes=gaps,progress=progress)
        target=OUT/"archived-worker"
        target.mkdir(parents=True,exist_ok=True)
        for name in ["agent.sqlite3","result.json"]:
            if name=="result.json":
                (state/name).write_text(json.dumps(report,indent=2))
            partial=target/(name+".tmp")
            shutil.copyfile(state/name,partial)
            partial.replace(target/name)
    return report


if __name__=="__main__":
    result=simulate(Path(sys.argv[1]).resolve())
    print({k:v for k,v in result.items() if k not in ("attempted_clocks_without_quotes","progress")})
    print(result["progress"])
