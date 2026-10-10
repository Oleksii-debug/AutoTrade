"""Offline fixture tests: no provider accounts, no network requests, no claimed profits."""
import csv
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED
import stat
from unittest.mock import patch
import os
import json

from tools.historical_public_klines import (
    HistoricalArchiveError, _month_range, verify_archive, collect_month, collect_day, _fetch,
)


def _publisher_checksum(raw: bytes, filename: str) -> bytes:
    # Explicit separator and newline; never accidentally put escaped literal
    # backslash-n into publisher CHECKSUM fixtures.
    return sha256(raw).hexdigest().encode("ascii") + b"  " + filename.encode("ascii") + bytes((10,))


def _bundle(month="2024-01", interval="1h", gap=False, corrupt=False, unit="ms", day=None, count=3):
    # Synthetic data ONLY for parser/negative testing, never for trade performance.
    first = datetime.fromisoformat((day or (month + "-01")) + "T00:00:00+00:00")
    rows = []
    for i in range(count):
        instant = first + timedelta(hours=i + (1 if gap and i > 0 else 0))
        stamp = int(instant.timestamp()) * (1000 if unit == "ms" else 1_000_000)
        rows.append([str(stamp), "100", "102", "99", "101", "1",
                     str(stamp + (3599999 if unit == "ms" else 3599999999)),
                     "101", "3", "0.1", "10.1", "0"])
    data = StringIO()
    csv.writer(data, lineterminator="\n").writerows(rows)
    filename = f"BTCUSDT-{interval}-{day or month}.zip"
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

    def test_rejects_current_and_future_month_without_network(self):
        # Only completed UTC monthly periods are eligible for historical use.
        now = datetime.now(timezone.utc)
        current = f"{now.year:04d}-{now.month:02d}"
        with TemporaryDirectory() as tmp:
            for month in (current, "2099-01"):
                with self.subTest(month=month):
                    with self.assertRaisesRegex(HistoricalArchiveError, "completed UTC month"):
                        collect_month(
                            symbol="BTCUSDT", interval="1h", month=month,
                            output=Path(tmp),
                            fetch=lambda *_: self.fail("must reject before network access"),
                        )
                    with self.assertRaisesRegex(HistoricalArchiveError, "completed UTC month"):
                        verify_archive(b"invalid", b"invalid", symbol="BTCUSDT",
                                       interval="1h", month=month)

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

    def test_rejects_cross_year_timestamp_units_and_false_close_time(self):
        raw, check = _bundle(month="2025-01", unit="ms")
        with self.assertRaisesRegex(HistoricalArchiveError, "unit"):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month="2025-01")
        raw, check = _bundle(month="2024-01", unit="us")
        with self.assertRaisesRegex(HistoricalArchiveError, "unit"):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1h", month="2024-01")

        raw, _ = _bundle()
        with ZipFile(BytesIO(raw)) as z:
            name = z.namelist()[0]
            csv_bytes = z.read(name).decode()
        lines = csv_bytes.splitlines()
        parts = lines[0].split(",")
        parts[6] = str(int(parts[6]) + 1)
        lines[0] = ",".join(parts)
        out = BytesIO()
        with ZipFile(out, "w") as z:
            z.writestr(name, "\n".join(lines) + "\n")
        invalid = out.getvalue()
        with self.assertRaisesRegex(HistoricalArchiveError, "close time"):
            verify_archive(invalid, _publisher_checksum(invalid, "BTCUSDT-1h-2024-01.zip"),
                           symbol="BTCUSDT", interval="1h", month="2024-01")

    def test_public_source_requests_have_a_bounded_pace(self):
        # Synthetic clock only; no live network, account, or provider requests.
        from tools import historical_public_klines as ingest
        with patch.object(ingest, "_NEXT_REQUEST_NOT_BEFORE", 0.0), \
             patch.object(ingest.time, "monotonic", side_effect=(100.0, 100.0, 100.5)), \
             patch.object(ingest.time, "sleep") as sleep:
            ingest._pace_request()
            ingest._pace_request()
            ingest._pace_request()
        sleep.assert_called_once_with(ingest.MIN_REQUEST_GAP_SECONDS)

    def test_public_source_url_rejection_occurs_before_rate_reservation(self):
        from tools import historical_public_klines as ingest
        with patch.object(ingest, "_pace_request") as pace:
            with self.assertRaisesRegex(HistoricalArchiveError, "unexpected archive URL"):
                ingest._fetch("https://example.invalid/evil.zip", 4096)
            pace.assert_not_called()

    def test_compressed_zip_bomb_and_redirect_prevented(self):
        from tools.historical_public_klines import _RejectRedirects
        with self.assertRaisesRegex(HistoricalArchiveError, "redirect"):
            _RejectRedirects().redirect_request(None, None, 302, "redirect", {}, "https://evil.test")
        raw_file = b"0" * 100_000
        out = BytesIO()
        with ZipFile(out, "w", compression=ZIP_DEFLATED) as z:
            z.writestr("BTCUSDT-1h-2024-01.csv", raw_file)
        raw = out.getvalue()
        with self.assertRaisesRegex(HistoricalArchiveError, "compression ratio unsafe"):
            verify_archive(raw, _publisher_checksum(raw, "BTCUSDT-1h-2024-01.zip"),
                           symbol="BTCUSDT", interval="1h", month="2024-01")

    def test_rejects_zip_symlink_member_even_with_valid_checksum_and_csv(self):
        # ZIP members are never extracted, but nonregular publisher metadata
        # must also fail closed before claiming a verified ordinary CSV.
        ordinary, _ = _bundle()
        with ZipFile(BytesIO(ordinary)) as bundle:
            csv_payload = bundle.read("BTCUSDT-1h-2024-01.csv")
        pseudo = ZipInfo("BTCUSDT-1h-2024-01.csv")
        pseudo.create_system = 3
        pseudo.external_attr = (stat.S_IFLNK | 0o777) << 16
        stream = BytesIO()
        with ZipFile(stream, "w") as archive:
            archive.writestr(pseudo, csv_payload)
        malicious = stream.getvalue()
        with self.assertRaisesRegex(HistoricalArchiveError, "regular file"):
            verify_archive(
                malicious, _publisher_checksum(malicious, "BTCUSDT-1h-2024-01.zip"),
                symbol="BTCUSDT", interval="1h", month="2024-01",
            )

    def test_provenance_rights_and_interrupted_publication_recover_without_partial_archive(self):
        archive, checksum = _bundle()
        def source(url, limit):
            return checksum if url.endswith(".CHECKSUM") else archive
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            with patch("tools.historical_public_klines.os.link", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                  output=base, fetch=source)
            dataset = base / "spot/BTCUSDT/1h"
            self.assertFalse((dataset / "2024-01.ohlcv.csv").exists())
            self.assertFalse((dataset / "2024-01.manifest.json").exists())
            self.assertFalse(list(dataset.glob("*.partial")))
            first = collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                  output=base, fetch=source)
            self.assertIn("NON_COMMERCIAL", first["rights_scope"])
            self.assertIn("NON-COMMERCIAL", first["license"])
            self.assertIsNone(first["published_at_utc"])
            self.assertEqual(first["verification"]["publisher_companion_sha256"], "PASS")
            self.assertEqual(len(first["source_checksum_sha256"]), 64)
            second = collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                   output=base, fetch=source)
            self.assertEqual(first, second)


    def test_completed_daily_archive_and_immutable_replay(self):
        day = "2024-01-02"
        raw, check = _bundle(day=day, count=24)
        receipt = verify_archive(raw, check, symbol="BTCUSDT",
                                 interval="1h", month="2024-01", day=day)
        self.assertEqual(receipt["period_granularity"], "DAILY")
        self.assertEqual(receipt["row_count"], 24)
        self.assertEqual(receipt["missing_candle_intervals"], 0)
        self.assertTrue(receipt["usable_as_complete_causal_interval"])
        self.assertIn("/daily/klines/", receipt["source_url"])
        def source(url, _limit):
            return check if url.endswith(".CHECKSUM") else raw
        with TemporaryDirectory() as tmp:
            first = collect_day(symbol="BTCUSDT", interval="1h", day=day,
                                output=Path(tmp), fetch=source)
            second = collect_day(symbol="BTCUSDT", interval="1h", day=day,
                                 output=Path(tmp), fetch=source)
            self.assertEqual(first, second)
            self.assertTrue((Path(tmp) / "spot/BTCUSDT/1h/daily/2024-01-02.ohlcv.csv").is_file())
            self.assertEqual(first["verification"]["publisher_companion_sha256"], "PASS")

    def test_daily_fail_closed_on_future_invalid_other_day_and_partial_coverage(self):
        day = "2024-01-02"
        raw, check = _bundle(day=day, count=23)
        report = verify_archive(raw, check, symbol="BTCUSDT",
                                interval="1h", month="2024-01", day=day)
        self.assertEqual(report["missing_candle_intervals"], 1)
        self.assertFalse(report["usable_as_complete_causal_interval"])
        with self.assertRaises(HistoricalArchiveError):
            verify_archive(raw, check, symbol="BTCUSDT", interval="1h",
                           month="2024-01", day="2024-01-03")
        with self.assertRaises(HistoricalArchiveError):
            collect_day(symbol="BTCUSDT", interval="1h", day="2099-01-01",
                        output=Path("."), fetch=lambda u, l: raw)
        with self.assertRaises(HistoricalArchiveError):
            collect_day(symbol="BTCUSDT", interval="1h", day="2024-02-30",
                        output=Path("."), fetch=lambda u, l: raw)
        with self.assertRaisesRegex(HistoricalArchiveError, "unexpected archive URL"):
            _fetch("https://evil.example/data/spot/daily/klines/BTCUSDT/1h/x.zip", 4096)
        with self.assertRaisesRegex(HistoricalArchiveError, "SHA-256"):
            verify_archive(raw, b"0" * 64 + b"  BTCUSDT-1h-2024-01-02.zip\n",
                           symbol="BTCUSDT", interval="1h", month="2024-01", day=day)

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


    def test_network_and_partial_archive_failure_never_publish(self):
        """404, truncated source ZIP, and checksum loss never publish evidence."""
        archive, checksum = _bundle()
        name = "BTCUSDT-1h-2024-01.zip"
        truncated = archive[:24]
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            def missing_source(url, limit):
                raise HistoricalArchiveError("public archive unavailable: HTTPError")
            with self.assertRaisesRegex(HistoricalArchiveError, "unavailable"):
                collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                              output=base, fetch=missing_source)
            def missing_checksum(url, limit):
                if url.endswith(".CHECKSUM"):
                    raise HistoricalArchiveError("public archive unavailable: HTTPError")
                return archive
            with self.assertRaisesRegex(HistoricalArchiveError, "unavailable"):
                collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                              output=base, fetch=missing_checksum)
            def truncated_source(url, limit):
                if url.endswith(".CHECKSUM"):
                    return _publisher_checksum(truncated, name)
                return truncated
            with self.assertRaisesRegex(HistoricalArchiveError, "invalid ZIP"):
                collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                              output=base, fetch=truncated_source)
            series = base / "spot" / "BTCUSDT" / "1h"
            self.assertFalse(list(series.glob("*.manifest.json")))
            self.assertFalse(list(series.glob("*.ohlcv.csv")))
            self.assertFalse(list(series.glob("*.partial")))

    def test_verified_source_revision_never_overwrites_immutable_history(self):
        """Even a new valid publisher checksum cannot silently replace a local snapshot."""
        first_raw, first_checksum = _bundle()
        revised_raw, revised_checksum = _bundle(gap=True)
        self.assertNotEqual(sha256(first_raw).digest(), sha256(revised_raw).digest())
        def first_source(url, limit):
            return first_checksum if url.endswith(".CHECKSUM") else first_raw
        def revised_source(url, limit):
            return revised_checksum if url.endswith(".CHECKSUM") else revised_raw
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            original = collect_month(symbol="BTCUSDT", interval="1h",
                                     month="2024-01", output=base, fetch=first_source)
            series = base / "spot" / "BTCUSDT" / "1h"
            csv_before = (series / "2024-01.ohlcv.csv").read_bytes()
            manifest_before = (series / "2024-01.manifest.json").read_bytes()
            with self.assertRaisesRegex(HistoricalArchiveError, "revision conflict"):
                collect_month(symbol="BTCUSDT", interval="1h",
                              month="2024-01", output=base, fetch=revised_source)
            self.assertEqual((series / "2024-01.ohlcv.csv").read_bytes(), csv_before)
            self.assertEqual((series / "2024-01.manifest.json").read_bytes(), manifest_before)
            self.assertEqual(json.loads(manifest_before)["source_zip_sha256"],
                             original["source_zip_sha256"])


if __name__ == "__main__":
    unittest.main()
