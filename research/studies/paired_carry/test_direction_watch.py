from contextlib import closing, ExitStack
from datetime import datetime, UTC, timedelta
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from decimal import Decimal as D
import direction_watch as watch


class Clock(datetime):
    current = datetime.fromtimestamp(watch.ANCHOR,UTC)

    @classmethod
    def now(cls, tz=None):
        return cls.current


class DirectionWatchTests(unittest.TestCase):
    def test_weekly_comparison_and_restart(self):
        level = [D(100)]
        def capture():
            books = {m:SimpleNamespace(asks=[(str(level[0]+D(".01")),"1000")],
                                       bids=[(str(level[0]-D(".01")),"1000")]) for m in watch.MARKETS}
            return books,Clock.current
        def settlement(old,now,market,save):
            return {int(now):(D(".0001"),level[0])},[]
        with TemporaryDirectory() as tmp, ExitStack() as mocks:
            path=Path(tmp)
            rules={m:dict(step=".01",min_qty=".01",min_value="1") for m in watch.MARKETS}
            (path/"market-rules.json").write_text(json.dumps(rules))
            for k,v in dict(OUT=path,ROOT=path,datetime=Clock,capture=capture,keep=lambda b:{},settlements=settlement).items():
                mocks.enter_context(patch.object(watch,k,v))
            clock=datetime.fromtimestamp(watch.ANCHOR,UTC)
            Clock.current=clock-timedelta(minutes=1)
            self.assertEqual(watch.tick()["state"],"waiting")
            Clock.current=clock+timedelta(minutes=12)
            self.assertEqual(watch.tick()["state"],"missed")
            Clock.current=clock+timedelta(minutes=4)
            first=watch.tick()
            self.assertLess(D(first["long_nav"]),1000)
            self.assertLess(D(first["short_nav"]),1000)
            level[0]=D(110)
            Clock.current+=timedelta(days=7)
            second=watch.tick()
            self.assertEqual(second["week_counts"],dict(measured=1,long_hits=1,short_hits=0))
            self.assertEqual(second["unobserved_hours"],167)
            self.assertEqual(D(second["weekly"]["long"]),D(second["long_nav"])/1000-1)
            self.assertGreater(D(second["short_funding"]),0)
            self.assertEqual(watch.tick(),second)
            with closing(sqlite3.connect(path/"observer.sqlite3")) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM marks").fetchone()[0],2)


if __name__=="__main__":
    unittest.main()
