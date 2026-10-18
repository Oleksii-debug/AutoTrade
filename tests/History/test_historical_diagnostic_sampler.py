"""Durable technical telemetry must survive restart and detect changed samples."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from tools.historical_diagnostic_sampler import HistoricalDiagnosticSampler


class HistoricalDiagnosticSamplerTests(unittest.TestCase):
    def test_worker_samples_without_supervisor_calls(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with HistoricalDiagnosticSampler(root, interval=0.1) as sampler:
                sampler.update_phase("WAITING_FOR_COMPLETED_UTC_MONTH", "2026-09")
                time.sleep(0.35)
            rows = [json.loads(line) for line in
                    (root / "runtime-diagnostics.jsonl").read_text().splitlines()]
            self.assertGreaterEqual(len(rows), 3)
            self.assertEqual([row["sequence"] for row in rows], list(range(1, len(rows) + 1)))
            self.assertIn("WAITING_FOR_COMPLETED_UTC_MONTH",
                          {row["supervisor_phase"] for row in rows})

    def test_rotated_chain_continues_on_restart_and_rejects_modified_sample(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with HistoricalDiagnosticSampler(root, max_segment_bytes=1024) as sampler:
                sampler.update_phase("RUNNING", "2026-09")
                for _ in range(40):
                    sampler.sample_once()
            segments = sorted(root.glob("runtime-diagnostics-*.jsonl"))
            self.assertTrue(segments)
            before = sum(len(path.read_bytes().splitlines()) for path in segments)
            before += len((root / "runtime-diagnostics.jsonl").read_bytes().splitlines())
            with HistoricalDiagnosticSampler(root, max_segment_bytes=1024) as restarted:
                self.assertIsNone(restarted.error)
                restarted.sample_once()
            after = sum(len(path.read_bytes().splitlines()) for path in root.glob("*.jsonl"))
            self.assertGreater(after, before)
            first = segments[0]
            lines = first.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0]); row["supervisor_phase"] = "TAMPERED"
            lines[0] = json.dumps(row, sort_keys=True)
            first.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with HistoricalDiagnosticSampler(root) as invalid:
                self.assertIn("hash chain broken", invalid.error)


if __name__ == "__main__":
    unittest.main()
