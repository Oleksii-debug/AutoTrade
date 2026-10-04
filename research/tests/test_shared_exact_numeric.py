from decimal import Decimal
from fractions import Fraction
from pathlib import Path
import unittest

from autotrade_numeric import (
    MAX_INTEGER_DIGITS, MAX_RATIONAL_DIGITS, MAX_SCALE, MAX_SIGNIFICANT_DIGITS,
    as_fraction, bounded_fraction, parse_canonical_decimal_text,
)
from autotrade_numeric._generated_common_scalars import CONTRACT_VERSION


class SharedResearchNumericDependencyTests(unittest.TestCase):
    def test_research_consumes_the_identical_shipped_v5_numeric_authority(self):
        self.assertEqual((MAX_SIGNIFICANT_DIGITS, MAX_SCALE, MAX_INTEGER_DIGITS, MAX_RATIONAL_DIGITS), (256, 256, 256, 1024))
        self.assertEqual(CONTRACT_VERSION, "5.0.0")
        self.assertEqual(as_fraction(Decimal("0.125")), Fraction(1, 8))
        self.assertEqual(bounded_fraction(Fraction(1, 3)), Fraction(1, 3))
        self.assertEqual(parse_canonical_decimal_text("1.25"), Decimal("1.25"))

    def test_research_ci_gates_shared_numeric_and_generator_changes(self):
        workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "research-primitives.yml").read_text(encoding="utf-8")
        target = workflow.split("  pull_request:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn('- "autotrade_numeric/**"', target)
        self.assertIn('- "pyproject.toml"', target)
        self.assertIn('- "tools/generate_common_scalar_bindings.py"', target)


if __name__ == "__main__":
    unittest.main()
