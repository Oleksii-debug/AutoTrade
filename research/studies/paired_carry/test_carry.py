from contextlib import closing
from datetime import datetime, UTC
from decimal import Decimal as D
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import forward
from data import HOUR, WEEK
from study import Account, forecasts


class AccountingTests(unittest.TestCase):
    def test_round_trip_at_flat_prices_debits_four_fills(self):
        rules = dict(step="0.001", min_qty="0.001", min_value="1")
        wallet = Account(D(1000), D(".001"))
        self.assertTrue(wallet.enter("BTC", D(100), D(100), {"BTC_USDT": rules, "BTC_PERP": rules}))
        q = wallet.q
        wallet.cash += q * D(".01") * 100
        wallet.close(D(100), D(100))
        expected = D(1000) + q - q * D(".72")
        self.assertEqual(wallet.cash, expected)

    def test_selection_ignores_settlements_at_cutoff_and_later(self):
        cutoff = 5 * WEEK
        times = list(range(0, cutoff + 24 * HOUR, 8 * HOUR))
        samples = {t: (D(".0001"), D(100)) for t in times}
        probe = lambda: forecasts({"BTC": samples}, {"BTC": times}, cutoff + HOUR)
        original = probe()
        self.assertIn("BTC", original)
        for t in times:
            if t >= cutoff:
                samples[t] = (D(1), D(100))
        self.assertEqual(original, probe())

    def test_restart_in_same_hour_does_not_book_again(self):
        with TemporaryDirectory() as directory:
            folder = Path(directory)
            slot = int(datetime.now(UTC).timestamp()) // HOUR
            mark = {"slot": slot, "closed": False, "funding_usdt": "2"}
            with closing(sqlite3.connect(folder/"ledger.sqlite3")) as db:
                db.execute("CREATE TABLE marks(slot INTEGER PRIMARY KEY, body TEXT NOT NULL)")
                db.execute("INSERT INTO marks VALUES(?,?)", (slot, json.dumps(mark)))
                db.commit()
            with patch.object(forward, "OUT", folder), patch.object(forward, "paired_books", side_effect=AssertionError):
                self.assertEqual(mark, forward.tick())
                self.assertEqual(mark, forward.tick())

    def test_depth_weighted_quote_requires_full_size(self):
        levels = [("100", ".4"), ("101", ".6")]
        self.assertEqual(forward.average(levels, D(1)), D("100.6"))
        with self.assertRaises(ValueError):
            forward.average(levels, D("1.01"))


if __name__ == "__main__":
    unittest.main()
