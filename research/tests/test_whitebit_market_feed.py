"""Offline integration tests for public WhiteBIT evidence and continuous delivery."""
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from autotrade_research.artifacts.store import ArtifactIntegrityError, ArtifactStore
from autotrade_research.integrations.whitebit_market_feed import WhiteBitMarketFeed
from autotrade_research.integrations.whitebit_public import (
    PublicDataError, WhiteBitPublicClient, parse_orderbook,
)
from mvp.autotrade_mvp.market_data import RawMarketUpdate


def book():
    now = datetime.now(timezone.utc)
    raw = json.dumps({
        "ticker_id": "BTC_USDT", "timestamp": int(now.timestamp()),
        "bids": [["100", "2"]], "asks": [["101", "3"]],
    }, separators=(",", ":")).encode()
    return parse_orderbook(raw, "BTC_USDT", now)


class WhiteBitMarketFeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = ArtifactStore(Path(self.directory.name) / "artifacts")
        self.client = WhiteBitPublicClient()
        self.feed = WhiteBitMarketFeed(client=self.client, artifacts=self.store,
                                       market="BTC_USDT")

    def test_exact_market_contract_and_durable_raw_evidence(self):
        snapshot = book()
        with patch.object(self.client, "orderbook", return_value=snapshot):
            update = self.feed.poll_once()
        self.assertIs(type(update), RawMarketUpdate)
        self.assertEqual(update.kind, "BOOK_SNAPSHOT")
        self.assertEqual((update.provider_id, update.venue_id, update.provider_symbol),
                         ("WHITEBIT", "WHITEBIT_SPOT", "BTC_USDT"))
        self.assertEqual(update.payload["bids"][0], ("100", "2"))
        artifact_id = update.raw_evidence_ref["artifact_id"]
        reopened = ArtifactStore(self.store.root)
        manifest = reopened.load_manifest(artifact_id)
        self.assertEqual(reopened.read_bytes(artifact_id), snapshot.raw)
        self.assertEqual(manifest["sha256"], snapshot.digest)
        self.assertFalse(manifest["rights"]["export"])
        self.assertEqual(manifest["source_refs"], [snapshot.source_uri])
        self.assertEqual(update.raw_evidence_ref["sha256"], manifest["sha256"])

    def test_bad_public_book_never_publishes(self):
        with patch.object(self.client, "orderbook", side_effect=PublicDataError("STALE_MARKET_DATA")):
            with self.assertRaisesRegex(PublicDataError, "STALE"):
                self.feed.poll_once()
        self.assertEqual(list(self.store.manifests.iterdir()), [])

    def test_wrong_market_book_fails_before_evidence_write(self):
        from dataclasses import replace
        with patch.object(self.client, "orderbook", return_value=replace(book(), market="ETH_USDT")):
            with self.assertRaisesRegex(PublicDataError, "MARKET_MISMATCH"):
                self.feed.poll_once()
        self.assertEqual(list(self.store.manifests.iterdir()), [])

    def test_authenticated_read_failure_never_delivers(self):
        with patch.object(self.client, "orderbook", return_value=book()), patch.object(
                ArtifactStore, "read_bytes", side_effect=ArtifactIntegrityError("corrupt")):
            with self.assertRaisesRegex(ArtifactIntegrityError, "corrupt"):
                self.feed.poll_once()

    def test_network_recovery_and_stop_deliver_sequentially(self):
        stop = threading.Event()
        updates, failures = [], []
        snapshot = book()
        def consume(update):
            updates.append(update)
            stop.set()
        with patch.object(self.client, "orderbook", side_effect=[
                PublicDataError("PUBLIC_API_NETWORK_UNAVAILABLE"), snapshot]), patch.object(
                stop, "wait", side_effect=lambda delay: False) as wait:
            self.feed.run(consume, stop, interval_seconds=2,
                          on_public_error=failures.append)
        self.assertEqual(len(updates), 1)
        self.assertEqual(failures, ["PUBLIC_API_NETWORK_UNAVAILABLE"])
        self.assertEqual(wait.call_count, 2)

    def test_consumer_failure_is_not_retried(self):
        with patch.object(self.client, "orderbook", return_value=book()) as read:
            with self.assertRaisesRegex(RuntimeError, "sink offline"):
                self.feed.run(lambda update: (_ for _ in ()).throw(RuntimeError("sink offline")),
                              threading.Event(), interval_seconds=2)
        read.assert_called_once_with("BTC_USDT")

    def test_no_private_or_trading_client_and_invalid_interval(self):
        with self.assertRaises(TypeError):
            WhiteBitMarketFeed(client=object(), artifacts=self.store, market="BTC_USDT")
        with self.assertRaises(PublicDataError):
            WhiteBitMarketFeed(client=self.client, artifacts=self.store,
                               market="BTC_USDT/withdraw")
        with self.assertRaises(ValueError):
            self.feed.run(lambda _: None, threading.Event(), interval_seconds=0)


if __name__ == "__main__":
    unittest.main()
