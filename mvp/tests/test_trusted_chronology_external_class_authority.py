from __future__ import annotations

import unittest

from mvp.autotrade_mvp import qualification_attestation as qualification_module
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyExternalClassAuthorityTests(unittest.TestCase):
    def test_signed_receipt_parser_rejects_uuid_init_rebinding_before_execution(self) -> None:
        uuid_type = qualification_module.UUID
        original = uuid_type.__init__
        forged_calls = 0

        def forged(self, *args, **kwargs):
            nonlocal forged_calls
            forged_calls += 1
            uuid_type.__init__ = original
            return original(self, *args, **kwargs)

        try:
            uuid_type.__init__ = forged
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology signed receipt parser external class member changed: uuid\.UUID\.__init__",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            uuid_type.__init__ = original

        self.assertEqual(forged_calls, 0)

    def test_signed_receipt_parser_rejects_uuid_init_code_mutation_without_rebinding(self) -> None:
        uuid_type = qualification_module.UUID
        initializer = uuid_type.__init__
        original_code = initializer.__code__

        def forged(self, *args, **kwargs):
            raise AssertionError("forged UUID initializer executed")

        self.assertEqual(forged.__code__.co_freevars, ())
        try:
            initializer.__code__ = forged.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                r"trusted chronology signed receipt parser external class executable changed: uuid\.UUID\.__init__",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            initializer.__code__ = original_code

    def test_external_class_namespace_key_mutation_is_rejected(self) -> None:
        class ExternalValue:
            def __init__(self, value):
                self.value = value

        namespace: dict[str, object] = {
            "__name__": "synthetic_parser_module",
            "ExternalValue": ExternalValue,
        }
        exec(
            "def root(value):\n"
            "    return ExternalValue(value)\n",
            namespace,
        )
        guard = chronology._build_external_function_graph_guard(
            root=namespace["root"],
            label="synthetic parser",
        )
        try:
            ExternalValue.injected = object()
            with self.assertRaisesRegex(
                RuntimeError,
                r"synthetic parser external class namespace changed: .*ExternalValue",
            ):
                guard()
        finally:
            del ExternalValue.injected

        guard()


if __name__ == "__main__":
    unittest.main()
