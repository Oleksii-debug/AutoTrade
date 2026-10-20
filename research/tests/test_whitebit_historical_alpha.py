"""Focused provenance, causality and subprocess tests for the history bridge."""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from hashlib import sha256
from importlib.util import find_spec
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from tools.historical_public_klines import HistoricalArchiveError
from tools.run_whitebit_historical_alpha import build_alpha_input
from tools.whitebit_historical_klines import SCHEMA, _canonical_json


UTC = timezone.utc
START = 1_788_919_200
STEP = 3_600


def _utc(epoch):
    return datetime.fromtimestamp(epoch, UTC).isoformat().replace("+00:00", "Z")


def fixture(root: Path, *, rows=200) -> Path:
    stream = StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(["open_time_utc", "open", "high", "low", "close", "volume"])
    for index in range(rows):
        price = 100 + index / 10
        writer.writerow([_utc(START + index * STEP), f"{price:.1f}", f"{price + 1:.1f}",
                         f"{price - 1:.1f}", f"{price + .2:.1f}", "2"])
    csv_bytes = stream.getvalue().encode()
    end = START + (rows - 1) * STEP
    page = {
        "index": 0, "start": START, "end": end + STEP, "limit": rows,
        "source_uri": ("https://whitebit.com/api/v1/public/kline?market=BTC_USDT"
                       f"&end={end + STEP}&interval=1h&limit={rows}"),
        "received_at_utc": _utc(end + 2 * STEP), "raw_sha256": "a" * 64,
        "row_count": rows, "first_open_time_utc": _utc(START),
        "last_open_time_utc": _utc(end),
    }
    identity = {
        "schema": SCHEMA, "provider": "WHITEBIT", "market": "BTC_USDT",
        "interval": "1h", "start_open_epoch": START, "end_open_epoch": end,
        "cutoff_utc": _utc(end + 2 * STEP),
        "normalized_csv_sha256": sha256(csv_bytes).hexdigest(),
        "pages": [{key: page[key] for key in
                   ("index", "start", "end", "source_uri", "received_at_utc", "raw_sha256")}],
    }
    manifest = {
        **identity, "dataset_identity_sha256": sha256(_canonical_json(identity)).hexdigest(),
        "exchange": "WHITEBIT_SPOT", "source_kind": "WHITEBIT_PRODUCTION_PUBLIC_REST_KLINE",
        "execution_environment": "SIMULATION_ONLY", "private_credentials_used": False,
        "real_orders_enabled": False, "completed_candles_only": True,
        "pagination_semantics": "LIMIT_DERIVES_START_FROM_END", "row_count": rows,
        "missing_candle_intervals": 0, "usable_as_complete_causal_interval": True,
        "start_open_time_utc": _utc(START), "end_open_time_utc": _utc(end),
        "canonical_csv_columns": ["open_time_utc", "open", "high", "low", "close", "volume"],
        "pages": [page], "adapter_version": "whitebit-public-activity-kline-v1",
        "trust_boundary": "public recorded fixture",
    }
    base = root / f"{START}-{end}"
    csv_path = base.with_suffix(".ohlcv.csv")
    manifest_path = Path(str(base) + ".manifest.json")
    csv_path.write_bytes(csv_bytes)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    return manifest_path


class WhiteBitHistoricalAlphaTests(unittest.TestCase):
    def test_authenticates_tail_and_builds_exact_causal_public_input(self):
        with TemporaryDirectory() as tmp:
            manifest = fixture(Path(tmp))
            first, selection = build_alpha_input(manifest, bars=160)
            second, repeated = build_alpha_input(manifest, bars=160)
            self.assertEqual(first, second)
            self.assertEqual(selection, repeated)
            series = first["series"]
            self.assertEqual(series["evidence_class"], "RECORDED_PUBLIC")
            self.assertEqual(len(series["closes"]), 160)
            self.assertEqual(series["closed_at_ms"][-1] + 1,
                             series["available_at_ms"][-1])
            self.assertEqual(series["asof_ms"], series["available_at_ms"][-1])
            self.assertEqual(series["closed_at_ms"][1] - series["closed_at_ms"][0], 3_600_000)
            self.assertEqual(selection["availability_policy"],
                             "FIRST_MILLISECOND_AFTER_CANDLE_CLOSE")
            self.assertEqual(first["costs"]["source_ref"],
                             "HYPOTHETICAL_NOT_ACCOUNT_FEES:WHITEBIT_ALPHA_BRIDGE_V1")

    def test_tampered_csv_and_manifest_identity_fail_closed(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            csv_path = Path(str(manifest).replace(".manifest.json", ".ohlcv.csv"))
            csv_path.write_bytes(csv_path.read_bytes().replace(b"100.2", b"999.2", 1))
            with self.assertRaisesRegex(HistoricalArchiveError, "hash differs"):
                build_alpha_input(manifest, bars=160)
        with TemporaryDirectory() as tmp:
            manifest = fixture(Path(tmp))
            value = json.loads(manifest.read_text())
            value["dataset_identity_sha256"] = "0" * 64
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalArchiveError, "identity hash differs"):
                build_alpha_input(manifest, bars=160)

    def test_gap_and_unclosed_manifest_are_rejected(self):
        with TemporaryDirectory() as tmp:
            manifest = fixture(Path(tmp))
            csv_path = Path(str(manifest).replace(".manifest.json", ".ohlcv.csv"))
            text = csv_path.read_text()
            text = text.replace(_utc(START + 50 * STEP), _utc(START + 51 * STEP), 1)
            csv_path.write_text(text, encoding="utf-8")
            value = json.loads(manifest.read_text())
            value["normalized_csv_sha256"] = sha256(csv_path.read_bytes()).hexdigest()
            identity = {key: value[key] for key in (
                "schema", "provider", "market", "interval", "start_open_epoch",
                "end_open_epoch", "cutoff_utc", "normalized_csv_sha256", "pages")}
            identity["pages"] = [{key: page[key] for key in
                                  ("index", "start", "end", "source_uri",
                                   "received_at_utc", "raw_sha256")}
                                 for page in value["pages"]]
            value["dataset_identity_sha256"] = sha256(_canonical_json(identity)).hexdigest()
            manifest.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(HistoricalArchiveError, "cadence gap"):
                build_alpha_input(manifest, bars=160)

    def test_prepare_only_subprocess_is_deterministic_and_financially_inert(self):
        with TemporaryDirectory() as tmp:
            manifest = fixture(Path(tmp))
            command = [sys.executable, "tools/run_whitebit_historical_alpha.py",
                       "--manifest", str(manifest), "--bars", "160", "--prepare-only"]
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join((str(Path.cwd()), str(Path.cwd() / "research")))
            first = subprocess.run(command, cwd=Path.cwd(), env=environment,
                                   capture_output=True, text=True, timeout=30)
            second = subprocess.run(command, cwd=Path.cwd(), env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, second.stdout)
            output = json.loads(first.stdout)
            self.assertFalse(output["financial_operation_executed"])
            self.assertEqual(output["payload"]["series"]["evidence_class"], "RECORDED_PUBLIC")

    def test_subprocess_rejects_tamper_without_traceback(self):
        with TemporaryDirectory() as tmp:
            manifest = fixture(Path(tmp))
            value = json.loads(manifest.read_text())
            value["private_credentials_used"] = True
            manifest.write_text(json.dumps(value), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "tools/run_whitebit_historical_alpha.py", "--manifest",
                 str(manifest), "--prepare-only"], cwd=Path.cwd(), capture_output=True,
                text=True, timeout=30)
            self.assertEqual(result.returncode, 2)
            self.assertIn("no financial operation executed", result.stderr)
            self.assertNotIn("Traceback", result.stderr)

    @unittest.skipUnless(find_spec("threadpoolctl") is not None,
                         "Alpha core dependency installation is still in progress")
    def test_repeated_enqueue_reuses_one_durable_job(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = fixture(root)
            state = root / "state"
            command = [
                sys.executable, "tools/run_whitebit_historical_alpha.py", "--manifest",
                str(manifest), "--bars", "160", "--state-dir", str(state),
                "--steps", "1", "--backends", "DLINEAR", "--enqueue-only",
            ]
            environment = dict(os.environ)
            environment["PYTHONPATH"] = os.pathsep.join(
                (str(Path.cwd()), str(Path.cwd() / "research"), str(Path.cwd() / "mvp")))
            first = subprocess.run(command, cwd=Path.cwd(), env=environment,
                                   capture_output=True, text=True, timeout=30)
            second = subprocess.run(command, cwd=Path.cwd(), env=environment,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(second.returncode, 0, second.stderr)
            one, two = json.loads(first.stdout), json.loads(second.stdout)
            self.assertTrue(one["created"])
            self.assertFalse(two["created"])
            self.assertEqual(one["job_id"], two["job_id"])
            self.assertTrue((state / "jobs.sqlite3").is_file())


if __name__ == "__main__":
    unittest.main()
