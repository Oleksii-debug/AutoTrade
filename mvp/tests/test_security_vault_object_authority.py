from __future__ import annotations

from hashlib import sha256
from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"vault-object-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultObjectAuthorityTests(unittest.TestCase):
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

    def _boundary(self, vault):
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

    def test_post_composition_vault_object_replacement_cannot_retarget_plaintext(self):
        with TemporaryDirectory() as root:
            canonical_vault, canonical_handle = self._vault(
                Path(root) / "canonical.json",
                secret="canonical-secret",
            )
            hostile_vault, hostile_handle = self._vault(
                Path(root) / "hostile.json",
                secret="hostile-secret",
            )
            self.assertEqual(canonical_handle, hostile_handle)
            boundary, token = self._boundary(canonical_vault)

            boundary._credential_vault = hostile_vault

            with self._lease(boundary, token, canonical_handle) as plaintext:
                self.assertEqual(plaintext, "canonical-secret")

    def test_retained_vault_object_still_reads_its_current_rotated_generation(self):
        with TemporaryDirectory() as root:
            canonical_vault, canonical_handle = self._vault(
                Path(root) / "canonical.json",
                secret="canonical-secret",
            )
            hostile_vault, hostile_handle = self._vault(
                Path(root) / "hostile.json",
                secret="hostile-secret",
            )
            boundary, token = self._boundary(canonical_vault)
            canonical_rotated = canonical_vault.rotate(
                canonical_handle,
                execution_identity="host-a",
                new_secret_value="canonical-rotated",
            )
            hostile_rotated = hostile_vault.rotate(
                hostile_handle,
                execution_identity="host-a",
                new_secret_value="hostile-rotated",
            )
            self.assertEqual(canonical_rotated, hostile_rotated)

            boundary._credential_vault = hostile_vault

            with self._lease(boundary, token, canonical_rotated) as plaintext:
                self.assertEqual(plaintext, "canonical-rotated")

    def test_constructor_exposes_no_vault_registry_override(self):
        parameters = signature(SecurityBoundary.__init__).parameters
        self.assertNotIn("vault_registry", parameters)
        self.assertNotIn("vault_for_boundary", parameters)
        self.assertNotIn("execution_vault", parameters)


if __name__ == "__main__":
    unittest.main()
