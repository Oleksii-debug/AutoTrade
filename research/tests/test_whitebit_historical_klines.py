import json
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autotrade_research.integrations.whitebit_public import (
    PublicDataError,
    _kline_query,
    _public_url,
    parse_klines,
)
from tools.historical_public_klines import HistoricalArchiveError
from tools.whitebit_historical_klines import collect_whitebit_history


UTC = timezone.utc
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
START = int(datetime(2026, 10, 20, 6, 0, tzinfo=UTC).timestamp())


def batch(start, count, *, interval="1h", received=NOW):
    rows = []
    for offset in range(count):
        opened = start + offset * 3600
        rows.append([opened, "100", "101", "102", "99", "2", "201"])
    raw = json.dumps({"success": True, "message": None, "result": rows},
                     separators=(",", ":")).encode()
    end = start + count * 3600
    uri = _public_url("kline", _kline_query(
        "BTC_USDT", interval, count, end=end))
    return parse_klines(
        raw, "BTC_USDT", interval, received, source_uri=uri,
        requested_limit=count, end=end, training=True,
    )


class Client:
    def __init__(self, failures=None):
        self.calls = []
        self.failures = list(failures or [])

    def klines(self, market, *, interval, start=None, end, limit, training):
        self.calls.append((market, interval, start, end, limit, training))
        if self.failures:
            raise PublicDataError(self.failures.pop(0))
        self.assert_no_start(start)
        return batch(end - limit * 3600, limit, interval=interval)

    @staticmethod
    def assert_no_start(start):
        if start is not None:
            raise AssertionError("WhiteBIT limit pagination must derive start from end")


class WhiteBitHistoricalTests(unittest.TestCase):
    def test_writes_canonical_csv_and_hashed_public_provenance(self):
        with TemporaryDirectory() as tmp:
            client = Client()
            result = collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 4 * 3600, output=Path(tmp), client=client,
                page_limit=2, now=NOW,
            )
            self.assertEqual(result["row_count"], 5)
            self.assertEqual(len(result["pages"]), 3)
            self.assertTrue(result["completed_candles_only"])
            self.assertFalse(result["private_credentials_used"])
            self.assertFalse(result["real_orders_enabled"])
            series = Path(tmp) / "spot" / "BTC_USDT" / "1h"
            csv_path = series / f"{START}-{START + 4 * 3600}.ohlcv.csv"
            manifest_path = series / f"{START}-{START + 4 * 3600}.manifest.json"
            self.assertEqual(
                csv_path.read_text(encoding="utf-8").splitlines()[0],
                "open_time_utc,open,high,low,close,volume",
            )
            self.assertEqual(json.loads(manifest_path.read_text())["dataset_identity_sha256"],
                             result["dataset_identity_sha256"])
            self.assertEqual([call[4] for call in client.calls], [2, 2, 1])

    def test_incomplete_candle_is_rejected_before_network(self):
        with TemporaryDirectory() as tmp:
            client = Client()
            with self.assertRaisesRegex(HistoricalArchiveError, "completed candles only"):
                collect_whitebit_history(
                    market="BTC_USDT", interval="1h", start=START,
                    end=int(NOW.timestamp()), output=Path(tmp), client=client, now=NOW,
                )
            self.assertEqual(client.calls, [])

    def test_market_path_injection_is_rejected_before_files_or_network(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = Client()
            with self.assertRaisesRegex(HistoricalArchiveError, "market is invalid"):
                collect_whitebit_history(
                    market="../BTC_USDT", interval="1h", start=START,
                    end=START + 3600, output=root, client=client, now=NOW,
                )
            self.assertEqual(client.calls, [])
            self.assertEqual(list(root.iterdir()), [])

    def test_interrupted_run_resumes_without_refetching_accepted_page(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = Client(["PUBLIC_API_NETWORK_UNAVAILABLE"])
            with self.assertRaises(HistoricalArchiveError):
                collect_whitebit_history(
                    market="BTC_USDT", interval="1h", start=START,
                    end=START + 2 * 3600, output=root, client=first,
                    page_limit=2, max_attempts=1, now=NOW,
                )
            self.assertEqual([call[3] for call in first.calls], [START + 2 * 3600])
            # The failure happened on the first call, so no page was accepted.
            second = Client()
            collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 2 * 3600, output=root, client=second,
                page_limit=2, now=NOW,
            )
            self.assertEqual([call[3] for call in second.calls],
                             [START + 2 * 3600, START + 3 * 3600])

            # A third idempotent run authenticates both page envelopes and does no network I/O.
            third = Client()
            collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 2 * 3600, output=root, client=third,
                page_limit=2, now=NOW,
            )
            self.assertEqual(third.calls, [])

    def test_resume_after_second_page_failure_reuses_first_page(self):
        class FailSecond(Client):
            def klines(self, market, *, interval, start=None, end, limit, training):
                if len(self.calls) == 1:
                    self.calls.append((market, interval, start, end, limit, training))
                    raise PublicDataError("PUBLIC_API_NETWORK_UNAVAILABLE")
                return super().klines(market, interval=interval, start=start, end=end,
                                      limit=limit, training=training)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = FailSecond()
            with self.assertRaises(HistoricalArchiveError):
                collect_whitebit_history(
                    market="BTC_USDT", interval="1h", start=START,
                    end=START + 2 * 3600, output=root, client=first,
                    page_limit=2, max_attempts=1, now=NOW,
                )
            second = Client()
            collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 2 * 3600, output=root, client=second,
                page_limit=2, now=NOW,
            )
            self.assertEqual([call[3] for call in second.calls], [START + 3 * 3600])

    def test_tampered_page_fails_closed_without_network(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 3600, output=root, client=Client(),
                page_limit=2, now=NOW,
            )
            page = next(root.rglob("000000.json"))
            value = json.loads(page.read_text())
            value["raw_sha256"] = "0" * 64
            page.write_text(json.dumps(value), encoding="utf-8")
            client = Client()
            with self.assertRaisesRegex(HistoricalArchiveError, "authentication"):
                collect_whitebit_history(
                    market="BTC_USDT", interval="1h", start=START,
                    end=START + 3600, output=root, client=client,
                    page_limit=2, now=NOW,
                )
            self.assertEqual(client.calls, [])

    def test_bounded_transient_retry(self):
        with TemporaryDirectory() as tmp:
            client = Client([
                "PUBLIC_API_NETWORK_UNAVAILABLE",
                "PUBLIC_API_RATE_LIMIT",
            ])
            sleeps = []
            collect_whitebit_history(
                market="BTC_USDT", interval="1h", start=START,
                end=START + 3600, output=Path(tmp), client=client,
                page_limit=2, max_attempts=3, now=NOW, sleeper=sleeps.append,
            )
            self.assertEqual(len(client.calls), 3)
            self.assertEqual(sleeps, [0.25, 0.5])

    def test_partial_page_never_publishes_dataset(self):
        class Partial(Client):
            def klines(self, market, *, interval, start=None, end, limit, training):
                self.calls.append((market, interval, start, end, limit, training))
                return batch(end - limit * 3600, limit - 1, interval=interval)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaisesRegex(HistoricalArchiveError, "cadence gap"):
                collect_whitebit_history(
                    market="BTC_USDT", interval="1h", start=START,
                    end=START + 3600, output=root, client=Partial(),
                    page_limit=2, now=NOW,
                )
            self.assertEqual(list(root.rglob("*.ohlcv.csv")), [])


if __name__ == "__main__":
    unittest.main()
