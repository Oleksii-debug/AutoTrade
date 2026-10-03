from __future__ import annotations

import unittest

from mvp.autotrade_mvp import qualification_attestation as qualification_module
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyExternalClassDependencyAuthorityTests(unittest.TestCase):
    def test_signed_receipt_parser_rejects_uuid_initializer_global_rebinding_before_parse(self) -> None:
        initializer = qualification_module.UUID.__init__
        namespace = initializer.__globals__
        original = namespace["int_"]
        forged_calls = 0

        def forged(value, base=10):
            nonlocal forged_calls
            forged_calls += 1
            return original(value, base)

        try:
            namespace["int_"] = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology signed receipt parser dependency changed: int_",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            namespace["int_"] = original

        self.assertEqual(forged_calls, 0)

    def test_external_class_member_global_dependency_is_sealed_and_recovers(self) -> None:
        external_namespace: dict[str, object] = {"__name__": "synthetic_external"}
        exec(
            "def helper(value):\n"
            "    return value\n"
            "class ExternalValue:\n"
            "    def __init__(self, value):\n"
            "        self.value = helper(value)\n",
            external_namespace,
        )
        parser_namespace: dict[str, object] = {
            "__name__": "synthetic_parser",
            "ExternalValue": external_namespace["ExternalValue"],
        }
        exec(
            "def root(value):\n"
            "    return ExternalValue(value)\n",
            parser_namespace,
        )
        guard = chronology._build_external_function_graph_guard(
            root=parser_namespace["root"],
            label="synthetic parser",
        )
        original = external_namespace["helper"]

        def forged(value):
            raise AssertionError("forged external helper executed")

        try:
            external_namespace["helper"] = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"synthetic parser dependency changed: helper",
            ):
                guard()
        finally:
            external_namespace["helper"] = original

        guard()

    def test_external_class_member_closure_dependency_is_sealed_and_recovers(self) -> None:
        def build_external_type():
            helper = lambda value: value

            class ExternalValue:
                def __init__(self, value):
                    self.value = helper(value)

            return ExternalValue

        external_type = build_external_type()
        initializer = external_type.__init__
        self.assertIsNotNone(initializer.__closure__)
        closure_cell = initializer.__closure__[0]
        original = closure_cell.cell_contents
        parser_namespace: dict[str, object] = {
            "__name__": "synthetic_parser_closure",
            "ExternalValue": external_type,
        }
        exec(
            "def root(value):\n"
            "    return ExternalValue(value)\n",
            parser_namespace,
        )
        guard = chronology._build_external_function_graph_guard(
            root=parser_namespace["root"],
            label="synthetic closure parser",
        )

        def forged(value):
            raise AssertionError("forged closure helper executed")

        try:
            closure_cell.cell_contents = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"synthetic closure parser closure changed",
            ):
                guard()
        finally:
            closure_cell.cell_contents = original

        guard()


if __name__ == "__main__":
    unittest.main()
