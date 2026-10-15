"""Synthetic source/causality checks; no ROI or physical Windows claims."""
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from autotrade_runtime.artifacts import ArtifactStore
from autotrade_research.evaluation.replay.feeder import (
    CausalFeeder, CausalReplayError, CausalDataView, CausalObservation,
    FeederCheckpoint, CausalDataset, CausalEvent,
)
from tools.historical_public_klines import collect_month, collect_day, HistoricalArchiveError
from tools.historical_causal_replay import (
    load_causal_archive, closed_candle_simulation_input, BAR_CLOSE_SCENARIO,
)
from tests.History.test_run_historical_zero import full_month_fixture
from tests.History.test_public_klines import _bundle


class HistoricalCausalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = ArtifactStore(self.root / ".artifact-store")
        raw, checksum, _ = full_month_fixture()
        collect_month(symbol="BTCUSDT", interval="1h", month="2024-01",
                      output=self.root, artifact_store=self.store,
                      fetch=lambda url, _: checksum if url.endswith(".CHECKSUM") else raw)

    def load(self, **kwargs):
        return load_causal_archive(self.root, symbol="BTCUSDT", interval="1h",
                                   from_month="2024-01", through_month="2024-01", **kwargs)

    def test_default_does_not_backdate_unknown_archive_publication(self):
        dataset = self.load()
        first = dataset.events[0]
        self.assertGreater(first.available_at, first.event_time)
        feeder = CausalFeeder(dataset, start_time=first.event_time)
        self.assertEqual(feeder.view().events, ())
        feeder.advance_to(first.available_at - timedelta(microseconds=1))
        self.assertEqual(feeder.published_count, 0)
        feeder.advance_to(first.available_at)
        self.assertEqual(feeder.published_count, 744)

    def test_zero_runner_binds_exact_causal_cut_and_closed_observations(self):
        dates, prices, identity = closed_candle_simulation_input(
            self.root, symbol="BTCUSDT", interval="1h",
            from_month="2024-01", through_month="2024-01",
            artifact_store=self.store,
        )
        dataset = self.load(availability_policy=BAR_CLOSE_SCENARIO)
        self.assertEqual(identity, dataset.dataset_sha256.removeprefix("sha256:"))
        self.assertEqual((len(dates), len(prices)), (744, 744))
        self.assertEqual((dates[0].isoformat(), prices[0]),
                         ("2024-01-01T00:00:00+00:00", "101"))
        self.assertEqual(dates[-1].isoformat(), "2024-01-31T23:00:00+00:00")

    def test_zero_runner_rejects_delayed_or_relabelled_causal_candle(self):
        source = self.load(availability_policy=BAR_CLOSE_SCENARIO)
        first = source.events[0]
        changed = []
        for delayed in (True, False):
            payload = dict(first.payload)
            if not delayed:
                payload["symbol"] = "ETHUSDT"
            changed.append(CausalDataset.create(
                manifest_sha256=source.manifest_sha256,
                events=(CausalEvent.create(
                    event_id=first.event_id, kind=first.kind,
                    event_time=first.event_time,
                    available_at=(first.event_time + timedelta(hours=1)
                                  if delayed else first.event_time),
                    ingested_at=first.ingested_at,
                    source_priority=first.source_priority,
                    source_sequence=first.source_sequence, payload=payload,
                ),),
            ))
        for dataset in changed:
            with self.subTest(dataset=dataset.dataset_sha256):
                with patch("tools.historical_causal_replay.load_causal_archive",
                           return_value=dataset):
                    with self.assertRaisesRegex(HistoricalArchiveError,
                                                "available|order/availability"):
                        closed_candle_simulation_input(
                            self.root, symbol="BTCUSDT", interval="1h",
                            from_month="2024-01", through_month="2024-01",
                            artifact_store=self.store,
                        )

    def test_scenario_only_publishes_closed_candles_and_hides_audit_cut(self):
        dataset = self.load(availability_policy=BAR_CLOSE_SCENARIO)
        first = dataset.events[0]
        feeder = CausalFeeder(dataset, start_time=first.event_time - timedelta(microseconds=1))
        self.assertEqual(feeder.view().events, ())
        feeder.advance_next_time()
        view = feeder.view()
        self.assertEqual(len(view.events), 1)
        self.assertEqual(view.events[0].payload["close"], "101")
        self.assertEqual(view.events[0].payload["availability_policy"], BAR_CLOSE_SCENARIO)
        self.assertFalse(hasattr(view, "dataset_sha256"))
        self.assertFalse(hasattr(view.events[0], "ingested_at"))
        with self.assertRaises(TypeError):
            view.events[0].payload["close"] = "999"
        with self.assertRaises(CausalReplayError):
            CausalDataView(view.simulation_time, (CausalObservation.from_event(dataset.events[1]),))
        with self.assertRaises(CausalReplayError):
            feeder.advance_to(view.simulation_time - timedelta(seconds=1))

    def test_exact_snapshot_restart_equivalence_and_tampered_cursor_rejected(self):
        dataset = self.load(availability_policy=BAR_CLOSE_SCENARIO)
        feeder = CausalFeeder(dataset, start_time=dataset.events[10].event_time)
        record = json.loads(json.dumps(feeder.checkpoint().to_record()))
        restored = CausalFeeder.restore(dataset=self.load(availability_policy=BAR_CLOSE_SCENARIO),
                                       checkpoint=FeederCheckpoint.from_record(record))
        self.assertEqual(restored.view(), feeder.view())
        self.assertEqual(restored.input_evidence(), feeder.input_evidence())
        self.assertEqual(restored.advance_next_time(), feeder.advance_next_time())
        with self.assertRaises(CausalReplayError):
            CausalFeeder.restore(dataset=dataset, checkpoint=replace(feeder.checkpoint(), cursor=20))
        with self.assertRaises(CausalReplayError):
            CausalFeeder.restore(dataset=self.load(), checkpoint=feeder.checkpoint())

    def test_daily_extension_is_contiguous_and_cannot_reuse_old_checkpoint(self):
        old = self.load(availability_policy=BAR_CLOSE_SCENARIO)
        checkpoint = CausalFeeder(old, start_time=old.events[-1].event_time).checkpoint()
        raw, checksum = _bundle(day="2024-02-01", month="2024-02", count=24)
        collect_day(symbol="BTCUSDT", interval="1h", day="2024-02-01",
                    output=self.root, artifact_store=self.store,
                    fetch=lambda url, _: checksum if url.endswith(".CHECKSUM") else raw)
        extended = self.load(daily_through="2024-02-01", availability_policy=BAR_CLOSE_SCENARIO)
        self.assertEqual(len(extended.events), 768)
        self.assertEqual(extended.events[744].event_time - extended.events[743].event_time,
                         timedelta(hours=1))
        self.assertNotEqual(old.dataset_sha256, extended.dataset_sha256)
        with self.assertRaises(CausalReplayError):
            CausalFeeder.restore(dataset=extended, checkpoint=checkpoint)
        with self.assertRaises(HistoricalArchiveError):
            self.load(daily_through="2024-02-02")

    def test_local_manifest_cannot_backdate_authenticated_capture(self):
        first = self.load()
        path = self.root / "spot/BTCUSDT/1h/2024-01.manifest.json"
        manifest = json.loads(path.read_text())
        manifest["downloaded_at_utc"] = "2024-01-01T00:00:00Z"
        manifest["published_at_utc"] = "2024-01-01T00:00:00Z"
        path.write_text(json.dumps(manifest))
        self.assertEqual(self.load().dataset_sha256, first.dataset_sha256)

    def test_missing_or_corrupt_authenticated_source_fails_closed(self):
        path = self.root / "spot/BTCUSDT/1h/2024-01.ohlcv.csv"
        path.write_bytes(path.read_bytes().replace(b",101,", b",100,", 1))
        with self.assertRaises(HistoricalArchiveError):
            self.load()


if __name__ == "__main__":
    unittest.main()
