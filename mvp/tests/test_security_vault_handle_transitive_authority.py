from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.provider_domain as provider_domain_module
import mvp.autotrade_mvp.windows_secrets as secrets_module
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
)


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"handle-transitive-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultHandleTransitiveAuthorityTests(unittest.TestCase):
    @staticmethod
    def _boundary(root: str):
        vault = ProtectedCredentialVault(
            Path(root) / "credentials.json",
            protector=DeterministicProtector(),
        )
        handle = vault.register(
            handle_id="cred-bybit-trade",
            owner_identity="host-a",
            account_id="account-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="canonical-secret",
        )
        boundary = SecurityBoundary(
            allowed_origins={_ORIGIN},
            credential_vault=vault,
            session_authorizer=lambda subject, role, origin: (
                subject == "host-a" and role == "OWNER" and origin == _ORIGIN
            ),
            now=lambda: 100.0,
        )
        session = boundary.create_session(
            subject="host-a",
            role="OWNER",
            origin=_ORIGIN,
            ttl_seconds=900,
        )
        return boundary, session.token, handle

    @staticmethod
    def _lease(boundary, token, handle):
        return boundary.lease_for_execution(
            token,
            origin=_ORIGIN,
            handle=handle,
            execution_identity="host-a",
            account_id="account-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
        )

    def _assert_canonical_plaintext(self, boundary, token, handle):
        with self._lease(boundary, token, handle) as plaintext:
            self.assertEqual(plaintext, "canonical-secret")

    def test_handle_post_init_rebinding_cannot_replace_terminal_handle_validation(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_post_init(_self):
                calls.append(True)
                raise AssertionError("rebound handle post-init executed")

            with patch.object(
                PersistentCredentialHandle,
                "__post_init__",
                hostile_post_init,
            ):
                self._assert_canonical_plaintext(boundary, token, handle)

            self.assertEqual(calls, [])

    def test_handle_text_global_rebinding_cannot_replace_terminal_validation(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_text(*_args, **_kwargs):
                calls.append(True)
                raise AssertionError("rebound handle text authority executed")

            with patch.object(secrets_module, "_text", hostile_text):
                self._assert_canonical_plaintext(boundary, token, handle)

            self.assertEqual(calls, [])

    def test_handle_provider_environment_global_rebinding_cannot_replace_terminal_validation(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_provider_environment(*_args, **_kwargs):
                calls.append(True)
                return "MAINNET"

            with patch.object(
                secrets_module,
                "_provider_environment",
                hostile_provider_environment,
            ):
                self._assert_canonical_plaintext(boundary, token, handle)

            self.assertEqual(calls, [])

    def test_handle_allowlist_global_rebinding_cannot_replace_terminal_scope_policy(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            with patch.object(
                secrets_module,
                "_ALLOWED_ENVIRONMENTS",
                frozenset({"EVIL"}),
            ), patch.object(
                secrets_module,
                "_ALLOWED_PURPOSES",
                frozenset({"EVIL"}),
            ):
                self._assert_canonical_plaintext(boundary, token, handle)

    def test_provider_domain_text_helper_rebinding_cannot_retarget_terminal_scope(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_environment_text(*_args, **_kwargs):
                calls.append(True)
                return "MAINNET"

            with patch.object(
                provider_domain_module,
                "_environment_text",
                hostile_environment_text,
            ):
                self._assert_canonical_plaintext(boundary, token, handle)

            self.assertEqual(calls, [])

    def test_provider_domain_runtime_allowlist_rebinding_cannot_retarget_terminal_scope(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            with patch.object(
                provider_domain_module,
                "_NORMALIZER_RUNTIME_ENVIRONMENTS",
                frozenset({"LIVE"}),
            ):
                self._assert_canonical_plaintext(boundary, token, handle)

    def test_provider_domain_bybit_allowlist_rebinding_cannot_retarget_terminal_scope(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            with patch.object(
                provider_domain_module,
                "_BYBIT_PROVIDER_ENVIRONMENTS",
                frozenset({"MAINNET"}),
            ):
                self._assert_canonical_plaintext(boundary, token, handle)


if __name__ == "__main__":
    unittest.main()
