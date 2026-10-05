from __future__ import annotations

from hashlib import sha256
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"load-path-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultLoadPathAuthorityTests(unittest.TestCase):
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

    def test_path_read_text_rebinding_cannot_retarget_same_scope_vault(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            canonical_vault, canonical_handle = self._vault(
                root_path / "canonical.json",
                secret="canonical-secret",
            )
            alternate_vault, _ = self._vault(
                root_path / "alternate.json",
                secret="alternate-secret",
            )
            alternate_raw = alternate_vault.path.read_text(encoding="utf-8")
            boundary, token = self._boundary(canonical_vault)
            calls = []
            original = Path.read_text

            def hostile(path, *args, **kwargs):
                calls.append(path)
                if path == canonical_vault.path:
                    return alternate_raw
                return original(path, *args, **kwargs)

            with patch.object(Path, "read_text", hostile):
                with self._lease(boundary, token, canonical_handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_path_open_rebinding_cannot_retarget_same_scope_vault(self):
        with TemporaryDirectory() as root:
            root_path = Path(root)
            canonical_vault, canonical_handle = self._vault(
                root_path / "canonical.json",
                secret="canonical-secret",
            )
            alternate_vault, _ = self._vault(
                root_path / "alternate.json",
                secret="alternate-secret",
            )
            alternate_raw = alternate_vault.path.read_text(encoding="utf-8")
            boundary, token = self._boundary(canonical_vault)
            calls = []
            original = Path.open

            def hostile(path, *args, **kwargs):
                calls.append(path)
                if path == canonical_vault.path:
                    return StringIO(alternate_raw)
                return original(path, *args, **kwargs)

            with patch.object(Path, "open", hostile):
                with self._lease(boundary, token, canonical_handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_path_exists_rebinding_cannot_replace_terminal_leaf_check(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(
                Path(root) / "canonical.json",
                secret="canonical-secret",
            )
            boundary, token = self._boundary(vault)
            calls = []

            def hostile(_path):
                calls.append(True)
                return False

            with patch.object(Path, "exists", hostile):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_path_stat_rebinding_cannot_replace_terminal_leaf_check(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(
                Path(root) / "canonical.json",
                secret="canonical-secret",
            )
            boundary, token = self._boundary(vault)
            calls = []
            original = Path.stat

            def hostile(path, *args, **kwargs):
                calls.append(path)
                return original(path, *args, **kwargs)

            with patch.object(Path, "stat", hostile):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
