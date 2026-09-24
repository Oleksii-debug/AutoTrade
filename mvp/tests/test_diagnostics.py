import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.diagnostics import (
    build_diagnostic_snapshot,
    redact_diagnostic_value,
)
from mvp.autotrade_mvp.pipeline import run_multi_episode, run_vertical_slice


class DiagnosticTraceTests(unittest.TestCase):
    def test_trace_reconstructs_all_simulated_decisions_from_durable_evidence(self):
        with TemporaryDirectory() as directory:
            run_multi_episode(
                [
                    [100, 101, 102, 103],
                    [100, 100, 100],
                    [103, 102, 101, 100],
                ],
                directory,
            )
            snapshot = build_diagnostic_snapshot(directory)
            self.assertEqual([trace.aggregate_version for trace in snapshot.traces], [1, 2, 3])
            self.assertEqual([trace.decision for trace in snapshot.traces], ["BUY", "HOLD", "SELL"])
            self.assertTrue(all(trace.reconciled for trace in snapshot.traces))
            self.assertEqual(snapshot.evidence_count, 3)
            self.assertEqual(snapshot.pending_outbox_sample_count, 3)
            text = snapshot.to_text()
            self.assertIn("Step 1", text)
            self.assertIn("decision BUY", text)
            self.assertNotIn("{", text)

    def test_missing_journal_linkage_fails_closed(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            for path in Path(directory).glob("journal.sqlite3*"):
                path.unlink()
            with self.assertRaisesRegex(ValueError, "journal"):
                build_diagnostic_snapshot(directory)

    def test_evidence_tamper_is_detected_against_journal(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            row = json.loads(evidence_path.read_text(encoding="utf-8"))
            row["risk_outcome"] = "tampered"
            evidence_path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "mismatch"):
                build_diagnostic_snapshot(directory)

    def test_duplicate_evidence_identifier_is_rejected(self):
        with TemporaryDirectory() as directory:
            run_vertical_slice([100, 101, 102, 103], directory)
            evidence_path = Path(directory) / "learning-evidence.jsonl"
            original = evidence_path.read_text(encoding="utf-8")
            evidence_path.write_text(original + original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                build_diagnostic_snapshot(directory)

    def test_credential_shaped_fields_are_redacted_recursively(self):
        payload = {
            "account": "demo",
            "api_key": "key-value",
            "nested": {
                "Authorization": "Bearer value",
                "accessToken": "token-value",
                "safe": "visible",
            },
            "rows": [{"password": "secret-value", "value": 3}],
        }
        redacted = redact_diagnostic_value(payload)
        self.assertEqual(redacted["account"], "demo")
        self.assertEqual(redacted["api_key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["Authorization"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["accessToken"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["safe"], "visible")
        self.assertEqual(redacted["rows"][0]["password"], "[REDACTED]")


if __name__ == "__main__":
    unittest.main()
