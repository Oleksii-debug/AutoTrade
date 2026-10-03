from __future__ import annotations

from types import ModuleType
import unittest

from mvp.autotrade_mvp import qualification_attestation as qualification_module
from mvp.autotrade_mvp import trusted_chronology as chronology_parser_module
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyParserModuleAttributeAuthorityTests(unittest.TestCase):
    def test_measurement_parser_rejects_json_loads_rebinding_before_execution(self) -> None:
        module = chronology_parser_module.json
        original = module.loads
        forged_calls = 0

        def forged(*_args, **_kwargs):
            nonlocal forged_calls
            forged_calls += 1
            module.loads = original
            return {}

        try:
            module.loads = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology measurement parser module attribute changed: json.loads",
            ):
                chronology.parse_challenge_bound_measurement(
                    b"{}",
                    challenge=object(),
                )
        finally:
            module.loads = original

        self.assertEqual(forged_calls, 0)

    def test_signed_receipt_parser_rejects_base64_decoder_rebinding_before_execution(self) -> None:
        module = qualification_module.base64
        original = module.b64decode
        forged_calls = 0

        def forged(*_args, **_kwargs):
            nonlocal forged_calls
            forged_calls += 1
            module.b64decode = original
            return b"forged"

        try:
            module.b64decode = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology signed receipt parser module attribute changed: base64.b64decode",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            module.b64decode = original

        self.assertEqual(forged_calls, 0)

    def test_external_guard_rejects_module_function_code_mutation_without_attr_rebinding(self) -> None:
        dependency = ModuleType("synthetic_dependency")

        def decode(value):
            return value

        def forged_decode(value):
            return "forged:" + value

        dependency.decode = decode
        namespace: dict[str, object] = {
            "__name__": "synthetic_parser_module",
            "dependency": dependency,
        }
        exec(
            "def root(value):\n"
            "    return dependency.decode(value)\n",
            namespace,
        )
        root = namespace["root"]
        guard = chronology._build_external_function_graph_guard(
            root=root,
            label="synthetic parser",
        )
        original_code = decode.__code__
        try:
            decode.__code__ = forged_decode.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic parser module function executable changed",
            ):
                guard()
        finally:
            decode.__code__ = original_code

        guard()
        self.assertEqual(root("x"), "x")


if __name__ == "__main__":
    unittest.main()
