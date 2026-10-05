from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import windows_secrets as secrets_module
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"vault-serialization-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CredentialVaultSerializationAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def _register(self, handle_id: str = "cred-serialization"):
        return self.vault.register(
            handle_id=handle_id,
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="secret-v1",
        )

    @staticmethod
    def _rebound(name: str, calls: list[object]):
        def rebound(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(f"pre-call vault serialization primitive executed: {name}")

        return rebound

    def test_write_ignores_pre_call_json_dumps_rebind(self) -> None:
        calls: list[object] = []
        with patch.object(
            secrets_module.json,
            "dumps",
            new=self._rebound("json.dumps", calls),
        ):
            handle = self._register()

        self.assertEqual(handle.handle_id, "cred-serialization")
        self.assertEqual(calls, [])

    def test_load_ignores_pre_call_json_loads_rebind(self) -> None:
        handle = self._register()
        calls: list[object] = []
        with patch.object(
            secrets_module.json,
            "loads",
            new=self._rebound("json.loads", calls),
        ):
            restarted = ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )
            description = restarted.describe(handle.handle_id)

        self.assertEqual(description["handle_id"], handle.handle_id)
        self.assertEqual(calls, [])

    def test_load_ignores_pre_call_leaf_validator_rebind(self) -> None:
        handle = self._register()
        calls: list[object] = []
        with patch.object(
            secrets_module,
            "_require_vault_leaf",
            new=self._rebound("_require_vault_leaf", calls),
        ):
            restarted = ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )
            description = restarted.describe(handle.handle_id)

        self.assertEqual(description["handle_id"], handle.handle_id)
        self.assertEqual(calls, [])

    def test_write_ignores_pre_call_leaf_validator_rebind(self) -> None:
        calls: list[object] = []
        with patch.object(
            secrets_module,
            "_require_vault_leaf",
            new=self._rebound("_require_vault_leaf", calls),
        ):
            handle = self._register()

        self.assertEqual(handle.handle_id, "cred-serialization")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
