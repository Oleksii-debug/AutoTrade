"""Plan10 1Г source chronology and cancellation acceptance cases."""
import csv
from hashlib import sha256
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import collect_month, verify_archive, HistoricalArchiveError
from tests.History.test_public_klines import _bundle


class PublicHistoryFailureTests(unittest.TestCase):
    def test_invalid_ohlcv_duplicate_and_reordered_source_never_publish(self):
        original, _ = _bundle()
        with ZipFile(BytesIO(original)) as source:
            rows = list(csv.reader(StringIO(source.read(source.namelist()[0]).decode())))
        cases = [rows + [rows[-1]], list(reversed(rows))]
        for field, value in ((1, "NaN"), (2, "90"), (3, "103"), (4, "0"), (5, "-1")):
            changed = [row[:] for row in rows]
            changed[0][field] = value
            cases.append(changed)
        for modified in cases:
            with self.subTest(row=modified[0]):
                text = StringIO()
                csv.writer(text).writerows(modified)
                buf = BytesIO()
                with ZipFile(buf, "w") as archive:
                    archive.writestr("BTCUSDT-1h-2024-01.csv", text.getvalue())
                raw = buf.getvalue()
                checksum = f"{sha256(raw).hexdigest()}  BTCUSDT-1h-2024-01.zip\n".encode()
                with self.assertRaises(HistoricalArchiveError):
                    verify_archive(raw, checksum, symbol="BTCUSDT", interval="1h", month="2024-01")

    def test_cancelled_publication_restarts_without_partial_or_duplicate_data(self):
        raw, checksum = _bundle()
        def fetch(url, _):
            return checksum if url.endswith(".CHECKSUM") else raw
        for interrupted_link in (1, 2):
            with self.subTest(interrupted_link=interrupted_link), TemporaryDirectory() as tmp:
                root = Path(tmp)
                # No ArtifactStore here: isolate the final two-file publication.
                import os
                real_link = os.link
                count = 0
                def interrupt(source, destination):
                    nonlocal count
                    count += 1
                    if count == interrupted_link:
                        raise KeyboardInterrupt()
                    return real_link(source, destination)
                with patch("tools.historical_public_klines.os.link", side_effect=interrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                      output=root, fetch=fetch)
                self.assertEqual(list(root.rglob("*.partial")), [])
                result = collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                       output=root, fetch=fetch)
                self.assertEqual(result["row_count"], 3)
                self.assertEqual(len(list(root.rglob("*.manifest.json"))), 1)
                self.assertEqual(collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                                              output=root, fetch=fetch), result)
