import json
from pathlib import Path
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from contracts.bindings.python.common_scalars import (
    CONTRACT_VERSION,
    is_valid_common_scalar,
)


ROOT = Path(__file__).resolve().parents[2]
COMMON = ROOT / "contracts" / "jsonschema" / "common.schema.json"
MANIFEST = ROOT / "contracts" / "manifest.json"
CORPUS = ROOT / "contracts" / "fixtures" / "common-scalars.corpus.json"


class CommonScalarConformanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.common = json.loads(COMMON.read_text(encoding="utf-8"))
        cls.manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        cls.corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
        cls.registry = Registry().with_resource(
            cls.common["$id"], Resource.from_contents(cls.common)
        )

    def test_corpus_is_bound_to_contract_version(self):
        self.assertEqual(
            self.corpus["contract_version"],
            self.manifest["contract_version"],
        )
        self.assertEqual(CONTRACT_VERSION, self.manifest["contract_version"])
        self.assertEqual(self.corpus["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.corpus["corpus_version"], CONTRACT_VERSION)
        base = self.manifest["schema_base_uri"].rstrip("/") + "/"
        self.assertEqual(self.common["$id"], base + "common.schema.json")
        self.assertEqual(base.rstrip("/").split("/")[-1], CONTRACT_VERSION)
        self.assertEqual(self.corpus["scope"], "common-scalar-subset")

    def test_python_binding_and_json_schema_accept_identical_corpus(self):
        names = set()
        for case in self.corpus["cases"]:
            with self.subTest(case=case["name"]):
                self.assertNotIn(case["name"], names)
                names.add(case["name"])
                definition = case["type"]
                validator = Draft202012Validator(
                    {"$ref": f"{self.common['$id']}#/$defs/{definition}"},
                    registry=self.registry,
                )
                schema_result = validator.is_valid(case["value"])
                binding_result = is_valid_common_scalar(definition, case["value"])
                self.assertEqual(schema_result, case["expected"])
                self.assertEqual(binding_result, case["expected"])


    def test_every_regex_scalar_rejects_terminal_line_endings(self):
        by_name = {case["name"]: case for case in self.corpus["cases"]}
        scalar_prefixes = ("decimal", "sequence", "digest", "currency", "unit")
        for prefix in scalar_prefixes:
            for suffix in ("terminal-lf", "terminal-crlf"):
                name = f"{prefix}-{suffix}"
                with self.subTest(case=name):
                    case = by_name[name]
                    self.assertFalse(case["expected"])
                    validator = Draft202012Validator(
                        {"$ref": f"{self.common['$id']}#/$defs/{case['type']}"},
                        registry=self.registry,
                    )
                    self.assertFalse(validator.is_valid(case["value"]))
                    self.assertFalse(
                        is_valid_common_scalar(case["type"], case["value"])
                    )


if __name__ == "__main__":
    unittest.main()
