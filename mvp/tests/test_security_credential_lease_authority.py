from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"security-lease-authority-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class ConstructorLeaseVault(ProtectedCredentialVault):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease_calls = []

    @contextmanager
    def lease(self, handle, **kwargs):
        self.lease_calls.append((handle, dict(kwargs)))
        yield "constructor-authority"


class SecurityCredentialLeaseAuthorityTests(unittest.TestCase):
    ORIGIN = "https://local.autotrade.invalid"

    def _boundary(self, vault):
        boundary = SecurityBoundary(
            allowed_origins={self.ORIGIN},
            credential_vault=vault,
            session_authorizer=lambda subject, role, origin: True,
            now=lambda: 1000.0,
        )
        owner = boundary.create_session(
            subject="owner",
            role="OWNER",
            origin=self.ORIGIN,
        )
        handle = boundary.register_secret(
            owner.token,
            origin=owner.origin,
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="top-secret",
        )
        return boundary, owner, handle

    def _lease(self, boundary, owner, handle):
        return boundary.lease_for_execution(
            owner.token,
            origin=owner.origin,
            handle=handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
        )

    def test_post_construction_lease_reassignment_cannot_replace_authority(self):
        with TemporaryDirectory() as directory:
            vault = ProtectedCredentialVault(
                Path(directory) / "credentials.json",
                protector=DeterministicProtector(),
            )
            boundary, owner, handle = self._boundary(vault)

            @contextmanager
            def reassigned_lease(*args, **kwargs):
                yield "post-construction-reassignment"

            vault.lease = reassigned_lease

            with self._lease(boundary, owner, handle) as plaintext:
                self.assertEqual(plaintext, "top-secret")

    def test_constructor_time_subclass_lease_is_the_bound_authority(self):
        with TemporaryDirectory() as directory:
            vault = ConstructorLeaseVault(
                Path(directory) / "credentials.json",
                protector=DeterministicProtector(),
            )
            boundary, owner, handle = self._boundary(vault)

            @contextmanager
            def reassigned_lease(*args, **kwargs):
                yield "post-construction-reassignment"

            vault.lease = reassigned_lease

            with self._lease(boundary, owner, handle) as plaintext:
                self.assertEqual(plaintext, "constructor-authority")
            self.assertEqual(len(vault.lease_calls), 1)
            observed_handle, observed_scope = vault.lease_calls[0]
            self.assertEqual(observed_handle, handle)
            self.assertEqual(observed_scope["execution_identity"], "windows-user-1")
            self.assertEqual(observed_scope["account_id"], "paper-1")
            self.assertEqual(observed_scope["provider"], "SIMULATED")
            self.assertEqual(observed_scope["environment"], "PAPER")
            self.assertEqual(observed_scope["purpose"], "TRADE")
            self.assertIsNone(observed_scope["provider_environment"])


if __name__ == "__main__":
    unittest.main()
