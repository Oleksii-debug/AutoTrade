from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from tools.verify_atomic_plan_status import (
    DEFAULT_REGISTRY,
    PlanRegistryError,
    atomic_plan_status,
    main,
)


class AtomicPlanStatusTests(unittest.TestCase):
    def test_current_registry_reports_totals_and_zero_unfinished_unambiguously(self) -> None:
        status = atomic_plan_status(DEFAULT_REGISTRY)

        self.assertEqual(status["total_sections"], 53)
        self.assertEqual(status["completed_sections"], 53)
        self.assertEqual(status["unfinished_sections"], 0)
        self.assertTrue(status["terminal_for_approved_private_zero_scope"])
        self.assertEqual(
            status["plans"],
            {
                "9": {
                    "total_sections": 30,
                    "completed_sections": 30,
                    "unfinished_sections": 0,
                    "unfinished_section_ids": [],
                },
                "10": {
                    "total_sections": 23,
                    "completed_sections": 23,
                    "unfinished_sections": 0,
                    "unfinished_section_ids": [],
                },
            },
        )

    def test_missing_row_fails_closed_instead_of_reporting_a_smaller_plan(self) -> None:
        text = DEFAULT_REGISTRY.read_text(encoding="utf-8")
        text = text.replace("| 10Б |", "| removed |", 1)
        with TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry.md"
            registry.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(PlanRegistryError, "29 atomic sections"):
                atomic_plan_status(registry)

    def test_duplicate_row_fails_closed(self) -> None:
        text = DEFAULT_REGISTRY.read_text(encoding="utf-8")
        row = next(line for line in text.splitlines() if line.startswith("| 1А |"))
        marker = "## План 10: 23 самостійних розділів"
        text = text.replace(marker, row + "\n" + marker, 1)
        with TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry.md"
            registry.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(PlanRegistryError, "duplicate section 1А"):
                atomic_plan_status(registry)

    def test_check_terminal_returns_one_for_a_reopened_section(self) -> None:
        text = DEFAULT_REGISTRY.read_text(encoding="utf-8")
        text = text.replace(
            "| 10В | Anything excluded/deferred is explicit and not advertised as supported. | **DONE**",
            "| 10В | Anything excluded/deferred is explicit and not advertised as supported. | **REOPENED**",
            1,
        )
        with TemporaryDirectory() as temporary:
            registry = Path(temporary) / "registry.md"
            registry.write_text(text, encoding="utf-8")
            with redirect_stdout(StringIO()):
                self.assertEqual(main(["--registry", str(registry), "--check-terminal"]), 1)


if __name__ == "__main__":
    unittest.main()
