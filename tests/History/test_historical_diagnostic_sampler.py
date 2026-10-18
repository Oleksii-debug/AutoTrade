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

    def test_retention_is_bounded_chain_continues_on_restart_and_rejects_modified_sample(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            with HistoricalDiagnosticSampler(
                    root, max_segment_bytes=1024, max_segments=3) as sampler:
                sampler.update_phase("RUNNING", "2026-09")
                for _ in range(40):
                    sampler.sample_once()
            segments = sorted(root.glob("runtime-diagnostics-*.jsonl"))
            self.assertTrue(segments)
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)
            before_rows = [json.loads(line) for path in sorted(root.glob("*.jsonl"))
                           for line in path.read_text(encoding="utf-8").splitlines()]
            with HistoricalDiagnosticSampler(
                    root, max_segment_bytes=1024, max_segments=3) as restarted:
                self.assertIsNone(restarted.error)
                restarted.sample_once()
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)
            rows = [json.loads(line) for path in sorted(root.glob("*.jsonl"))
                    for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertGreater(rows[-1]["sequence"], before_rows[-1]["sequence"])
            self.assertEqual(
                [row["sequence"] for row in rows],
                list(range(rows[0]["sequence"], rows[-1]["sequence"] + 1)),
            )
            first = sorted(root.glob("*.jsonl"))[0]
            lines = first.read_text(encoding="utf-8").splitlines()
            row = json.loads(lines[0]); row["supervisor_phase"] = "TAMPERED"
            lines[0] = json.dumps(row, sort_keys=True)
            first.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with HistoricalDiagnosticSampler(root) as invalid:
                self.assertIn("hash chain broken", invalid.error)

    def test_sample_redacts_secrets_before_hashing_and_persistence(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "telemetry"
            sampler = HistoricalDiagnosticSampler(root)
            root.mkdir()
            record = sampler._sample()
            record["api_secret"] = "DO-NOT-PERSIST"
            record["detail"] = "access_token=ALSO-SECRET"
            sampler._append(record)

            body = (root / "runtime-diagnostics.jsonl").read_bytes()
            self.assertNotIn(b"DO-NOT-PERSIST", body)
            self.assertNotIn(b"ALSO-SECRET", body)
            row = json.loads(body)
            self.assertEqual(row["api_secret"], "[REDACTED]")
            self.assertEqual(row["detail"], "access_token=[REDACTED]")
            with HistoricalDiagnosticSampler(root) as restarted:
                self.assertIsNone(restarted.error)
                restarted.sample_once()


if __name__ == "__main__":
    unittest.main()
