import json
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from contracts.bindings.python.common_scalars import (
    CONTRACT_VERSION,
    _within_decimal_envelope as contract_within_decimal_envelope,
    is_valid_common_scalar,
)
from mvp.autotrade_mvp._generated_common_scalars import (
    CONTRACT_VERSION as MVP_COMMON_SCALAR_CONTRACT_VERSION,
    _within_decimal_envelope as mvp_within_decimal_envelope,
    is_valid_common_scalar as mvp_is_valid_common_scalar,
)
from mvp.autotrade_mvp._generated_decimal_limits import (
    CONTRACT_VERSION as MVP_DECIMAL_CONTRACT_VERSION,
    MAX_DECIMAL_TEXT_LENGTH,
    MAX_INTEGER_DIGITS,
    MAX_SCALE,
    MAX_SIGNIFICANT_DIGITS,
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
        self.assertEqual(MVP_COMMON_SCALAR_CONTRACT_VERSION, CONTRACT_VERSION)
        self.assertEqual(MVP_DECIMAL_CONTRACT_VERSION, CONTRACT_VERSION)
        self.assertEqual(self.corpus["contract_version"], CONTRACT_VERSION)
        self.assertEqual(self.corpus["corpus_version"], CONTRACT_VERSION)
        base = self.manifest["schema_base_uri"].rstrip("/") + "/"
        self.assertEqual(self.common["$id"], base + "common.schema.json")
        self.assertEqual(base.rstrip("/").split("/")[-1], CONTRACT_VERSION)
        self.assertEqual(self.corpus["scope"], "common-scalar-subset")

    def test_python_bindings_and_json_schema_accept_identical_corpus(self):
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
                shipped_result = mvp_is_valid_common_scalar(
                    definition, case["value"]
                )
                if case["expected"]:
                    self.assertTrue(schema_result)
                elif definition != "Decimal":
                    self.assertFalse(schema_result)
                elif schema_result:
                    self.assertIn(
                        "x-autotrade-decimal-envelope",
                        self.common["$defs"]["Decimal"],
                    )
                self.assertEqual(binding_result, case["expected"])
                self.assertEqual(shipped_result, binding_result)

    def test_decimal_resource_envelope_is_schema_owned_and_exact(self):
        decimal = self.common["$defs"]["Decimal"]
        self.assertEqual(decimal["maxLength"], 259)
        self.assertEqual(
            decimal["x-autotrade-decimal-envelope"],
            {
                "max_significant_digits": 256,
                "max_scale": 256,
                "max_integer_digits": 256,
            },
        )
        self.assertEqual(MAX_DECIMAL_TEXT_LENGTH, decimal["maxLength"])
        self.assertEqual(
            (
                MAX_SIGNIFICANT_DIGITS,
                MAX_SCALE,
                MAX_INTEGER_DIGITS,
            ),
            (
                decimal["x-autotrade-decimal-envelope"]["max_significant_digits"],
                decimal["x-autotrade-decimal-envelope"]["max_scale"],
                decimal["x-autotrade-decimal-envelope"]["max_integer_digits"],
            ),
        )
        required = {
            "decimal-significant-at-limit": True,
            "decimal-significant-over-limit": False,
            "decimal-integer-at-limit": True,
            "decimal-integer-over-limit": False,
            "decimal-scale-at-limit": True,
            "decimal-scale-over-limit": False,
            "decimal-mixed-significant-over-limit": False,
            "decimal-negative-integer-at-limit": True,
            "decimal-negative-scale-at-limit": True,
            "decimal-raw-length-at-limit": True,
            "decimal-raw-length-over-limit": False,
        }
        actual = {
            case["name"]: case["expected"]
            for case in self.corpus["cases"]
            if case["name"] in required
        }
        self.assertEqual(actual, required)

    def test_zero_integer_magnitude_is_zero_in_both_python_placements(self):
        limits = (1, 1, 0)
        for helper in (
            contract_within_decimal_envelope,
            mvp_within_decimal_envelope,
        ):
            with self.subTest(helper=helper.__module__):
                self.assertTrue(helper("0", limits))
                self.assertTrue(helper("0.1", limits))
                self.assertFalse(helper("1", limits))

    def test_python_binding_rejects_string_subclasses_before_virtual_dispatch(self):
        class HostileText(str):
            def __len__(self):
                raise AssertionError("hostile __len__ was dispatched")

        value = HostileText("1.25")
        self.assertFalse(is_valid_common_scalar("Decimal", value))
        self.assertFalse(mvp_is_valid_common_scalar("Decimal", value))

    def test_shipped_exact_decimal_import_does_not_require_contracts_package(self):
        code = textwrap.dedent(
            """
            import importlib.abc
            import sys

            class DenyContracts(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "contracts" or fullname.startswith("contracts."):
                        raise ImportError("contracts package deliberately denied")
                    return None

            for name in tuple(sys.modules):
                if name == "contracts" or name.startswith("contracts."):
                    del sys.modules[name]
            sys.meta_path.insert(0, DenyContracts())
            from mvp.autotrade_mvp.exact_decimal import parse_canonical_decimal_text
            assert str(parse_canonical_decimal_text("1.25")) == "1.25"
            """
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"stdout={result.stdout}\nstderr={result.stderr}",
        )

    def test_utc_instant_terminal_lf_and_crlf_fail_at_schema_boundary(self):
        validator = Draft202012Validator(
            {"$ref": f"{self.common['$id']}#/$defs/UtcInstant"},
            registry=self.registry,
        )
        valid = "2026-09-29T01:02:03Z"
        self.assertTrue(validator.is_valid(valid))
        for suffix in ("\n", "\r\n"):
            with self.subTest(suffix=repr(suffix)):
                self.assertFalse(validator.is_valid(valid + suffix))


if __name__ == "__main__":
    unittest.main()
