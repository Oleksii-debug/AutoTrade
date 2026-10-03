from __future__ import annotations

from hashlib import sha256
import unittest

import mvp.autotrade_mvp.trusted_chronology_cut as chronology


class TrustedChronologyCallbackReadIntegrityTests(unittest.TestCase):
    def test_reader_callback_cannot_leave_self_restoring_hash_authority_for_caller(self) -> None:
        namespace: dict[str, object] = {"sha256": sha256}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        forged_calls = 0

        def forged_sha256(_raw):
            nonlocal forged_calls
            forged_calls += 1
            namespace["sha256"] = sha256
            return sha256(b"forged")

        def reader_factory(*_args, **_kwargs):
            def reader(_artifact_id):
                namespace["sha256"] = forged_sha256
                return {"artifact_id": "evidence"}, b"tampered"

            return reader

        _verifier, guarded_factory = chronology._build_callback_authority_wrappers(
            canonical_verifier=lambda *_args, **_kwargs: object(),
            authenticated_reader_factory=reader_factory,
            require_callback_authority=guard,
        )
        reader = guarded_factory("root")
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation authority changed: sha256",
            ):
                reader("evidence")
        finally:
            namespace["sha256"] = sha256

        self.assertEqual(forged_calls, 0)
        guard()

    def test_verifier_callback_cannot_leave_forged_requirement_helper_for_later_use(self) -> None:
        original_calls = 0
        forged_calls = 0

        def requirement_helper(_challenge, _raw):
            nonlocal original_calls
            original_calls += 1
            return "original"

        def forged_requirement_helper(_challenge, _raw):
            nonlocal forged_calls
            forged_calls += 1
            namespace["requirement_helper"] = requirement_helper
            return "forged"

        namespace: dict[str, object] = {
            "requirement_helper": requirement_helper,
        }
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )

        def verifier(*_args, **_kwargs):
            namespace["requirement_helper"] = forged_requirement_helper
            return object()

        guarded_verifier, _factory = chronology._build_callback_authority_wrappers(
            canonical_verifier=verifier,
            authenticated_reader_factory=lambda *_args, **_kwargs: (lambda *_a: ({}, b"x")),
            require_callback_authority=guard,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation authority changed: requirement_helper",
            ):
                guarded_verifier(object())
        finally:
            namespace["requirement_helper"] = requirement_helper

        self.assertEqual(original_calls, 0)
        self.assertEqual(forged_calls, 0)
        guard()

    def test_impl_guard_rejects_function_code_substitution_without_rebinding(self) -> None:
        def authority_function() -> int:
            return 1

        def forged_function() -> int:
            return 2

        namespace: dict[str, object] = {"authority_function": authority_function}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        original_code = authority_function.__code__
        try:
            authority_function.__code__ = forged_function.__code__
            self.assertEqual(namespace["authority_function"](), 2)
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation executable changed: authority_function",
            ):
                guard()
        finally:
            authority_function.__code__ = original_code

        guard()
        self.assertEqual(authority_function(), 1)

    def test_impl_guard_rejects_closure_cell_retargeting_without_function_rebinding(self) -> None:
        original = object()
        forged = object()

        def make_authority():
            selected = original

            def authority_function():
                return selected

            return authority_function

        authority_function = make_authority()
        namespace: dict[str, object] = {"authority_function": authority_function}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )
        cell = authority_function.__closure__[0]
        try:
            cell.cell_contents = forged
            self.assertIs(authority_function(), forged)
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation closure changed: authority_function",
            ):
                guard()
        finally:
            cell.cell_contents = original

        guard()
        self.assertIs(authority_function(), original)

    def test_callback_wrappers_fail_closed_when_reader_factory_changes_authority(self) -> None:
        original = object()
        namespace: dict[str, object] = {"authority": original}
        guard = chronology._build_impl_namespace_guard(
            namespace,
            excluded_names=frozenset(),
        )

        def reader_factory(*_args, **_kwargs):
            namespace["authority"] = object()
            return lambda *_reader_args: ({}, b"x")

        _verifier, guarded_factory = chronology._build_callback_authority_wrappers(
            canonical_verifier=lambda *_args, **_kwargs: object(),
            authenticated_reader_factory=reader_factory,
            require_callback_authority=guard,
        )
        try:
            with self.assertRaisesRegex(
                RuntimeError,
                "trusted chronology implementation authority changed: authority",
            ):
                guarded_factory("root")
        finally:
            namespace["authority"] = original

        guard()


if __name__ == "__main__":
    unittest.main()
