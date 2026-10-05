from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"lease-view-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class HostileProtector:
    def __init__(self) -> None:
        self.unprotect_calls = 0

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return plaintext

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        self.unprotect_calls += 1
        return b"hostile-secret"


class SecurityVaultLeaseViewAuthorityTests(unittest.TestCase):
    @staticmethod
    def _vault(path: Path, secret: str):
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

    def test_post_composition_protector_replacement_cannot_substitute_plaintext(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(Path(root) / "canonical.json", "canonical-secret")
            boundary, token = self._boundary(vault)
            hostile = HostileProtector()

            vault._protector = hostile

            with self._lease(boundary, token, handle) as plaintext:
                self.assertEqual(plaintext, "canonical-secret")
            self.assertEqual(hostile.unprotect_calls, 0)

    def test_post_composition_protector_method_rebinding_cannot_substitute_plaintext(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(Path(root) / "canonical.json", "canonical-secret")
            boundary, token = self._boundary(vault)
            hostile_calls = []

            def hostile_unprotect(_self, _ciphertext, *, entropy):
                _ = entropy
                hostile_calls.append(True)
                return b"hostile-secret"

            with patch.object(DeterministicProtector, "unprotect", hostile_unprotect):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(hostile_calls, [])

    def test_post_composition_load_rebinding_cannot_substitute_other_vault_state(self):
        with TemporaryDirectory() as root:
            canonical, handle = self._vault(
                Path(root) / "canonical.json", "canonical-secret"
            )
            hostile, hostile_handle = self._vault(
                Path(root) / "hostile.json", "hostile-secret"
            )
            self.assertEqual(handle, hostile_handle)
            hostile_state = hostile._load()
            boundary, token = self._boundary(canonical)
            calls = []

            def hostile_load(_self):
                calls.append(True)
                return hostile_state

            with patch.object(ProtectedCredentialVault, "_load", hostile_load):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_post_composition_handle_rebinding_cannot_replace_generation_reader(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(Path(root) / "canonical.json", "canonical-secret")
            boundary, token = self._boundary(vault)
            calls = []

            def hostile_handle(_record):
                calls.append(True)
                return handle

            with patch.object(ProtectedCredentialVault, "_handle", staticmethod(hostile_handle)):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_post_composition_scope_normalizer_rebinding_cannot_change_lease_scope(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(Path(root) / "canonical.json", "canonical-secret")
            boundary, token = self._boundary(vault)
            calls = []

            def hostile_normalize_scope(**_kwargs):
                calls.append(True)
                return (
                    "host-a",
                    "account-1",
                    "BYBIT",
                    "PAPER",
                    "TESTNET",
                    "TRADE",
                )

            with patch.object(
                ProtectedCredentialVault,
                "_normalize_scope",
                staticmethod(hostile_normalize_scope),
            ):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(calls, [])

    def test_post_composition_path_replacement_cannot_retarget_durable_state(self):
        with TemporaryDirectory() as root:
            canonical, handle = self._vault(
                Path(root) / "canonical.json", "canonical-secret"
            )
            hostile, hostile_handle = self._vault(
                Path(root) / "hostile.json", "hostile-secret"
            )
            self.assertEqual(handle, hostile_handle)
            boundary, token = self._boundary(canonical)

            canonical.path = hostile.path
            canonical.lock_path = hostile.lock_path

            with self._lease(boundary, token, handle) as plaintext:
                self.assertEqual(plaintext, "canonical-secret")

    def test_retained_lease_view_reads_current_canonical_rotation(self):
        with TemporaryDirectory() as root:
            vault, handle = self._vault(Path(root) / "canonical.json", "canonical-secret")
            boundary, token = self._boundary(vault)
            rotated = vault.rotate(
                handle,
                execution_identity="host-a",
                new_secret_value="rotated-secret",
            )

            with self._lease(boundary, token, rotated) as plaintext:
                self.assertEqual(plaintext, "rotated-secret")


if __name__ == "__main__":
    unittest.main()
