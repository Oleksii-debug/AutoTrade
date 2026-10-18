"""Shareable reports must remain faithful to durable evidence and inert."""
import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from mvp.autotrade_mvp.diagnostic_export import (
    historical_diagnostic_export, runtime_diagnostic_export, runtime_diagnostic_zip,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice


class DiagnosticExportTests(unittest.TestCase):
    def test_runtime_report_preserves_journal_and_masks_artifact_credentials(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            run_vertical_slice([100, 101, 102, 103], state)
            checkpoint = state / "checkpoint.json"
            value = json.loads(checkpoint.read_text(encoding="utf-8"))
            value["api_key"] = "secret-sentinel-453"
            checkpoint.write_text(json.dumps(value), encoding="utf-8")
            report = runtime_diagnostic_export(JournalStore(state / "journal.sqlite3"), state)
            decoded = json.loads(report.data)
            self.assertEqual(decoded["event_count"], len(decoded["events"]))
            self.assertGreater(decoded["event_count"], 0)
            self.assertEqual(decoded["artifacts"]["checkpoint.json"]["api_key"], "[REDACTED]")
            self.assertNotIn(b"secret-sentinel-453", report.data)

    def test_historical_report_rejects_linked_artifact_instead_of_reading_it(self):
        with TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            models = workspace / "models"
            models.mkdir(parents=True)
            outside = Path(temporary) / "outside.json"
            outside.write_text('{"password":"outside-secret"}', encoding="utf-8")
            (models / "linked.json").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                historical_diagnostic_export(workspace)

    def test_full_zip_carries_integrity_and_explicit_telemetry_coverage(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            run_vertical_slice([100, 101, 102, 103], state)
            (state / "checkpoint.json").write_text(
                '{"api_key":"secret-sentinel-453"}', encoding="utf-8")
            report = runtime_diagnostic_zip(
                JournalStore(state / "journal.sqlite3"), state,
                source_revision="a" * 40)
            self.assertNotIn(b"secret-sentinel-453", report.data)
            with ZipFile(BytesIO(report.data)) as archive:
                manifest = json.loads(archive.read("manifest.json"))
                self.assertFalse(manifest["technical_sampling_complete"])
                self.assertTrue(manifest["complete_journal"])
                for name, evidence in manifest["members"].items():
                    self.assertEqual(sha256(archive.read(name)).hexdigest(), evidence["sha256"])
                self.assertEqual(len(archive.read("journal-events.jsonl").splitlines()),
                                 manifest["journal_event_count"])


if __name__ == "__main__":
    unittest.main()
