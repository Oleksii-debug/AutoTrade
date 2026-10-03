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
                r"trusted chronology signed receipt parser external class dependency changed: uuid\.UUID\.__init__\.int_",
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
                r"synthetic parser external class dependency changed: synthetic_external\.ExternalValue\.__init__\.helper",
            ):
                guard()
        finally:
            external_namespace["helper"] = original

        guard()


if __name__ == "__main__":
    unittest.main()
