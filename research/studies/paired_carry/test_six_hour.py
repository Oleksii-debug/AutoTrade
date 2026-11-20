from contextlib import closing, redirect_stdout
from datetime import datetime, UTC
from decimal import Decimal as D
from io import StringIO
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import six_hour_forward as agent


class SixHourTests(unittest.TestCase):
    def setUp(self):
        folder=Path(self.enterContext(TemporaryDirectory()))
        rules={agent.MARKET: dict(step="0.001", min_qty="0.001", min_value="5")}
        (folder/"market-rules.json").write_text(json.dumps(rules))
        self.enterContext(patch.object(agent,"ROOT",folder))

    def test_decision_entry_review_and_restart(self):
        for side in (-1,0,1):
            with self.subTest(side=side), TemporaryDirectory() as tmp:
                folder=Path(tmp)
                now=[agent.FIRST-2*3600]
                def quote():
                    px=100 if now[0]<agent.FIRST+agent.STEP else 110
                    return SimpleNamespace(asks=[(str(px+1),"1000")],bids=[(str(px-1),"1000")],
                                           source_time=datetime.fromtimestamp(now[0],UTC)),now[0]
                with (patch.object(agent,"OUT",folder),patch.object(agent.time,"time",lambda:now[0]),
                      patch.object(agent,"quote",quote),patch.object(agent,"keep",lambda b:{"source":"fixture"}),
                      patch.object(agent,"settlements",lambda *a:({0:(D(".001"),D(100))},[])),redirect_stdout(StringIO())):
                    agent.run()
                    with closing(sqlite3.connect(folder/"agent.sqlite3")) as db:
                        db.execute("INSERT INTO decisions VALUES(?,?,?)",
                                   (agent.FIRST,agent.FIRST-600,json.dumps(dict(side=side,model={"expected":0}))))
                        db.commit()
                    now[0]=agent.FIRST+22
                    agent.run()
                    agent.run()
                    now[0]+=agent.STEP
                    agent.run()
                    agent.run()
                    with closing(sqlite3.connect(folder/"agent.sqlite3")) as db:
                        self.assertEqual(db.execute("select count(*) from entries").fetchone()[0],1)
                        reviews=db.execute("select body from reviews").fetchall()
                        self.assertEqual(len(reviews),1)
                        r=json.loads(reviews[0][0])
                        q=D(json.loads(db.execute("select body from entries").fetchone()[0])["reference_quantity"])
                        self.assertEqual(D(r["matched_alternatives"]["long"]),q*(D("7.9")-D(210)*agent.FEE))
                        self.assertEqual(D(r["matched_alternatives"]["short"]),q*(D("-11.9")-D(210)*agent.FEE))
                        self.assertEqual(D(r["capital"])-1000,D(r["net"]))
                        self.assertEqual(D(r["net"]),D(r["matched_alternatives"][{1:"long",-1:"short"}[side]]) if side else 0)
                        stats=json.loads(db.execute("SELECT body FROM progress").fetchone()[0])["forecast_review"]
                        self.assertEqual(stats["timely_examples"],1)
                        self.assertEqual(D(stats["mean_absolute_errors"]["flat_market"]),D(".099"))

    def test_missed_entry_stays_empty(self):
        with (TemporaryDirectory() as tmp,patch.object(agent,"OUT",Path(tmp)),
              patch.object(agent.time,"time",lambda:agent.FIRST+320),redirect_stdout(StringIO())):
            agent.run()
            with closing(sqlite3.connect(Path(tmp)/"agent.sqlite3")) as db:
                self.assertEqual(db.execute("select count(*) from entries").fetchone()[0],0)

    def test_settlement_retry_uses_original_exit_even_without_opposite_depth(self):
        with TemporaryDirectory() as tmp:
            folder=Path(tmp)
            now=[agent.FIRST+22]
            def quote():
                at=now[0]
                entry=at<agent.FIRST+agent.STEP
                return SimpleNamespace(asks=[("101" if entry else "115","100" if entry else ".001")],
                                       bids=[("99" if entry else "110",".001" if entry else "100")],
                                       source_time=datetime.fromtimestamp(at,UTC)),at
            with (patch.object(agent,"OUT",folder),patch.object(agent.time,"time",lambda:now[0]),
                  patch.object(agent,"keep",lambda b:{"source":"fixture"}),
                  patch.object(agent,"quote",side_effect=quote) as books,redirect_stdout(StringIO())):
                agent.run()
                with closing(sqlite3.connect(folder/"agent.sqlite3")) as db:
                    db.execute("INSERT INTO decisions VALUES(?,?,?)",
                               (agent.FIRST,agent.FIRST-600,json.dumps(dict(side=1,model={"expected":0}))))
                    db.commit()
                agent.run()
                now[0]+=agent.STEP
                captured_at=now[0]
                with patch.object(agent,"settlements",side_effect=TimeoutError),self.assertRaises(TimeoutError):
                    agent.run()
                now[0]+=900
                with patch.object(agent,"settlements",return_value=({},[])):
                    agent.run()
                self.assertEqual(books.call_count,2)
                with closing(sqlite3.connect(folder/"agent.sqlite3")) as db:
                    result=json.loads(db.execute("SELECT body FROM reviews").fetchone()[0])
                    self.assertEqual(result["fill"],"110")
                    self.assertEqual(result["closed"],captured_at)
                    self.assertEqual(result["reviewed_at"],now[0])
                    self.assertTrue(result["timely"])
                    self.assertIsNone(result["matched_alternatives"]["short"])
                    self.assertEqual(result["net"],result["matched_alternatives"]["long"])

if __name__=="__main__": unittest.main()
