"""Exercise the weekly Windows worker with an isolated account and actual public market responses."""
from contextlib import closing
import json
import sqlite3
from unittest.mock import patch

import market_ranking_forward as worker


def run():
    seed=json.loads((worker.STATE/"initial-experience.json").read_bytes())
    clock=max(map(int,seed["records"]))
    seed["records"]={k:v for k,v in seed["records"].items() if int(k)+worker.WEEK<=clock-worker.DAY}
    directory=worker.STATE/"archived-rehearsal"
    directory.mkdir(parents=True,exist_ok=True)
    (directory/"initial-experience.json").write_text(json.dumps(seed,separators=(",",":")))
    now=[clock-worker.DAY+2*worker.HOUR]
    with (patch.object(worker,"STATE",directory),patch.object(worker,"FIRST",clock),
          patch.object(worker,"END",clock+worker.WEEK),patch.object(worker.time,"time",lambda:now[0])):
        worker.run()
        worker.run()
        now[0]=clock+worker.WEEK+2*worker.HOUR
        worker.run()
        worker.run()
    with closing(sqlite3.connect(directory/"learning.sqlite3")) as db:
        observed=list(db.execute("SELECT clock,recorded,body FROM forecasts"))
        assert len(observed)==1 and observed[0][1]<clock
        row=json.loads(db.execute("SELECT outcome FROM examples WHERE clock=?",(clock,)).fetchone()[0])
        assert db.execute("PRAGMA integrity_check").fetchone()==("ok",)
        assert row["features_revised"] is False
        print("Historical rehearsal:",clock,row["marked_per_1000"])


if __name__=="__main__": run()
