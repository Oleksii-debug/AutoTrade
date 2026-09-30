import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from tempfile import TemporaryDirectory
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

from tools.generate_common_scalar_corpus import (
    _within_decimal_envelope as corpus_within_decimal_envelope,
    _within_decimal_geometry as corpus_within_decimal_geometry,
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

    def test_corpus_decimal_zero_geometry_agrees_with_shipped_bindings(self):
        # Production envelope limits are large enough to mask treating the
        # zero integer-part token as one digit. Use a direct geometry cut
        # with max integer magnitude zero to protect this invariant.
        limits = (1, 1, 0)
        for helper in (
            corpus_within_decimal_geometry,
            contract_within_decimal_envelope,
            mvp_within_decimal_envelope,
        ):
            with self.subTest(helper=helper.__module__):
                for value, expected in (
                    ("0", True),
                    ("0.1", True),
                    ("-0.1", True),
                    ("1", False),
                    ("-1", False),
                    ("0.11", False),
                    ("0.01", False),
                ):
                    with self.subTest(value=value):
                        self.assertIs(helper(value, limits), expected)

    def test_corpus_schema_envelope_and_binding_agree_on_zero_and_limits(self):
        definition = self.common["$defs"]["Decimal"]
        cases = (
            "0",
            "0.1",
            "-0.1",
            "1",
            "-1",
            "0." + "0" * (MAX_SCALE - 1) + "1",
            "-0." + "0" * (MAX_SCALE - 1) + "1",
            "0." + "0" * MAX_SCALE + "1",
            "9" * MAX_INTEGER_DIGITS,
            "9" * (MAX_INTEGER_DIGITS + 1),
        )
        for value in cases:
            with self.subTest(value_head=value[:12], length=len(value)):
                result = corpus_within_decimal_envelope(value, definition)
                self.assertEqual(
                    result,
                    contract_within_decimal_envelope(
                        value,
                        (MAX_SIGNIFICANT_DIGITS, MAX_SCALE, MAX_INTEGER_DIGITS),
                    ),
                )
                self.assertEqual(
                    result,
                    mvp_within_decimal_envelope(
                        value,
                        (MAX_SIGNIFICANT_DIGITS, MAX_SCALE, MAX_INTEGER_DIGITS),
                    ),
                )

    def test_python_binding_rejects_string_subclasses_before_virtual_dispatch(self):
        class HostileText(str):
            def __len__(self):
                raise AssertionError("hostile __len__ was dispatched")

        value = HostileText("1.25")
        self.assertFalse(is_valid_common_scalar("Decimal", value))
        self.assertFalse(mvp_is_valid_common_scalar("Decimal", value))

    def _staged_exact_decimal_probe(self, *, include_scalar: bool) -> subprocess.CompletedProcess:
        """Exercise only shipped numeric components, never the checkout or site.

        The real installed-host distribution oracle remains a separate WP-43
        gate; this guards the numeric component's direct import dependency.
        """
        with TemporaryDirectory() as directory:
            temporary = Path(directory)
            staging = temporary / "installed-component"
            package = staging / "mvp" / "autotrade_mvp"
            package.mkdir(parents=True)
            # The full MVP package initializer loads product runtime modules;
            # this component test deliberately has only synthetic package
            # markers and the exact three shipped first-party source files.
            (staging / "mvp" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            source = ROOT / "mvp" / "autotrade_mvp"
            filenames = ["exact_decimal.py", "_generated_decimal_limits.py"]
            if include_scalar:
                filenames.append("_generated_common_scalars.py")
            for name in filenames:
                shutil.copy2(source / name, package / name)

            script = textwrap.dedent(
                """
                import importlib.abc
                import os
                from pathlib import Path
                import sys

                assert sys.flags.isolated == 1
                assert sys.flags.no_site == 1
                staging = Path(os.environ["AUTOTRADE_STAGING"]).resolve(strict=True)
                sys.path.insert(0, str(staging))

                class DenyContracts(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == "contracts" or fullname.startswith("contracts."):
                            raise AssertionError("checkout contracts package was accessed")
                        return None

                sys.meta_path.insert(0, DenyContracts())
                for name in tuple(sys.modules):
                    assert name != "contracts" and not name.startswith("contracts.")

                missing_scalar = os.environ["AUTOTRADE_MISSING_SCALAR"] == "1"
                try:
                    from mvp.autotrade_mvp.exact_decimal import parse_canonical_decimal_text
                except ModuleNotFoundError as error:
                    if missing_scalar and error.name == (
                        "mvp.autotrade_mvp._generated_common_scalars"
                    ):
                        print("MISSING_SCALAR_FAIL_CLOSED")
                        sys.exit(0)
                    raise
                if missing_scalar:
                    raise AssertionError("missing shipped scalar fell back to another install")

                from mvp.autotrade_mvp._generated_common_scalars import (
                    is_valid_common_scalar,
                )
                assert is_valid_common_scalar("Decimal", "0.1")
                assert str(parse_canonical_decimal_text("1.25")) == "1.25"
                for name, module in tuple(sys.modules.items()):
                    if name == "mvp" or name.startswith("mvp."):
                        filename = getattr(module, "__file__", None)
                        assert filename is not None, name
                        origin = Path(filename).resolve(strict=True)
                        assert origin.is_relative_to(staging), (name, origin, staging)
                    assert name != "contracts" and not name.startswith("contracts.")
                print("STAGED_SCALAR_HERMETIC_OK")
                """
            )
            environment = os.environ.copy()
            # Deliberately poison the ambient import path. -I -S must ignore it.
            environment["PYTHONPATH"] = str(ROOT)
            environment.pop("PYTHONHOME", None)
            environment["AUTOTRADE_STAGING"] = str(staging)
            environment["AUTOTRADE_MISSING_SCALAR"] = (
                "0" if include_scalar else "1"
            )
            return subprocess.run(
                [sys.executable, "-I", "-S", "-c", script],
                cwd=temporary,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )

    def test_shipped_scalar_import_is_hermetic_without_checkout_or_site(self):
        result = self._staged_exact_decimal_probe(include_scalar=True)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "STAGED_SCALAR_HERMETIC_OK")

    def test_missing_shipped_scalar_cannot_fall_back_to_checkout_or_site(self):
        result = self._staged_exact_decimal_probe(include_scalar=False)
        self.assertEqual(result.returncode, 0, msg=result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), "MISSING_SCALAR_FAIL_CLOSED")

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
