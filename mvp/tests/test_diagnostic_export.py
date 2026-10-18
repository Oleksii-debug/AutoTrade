"""Shareable reports must remain faithful to durable evidence and inert."""
import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from mvp.autotrade_mvp.diagnostic_export import (
    agent_report_zip,
    historical_diagnostic_export,
    runtime_diagnostic_export,
    runtime_diagnostic_zip,
    technical_diagnostic_zip,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.pipeline import run_vertical_slice
from mvp.autotrade_mvp.runtime_diagnostic_sampler import RuntimeDiagnosticSampler


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
            try:
                (models / "linked.json").symlink_to(outside)
            except OSError as error:
                self.skipTest(f"file symlinks unavailable on this host: {error.winerror}")
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

    def test_agent_and_technical_downloads_are_independent_and_redacted(self):
        with TemporaryDirectory() as temporary:
            state = Path(temporary)
            run_vertical_slice([100, 101, 102, 103], state)
            (state / "checkpoint.json").write_text(
                '{"access_token":"agent-secret-sentinel"}', encoding="utf-8")
            sampler = RuntimeDiagnosticSampler(state / "technical-diagnostics")
            sampler.root.mkdir()
            sampler._sample = lambda: {
                "schema_version": "1.0.0",
                "recorded_at_utc": "2026-10-18T00:00:00+00:00",
                "sequence": 1,
                "previous_sha256": "0" * 64,
                "runtime_phase": "RUNNING",
                "api_key": "technical-secret-sentinel",
            }
            sampler.sample_once()

            agent = agent_report_zip(
                JournalStore(state / "journal.sqlite3"),
                state,
                source_revision="b" * 40,
            )
            technical = technical_diagnostic_zip(
                sampler,
                source_revision="b" * 40,
            )

            self.assertEqual(agent.filename, "autotrade-agent-report.zip")
            self.assertEqual(
                technical.filename,
                "autotrade-technical-diagnostics.zip",
            )
            self.assertNotIn(b"agent-secret-sentinel", agent.data)
            self.assertNotIn(b"technical-secret-sentinel", technical.data)
            with ZipFile(BytesIO(agent.data)) as archive:
                self.assertNotIn("technical-heartbeats.jsonl", archive.namelist())
                self.assertFalse(any(name.startswith("segments/") for name in archive.namelist()))
                self.assertEqual(
                    json.loads(archive.read("manifest.json"))["report_kind"],
                    "AGENT_REPORT",
                )
            with ZipFile(BytesIO(technical.data)) as archive:
                self.assertNotIn("journal-events.jsonl", archive.namelist())
                self.assertEqual(
                    json.loads(archive.read("manifest.json"))["report_kind"],
                    "TECHNICAL_DIAGNOSTICS",
                )
                heartbeat = json.loads(
                    archive.read("technical-heartbeats.jsonl").splitlines()[0]
                )
                self.assertEqual(heartbeat["api_key"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
