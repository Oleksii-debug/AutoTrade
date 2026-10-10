"""Offline fixture tests: no provider accounts, no network requests, no claimed profits."""
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from tools.historical_public_klines import (
    HistoricalArchiveError, _month_range, verify_archive, collect_month,
)


def _bundle(month="2024-01", interval="1h", gap=False, corrupt=False, unit="ms"):
    # Synthetic data ONLY for parser/negative testing, never for trade performance.
    first = datetime.fromisoformat(month + "-01T00:00:00+00:00")
    rows = []
    for i in range(3):
        instant = first + timedelta(hours=i + (1 if gap and i > 0 else 0))
        stamp = int(instant.timestamp()) * (1000 if unit == "ms" else 1_000_000)
        rows.append([str(stamp), "100", "102", "99", "101", "1",
                     str(stamp + (3599999 if unit == "ms" else 3599999999)),
                     "101", "3", "0.1", "10.1", "0"])
    data = StringIO()
    csv.writer(data, lineterminator="\n").writerows(rows)
    filename = f"BTCUSDT-{interval}-{month}.zip"
    bio = BytesIO()
    with ZipFile(bio, "w") as z:
        z.writestr(filename[:-4] + ".csv", data.getvalue())
    archive = bio.getvalue()
    digest = sha256(archive).hexdigest()
    if corrupt:
        digest = "a" * 64 if digest != "a" * 64 else "b" * 64
    checksum = f"{digest}  {filename}\n".encode("ascii")
    return archive, checksum


class PublicHistoryTests(unittest.TestCase):
    def test_month_range_has_bounded_chronology(self):
        self.assertEqual(list(_month_range("2024-12", "2025-02")),
                         ["2024-12", "2025-01", "2025-02"])
        with self.assertRaises(HistoricalArchiveError):
            list(_month_range("2025-01", "2024-12"))
        with self.assertRaises(HistoricalArchiveError):
            list(_month_range("2023-12", "2024-01"))

    def test_milliseconds_2024_and_microseconds_2025(self):
        for month, unit in (("2024-01", "ms"), ("2025-01", "us")):
            with self.subTest(month=month):
                raw, check = _bundle(month=month, unit=unit)
                result = verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month=month)
                self.assertEqual(result["row_count"], 3)
                self.assertEqual(result["ohlcv_rows"][0][0][:7], month)
                self.assertTrue(result["ohlcv_rows"][0][0].endswith("Z"))

    def test_detect_interior_gap_without_silent_interpolation(self):
        raw, check = _bundle(gap=True)
        result = verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month="2024-01")
        self.assertGreaterEqual(result["missing_candle_intervals"], 1)
        self.assertFalse(result["usable_as_complete_causal_interval"])

    def test_broken_checksum_rejected(self):
        raw, check = _bundle(corrupt=True)
        with self.assertRaisesRegex(HistoricalArchiveError, "SHA-256"):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month="2024-01")

    def test_wrong_archive_symbol_and_unsafe_options(self):
        raw, check = _bundle()
        with self.assertRaises(HistoricalArchiveError):
            verify_archive(raw, check, symbol="../BTC", interval="1h", month="2024-01")
        with self.assertRaises(HistoricalArchiveError):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1s", month="2024-01")
        with self.assertRaises(HistoricalArchiveError):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month="2024-02")

    def test_collect_reuses_identical_verified_dataset_and_rejects_changed_one(self):
        archive, checksum = _bundle()
        def frozen_fetch(url, _max_bytes):
            return checksum if url.endswith(".CHECKSUM") else archive
        with TemporaryDirectory() as tmp:
            path = Path(tmp)
            first = collect_month(symbol="BTCUSDT", interval="1h", month="2024-01", output=path, fetch=frozen_fetch)
            second = collect_month(symbol="BTCUSDT", interval="1h", month="2024-01", output=path, fetch=frozen_fetch)
            self.assertEqual(first, second)
            self.assertTrue((path / "spot/BTCUSDT/1h/2024-01.ohlcv.csv").is_file())
            manifest = path / "spot/BTCUSDT/1h/2024-01.manifest.json"
            manifest.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(HistoricalArchiveError, "revision conflict"):
                collect_month(symbol="BTCUSDT", interval="1h", month="2024-01", output=path, fetch=frozen_fetch)


if __name__ == "__main__":
    unittest.main()
