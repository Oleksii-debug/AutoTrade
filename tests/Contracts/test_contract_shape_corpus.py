import json
import unittest
from pathlib import Path

from contracts.bindings.python.contract_shapes import (
    CONTRACT_VERSION,
    is_valid_contract_shape,
)


ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "contracts" / "fixtures" / "contract-shapes.corpus.json"


class ContractShapeCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))

    def test_shared_shape_corpus_version_and_coverage(self):
        self.assertEqual(self.corpus["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.corpus["scope"], "closed-object-shape-subset")
        cases = self.corpus["cases"]
        self.assertEqual(len(cases), self.corpus["case_count"])
        self.assertEqual(
            len({case["contract"] for case in cases}),
            self.corpus["definition_count"],
        )
        dimensions = {case["dimension"] for case in cases}
        self.assertEqual(
            dimensions,
            {"shape", "unknown-field", "required-field", "enum"},
        )
        names = [case["name"] for case in cases]
        self.assertEqual(len(names), len(set(names)))

    def test_python_binding_accepts_and_rejects_shared_shape_corpus(self):
        for case in self.corpus["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    is_valid_contract_shape(case["contract"], case["value"]),
                    case["expected"],
                )

    def test_binding_rejects_unknown_contract_identity(self):
        with self.assertRaisesRegex(ValueError, "unsupported closed-object contract"):
            is_valid_contract_shape("missing.schema.json#/$defs/Missing", {})


if __name__ == "__main__":
    unittest.main()
