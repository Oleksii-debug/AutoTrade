from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.windows_secrets import (
    CredentialReattachmentRequirement,
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    SecretVaultError,
)


class _DeterministicProtector:
    PREFIX = b"reseal-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class _HostileText(str):
    calls = 0

    def strip(self, *args, **kwargs):
        type(self).calls += 1
        raise AssertionError("hostile text strip must not run")

    def __hash__(self):
        type(self).calls += 1
        raise AssertionError("hostile text hash must not run")

    def __eq__(self, other):
        type(self).calls += 1
        raise AssertionError("hostile text equality must not run")


class _HostileInt(int):
    calls = 0

    def __lt__(self, other):
        type(self).calls += 1
        raise AssertionError("hostile int comparison must not run")

    def __eq__(self, other):
        type(self).calls += 1
        raise AssertionError("hostile int equality must not run")


class PersistentCredentialHandleResealTests(unittest.TestCase):
    def _vault(self, root: str) -> ProtectedCredentialVault:
        return ProtectedCredentialVault(
            Path(root) / "credentials.json",
            protector=_DeterministicProtector(),
        )

    @staticmethod
    def _copy(handle: PersistentCredentialHandle) -> PersistentCredentialHandle:
        return PersistentCredentialHandle(
            handle_id=handle.handle_id,
            account_id=handle.account_id,
            provider=handle.provider,
            environment=handle.environment,
            provider_environment=handle.provider_environment,
            purpose=handle.purpose,
            generation=handle.generation,
        )

    def test_mutated_exact_handle_fields_fail_before_lock_hash_or_equality(self):
        with TemporaryDirectory() as root:
            vault = self._vault(root)
            original = vault.register(
                handle_id="cred-reseal",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="top-secret",
            )
            before = vault.path.read_bytes()
            common = {
                "execution_identity": "windows-user-1",
                "account_id": "paper-1",
                "provider": "SIMULATED",
                "environment": "PAPER",
                "purpose": "TRADE",
            }

            hostile_text_handle = self._copy(original)
            object.__setattr__(
                hostile_text_handle,
                "handle_id",
                _HostileText(original.handle_id),
            )
            hostile_int_handle = self._copy(original)
            object.__setattr__(
                hostile_int_handle,
                "generation",
                _HostileInt(original.generation),
            )

            for handle in (hostile_text_handle, hostile_int_handle):
                with self.subTest(field=type(handle.handle_id).__name__):
                    with patch(
                        "mvp.autotrade_mvp.windows_secrets._exclusive_file_lock",
                        side_effect=AssertionError("vault lock must not be entered"),
                    ):
                        with self.assertRaises(SecretVaultError):
                            vault.resolve(handle, **common)
                        with self.assertRaises(SecretVaultError):
                            with vault.lease(handle, **common):
                                self.fail("tampered handle lease must not open")
                        with self.assertRaises(SecretVaultError):
                            vault.rotate(
                                handle,
                                execution_identity="windows-user-1",
                                new_secret_value="must-not-store",
                            )
                        with self.assertRaises(SecretVaultError):
                            vault.revoke(
                                handle,
                                execution_identity="windows-user-1",
                            )
                    with self.assertRaises(SecretVaultError):
                        CredentialReattachmentRequirement(
                            handle=handle,
                            was_active=True,
                        )

            self.assertEqual(_HostileText.calls, 0)
            self.assertEqual(_HostileInt.calls, 0)
            self.assertEqual(vault.path.read_bytes(), before)
            self.assertEqual(vault.resolve(original, **common), "top-secret")


if __name__ == "__main__":
    unittest.main()
