from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_secrets as windows_secrets


class DeterministicProtector:
    PREFIX = b"json-authority-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class HostileJsonModule:
    class JSONDecodeError(Exception):
        pass

    @staticmethod
    def loads(*args, **kwargs):
        raise AssertionError("mutable json.loads must not become vault authority")

    @staticmethod
    def dumps(*args, **kwargs):
        raise AssertionError("mutable json.dumps must not become vault authority")


class WindowsSecretsJsonAuthorityTests(unittest.TestCase):
    def _vault(self, path: Path) -> windows_secrets.ProtectedCredentialVault:
        return windows_secrets.ProtectedCredentialVault(
            path,
            protector=DeterministicProtector(),
        )

    @staticmethod
    def _resolve(vault, handle):
        return vault.resolve(
            handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
        )

    def test_json_module_rebinding_cannot_change_scope_load_or_write_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            vault = self._vault(path)
            handle = vault.register(
                handle_id="cred-json-authority",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="original-secret",
            )

            with patch.object(windows_secrets, "json", HostileJsonModule):
                self.assertEqual(self._resolve(vault, handle), "original-secret")
                rotated = vault.rotate(
                    handle,
                    execution_identity="windows-user-1",
                    new_secret_value="rotated-secret",
                )
                self.assertEqual(self._resolve(vault, rotated), "rotated-secret")

                second_path = Path(directory) / "second-credentials.json"
                second_vault = self._vault(second_path)
                second_handle = second_vault.register(
                    handle_id="cred-json-authority-second",
                    owner_identity="windows-user-1",
                    account_id="paper-1",
                    provider="SIMULATED",
                    environment="PAPER",
                    purpose="TRADE",
                    secret_value="second-secret",
                )
                self.assertEqual(
                    self._resolve(second_vault, second_handle),
                    "second-secret",
                )

            restarted = self._vault(path)
            self.assertEqual(self._resolve(restarted, rotated), "rotated-secret")

    def test_json_module_rebinding_cannot_bypass_corrupt_vault_fail_closed(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            self._vault(path)
            path.write_text("{not-valid-json", encoding="utf-8")

            with patch.object(windows_secrets, "json", HostileJsonModule):
                with self.assertRaisesRegex(
                    windows_secrets.SecretVaultError,
                    "corrupt or unreadable",
                ):
                    self._vault(path)


if __name__ == "__main__":
    unittest.main()
