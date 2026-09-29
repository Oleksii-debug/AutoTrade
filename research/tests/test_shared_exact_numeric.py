from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import unittest

from autotrade_numeric import (
    MAX_INTEGER_DIGITS,
    MAX_RATIONAL_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
    as_fraction,
    validate_fraction,
)


class SharedExactNumericDependencyTests(unittest.TestCase):
    def test_isolated_research_install_resolves_shared_numeric_authority(self):
        self.assertEqual(MAX_SIGNIFICANT_DIGITS, 256)
        self.assertEqual(MAX_SCALE, 256)
        self.assertEqual(MAX_INTEGER_DIGITS, 256)
        self.assertEqual(MAX_RATIONAL_DIGITS, 1024)
        self.assertEqual(as_fraction(Decimal("0.125")), Fraction(1, 8))
        self.assertEqual(validate_fraction(Fraction(1, 3)), Fraction(1, 3))

    def test_research_ci_gates_shared_numeric_pr_changes(self):
        workflow = (
            Path(__file__).resolve().parents[2]
            / ".github"
            / "workflows"
            / "research-primitives.yml"
        ).read_text(encoding="utf-8")
        pull_request_block = workflow.split("  pull_request:", 1)[1].split(
            "\n\n", 1
        )[0]
        self.assertIn('- "autotrade_numeric/**"', pull_request_block)
        self.assertIn('- "pyproject.toml"', pull_request_block)


if __name__ == "__main__":
    unittest.main()
