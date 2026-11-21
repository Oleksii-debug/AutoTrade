import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import closing,redirect_stdout
from io import StringIO

import market_ranking_forward as worker


class MarketRankingTests(unittest.TestCase):
    def test_saved_forecast_survives_restart_and_gets_exactly_one_later_review(self):
        t=worker.FIRST
        week=worker.WEEK
        x=[[float(i),0.,0.,0.] for i in range(6)]
        y=[(i-2.5)*.03 for i in range(6)]
        initial={t-n*week:dict(x=x,y=y) for n in range(2,54)}
        completed=dict(x=x,y=y,alternatives_per_1000={"XRP/ADA":dict(net="25")})
        now=[t-worker.DAY]
        with (TemporaryDirectory() as tmp,patch.object(worker,"STATE",Path(tmp)),
              patch.object(worker.time,"time",lambda:now[0]),patch.object(worker,"current_features",return_value=(x,{})) as source,
              patch.object(worker,"completed",return_value=completed) as closed,redirect_stdout(StringIO())):
            (Path(tmp)/"initial-experience.json").write_text(json.dumps(dict(identity=worker.IDENTITY,records=initial)))
            worker.run()
            now[0]+=worker.HOUR
            worker.run()
            worker.run()
            self.assertEqual(source.call_count,1)
            now[0]=t+week+worker.HOUR
            worker.run()
            worker.run()
            self.assertEqual(closed.call_count,2)
            with closing(sqlite3.connect(Path(tmp)/"learning.sqlite3")) as db:
                self.assertEqual(db.execute("select count(*) from forecasts").fetchone()[0],1)
                predicted=db.execute("select recorded,body from forecasts").fetchone()
                self.assertLess(predicted[0],t)
                self.assertTrue(all(m["last_outcome"]<=t-worker.DAY for m in json.loads(predicted[1])["models"]))
                row=json.loads(db.execute("select outcome from examples where clock=?",(t,)).fetchone()[0])
                self.assertTrue(row["research_only"])
                self.assertEqual(row["marked_per_1000"]["selected_net"],"25")
                self.assertEqual(db.execute("select outcome from examples where clock=?",(t-week,)).fetchone()[0],"null")


if __name__=="__main__": unittest.main()
