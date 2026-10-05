from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from inspect import signature
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.security as security_module
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


_ORIGIN = "http://127.0.0.1:18765"


class DeterministicProtector:
    PREFIX = b"vault-lease-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class SecurityVaultLeaseAuthorityTests(unittest.TestCase):
    def _boundary(self, root: str):
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
                subject == "host-a"
                and role in {"OWNER", "OPERATOR", "OBSERVER"}
                and origin == _ORIGIN
            ),
            now=lambda: 100.0,
        )
        session = boundary.create_session(
            subject="host-a",
            role="OWNER",
            origin=_ORIGIN,
            ttl_seconds=900,
        )
        return vault, boundary, session.token, handle

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

    def test_post_import_vault_lease_rebinding_cannot_retarget_plaintext_authority(self):
        with TemporaryDirectory() as root:
            _vault, boundary, token, handle = self._boundary(root)
            hostile_calls = []

            @contextmanager
            def hostile_lease(_self, *_args, **_kwargs):
                hostile_calls.append(True)
                yield "hostile-secret"

            with patch.object(ProtectedCredentialVault, "lease", hostile_lease):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(hostile_calls, [])

    def test_installed_lease_executable_still_reads_current_rotated_generation(self):
        with TemporaryDirectory() as root:
            vault, boundary, token, handle = self._boundary(root)
            rotated = vault.rotate(
                handle,
                execution_identity="host-a",
                new_secret_value="rotated-secret",
            )
            hostile_calls = []

            @contextmanager
            def hostile_lease(_self, *_args, **_kwargs):
                hostile_calls.append(True)
                yield "hostile-secret"

            with patch.object(ProtectedCredentialVault, "lease", hostile_lease):
                with self._lease(boundary, token, rotated) as plaintext:
                    self.assertEqual(plaintext, "rotated-secret")

            self.assertEqual(hostile_calls, [])

    def test_session_validator_rebinding_cannot_authorize_forged_lease(self):
        with TemporaryDirectory() as root:
            _vault, boundary, _token, handle = self._boundary(root)
            hostile_calls = []

            def hostile_validate(_self, *_args, **_kwargs):
                hostile_calls.append(True)
                return object()

            with patch.object(SecurityBoundary, "validate_session", hostile_validate):
                with self.assertRaisesRegex(PermissionError, "Unknown session"):
                    with self._lease(boundary, "forged-token", handle):
                        self.fail("forged session reached credential plaintext")

            self.assertEqual(hostile_calls, [])

    def test_scope_normalizer_rebinding_cannot_retarget_lease_scope(self):
        with TemporaryDirectory() as root:
            _vault, boundary, token, handle = self._boundary(root)
            hostile_calls = []

            def hostile_text(*_args, **_kwargs):
                hostile_calls.append(True)
                raise AssertionError("rebound credential scope normalizer executed")

            with patch.object(security_module, "_credential_text", hostile_text):
                with self._lease(boundary, token, handle) as plaintext:
                    self.assertEqual(plaintext, "canonical-secret")

            self.assertEqual(hostile_calls, [])

    def test_execution_role_rebinding_cannot_promote_observer_to_plaintext(self):
        with TemporaryDirectory() as root:
            _vault, boundary, _token, handle = self._boundary(root)
            observer = boundary.create_session(
                subject="host-a",
                role="OBSERVER",
                origin=_ORIGIN,
                ttl_seconds=900,
            )
            with patch.object(SecurityBoundary, "_EXECUTION_ROLES", {"OBSERVER"}):
                with self.assertRaisesRegex(PermissionError, "Role is not authorized"):
                    with self._lease(boundary, observer.token, handle):
                        self.fail("observer reached credential plaintext")

    def test_public_lease_signature_exposes_no_executable_authority_override(self):
        parameters = signature(SecurityBoundary.lease_for_execution).parameters
        self.assertNotIn("_vault_lease", parameters)
        self.assertNotIn("vault_lease", parameters)
        self.assertNotIn("lease_impl", parameters)
        self.assertNotIn("validate_session", parameters)
        self.assertNotIn("credential_text", parameters)


if __name__ == "__main__":
    unittest.main()
