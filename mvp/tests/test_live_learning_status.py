from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from autotrade_mvp.live_learning_status import read_whitebit_live_learning_status


NOW = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)


def state():
    return dict(schema="autotrade-live-learning-cycle-v1", phase="EVALUATING", market="BTC_USDT",
        cycle=1, trials=1, episodes=20, config=dict(trials=3), incumbent_version=0,
        candidate_version=1, incumbent_sha256="sha256:"+"a"*64,
        candidate_sha256="sha256:"+"b"*64, network_orders=False,
        trading_authority=False, champion_promotion=False,
        analysis=dict(net_usdt="-1.23", fees_usdt="0.10", episodes=20,
                      finding="SECRET_KEY_DO_NOT_EXPORT"),
        evaluation=dict(candidate="0.1", incumbent="-0.1", drawdown="0", no_trade="0",
                        count=4, candidate_trades=1, incumbent_trades=2, invalid_gap_count=0),
        secret="SECRET_KEY_DO_NOT_EXPORT")


class LiveLearningStatusTests(unittest.TestCase):
    def seed(self, root, payload=None, timestamp=None):
        path = root / "episode-memory.sqlite3"
        with closing(sqlite3.connect(path)) as con:
            con.executescript("CREATE TABLE live_cycle(id INTEGER PRIMARY KEY,state TEXT);"
                "CREATE TABLE observations(available_at TEXT UNIQUE);"
                "CREATE TABLE models(version INTEGER PRIMARY KEY,model_sha256 TEXT);")
            con.executemany("INSERT INTO models VALUES(?,?)", [(0,"sha256:"+"a"*64),(1,"sha256:"+"b"*64)])
            con.execute("INSERT INTO live_cycle VALUES(1,?)", (json.dumps(payload or state()),))
            con.execute("INSERT INTO observations VALUES(?)", ((timestamp or NOW).isoformat(),))
            con.commit()
        return path

    def test_missing_never_creates_database_or_directory(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)/"absent"
            self.assertEqual(read_whitebit_live_learning_status(root, now=NOW)["status"], "MISSING")
            self.assertFalse(root.exists())

    def test_whitelist_and_readonly_stable_snapshot(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = self.seed(root)
            before = path.read_bytes()
            with closing(sqlite3.connect(path)) as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.execute("UPDATE live_cycle SET state=?", (json.dumps({**state(), "phase":"RUNNING"}),))
                result = read_whitebit_live_learning_status(root, now=NOW)
                self.assertEqual(result["status"], "READY")
                self.assertEqual(result["phase"], "EVALUATING")
                writer.rollback()
            self.assertEqual(path.read_bytes(), before)
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertFalse(result["network_orders"])
            self.assertIsNone(result["model_version_count"])

    def test_stale_and_future(self):
        for seconds, expected in ((181, "STALE"), (-60,"CORRUPT")):
            with self.subTest(seconds=seconds), TemporaryDirectory() as tmp:
                root=Path(tmp)
                self.seed(root, timestamp=NOW-timedelta(seconds=seconds))
                self.assertEqual(read_whitebit_live_learning_status(root, now=NOW)["status"], expected)

    def test_bad_states_fail_closed(self):
        mutations = [dict(network_orders=True), dict(trading_authority=True),
            dict(episodes=True), dict(phase="SECRET"), dict(secret="x"*70000),
            dict(analysis={"net_usdt":"NaN"}), dict(evaluation={"candidate":"Infinity"}),
            dict(candidate_version="SECRET"), dict(cycle=float("nan"))]
        for change in mutations:
            with self.subTest(change=list(change)), TemporaryDirectory() as tmp:
                root=Path(tmp)
                self.seed(root, {**state(), **change})
                result=read_whitebit_live_learning_status(root, now=NOW)
                self.assertEqual(result["status"], "CORRUPT")
                self.assertIsNone(result["phase"])
                self.assertNotIn("SECRET", json.dumps(result))

    def test_malformed_database_and_busy_fail_safely(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            path=self.seed(root)
            with closing(sqlite3.connect(path)) as con:
                con.execute("BEGIN EXCLUSIVE")
                self.assertEqual(read_whitebit_live_learning_status(root, now=NOW)["status"], "CORRUPT")
                con.rollback()
                con.execute("UPDATE live_cycle SET state='{' ")
                con.commit()
            self.assertEqual(read_whitebit_live_learning_status(root, now=NOW)["status"], "CORRUPT")

    def test_hardlink_and_symlink_ancestor_rejected(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp)
            actual=root/"actual"
            actual.mkdir()
            path=self.seed(actual)
            os.link(path, actual/"alias.sqlite3")
            self.assertEqual(read_whitebit_live_learning_status(actual, now=NOW)["status"], "CORRUPT")
            (actual/"alias.sqlite3").unlink()
            try:
                (root/"linked").symlink_to(actual, target_is_directory=True)
            except OSError:
                return  # Windows may disallow unprivileged symlink creation.
            self.assertEqual(read_whitebit_live_learning_status(root/"linked", now=NOW)["status"], "CORRUPT")


if __name__ == "__main__":
    unittest.main()
