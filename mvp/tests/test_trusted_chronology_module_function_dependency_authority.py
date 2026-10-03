from __future__ import annotations

import base64
import json
import unittest

import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyModuleFunctionDependencyAuthorityTests(unittest.TestCase):
    def test_measurement_parser_rejects_json_loads_global_rebinding_before_parse(self) -> None:
        namespace = json.loads.__globals__
        original = namespace["_default_decoder"]

        class ForgedDecoder:
            calls = 0

            def decode(self, value):
                type(self).calls += 1
                return original.decode(value)

        forged = ForgedDecoder()
        try:
            namespace["_default_decoder"] = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology measurement parser dependency changed: _default_decoder",
            ):
                chronology.parse_challenge_bound_measurement(b"", challenge=None)
        finally:
            namespace["_default_decoder"] = original

        self.assertEqual(ForgedDecoder.calls, 0)

    def test_signed_receipt_parser_rejects_b64decode_global_rebinding_before_parse(self) -> None:
        namespace = base64.b64decode.__globals__
        original = namespace["_bytes_from_decode_data"]
        forged_calls = 0

        def forged(value):
            nonlocal forged_calls
            forged_calls += 1
            return original(value)

        try:
            namespace["_bytes_from_decode_data"] = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology signed receipt parser dependency changed: _bytes_from_decode_data",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            namespace["_bytes_from_decode_data"] = original

        self.assertEqual(forged_calls, 0)

    def test_direct_module_function_closure_dependency_is_sealed_and_recovers(self) -> None:
        class SyntheticModule:
            pass

        module = SyntheticModule()
        module.__name__ = "synthetic_module"

        def make_module_function():
            helper = lambda value: value

            def decode(value):
                return helper(value)

            return decode

        module.decode = make_module_function()

        parser_namespace: dict[str, object] = {
            "__name__": "synthetic_parser_module",
            "external": module,
        }
        exec(
            "def root(value):\n"
            "    return external.decode(value)\n",
            parser_namespace,
        )

        # The graph recognises real module objects only. Use a tiny dynamically
        # created ModuleType so the direct module.attr bytecode path is exercised.
        from types import ModuleType

        real_module = ModuleType("synthetic_module")
        real_module.decode = module.decode
        parser_namespace["external"] = real_module
        guard = chronology._build_external_function_graph_guard(
            root=parser_namespace["root"],
            label="synthetic module parser",
        )

        function = real_module.decode
        self.assertIsNotNone(function.__closure__)
        cell = function.__closure__[0]
        original = cell.cell_contents

        def forged(value):
            raise AssertionError("forged module closure helper executed")

        try:
            cell.cell_contents = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"synthetic module parser closure changed",
            ):
                guard()
        finally:
            cell.cell_contents = original

        guard()


if __name__ == "__main__":
    unittest.main()
