from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_secrets as secrets_module
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"lease-global-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultLeaseGlobalAuthorityTests(unittest.TestCase):
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

    def test_file_lock_global_rebinding_cannot_replace_terminal_lock_authority(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            @contextmanager
            def hostile_lock(*_args, **_kwargs):
                calls.append(True)
                yield

            with patch.object(secrets_module, "_exclusive_file_lock", hostile_lock):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_scope_entropy_global_rebinding_cannot_replace_terminal_entropy(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_entropy(**_kwargs):
                calls.append(True)
                raise AssertionError("rebound scope entropy executed")

            with patch.object(secrets_module, "_scope_entropy", hostile_entropy):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_base64_decoder_global_rebinding_cannot_substitute_ciphertext(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_decode(*_args, **_kwargs):
                calls.append(True)
                return b"hostile-ciphertext"

            with patch.object(secrets_module, "b64decode", hostile_decode):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_vault_leaf_helper_global_rebinding_cannot_replace_load_authority(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_leaf(_path):
                calls.append(True)
                raise AssertionError("rebound vault leaf verifier executed")

            with patch.object(secrets_module, "_require_vault_leaf", hostile_leaf):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_handle_type_global_rebinding_cannot_replace_terminal_handle_authority(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)

            with patch.object(secrets_module, "PersistentCredentialHandle", object):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

    def test_scope_policy_class_attribute_rebinding_cannot_expand_terminal_scope(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            original_environments = ProtectedCredentialVault.ALLOWED_ENVIRONMENTS
            original_purposes = ProtectedCredentialVault.ALLOWED_PURPOSES
            try:
                ProtectedCredentialVault.ALLOWED_ENVIRONMENTS = frozenset({"EVIL"})
                ProtectedCredentialVault.ALLOWED_PURPOSES = frozenset({"EVIL"})
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")
            finally:
                ProtectedCredentialVault.ALLOWED_ENVIRONMENTS = original_environments
                ProtectedCredentialVault.ALLOWED_PURPOSES = original_purposes


if __name__ == "__main__":
    unittest.main()
