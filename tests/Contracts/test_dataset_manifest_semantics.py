import json
import tomllib
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from contracts.bindings.python.dataset_manifest import (
    CONTRACT_VERSION,
    DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID,
    is_valid_dataset_manifest_semantics,
)
from autotrade_numeric.dataset_manifest import (
    CONTRACT_VERSION as INSTALLED_CONTRACT_VERSION,
    DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID as INSTALLED_VALIDATOR_ID,
    is_valid_dataset_manifest_semantics as installed_is_valid_dataset_manifest_semantics,
)

ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ROOT / "contracts" / "jsonschema"
FIXTURES = ROOT / "contracts" / "fixtures"
CONTRACT_MANIFEST = ROOT / "contracts" / "manifest.json"
VALIDATOR_ID = "dataset-manifest-content-authority-v1"
PACKAGE_VERSION = "0.0.2"


class DatasetManifestSemanticContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in SCHEMAS.glob("*.json")
        }
        cls.registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in cls.schemas.values()]
        )
        schema = cls.schemas["data.schema.json"]
        cls.validator = Draft202012Validator(
            {"$ref": f"{schema['$id']}#/$defs/DatasetManifest"},
            registry=cls.registry,
            format_checker=FormatChecker(),
        )
        cls.fixture = json.loads(
            (FIXTURES / "dataset-manifest.valid.json").read_text(encoding="utf-8")
        )
        cls.corpus = json.loads(
            (FIXTURES / "dataset-manifest.semantic.corpus.json").read_text(
                encoding="utf-8"
            )
        )

    def test_contract_declares_versioned_semantic_validator(self):
        schema = self.schemas["data.schema.json"]["$defs"]["DatasetManifest"]
        contract_manifest = json.loads(CONTRACT_MANIFEST.read_text(encoding="utf-8"))
        self.assertEqual(CONTRACT_VERSION, contract_manifest["contract_version"])
        self.assertEqual(DATASET_MANIFEST_SEMANTIC_VALIDATOR_ID, VALIDATOR_ID)
        self.assertEqual(schema["x-autotrade-semantic-validator"], VALIDATOR_ID)
        declarations = contract_manifest["semantic_validators"]
        self.assertEqual([item["id"] for item in declarations], [VALIDATOR_ID])
        self.assertEqual(declarations[0]["definition"], "DatasetManifest")
        self.assertEqual(INSTALLED_CONTRACT_VERSION, CONTRACT_VERSION)
        self.assertEqual(INSTALLED_VALIDATOR_ID, VALIDATOR_ID)
        self.assertEqual(
            declarations[0]["installed_bindings"]["python"],
            "autotrade_numeric/dataset_manifest.py",
        )
        root_package = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        research_package = tomllib.loads(
            (ROOT / "research" / "pyproject.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(root_package["project"]["name"], "autotrade-exact-numeric")
        self.assertEqual(root_package["project"]["version"], PACKAGE_VERSION)
        self.assertIn(
            f"autotrade-exact-numeric=={PACKAGE_VERSION}",
            research_package["project"]["dependencies"],
        )

    def test_canonical_fixture_passes_structure_and_semantics(self):
        self.assertTrue(self.validator.is_valid(self.fixture))
        self.assertTrue(is_valid_dataset_manifest_semantics(self.fixture))

    def test_partial_or_disjoint_arrays_are_structurally_valid_but_semantically_invalid(self):
        disjoint = json.loads(json.dumps(self.fixture))
        disjoint["source_evidence"][0]["sha256"] = "sha256:" + "d" * 64
        self.assertTrue(
            self.validator.is_valid(disjoint),
            "JSON Schema cannot express sibling-array digest equality; semantic validator must own it",
        )
        self.assertFalse(is_valid_dataset_manifest_semantics(disjoint))

        partial = json.loads(json.dumps(self.fixture))
        partial["content_hashes"].append("sha256:" + "e" * 64)
        self.assertTrue(self.validator.is_valid(partial))
        self.assertFalse(is_valid_dataset_manifest_semantics(partial))

    def test_shared_semantic_corpus(self):
        self.assertEqual(self.corpus["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.corpus["validator_id"], VALIDATOR_ID)
        names = set()
        for case in self.corpus["cases"]:
            with self.subTest(case=case["name"]):
                self.assertNotIn(case["name"], names)
                names.add(case["name"])
                self.assertEqual(
                    is_valid_dataset_manifest_semantics(case["value"]),
                    case["expected"],
                )
                self.assertEqual(
                    installed_is_valid_dataset_manifest_semantics(case["value"]),
                    case["expected"],
                )


if __name__ == "__main__":
    unittest.main()
