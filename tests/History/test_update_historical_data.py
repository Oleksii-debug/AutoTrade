"""Finite update/retry/cache failure qualification with synthetic archives."""
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.error import HTTPError

from autotrade_runtime.artifacts import ArtifactStore
from tools.historical_public_klines import HistoricalArchiveError
from tools.update_historical_data import update_history
from tests.History.test_run_historical_zero import full_month_fixture


class HistoricalUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw, self.checksum, _ = full_month_fixture()
        self.calls = []

    def source(self, url, _):
        self.calls.append(url)
        return self.checksum if url.endswith(".CHECKSUM") else self.raw

    def update(self, **kwargs):
        kwargs.setdefault("fetch", self.source)
        return update_history(self.root, from_month="2024-01", through_month="2024-01",
                              symbols=("BTCUSDT",), **kwargs)

    def test_restart_reuses_authenticated_cache_and_preserves_inventories(self):
        first = self.update()
        self.assertTrue(first["complete"])
        self.assertEqual(len(self.calls), 2)
        second = self.update(fetch=lambda *_: self.fail("cache must not use network"))
        self.assertTrue(second["complete"])
        self.assertEqual(second["entries"][0]["status"], "VERIFIED_CACHE")
        store = ArtifactStore(self.root / ".artifact-store")
        first_bytes = store.read_bytes(first["inventory_artifact_id"])
        self.assertEqual("sha256:" + sha256(first_bytes).hexdigest(), first["inventory_sha256"])
        self.assertNotEqual(first["inventory_artifact_id"], second["inventory_artifact_id"])
        self.assertIn(b'"export": false', (self.root / ".artifact-store" / "manifests" /
                      (first["inventory_artifact_id"] + ".json")).read_bytes())

    def test_corrupt_cache_is_not_silently_replaced(self):
        self.update()
        csv = self.root / "spot/BTCUSDT/1h/2024-01.ohlcv.csv"
        csv.write_bytes(b"corrupt")
        report = self.update(fetch=lambda *_: self.fail("must not replace corrupt history"))
        self.assertFalse(report["complete"])
        self.assertEqual(report["entries"][0]["status"], "REJECTED")
        self.assertEqual(csv.read_bytes(), b"corrupt")

    def test_transient_rate_limit_is_bounded_and_404_is_not_retried(self):
        attempts, delays = [], []
        def rate_limited(url, limit):
            attempts.append(url)
            if len(attempts) < 3:
                raise HistoricalArchiveError("HTTP 429") from HTTPError(url, 429, "slow", {}, None)
            return self.source(url, limit)
        self.assertTrue(self.update(fetch=rate_limited, sleep=delays.append)["complete"])
        self.assertEqual(delays, [1, 2])
        attempts.clear()
        def missing(url, _):
            attempts.append(url)
            raise HistoricalArchiveError("HTTP 404") from HTTPError(url, 404, "missing", {}, None)
        result = self.update(recheck_source=True, fetch=missing, sleep=lambda _: self.fail("404 retry"))
        self.assertFalse(result["complete"])
        self.assertEqual(result["entries"][0]["status"], "MISSING")
        self.assertEqual(len(attempts), 1)

    def test_disk_budget_rejects_download_without_eviction(self):
        result = self.update(max_disk_bytes=1024 * 1024, fetch=lambda *_: self.fail("disk budget before network"))
        self.assertFalse(result["complete"])
        self.assertIn("disk budget", result["entries"][0]["reason"])
        self.assertFalse((self.root / "spot").exists())

    def test_interruption_keeps_accepted_cache_and_retry_is_safe(self):
        self.update()
        before = (self.root / "spot/BTCUSDT/1h/2024-01.ohlcv.csv").read_bytes()
        def cancelled(*_):
            raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.update(recheck_source=True, fetch=cancelled)
        self.assertEqual((self.root / "spot/BTCUSDT/1h/2024-01.ohlcv.csv").read_bytes(), before)
        self.assertTrue(self.update()["complete"])

    def test_exhausted_retry_is_reported_without_publishing_fake_data(self):
        attempts, delays = [], []
        def unavailable(url, _):
            attempts.append(url)
            raise HistoricalArchiveError("HTTP 503") from HTTPError(url, 503, "down", {}, None)
        result = self.update(fetch=unavailable, sleep=delays.append)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(delays, [1, 2])
        self.assertFalse(result["complete"])
        self.assertEqual(result["entries"][0]["status"], "TRANSIENT")
        self.assertFalse((self.root / "spot").exists())

    def test_recheck_source_revision_retains_accepted_original(self):
        self.update()
        original = (self.root / "spot/BTCUSDT/1h/2024-01.manifest.json").read_bytes()
        from io import BytesIO
        from zipfile import ZipFile
        changed = BytesIO()
        with ZipFile(BytesIO(self.raw)) as old, ZipFile(changed, "w") as new:
            name = old.namelist()[0]
            new.writestr(name, old.read(name).replace(b",101,", b",100,", 1))
        raw = changed.getvalue()
        checksum = f"{sha256(raw).hexdigest()}  BTCUSDT-1h-2024-01.zip\n".encode()
        result = self.update(recheck_source=True,
                             fetch=lambda url, _: checksum if url.endswith(".CHECKSUM") else raw)
        self.assertFalse(result["complete"])
        self.assertEqual(result["entries"][0]["status"], "REJECTED")
        self.assertEqual((self.root / "spot/BTCUSDT/1h/2024-01.manifest.json").read_bytes(), original)
        self.assertTrue(self.update()["complete"])


if __name__ == "__main__":
    unittest.main()
