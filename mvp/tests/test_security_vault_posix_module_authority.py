from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_secrets as secrets_module
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"posix-module-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


@unittest.skipIf(sys.platform == "win32", "POSIX terminal lock authority only")
class SecurityVaultPosixModuleAuthorityTests(unittest.TestCase):
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

    def test_shared_os_module_attribute_rebinding_cannot_retarget_terminal_lock(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile(*_args, **_kwargs):
                calls.append(True)
                raise AssertionError("rebound shared os primitive executed")

            with patch.object(secrets_module.os, "open", hostile), patch.object(
                secrets_module.os, "fstat", hostile
            ), patch.object(secrets_module.os, "fdopen", hostile), patch.object(
                secrets_module.os, "fsync", hostile
            ), patch.object(secrets_module.os, "close", hostile), patch.object(
                secrets_module.os, "lstat", hostile
            ):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_shared_os_flag_rebinding_cannot_disable_no_follow_lock_verification(self):
        if not hasattr(secrets_module.os, "O_NOFOLLOW"):
            self.skipTest("platform has no O_NOFOLLOW")
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            with patch.object(secrets_module.os, "O_NOFOLLOW", 0):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

    def test_shared_stat_module_attribute_rebinding_cannot_replace_leaf_checks(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile_mode_check(_mode):
                calls.append(True)
                raise AssertionError("rebound shared stat primitive executed")

            with patch.object(secrets_module.stat, "S_ISREG", hostile_mode_check), patch.object(
                secrets_module.stat, "S_ISLNK", hostile_mode_check
            ):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
