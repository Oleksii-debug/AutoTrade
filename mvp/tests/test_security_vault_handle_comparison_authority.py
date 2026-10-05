from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
)


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"handle-comparison-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultHandleComparisonAuthorityTests(unittest.TestCase):
    @staticmethod
    def _boundary(root: str):
        vault = ProtectedCredentialVault(
            Path(root) / "credentials.json",
            protector=DeterministicProtector(),
        )
        original = vault.register(
            handle_id="cred-bybit-trade",
            owner_identity="host-a",
            account_id="account-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="generation-one-secret",
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
        rotated = vault.rotate(
            original,
            execution_identity="host-a",
            new_secret_value="generation-two-secret",
        )
        return boundary, session.token, original, rotated

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

    def test_eq_rebinding_cannot_promote_stale_generation_to_current(self):
        with TemporaryDirectory() as root:
            boundary, token, stale, _rotated = self._boundary(root)
            calls = []

            def hostile_eq(_self, _other):
                calls.append(True)
                return True

            with patch.object(PersistentCredentialHandle, "__eq__", hostile_eq):
                with self.assertRaisesRegex(
                    PermissionError,
                    "Credential handle generation is stale",
                ):
                    with self._lease(boundary, token, stale):
                        self.fail("stale handle reached the rotated credential")

            self.assertEqual(calls, [])

    def test_ne_rebinding_cannot_promote_stale_generation_to_current(self):
        with TemporaryDirectory() as root:
            boundary, token, stale, _rotated = self._boundary(root)
            calls = []

            def hostile_ne(_self, _other):
                calls.append(True)
                return False

            with patch.object(PersistentCredentialHandle, "__ne__", hostile_ne):
                with self.assertRaisesRegex(
                    PermissionError,
                    "Credential handle generation is stale",
                ):
                    with self._lease(boundary, token, stale):
                        self.fail("stale handle reached the rotated credential")

            self.assertEqual(calls, [])

    def test_eq_rebinding_cannot_break_current_generation_validation(self):
        with TemporaryDirectory() as root:
            boundary, token, _stale, rotated = self._boundary(root)
            calls = []

            def hostile_eq(_self, _other):
                calls.append(True)
                raise AssertionError("public handle equality executed")

            with patch.object(PersistentCredentialHandle, "__eq__", hostile_eq):
                with self._lease(boundary, token, rotated) as plaintext:
                    self.assertEqual(plaintext, "generation-two-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
