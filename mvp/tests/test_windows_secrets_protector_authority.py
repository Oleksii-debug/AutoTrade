from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_secrets as windows_secrets
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"vault-protector-authority-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class DeterministicDpapiProtector(windows_secrets.DpapiCurrentUserProtector):
    PREFIX = b"dpapi-crypt-authority-test-v1:"

    @staticmethod
    def _crypt(*, data: bytes, entropy: bytes, decrypt: bool) -> bytes:
        marker = DeterministicDpapiProtector.PREFIX + sha256(entropy).digest()
        if decrypt:
            if not data.startswith(marker):
                raise OSError("scope entropy mismatch")
            return data[len(marker):][::-1]
        return marker + data[::-1]


class VaultProtectorAuthorityTests(unittest.TestCase):
    def _scope(self):
        return {
            "owner_identity": "windows-user-1",
            "account_id": "paper-1",
            "provider": "SIMULATED",
            "environment": "PAPER",
            "purpose": "TRADE",
        }

    def test_post_construction_dpapi_crypt_reassignment_cannot_replace_authority(self):
        with patch.object(windows_secrets.os, "name", "nt"):
            protector = DeterministicDpapiProtector()

        def hostile_crypt(*, data: bytes, entropy: bytes, decrypt: bool) -> bytes:
            raise AssertionError("post-construction DPAPI crypt reassignment was used")

        protector._crypt = hostile_crypt
        with patch.object(
            DeterministicDpapiProtector,
            "_crypt",
            staticmethod(hostile_crypt),
        ):
            entropy = b"frozen-dpapi-authority"
            ciphertext = protector.protect(b"top-secret", entropy=entropy)
            self.assertNotEqual(ciphertext, b"top-secret")
            self.assertEqual(
                protector.unprotect(ciphertext, entropy=entropy),
                b"top-secret",
            )

    def test_post_construction_protector_reassignment_cannot_replace_read_authority(self):
        with TemporaryDirectory() as directory:
            protector = DeterministicProtector()
            vault = ProtectedCredentialVault(
                Path(directory) / "credentials.json",
                protector=protector,
            )
            scope = self._scope()
            handle = vault.register(secret_value="top-secret", **scope)

            def reassigned_unprotect(ciphertext: bytes, *, entropy: bytes) -> bytes:
                return b"post-construction-reassignment"

            protector.unprotect = reassigned_unprotect

            self.assertEqual(
                vault.resolve(
                    handle,
                    execution_identity=scope["owner_identity"],
                    account_id=scope["account_id"],
                    provider=scope["provider"],
                    environment=scope["environment"],
                    purpose=scope["purpose"],
                ),
                "top-secret",
            )
            with vault.lease(
                handle,
                execution_identity=scope["owner_identity"],
                account_id=scope["account_id"],
                provider=scope["provider"],
                environment=scope["environment"],
                purpose=scope["purpose"],
            ) as plaintext:
                self.assertEqual(plaintext, "top-secret")

    def test_post_construction_protector_reassignment_cannot_replace_write_authority(self):
        with TemporaryDirectory() as directory:
            protector = DeterministicProtector()
            vault = ProtectedCredentialVault(
                Path(directory) / "credentials.json",
                protector=protector,
            )
            scope = self._scope()

            def reassigned_protect(plaintext: bytes, *, entropy: bytes) -> bytes:
                raise AssertionError("post-construction protect reassignment was used")

            protector.protect = reassigned_protect
            handle = vault.register(secret_value="top-secret", **scope)
            rotated = vault.rotate(
                handle,
                execution_identity=scope["owner_identity"],
                new_secret_value="rotated-secret",
            )
            self.assertEqual(
                vault.resolve(
                    rotated,
                    execution_identity=scope["owner_identity"],
                    account_id=scope["account_id"],
                    provider=scope["provider"],
                    environment=scope["environment"],
                    purpose=scope["purpose"],
                ),
                "rotated-secret",
            )


if __name__ == "__main__":
    unittest.main()
