"""Adversarial receipt validation independent of GitHub runner availability."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.verify_plan3_qualification_receipts import EXPECTED, WORKFLOW, verify

SHA = "a" * 40


def receipt(suite: str, os_name: str, command: str) -> dict:
    return {
        "schema_version": "1.0.0",
        "source_sha": SHA,
        "checked_out_sha": SHA,
        "suite": suite,
        "command": command,
        "result": "PASS",
        "runner_os": os_name,
        "python_version": "3.12.10",
        "github": {
            "event_name": "pull_request",
            "run_id": "123",
            "run_attempt": "1",
            "workflow": WORKFLOW,
        },
        "generated_at": "2026-10-08T00:00:00+00:00",
        "contains_secrets": False,
    }


class Section8ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.rows = [
            receipt(suite, os_name, command)
            for (suite, os_name), command in EXPECTED.items()
        ]
        self.write()

    def write(self):
        for old in self.directory.glob("*.json"):
            old.unlink()
        for index, row in enumerate(self.rows):
            (self.directory / f"{index}.json").write_text(
                json.dumps(row), encoding="utf-8"
            )

    def check(self):
        verify(self.directory, source=SHA, run_id="123",
               attempt="1", event="pull_request")

    def test_complete_exact_run_is_accepted(self):
        self.check()

    def test_missing_cross_os_receipt_is_rejected(self):
        self.rows.pop()
        self.write()
        with self.assertRaises(ValueError):
            self.check()

    def test_stale_source_and_cross_attempt_are_rejected(self):
        for key, value in (("source_sha", "b" * 40),
                           ("checked_out_sha", "b" * 40)):
            with self.subTest(key=key):
                self.rows[0][key] = value
                self.write()
                with self.assertRaises(ValueError):
                    self.check()
                self.rows[0][key] = SHA
        self.rows[0]["github"]["run_attempt"] = "2"
        self.write()
        with self.assertRaises(ValueError):
            self.check()

    def test_duplicate_os_or_missing_native_is_rejected(self):
        self.rows[2] = deepcopy(self.rows[1])
        self.write()
        with self.assertRaises(ValueError):
            self.check()

    def test_secret_signal_unknown_command_and_extra_payload_are_rejected(self):
        for field, value in (("contains_secrets", True),
                             ("command", "skipped unittest"),
                             ("result", "SKIPPED")):
            row = self.rows[0]
            old = row[field]
            row[field] = value
            self.write()
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.check()
            row[field] = old
        self.rows[0]["raw_token"] = "must-never-enter-evidence"
        self.write()
        with self.assertRaises(ValueError):
            self.check()

    def test_cross_run_and_wrong_workflow_are_rejected(self):
        self.rows[0]["github"]["run_id"] = "124"
        self.write()
        with self.assertRaises(ValueError):
            self.check()
        self.rows[0]["github"]["run_id"] = "123"
        self.rows[0]["github"]["workflow"] = "unrelated"
        self.write()
        with self.assertRaises(ValueError):
            self.check()

    def test_symlink_receipt_is_rejected(self):
        file = self.directory / "0.json"
        original = Path(self.temp.name + "-payload.txt")
        self.addCleanup(lambda: original.unlink(missing_ok=True))
        original.write_bytes(file.read_bytes())
        file.unlink()
        try:
            file.symlink_to(original)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unsupported")
        with self.assertRaises(ValueError):
            self.check()


if __name__ == "__main__":
    unittest.main()
