"""Provider-free historical adapter tests: synthetic fixtures, NO trading evidence."""
import csv
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from autotrade_runtime.artifacts import ArtifactStore

from tools.historical_public_klines import collect_month, HistoricalArchiveError
from tools.historical_causal_replay import BAR_CLOSE_SCENARIO
from tools.run_historical_zero import load_verified_months, main


def full_month_fixture(month="2024-01", close_at=None):
    origin = datetime.fromisoformat(month+"-01T00:00:00+00:00")
    limit = (datetime(origin.year+1,1,1,tzinfo=timezone.utc)
             if origin.month == 12 else datetime(origin.year,origin.month+1,1,tzinfo=timezone.utc))
    csv_text = StringIO()
    writer = csv.writer(csv_text,lineterminator="\n")
    i = 0
    while origin+timedelta(hours=i) < limit:
        t = int((origin+timedelta(hours=i)).timestamp())
        open_millis = t*1000 if origin.year == 2024 else t*1_000_000
        unit = 1000 if origin.year == 2024 else 1_000_000
        closing = str(close_at(i)) if close_at is not None else "101"
        writer.writerow([str(open_millis),"100","102","99",closing,"1",
                         str(open_millis+3600*unit-1),"101","5","1","101","0"])
        i += 1
    filename=f"BTCUSDT-1h-{month}.zip"
    buf=BytesIO()
    with ZipFile(buf,"w") as z:
        z.writestr(filename[:-4]+".csv",csv_text.getvalue())
    raw=buf.getvalue()
    check=f"{sha256(raw).hexdigest()}  {filename}\n".encode()
    return raw,check,i


class HistoricalZeroTests(unittest.TestCase):
    def _dataset(self,tmp, *, close_at=None):
        archive, checksum, count=full_month_fixture(close_at=close_at)
        def no_network(url,limit):
            return checksum if url.endswith(".CHECKSUM") else archive
        manifest=collect_month(symbol="BTCUSDT",interval="1h",month="2024-01",
                               output=Path(tmp),fetch=no_network,
                               artifact_store=ArtifactStore(Path(tmp) / ".artifact-store"))
        self.assertTrue(manifest["usable_as_complete_causal_interval"])
        self.assertEqual(manifest["row_count"],count)
        return count

    def test_full_realistic_calendar_passes_source_digest_and_clock(self):
        with TemporaryDirectory() as tmp:
            count=self._dataset(tmp)
            times, prices, digest=load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")
            self.assertEqual(count,744)
            self.assertEqual(len(prices),count)
            self.assertEqual(times[0].isoformat(),"2024-01-01T00:00:00+00:00")
            self.assertEqual(times[-1].isoformat(),"2024-01-31T23:00:00+00:00")
            self.assertEqual(len(digest),64)

    def test_replay_authenticates_canonical_artifact_store_evidence(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            store = ArtifactStore(Path(tmp) / ".artifact-store")
            times, prices, _digest = load_verified_months(
                Path(tmp), "BTCUSDT", "1h", "2024-01", "2024-01",
                artifact_store=store,
            )
            self.assertEqual((len(times), len(prices)), (744, 744))

            manifest_path = Path(tmp) / "spot/BTCUSDT/1h/2024-01.manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifact_evidence"]["source_zip"]["sha256"] = "sha256:" + "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(
                HistoricalArchiveError,
                "artifact evidence conflicts",
            ):
                load_verified_months(
                    Path(tmp), "BTCUSDT", "1h", "2024-01", "2024-01",
                    artifact_store=ArtifactStore(Path(tmp) / ".artifact-store"),
                )

    def test_nested_parent_symlink_blocks_archive_before_network_and_replay(self):
        # Even when data-root itself is not a symlink, one of its higher
        # ancestors can redirect writes and reads to a different dataset.
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            physical = root / "physical"
            physical.mkdir()
            alias = root / "alias"
            try:
                alias.symlink_to(physical, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks unavailable on this host")
            shadow_root = alias / "dataset"
            archive, checksum, _ = full_month_fixture()
            network_attempts = []
            def fake_source(url, limit):
                network_attempts.append(url)
                return checksum if url.endswith(".CHECKSUM") else archive
            with self.assertRaisesRegex(HistoricalArchiveError, "symlink ancestor"):
                collect_month(symbol="BTCUSDT", interval="1h",
                              month="2024-01", output=shadow_root, fetch=fake_source)
            self.assertEqual(network_attempts, [])
            self.assertFalse((physical / "dataset").exists())
            self._dataset(physical / "dataset")
            with self.assertRaisesRegex(HistoricalArchiveError, "symlink ancestor"):
                load_verified_months(shadow_root, "BTCUSDT", "1h", "2024-01", "2024-01")
            with self.assertRaisesRegex(HistoricalArchiveError, "parent traversal"):
                load_verified_months(physical / ".." / "physical" / "dataset",
                                     "BTCUSDT", "1h", "2024-01", "2024-01")

    def test_replay_rejects_truncated_month_even_if_manifest_hash_is_recomputed(self):
        # A coherent-looking local manifest must not turn a missing opening
        # candle into a COMPLETE dataset after a disk fault or tampering.
        with TemporaryDirectory() as tmp:
            count = self._dataset(tmp)
            base = Path(tmp) / "spot/BTCUSDT/1h"
            datafile = base / "2024-01.ohlcv.csv"
            manifestfile = base / "2024-01.manifest.json"
            lines = datafile.read_bytes().splitlines(keepends=True)
            truncated = b"".join([lines[0], *lines[2:]])
            datafile.write_bytes(truncated)
            manifest = json.loads(manifestfile.read_text(encoding="utf-8"))
            manifest["row_count"] = count - 1
            manifest["start_open_time_utc"] = "2024-01-01T01:00:00Z"
            manifest["missing_candle_intervals"] = 0
            manifest["usable_as_complete_causal_interval"] = True
            manifest["normalized_csv_sha256"] = sha256(truncated).hexdigest()
            manifestfile.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalArchiveError, "full UTC month"):
                load_verified_months(Path(tmp), "BTCUSDT", "1h", "2024-01", "2024-01")

    def test_entrypoint_reuses_existing_zero_financial_engine_without_live_route(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            with patch("tools.run_historical_zero.run_autonomous_simulation",return_value={
                 "status":"SUCCEEDED","environment":"SIMULATION","mode":"ZERO","completed_episodes":744
            }) as sim:
                status=main(["--data-root",tmp,"--symbol","BTCUSDT","--interval","1h",
                             "--from-month","2024-01","--through-month","2024-01",
                             "--state-dir",str(Path(tmp)/"state"),"--run-id","trial"])
            self.assertEqual(status,0)
            args,kw=sim.call_args
            self.assertEqual(len(args[0]),744)
            self.assertEqual(kw["now"],"2024-01-01T01:00:00Z")
            self.assertEqual(kw["observation_interval_seconds"],3600)
            # $250 research target comes from the FIRST COMPLETED close (101),
            # never the last/future candles; canonical engine handles fills.
            self.assertEqual(kw["instrument_profile"],"FRACTIONAL_SPOT_RESEARCH")
            self.assertEqual(kw["target_quantity"],"2.47524752")
            self.assertEqual(kw["virtual_cash"],"1000")
            self.assertTrue(kw["run_id"].startswith("trial-"))
            self.assertEqual(len(kw["run_id"]),len("trial-")+16)

            with patch("tools.run_historical_zero.run_autonomous_simulation", return_value={
                "status": "SUCCEEDED", "environment": "SIMULATION", "mode": "ZERO",
                "completed_episodes": 744,
            }) as small_sim:
                status = main(["--data-root", tmp, "--symbol", "BTCUSDT", "--interval", "1h",
                               "--from-month", "2024-01", "--through-month", "2024-01",
                               "--run-id", "small", "--virtual-cash", "200"])
            self.assertEqual(status, 0)
            self.assertEqual(small_sim.call_args.args[1], "historical-zero-state-200")
            self.assertEqual(small_sim.call_args.kwargs["virtual_cash"], "200")
            self.assertEqual(small_sim.call_args.kwargs["target_quantity"], "0.49504950")

    def test_causal_archive_runs_and_resumes_canonical_small_account(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            state = str(Path(tmp) / "state-200")
            base = ["--data-root", tmp, "--through-month", "2024-01",
                    "--virtual-cash", "200", "--state-dir", state,
                    "--run-id", "causal-resume"]
            for stop, resumed in ((2, False), (3, True)):
                output = StringIO()
                with redirect_stdout(output):
                    code = main([*base, "--stop-after-episodes", str(stop)])
                self.assertEqual(code, 0, output.getvalue())
                result = json.loads(output.getvalue())
                self.assertEqual(result["observation_count"], 744)
                self.assertEqual(result["virtual_cash_usd"], "200")
                self.assertEqual(result["availability_policy"], BAR_CLOSE_SCENARIO)
                sim = result["simulation"]
                self.assertEqual(sim["completed_episodes"], stop)
                self.assertEqual(sim["cash"], "200")
                self.assertEqual(sim["new_outbound_requests"], 0)
                self.assertTrue(sim["reconciled"])
                self.assertEqual(sim["resumed"], resumed)

    def test_next_open_cut_hides_execution_prices_from_strategy_and_resumes(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            from mvp.autotrade_mvp.pipeline import MovingAverageStrategy
            from mvp.autotrade_mvp import product_agent
            original_decide = MovingAverageStrategy.decide
            original_agent = product_agent.decide
            observed_cuts = []
            agent_cuts = []
            def observe_strategy(self, prices, quantity):
                observed_cuts.append(tuple(str(price) for price in prices))
                return original_decide(self, prices, quantity)
            def observe_agent(journal, protocol, **kwargs):
                self.assertNotIn("prices", protocol)
                self.assertEqual(len(protocol["signal_prices"]), kwargs["episode"])
                agent_cuts.append(tuple(protocol["signal_prices"]))
                return original_agent(journal, protocol, **kwargs)
            state = str(Path(tmp) / "next-open-200")
            base = ["--data-root", tmp, "--through-month", "2024-01",
                    "--virtual-cash", "200", "--state-dir", state,
                    "--run-id", "next-open", "--next-bar-open"]
            for stop in (2, 3):
                output = StringIO()
                with redirect_stdout(output), \
                     patch.object(MovingAverageStrategy, "decide", observe_strategy), \
                     patch.object(product_agent, "decide", observe_agent):
                    code = main([*base, "--stop-after-episodes", str(stop)])
                self.assertEqual(code, 0, output.getvalue())
                result = json.loads(output.getvalue())
                self.assertEqual(result["observation_count"], 744)
                self.assertEqual(result["execution_count"], 743)
                self.assertEqual(result["simulation"]["completed_episodes"], stop)
                self.assertEqual(result["simulation"]["new_outbound_requests"], 0)
                self.assertTrue(result["simulation"]["reconciled"])
                self.assertEqual(result["simulation"]["resumed"], stop == 3)
                report = result["comparative_report"]
                self.assertEqual(report["through_episode"], stop)
                self.assertEqual(report["cash_baseline_equity_usd"], "200")
                self.assertEqual(report["agent_mark_pnl_usd"], "0")
                self.assertLess(Decimal(report["buy_hold_liquidation_scenario_pnl_usd"]), 0)
                self.assertEqual(report["asof_execution_open_utc"],
                                 (datetime(2024, 1, 1, stop, tzinfo=timezone.utc)).isoformat())
            self.assertEqual(observed_cuts, [("101",), ("101", "101"),
                                             ("101", "101", "101")])
            self.assertEqual(agent_cuts, observed_cuts)
            from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
            store = JournalStore(Path(state) / "journal.sqlite3")
            events = store.load_events("canonical_autonomous_simulation",
                                       result["simulation"]["run_id"])
            protocol = events[0]["payload"]["protocol"]
            self.assertEqual(protocol["execution_timing"],
                             "PRIOR_CLOSED_BAR_TO_NEXT_BAR_OPEN_SCENARIO")
            self.assertEqual(protocol["signal_prices"][0], "101")
            self.assertEqual(protocol["prices"][0], "100")
            self.assertEqual(protocol["adverse_slippage_bps"], "10")
            research = store.load_events("provider_free_research", protocol["run_id"])
            self.assertEqual(len(research), 3)
            for episode, event in enumerate(research, 1):
                payload = event["payload"]
                self.assertEqual(payload["through_episode"], episode)
                self.assertEqual(payload["data_origin"], "HISTORICAL_BAR_CLOSE_SCENARIO")
                self.assertEqual(payload["proposal"]["episode"], episode)
                self.assertTrue(payload["proposal_prefix_digest"].startswith("sha256:"))
                self.assertNotIn("proposals", payload)
                self.assertEqual(payload["dataset_digest"],
                                 payload_digest(protocol["signal_prices"][:episode]))
            from mvp.autotrade_mvp.simulation_session import run_autonomous_simulation
            changed_signals = list(protocol["signal_prices"])
            changed_signals[0] = "102"
            with self.assertRaisesRegex(ValueError, "protocol/input identity changed"):
                run_autonomous_simulation(
                    protocol["prices"], state, run_id=protocol["run_id"],
                    now=protocol["start_time"], virtual_cash="200",
                    observation_interval_seconds=3600,
                    instrument_profile="FRACTIONAL_SPOT_RESEARCH",
                    target_quantity=protocol["target_quantity"],
                    signal_prices=changed_signals, slippage_bps="10",
                    stop_after_episodes=3,
                )

    def test_next_open_buy_sell_adverse_fill_and_canonical_fee(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp, close_at=lambda i: ("100", "100", "102", "102", "100")[i]
                          if i < 5 else "101")
            output = StringIO()
            with redirect_stdout(output):
                code = main(["--data-root", tmp, "--through-month", "2024-01",
                             "--virtual-cash", "200", "--state-dir", str(Path(tmp) / "state"),
                             "--run-id", "next-buy", "--next-bar-open",
                             "--stop-after-episodes", "5"])
            self.assertEqual(code, 0, output.getvalue())
            result = json.loads(output.getvalue())
            sim = result["simulation"]
            self.assertEqual(sim["decisions"][2]["decision"], "BUY")
            self.assertEqual(sim["decisions"][2]["status"], "FILLED")
            self.assertEqual(sim["decisions"][2]["cash"], "149.89995")
            self.assertEqual(sim["decisions"][4]["decision"], "REDUCE")
            self.assertEqual(sim["decisions"][4]["status"], "FILLED")
            self.assertEqual(sim["cash"], "199.8")
            self.assertEqual(sim["position"], "0")
            self.assertEqual(sim["new_outbound_requests"], 2)
            comparison = result["comparative_report"]
            self.assertEqual(comparison["agent_mark_pnl_usd"], "-0.2")
            self.assertEqual(Decimal(comparison["agent_liquidation_scenario_equity_usd"]), Decimal("199.8"))
            self.assertEqual(comparison["cash_baseline_equity_usd"], "200")

    def test_invalid_next_open_cost_cannot_start_financial_state(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            state = Path(tmp) / "invalid-cost"
            for amount in ("-1", "101", "nan", "untrusted"):
                with self.subTest(amount=amount):
                    output = StringIO()
                    with redirect_stdout(output):
                        code = main(["--data-root", tmp, "--through-month", "2024-01",
                                     "--state-dir", str(state), "--next-bar-open",
                                     "--slippage-bps", amount])
                    self.assertEqual(code, 2)
                    self.assertFalse(state.exists())

    def test_tamper_and_missing_archive_rejected(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            path=Path(tmp)/"spot/BTCUSDT/1h/2024-01.ohlcv.csv"
            path.write_bytes(path.read_bytes()+b"bad")
            with self.assertRaisesRegex(HistoricalArchiveError,"digest mismatch"):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")
            with self.assertRaises(HistoricalArchiveError):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-02")

    def test_replay_rejects_relabelled_source_rights_and_malformed_digest(self):
        # A post-download manifest must not be relabelled as a different
        # exchange, archive, license or supposedly verified checksum.
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            path = Path(tmp) / "spot/BTCUSDT/1h/2024-01.manifest.json"
            trusted = json.loads(path.read_text(encoding="utf-8"))
            bad_fields = (
                ("source_url", "https://other.example/spot/BTCUSDT-1h-2024-01.zip"),
                ("source_checksum_url", "https://other.example/fake.CHECKSUM"),
                ("exchange", "UNVERIFIED_MARKET"),
                ("license", "COMMERCIAL_REUSE_ALLOWED"),
                ("license_terms_url", "https://other.example/terms"),
                ("rights_scope", "COMMERCIAL_USE_ALLOWED"),
                ("period_granularity", "DAILY"),
                ("day", "2024-01-01"),
                ("source_zip_sha256", "g" * 64),
                ("source_csv_sha256", "x" * 64),
                ("source_checksum_sha256", "z" * 64),
            )
            for field, forged in bad_fields:
                with self.subTest(field=field):
                    changed = dict(trusted)
                    changed[field] = forged
                    path.write_text(json.dumps(changed), encoding="utf-8")
                    with self.assertRaisesRegex(HistoricalArchiveError, "provenance"):
                        load_verified_months(Path(tmp), "BTCUSDT", "1h", "2024-01", "2024-01")
            path.write_text(json.dumps(trusted), encoding="utf-8")
            self.assertEqual(
                len(load_verified_months(Path(tmp), "BTCUSDT", "1h", "2024-01", "2024-01")[1]),
                744,
            )

    def test_replay_rejects_symlinked_dataset_ancestor(self):
        with TemporaryDirectory() as tmp, TemporaryDirectory() as alias_root:
            self._dataset(tmp)
            alias = Path(alias_root) / "spoofed"
            try:
                alias.symlink_to(Path(tmp), target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaisesRegex(HistoricalArchiveError, "symlink"):
                load_verified_months(alias, "BTCUSDT", "1h", "2024-01", "2024-01")

    def test_wrong_market_cannot_be_relabelled(self):
        with TemporaryDirectory() as tmp:
            self._dataset(tmp)
            manifest=Path(tmp)/"spot/BTCUSDT/1h/2024-01.manifest.json"
            j=json.loads(manifest.read_text())
            j["market"]="PROVIDER_QUALIFIED_LIVE"
            manifest.write_text(json.dumps(j))
            with self.assertRaisesRegex(HistoricalArchiveError,"source identity"):
                load_verified_months(Path(tmp),"BTCUSDT","1h","2024-01","2024-01")


if __name__ == "__main__":
    unittest.main()
