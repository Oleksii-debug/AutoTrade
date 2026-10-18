"""The product heartbeat remains durable, bounded and non-authoritative."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest

from mvp.autotrade_mvp.runtime_diagnostic_sampler import RuntimeDiagnosticSampler


class RuntimeDiagnosticSamplerTests(unittest.TestCase):
    def test_background_heartbeat_is_approximately_periodic(self):
        with TemporaryDirectory() as temporary:
            sampler = RuntimeDiagnosticSampler(
                Path(temporary) / "technical",
                interval_seconds=0.1,
            )
            sampler.start()
            time.sleep(0.34)
            sampler.stop()
            rows = [
                json.loads(line)
                for _, body in sampler.snapshot()
                for line in body.splitlines()
            ]
            self.assertGreaterEqual(len(rows), 3)
            self.assertEqual(
                [row["sequence"] for row in rows],
                list(range(1, len(rows) + 1)),
            )
            self.assertTrue(all(row["financial_authority"] is False for row in rows))

    def test_product_default_cadence_is_one_second(self):
        with TemporaryDirectory() as temporary:
            sampler = RuntimeDiagnosticSampler(Path(temporary) / "technical")
            self.assertEqual(sampler.interval_seconds, 1.0)

    def test_rotation_is_bounded_and_sequence_survives_restart(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / "technical"
            root.mkdir()
            first = RuntimeDiagnosticSampler(
                root,
                max_segment_bytes=1024,
                max_segments=3,
            )
            for _ in range(24):
                first.sample_once()
            before = [
                json.loads(line)["sequence"]
                for _, body in first.snapshot()
                for line in body.splitlines()
            ]
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)

            restarted = RuntimeDiagnosticSampler(
                root,
                max_segment_bytes=1024,
                max_segments=3,
            )
            restarted.start()
            deadline = time.monotonic() + 2.0
            while restarted._sequence <= before[-1] and time.monotonic() < deadline:
                time.sleep(0.01)
            restarted.stop()
            after = [
                json.loads(line)["sequence"]
                for _, body in restarted.snapshot()
                for line in body.splitlines()
            ]
            self.assertGreater(after[-1], before[-1])
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)
            self.assertEqual(after, list(range(after[0], after[-1] + 1)))


if __name__ == "__main__":
    unittest.main()
