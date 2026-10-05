from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.security as security_module
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"private-view-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultPrivateViewAuthorityTests(unittest.TestCase):
    @staticmethod
    def _vault(path: Path, *, secret: str):
        vault = ProtectedCredentialVault(path, protector=DeterministicProtector())
        handle = vault.register(
            handle_id="cred-bybit-trade",
            owner_identity="host-a",
            account_id="account-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value=secret,
        )
        return vault, handle

    @staticmethod
    def _boundary(vault):
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
        return boundary, session.token

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

    def test_retained_view_load_class_rebinding_cannot_retarget_durable_state(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            canonical_vault, canonical_handle = self._vault(
                root_path / "canonical.json",
                secret="canonical-secret",
            )
            alternate_vault, _alternate_handle = self._vault(
                root_path / "alternate.json",
                secret="alternate-secret",
            )
            boundary, token = self._boundary(canonical_vault)
            calls = []

            def hostile_load(_self):
                calls.append(True)
                return alternate_vault._load()

            with patch.object(
                security_module._RetainedCredentialLeaseView,
                "_load",
                hostile_load,
            ):
                with self._lease(boundary, token, canonical_handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_retained_unprotector_class_rebinding_cannot_replace_plaintext(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(
                Path(root) / "canonical.json",
                secret="canonical-secret",
            )
            boundary, token = self._boundary(vault)
            calls = []

            def hostile_unprotect(_self, _ciphertext, *, entropy):
                calls.append(entropy)
                return b"hostile-replacement-secret"

            with patch.object(
                security_module._RetainedCredentialUnprotector,
                "unprotect",
                hostile_unprotect,
            ):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_retained_view_handle_class_rebinding_cannot_replace_generation_metadata(self):
        with TemporaryDirectory() as root:
            vault, stale = self._vault(
                Path(root) / "canonical.json",
                secret="generation-one-secret",
            )
            boundary, token = self._boundary(vault)
            rotated = vault.rotate(
                stale,
                execution_identity="host-a",
                new_secret_value="generation-two-secret",
            )
            calls = []

            def hostile_handle(_self, _record):
                calls.append(True)
                return stale

            with patch.object(
                security_module._RetainedCredentialLeaseView,
                "_handle",
                hostile_handle,
            ):
                with self._lease(boundary, token, rotated) as plaintext:
                    self.assertEqual(plaintext, "generation-two-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
