from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.windows_secrets as windows_secrets


LEGACY_JSON_DUMPS = json.dumps


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


class HostileJsonEncoder:
    def __init__(self, *args, **kwargs):
        raise AssertionError("mutable json.JSONEncoder must not become vault authority")


class HostileJsonDecoder:
    def __init__(self, *args, **kwargs):
        raise AssertionError("mutable json.JSONDecoder must not become vault authority")


class HostileDefaultEncoder:
    def encode(self, *args, **kwargs):
        raise AssertionError("mutable json._default_encoder must not become vault authority")


class HostileDefaultDecoder:
    def decode(self, *args, **kwargs):
        raise AssertionError("mutable json._default_decoder must not become vault authority")


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

    def test_direct_encoder_preserves_legacy_entropy_and_vault_bytes(self):
        scope = {
            "handle_id": "cred-json-compatibility",
            "owner_identity": "windows-user-1",
            "account_id": "paper-1",
            "provider": "SIMULATED",
            "environment": "PAPER",
            "provider_environment": "PAPER",
            "purpose": "TRADE",
            "generation": 1,
        }
        legacy_scope = LEGACY_JSON_DUMPS(
            scope,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            windows_secrets._scope_entropy(**scope),
            sha256(legacy_scope).digest(),
        )

        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            vault = self._vault(path)
            vault.register(
                handle_id=scope["handle_id"],
                owner_identity=scope["owner_identity"],
                account_id=scope["account_id"],
                provider=scope["provider"],
                environment=scope["environment"],
                provider_environment=scope["provider_environment"],
                purpose=scope["purpose"],
                secret_value="compatibility-π-secret",
            )
            state = vault._load()
            legacy_vault = LEGACY_JSON_DUMPS(
                state,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            self.assertEqual(path.read_text(encoding="utf-8"), legacy_vault)

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

    def test_json_module_rebinding_cannot_suppress_durable_revocation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            vault = self._vault(path)
            handle = vault.register(
                handle_id="cred-json-revocation",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="revocable-secret",
            )

            with patch.object(windows_secrets, "json", HostileJsonModule):
                vault.revoke(
                    handle,
                    execution_identity="windows-user-1",
                )
                with self.assertRaisesRegex(PermissionError, "unavailable"):
                    self._resolve(vault, handle)

            restarted = self._vault(path)
            with self.assertRaisesRegex(PermissionError, "unavailable"):
                self._resolve(restarted, handle)

    def test_json_module_attribute_retargeting_cannot_change_frozen_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "credentials.json"
            vault = self._vault(path)
            handle = vault.register(
                handle_id="cred-json-attributes",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="original-secret",
            )
            corrupt_path = Path(directory) / "corrupt-credentials.json"
            corrupt_path.write_text("{not-valid-json", encoding="utf-8")

            imported_json = windows_secrets.json
            original_loads = imported_json.loads
            original_dumps = imported_json.dumps
            original_decode_error = imported_json.JSONDecodeError
            original_encoder = imported_json.JSONEncoder
            original_decoder = imported_json.JSONDecoder
            original_default_encoder = imported_json._default_encoder
            original_default_decoder = imported_json._default_decoder
            try:
                imported_json.loads = HostileJsonModule.loads
                imported_json.dumps = HostileJsonModule.dumps
                imported_json.JSONDecodeError = HostileJsonModule.JSONDecodeError
                imported_json.JSONEncoder = HostileJsonEncoder
                imported_json.JSONDecoder = HostileJsonDecoder
                imported_json._default_encoder = HostileDefaultEncoder()
                imported_json._default_decoder = HostileDefaultDecoder()

                restarted = self._vault(path)
                self.assertEqual(self._resolve(restarted, handle), "original-secret")
                rotated = restarted.rotate(
                    handle,
                    execution_identity="windows-user-1",
                    new_secret_value="rotated-secret",
                )
                self.assertEqual(self._resolve(restarted, rotated), "rotated-secret")

                second_path = Path(directory) / "second-credentials.json"
                second_vault = self._vault(second_path)
                second_handle = second_vault.register(
                    handle_id="cred-json-attributes-second",
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

                with self.assertRaisesRegex(
                    windows_secrets.SecretVaultError,
                    "corrupt or unreadable",
                ):
                    self._vault(corrupt_path)
            finally:
                imported_json.loads = original_loads
                imported_json.dumps = original_dumps
                imported_json.JSONDecodeError = original_decode_error
                imported_json.JSONEncoder = original_encoder
                imported_json.JSONDecoder = original_decoder
                imported_json._default_encoder = original_default_encoder
                imported_json._default_decoder = original_default_decoder

            restarted_after_restore = self._vault(path)
            self.assertEqual(
                self._resolve(restarted_after_restore, rotated),
                "rotated-secret",
            )

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
