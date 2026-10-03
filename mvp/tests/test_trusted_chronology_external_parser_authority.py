from __future__ import annotations

import unittest

from mvp.autotrade_mvp import qualification_attestation as qualification_module
from mvp.autotrade_mvp import trusted_chronology as chronology_parser_module
import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyExternalParserAuthorityTests(unittest.TestCase):
    def test_transitive_same_module_helper_rebinding_is_rejected_before_execution(self) -> None:
        namespace: dict[str, object] = {"__name__": "synthetic_parser_module"}
        exec(
            "def helper(value):\n"
            "    return value\n\n"
            "def root(value):\n"
            "    return helper(value)\n",
            namespace,
        )
        helper = namespace["helper"]
        root = namespace["root"]
        guard = chronology._build_external_function_graph_guard(
            root=root,
            label="synthetic parser",
        )
        forged_calls = 0

        def forged(value):
            nonlocal forged_calls
            forged_calls += 1
            namespace["helper"] = helper
            return value

        try:
            namespace["helper"] = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "synthetic parser dependency changed: helper",
            ):
                guard()
        finally:
            namespace["helper"] = helper

        self.assertEqual(forged_calls, 0)
        guard()

    def test_production_measurement_parser_rejects_helper_rebinding_before_parse(self) -> None:
        original = chronology_parser_module._strict_json_object
        forged_calls = 0

        def forged(_raw):
            nonlocal forged_calls
            forged_calls += 1
            chronology_parser_module._strict_json_object = original
            return {}

        try:
            chronology_parser_module._strict_json_object = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology measurement parser dependency changed: _strict_json_object",
            ):
                chronology.parse_challenge_bound_measurement(
                    b"{}",
                    challenge=object(),
                )
        finally:
            chronology_parser_module._strict_json_object = original

        self.assertEqual(forged_calls, 0)

    def test_production_signed_receipt_parser_rejects_helper_rebinding_before_parse(self) -> None:
        original = qualification_module._strict_mapping
        forged_calls = 0

        def forged(*_args, **_kwargs):
            nonlocal forged_calls
            forged_calls += 1
            qualification_module._strict_mapping = original
            return {}

        try:
            qualification_module._strict_mapping = forged
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology signed receipt parser dependency changed: _strict_mapping",
            ):
                chronology.parse_signed_qualification_attestation({})
        finally:
            qualification_module._strict_mapping = original

        self.assertEqual(forged_calls, 0)

    def test_production_measurement_parser_rejects_root_code_substitution(self) -> None:
        root = chronology_parser_module.parse_challenge_bound_measurement
        original_code = root.__code__

        def forged(data, *, challenge):
            raise AssertionError("forged measurement parser executed")

        self.assertEqual(forged.__code__.co_freevars, ())
        try:
            root.__code__ = forged.__code__
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology measurement parser executable changed",
            ):
                chronology.parse_challenge_bound_measurement(
                    b"{}",
                    challenge=object(),
                )
        finally:
            root.__code__ = original_code

    def test_guarded_parser_rechecks_graph_after_successful_parser_execution(self) -> None:
        state = {"changed": False}
        guard_calls: list[bool] = []

        def guard() -> None:
            guard_calls.append(state["changed"])
            if state["changed"]:
                raise RuntimeError("parser graph changed during parse")

        def parser(value):
            state["changed"] = True
            return value

        guarded = chronology._build_guarded_parser(
            parser=parser,
            require_parser_authority=guard,
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "parser graph changed during parse",
        ):
            guarded("accepted-looking-result")

        self.assertEqual(guard_calls, [False, True])


if __name__ == "__main__":
    unittest.main()
