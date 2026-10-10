"""One-off guarded real-source qualification for the canonical Plan 10 PR.

Runs before the general repository Control suites in hosted Verify CI. No keys,
trading providers, live broker calls or trade signals. Data is ephemeral and
is never embedded in Git history or exported as a product artifact.

The real Internet campaign is activated only on the owner's canonical PR
branch, or by an explicit operator opt-in. All other CI runs skip it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.historical_public_klines import _month_range, collect_month


_CANONICAL_PR_BRANCH = "owner/historical-simulation-plan10-20261010"
_REAL_ENABLED = (
    (os.environ.get("GITHUB_HEAD_REF") == _CANONICAL_PR_BRANCH and os.name == "posix")
    or os.environ.get("AUTOTRADE_PLAN10_REAL_ARCHIVE_SAMPLE") == "1"
)


def _last_published_month():
    # Publisher publishes monthly archives on the first Monday of the following
    # month. Day >=8 provides a conservative buffer; never assume a current
    # incomplete month is already a published monthly archive.
    now = datetime.now(timezone.utc)
    year, month = now.year, now.month
    if now.day < 8:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    month -= 1
    if month == 0:
        year, month = year - 1, 12
    return f"{year:04d}-{month:02d}"


@unittest.skipUnless(_REAL_ENABLED, "real historical download is opt-in / canonical Plan10 CI only")
class RealHistoryArchiveSample(unittest.TestCase):
    def test_verified_historical_btc_eth_hourly_2024_onward(self):
        end = _last_published_month() if os.environ.get("GITHUB_HEAD_REF") == _CANONICAL_PR_BRANCH else "2024-01"
        months = list(_month_range("2024-01", end))
        self.assertGreaterEqual(len(months), 1)
        with TemporaryDirectory(prefix="autotrade-plan10-real-") as tmp:
            root = Path(tmp)
            for symbol in ("BTCUSDT", "ETHUSDT"):
                for month in months:
                    result = collect_month(symbol=symbol, interval="1h", month=month,
                                           output=root)
                    self.assertEqual(result["missing_candle_intervals"], 0)
                    self.assertTrue(result["usable_as_complete_causal_interval"])
                    self.assertEqual(result["rights_scope"],
                                     "PERSONAL_NON_COMMERCIAL_RESEARCH_SIMULATION_ONLY")
                    self.assertIsNone(result["published_at_utc"])
                    self.assertEqual(result["verification"]["publisher_companion_sha256"], "PASS")
                    self.assertEqual(len(result["source_zip_sha256"]), 64)
                    self.assertEqual(len(result["normalized_csv_sha256"]), 64)
                    print("PLAN10_REAL_PUBLIC_SOURCE " + json.dumps({
                        "symbol": symbol,
                        "month": month,
                        "rows": result["row_count"],
                        "gaps": result["missing_candle_intervals"],
                        "source_zip_sha256": result["source_zip_sha256"],
                        "normalized_csv_sha256": result["normalized_csv_sha256"],
                        "downloaded_at_utc": result["downloaded_at_utc"],
                        "license": result["license"],
                        "research_only": True,
                    }, sort_keys=True), flush=True)
            self.assertEqual(
                len(list((root / "spot/BTCUSDT/1h").glob("*.manifest.json"))), len(months)
            )
            self.assertEqual(
                len(list((root / "spot/ETHUSDT/1h").glob("*.manifest.json"))), len(months)
            )


if __name__ == "__main__":
    unittest.main()
