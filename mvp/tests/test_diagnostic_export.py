"""Shareable reports must remain faithful to durable evidence and inert."""
from datetime import datetime, timezone
from decimal import Decimal
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
from mvp.autotrade_mvp.groq_advisory_research import (
    ADVISORY_ROLES,
    DurableGroqAdvisoryWave,
)
from mvp.autotrade_mvp.model_call import ModelCallObservation


GROQ_NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)


def _groq_evidence(directory: Path) -> None:
    calls = {}
    for role in ADVISORY_ROLES:
        def call(binding, _cancelled, selected=role):
            return ModelCallObservation(
                provider_id="groq",
                model_id=binding.model_id,
                revision=binding.revision,
                observed_at="2026-10-20T12:00:00Z",
                incurred_cost=Decimal(0),
                estimated_unbilled=Decimal(0),
                output={
                    "text": (
                        selected.role
                        + ": reject unproven hypothesis; gsk_SECRET1234567890"
                    ),
                    "prompt_tokens": 5,
                    "completion_tokens": 3,
                },
                provider_response_id="response-" + selected.role,
                usage_id="usage-" + selected.role,
            )
        calls[role.role] = call
    DurableGroqAdvisoryWave(
        directory, calls_by_role=calls, clock=lambda: GROQ_NOW
    ).run(
        wave_id="diagnostic-wave",
        research_input={"evidence_class": "PUBLIC_MARKET_ZERO"},
    )


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

    def test_agent_zip_includes_sanitized_groq_events_summary_and_quota(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            groq = root / "groq-advisory"
            run_vertical_slice([100, 101, 102, 103], state)
            _groq_evidence(groq)

            report = agent_report_zip(
                JournalStore(state / "journal.sqlite3"),
                state,
                source_revision="c" * 40,
                groq_advisory_state_dir=groq,
            )

            self.assertNotIn(b"gsk_", report.data)
            with ZipFile(BytesIO(report.data)) as archive:
                names = set(archive.namelist())
                self.assertIn("groq-advisory/events.jsonl", names)
                self.assertIn("groq-advisory/summary.json", names)
                self.assertIn("groq-advisory/quota-snapshot.json", names)
                self.assertNotIn("groq-advisory.sqlite3", names)
                self.assertNotIn("groq-quota.sqlite3", names)
                manifest = json.loads(archive.read("manifest.json"))
                self.assertTrue(manifest["groq_advisory"]["included"])
                self.assertEqual(manifest["groq_advisory"]["wave_count"], 1)
                summary = json.loads(
                    archive.read("groq-advisory/summary.json")
                )
                quota = json.loads(
                    archive.read("groq-advisory/quota-snapshot.json")
                )
                self.assertEqual(summary["status"], "COMPLETED")
                self.assertFalse(summary["financial_authority"])
                self.assertEqual(quota["status"], "AVAILABLE")
                self.assertEqual(len(quota["profiles"]), 12)
                for name, evidence in manifest["members"].items():
                    self.assertEqual(
                        sha256(archive.read(name)).hexdigest(), evidence["sha256"]
                    )

    def test_groq_export_fails_closed_for_nonfile_and_oversized_mirror(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            groq = root / "groq-advisory"
            run_vertical_slice([100, 101, 102, 103], state)
            _groq_evidence(groq)
            quota = groq / "groq-quota.sqlite3"
            quota.unlink()
            quota.mkdir()
            with self.assertRaisesRegex(ValueError, "ordinary unaliased file"):
                agent_report_zip(
                    JournalStore(state / "journal.sqlite3"),
                    state,
                    source_revision="d" * 40,
                    groq_advisory_state_dir=groq,
                )
            quota.rmdir()
            # A missing quota journal is represented explicitly, but an
            # oversized durable mirror is rejected before any export.
            mirror = groq / "groq-advisory-events.jsonl"
            with mirror.open("wb") as stream:
                stream.seek(16 * 1024 * 1024)
                stream.write(b"x")
            with self.assertRaisesRegex(ValueError, "size limit"):
                agent_report_zip(
                    JournalStore(state / "journal.sqlite3"),
                    state,
                    source_revision="e" * 40,
                    groq_advisory_state_dir=groq,
                )

    def test_groq_export_rejects_symlink_evidence(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "state"
            groq = root / "groq-advisory"
            run_vertical_slice([100, 101, 102, 103], state)
            _groq_evidence(groq)
            mirror = groq / "groq-advisory-events.jsonl"
            outside = root / "outside.jsonl"
            outside.write_text('{}\n', encoding="utf-8")
            mirror.unlink()
            try:
                mirror.symlink_to(outside)
            except OSError as error:
                self.skipTest(
                    "file symlinks unavailable on this host: "
                    + str(getattr(error, "winerror", error))
                )
            with self.assertRaisesRegex(ValueError, "symlink"):
                agent_report_zip(
                    JournalStore(state / "journal.sqlite3"),
                    state,
                    source_revision="f" * 40,
                    groq_advisory_state_dir=groq,
                )


if __name__ == "__main__":
    unittest.main()
