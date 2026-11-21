"""Skip duplicate Windows wake-ups before loading the numerical or network clients."""
from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import time


def needs_work(path,first,end,now):
    if not path.is_file():
        return True
    step=21600
    with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True,timeout=10)) as db:
        tables={name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"decisions","entries","reviews","progress","reference_reviews"}<=tables:
            return True
        if db.execute("""SELECT 1 FROM entries e LEFT JOIN reviews r USING(clock)
                         WHERE r.clock IS NULL AND e.clock+?<=? LIMIT 1""",(step,now)).fetchone():
            return True
        if db.execute("""SELECT 1 FROM decisions d LEFT JOIN reference_reviews r USING(clock)
                         WHERE r.clock IS NULL AND d.clock+?<=? LIMIT 1""",(step+60,now)).fetchone():
            return True
        current=first+int((now-first)//step)*step
        upcoming=max(first,current+step)
        if upcoming<end and upcoming-3600<=now and not db.execute(
            "SELECT 1 FROM decisions WHERE clock=?",(upcoming,)).fetchone():
            return True
        if first<=current<end and now<current+300 and db.execute(
            """SELECT 1 FROM decisions d LEFT JOIN entries e USING(clock)
               WHERE d.clock=? AND e.clock IS NULL""",(current,)).fetchone():
            return True
        row=db.execute("SELECT body FROM progress WHERE id=1").fetchone()
        if not row:
            return True
        progress=json.loads(row[0])
        due=set(range(first,min(end,int(now)-299),step))
        started={k for k, in db.execute("SELECT clock FROM entries")}
        return (due-started!=set(progress.get("missed_entries",())) or
                len(progress["weeks"])!=min((end-first)//604800,max(0,int((now-first)//604800))) or
                (now>=end and progress["status"]=="collecting"))


if __name__=="__main__":
    code=Path(__file__).resolve().parent
    state=Path(os.getenv("AUTOTRADE_CARRY_STATE_DIR",code))/"six-hour"/"agent.sqlite3"
    config=json.loads((code/"six-hour-forward-rule.json").read_bytes())
    first=int(datetime.fromisoformat(config["first_entry_utc"]).timestamp())
    if needs_work(state,first,first+config["duration_weeks"]*604800,time.time()):
        from six_hour_forward import worker_main
        worker_main()
