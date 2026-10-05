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
    PREFIX = b"handle-attribute-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultHandleAttributeAuthorityTests(unittest.TestCase):
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

    def test_getattribute_rebinding_cannot_promote_stale_generation(self):
        with TemporaryDirectory() as root:
            boundary, token, stale, _rotated = self._boundary(root)
            original_getattribute = PersistentCredentialHandle.__getattribute__
            calls = []

            def hostile_getattribute(instance, name):
                calls.append(name)
                if name == "generation":
                    return 2
                return original_getattribute(instance, name)

            with patch.object(
                PersistentCredentialHandle,
                "__getattribute__",
                hostile_getattribute,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "Credential handle generation is stale",
                ):
                    with self._lease(boundary, token, stale):
                        self.fail("stale handle reached the rotated credential")

            self.assertEqual(calls, [])

    def test_getattribute_rebinding_cannot_intercept_current_handle_fields(self):
        with TemporaryDirectory() as root:
            boundary, token, _stale, rotated = self._boundary(root)
            original_getattribute = PersistentCredentialHandle.__getattribute__
            calls = []

            def hostile_getattribute(instance, name):
                calls.append(name)
                if name in {
                    "handle_id",
                    "account_id",
                    "provider",
                    "environment",
                    "purpose",
                    "generation",
                    "provider_environment",
                }:
                    raise AssertionError("mutable public handle attribute lookup executed")
                return original_getattribute(instance, name)

            with patch.object(
                PersistentCredentialHandle,
                "__getattribute__",
                hostile_getattribute,
            ):
                with self._lease(boundary, token, rotated) as plaintext:
                    self.assertEqual(plaintext, "generation-two-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
