from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"posix-fcntl-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


@unittest.skipIf(sys.platform == "win32", "POSIX fcntl authority only")
class SecurityVaultPosixFcntlAuthorityTests(unittest.TestCase):
    @staticmethod
    def _vault(root: str) -> ProtectedCredentialVault:
        return ProtectedCredentialVault(
            Path(root) / "credentials.json",
            protector=DeterministicProtector(),
        )

    @classmethod
    def _boundary(cls, root: str):
        vault = cls._vault(root)
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

    def test_vault_lock_does_not_reimport_fcntl_after_module_import(self):
        calls = []

        def hostile(*_args, **_kwargs):
            calls.append(True)
            raise AssertionError("late hostile fcntl authority executed")

        hostile_module = SimpleNamespace(
            flock=hostile,
            LOCK_EX=0x40000000,
            LOCK_UN=0x20000000,
        )
        with TemporaryDirectory() as root, patch.dict(
            sys.modules,
            {"fcntl": hostile_module},
        ):
            vault = self._vault(root)
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
            self.assertEqual(handle.generation, 1)

        self.assertEqual(calls, [])

    def test_terminal_lock_retains_installed_flock_after_composition(self):
        import fcntl

        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile(*_args, **_kwargs):
                calls.append(True)
                raise AssertionError("rebound fcntl.flock executed")

            with patch.object(fcntl, "flock", hostile), patch.object(
                fcntl,
                "LOCK_EX",
                0x40000000,
            ), patch.object(
                fcntl,
                "LOCK_UN",
                0x20000000,
            ):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_terminal_lock_ignores_sys_modules_fcntl_retargeting(self):
        with TemporaryDirectory() as root:
            boundary, token, handle = self._boundary(root)
            calls = []

            def hostile(*_args, **_kwargs):
                calls.append(True)
                raise AssertionError("retargeted sys.modules fcntl executed")

            hostile_module = SimpleNamespace(
                flock=hostile,
                LOCK_EX=0x40000000,
                LOCK_UN=0x20000000,
            )
            with patch.dict(sys.modules, {"fcntl": hostile_module}):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
