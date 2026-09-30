import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
from tools import generate_common_scalar_corpus as corpus_generator


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

    def test_corpus_oracle_zero_integer_magnitude_matches_generated_contract(self):
        # Use a synthetic zero integer ceiling so current max=256 cannot mask drift.
        definition = self.common["$defs"]["Decimal"]
        with patch.object(corpus_generator, "_decimal_envelope", return_value=(1, 1, 0)):
            self.assertTrue(corpus_generator._within_decimal_envelope("0", definition))
            self.assertTrue(corpus_generator._within_decimal_envelope("0.1", definition))
            self.assertFalse(corpus_generator._within_decimal_envelope("1", definition))
            self.assertFalse(corpus_generator._within_decimal_envelope("1.1", definition))
            self.assertFalse(corpus_generator._within_decimal_envelope(0, definition))

    def test_shipped_scalar_component_is_hermetic_and_missing_scalar_fails_closed(self):
        # Narrow component oracle; WP-43 must still qualify the whole installed host.
        with TemporaryDirectory() as root_dir:
            root = Path(root_dir)
            stage = root / "stage"
            package = stage / "mvp" / "autotrade_mvp"
            package.mkdir(parents=True)
            (stage / "mvp" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            for filename in (
                "_generated_common_scalars.py",
                "_generated_decimal_limits.py",
                "exact_decimal.py",
            ):
                shutil.copy2(ROOT / "mvp" / "autotrade_mvp" / filename, package / filename)
            code = """
import os
import pathlib
import sys
assert sys.flags.isolated == 1 and sys.flags.no_site == 1
stage = pathlib.Path(os.environ['AUTOTRADE_STAGED_SCALARS']).resolve(strict=True)
sys.path.insert(0, str(stage))
try:
    from mvp.autotrade_mvp.exact_decimal import parse_canonical_decimal_text
    from mvp.autotrade_mvp._generated_common_scalars import is_valid_common_scalar
    if os.environ['AUTOTRADE_MISSING_SCALAR'] == '1':
        raise AssertionError('missing generated scalar was imported')
except ModuleNotFoundError as error:
    assert os.environ['AUTOTRADE_MISSING_SCALAR'] == '1', error
    assert error.name == 'mvp.autotrade_mvp._generated_common_scalars', error.name
    print('MISSING_SCALAR_DENIED')
else:
    assert str(parse_canonical_decimal_text('0.1')) == '0.1'
    assert is_valid_common_scalar('Environment', 'LIVE')
    assert not is_valid_common_scalar('Decimal', '1.0')
    for name, module in tuple(sys.modules.items()):
        if name in ('mvp', 'mvp.autotrade_mvp') or name.startswith('mvp.autotrade_mvp.'):
            path = pathlib.Path(module.__file__).resolve(strict=True)
            assert path.is_relative_to(stage), (name, path)
    assert not any(name == 'contracts' or name.startswith('contracts.') for name in sys.modules)
    print('HERMETIC_SCALARS_OK')
"""
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(ROOT)
            environment["AUTOTRADE_STAGED_SCALARS"] = str(stage)
            for missing, sentinel in ((False, "HERMETIC_SCALARS_OK"), (True, "MISSING_SCALAR_DENIED")):
                with self.subTest(missing=missing):
                    scalar = package / "_generated_common_scalars.py"
                    if missing:
                        scalar.unlink()
                    environment["AUTOTRADE_MISSING_SCALAR"] = "1" if missing else "0"
                    result = subprocess.run(
                        [sys.executable, "-I", "-S", "-c", code],
                        cwd=root,
                        env=environment,
                        text=True,
                        capture_output=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), sentinel)

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
