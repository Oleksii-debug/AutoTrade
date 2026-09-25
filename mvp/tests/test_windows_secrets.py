from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.windows_secrets import (
    DpapiCurrentUserProtector,
    ProtectedCredentialVault,
    SecretVaultError,
)


class DeterministicProtector:
    """Test-only protector that authenticates scope entropy without plaintext storage."""

    PREFIX = b"test-protected-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CannotDecryptProtector(DeterministicProtector):
    """Simulates the same metadata label under a different OS identity."""

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        raise OSError("different OS identity")


class ProtectedCredentialVaultTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def register(self, secret="top-secret"):
        return self.vault.register(
            handle_id="cred-test",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="simulated",
            environment="PAPER",
            purpose="trade",
            secret_value=secret,
        )

    def test_secret_is_ciphertext_at_rest_and_survives_restart(self):
        handle = self.register()
        raw = self.path.read_text(encoding="utf-8")
        self.assertNotIn("top-secret", raw)
        self.assertNotIn("terces-pot", raw)

        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        self.assertEqual(
            restarted.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "top-secret",
        )
        description = restarted.describe(handle.handle_id)
        self.assertNotIn("secret", " ".join(description.keys()).lower())
        self.assertNotIn("ciphertext", description)

    def test_scope_and_identity_are_cryptographically_bound(self):
        handle = self.register()
        cases = (
            dict(
                execution_identity="windows-user-2",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-2",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="OTHER",
                environment="PAPER",
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="READ",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="LIVE",
                purpose="TRADE",
            ),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(PermissionError):
                self.vault.resolve(handle, **values)

    def test_provider_environment_is_separate_from_runtime_environment(self):
        handle = self.vault.register(
            handle_id="cred-bybit-testnet",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="testnet-secret",
        )
        self.assertEqual(handle.environment, "PAPER")
        self.assertEqual(handle.provider_environment, "TESTNET")
        self.assertEqual(
            self.vault.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment="TESTNET",
                purpose="TRADE",
            ),
            "testnet-secret",
        )
        with self.assertRaisesRegex(PermissionError, "scope mismatch"):
            self.vault.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment="DEMO",
                purpose="TRADE",
            )

        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 3)
        self.assertEqual(
            raw["records"][handle.handle_id]["handle"]["provider_environment"],
            "TESTNET",
        )

    def test_v2_vault_requires_explicit_provider_environment_reattachment(self):
        self.path.unlink()
        self.path.write_text(
            json.dumps({"version": 2, "records": {}}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            SecretVaultError,
            "provider-environment binding",
        ):
            ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )

    def test_rotation_invalidates_old_generation_across_restart(self):
        old = self.register()
        new = self.vault.rotate(
            old,
            execution_identity="windows-user-1",
            new_secret_value="rotated-value",
        )
        self.assertEqual(new.generation, 2)
        with self.assertRaisesRegex(PermissionError, "stale"):
            self.vault.resolve(
                old,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        self.assertEqual(
            restarted.resolve(
                new,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "rotated-value",
        )
        self.assertNotIn("rotated-value", self.path.read_text(encoding="utf-8"))

    def test_rotation_requires_actual_decryption_under_current_identity(self):
        old = self.register(secret="original-secret")
        before = self.path.read_bytes()
        foreign = ProtectedCredentialVault(
            self.path,
            protector=CannotDecryptProtector(),
        )

        with self.assertRaisesRegex(PermissionError, "cannot be decrypted"):
            foreign.rotate(
                old,
                execution_identity="windows-user-1",
                new_secret_value="attacker-rebound-secret",
            )

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            self.vault.resolve(
                old,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "original-secret",
        )

    def test_revocation_requires_actual_decryption_under_current_identity(self):
        handle = self.register(secret="original-secret")
        before = self.path.read_bytes()
        foreign = ProtectedCredentialVault(
            self.path,
            protector=CannotDecryptProtector(),
        )

        with self.assertRaisesRegex(PermissionError, "cannot be decrypted"):
            foreign.revoke(
                handle,
                execution_identity="windows-user-1",
            )

        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(
            self.vault.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "original-secret",
        )

    def test_revocation_erases_ciphertext_and_fails_closed_after_restart(self):
        handle = self.register()
        before = json.loads(self.path.read_text(encoding="utf-8"))
        before_ciphertext = before["records"][handle.handle_id]["ciphertext"]
        self.vault.revoke(handle, execution_identity="windows-user-1")
        after = json.loads(self.path.read_text(encoding="utf-8"))
        record = after["records"][handle.handle_id]
        self.assertFalse(record["active"])
        self.assertNotEqual(record["ciphertext"], before_ciphertext)

        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        with self.assertRaises(PermissionError):
            restarted.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_only_explicit_read_and_trade_credential_purposes_are_allowed(self):
        for purpose in (
            "WITHDRAWAL",
            "WITHDRAW",
            "TRANSFER",
            "EXTERNAL_TRANSFER",
            "PAYOUT",
            "ADMIN",
            "UNKNOWN",
        ):
            with self.subTest(purpose=purpose), self.assertRaises(PermissionError):
                self.vault.register(
                    owner_identity="windows-user-1",
                    account_id="paper-1",
                    provider="SIMULATED",
                    environment="PAPER",
                    purpose=purpose,
                    secret_value="must-not-store",
                )

        read_handle = self.vault.register(
            handle_id="cred-read",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="read",
            secret_value="read-secret",
        )
        self.assertEqual(read_handle.purpose, "READ")

    def test_concurrent_registrations_do_not_lose_records(self):
        def register_one(index):
            vault = ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )
            return vault.register(
                handle_id=f"cred-concurrent-{index}",
                owner_identity="windows-user-1",
                account_id=f"paper-{index}",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value=f"secret-{index}",
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            handles = list(executor.map(register_one, range(24)))

        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        self.assertEqual(len(handles), 24)
        for index, handle in enumerate(handles):
            self.assertEqual(
                restarted.resolve(
                    handle,
                    execution_identity="windows-user-1",
                    account_id=f"paper-{index}",
                    provider="SIMULATED",
                    environment="PAPER",
                    purpose="TRADE",
                ),
                f"secret-{index}",
            )

    def test_legacy_v1_vault_requires_explicit_reattachment(self):
        legacy = {"version": 1, "records": {}}
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        with self.assertRaisesRegex(SecretVaultError, "reattachment"):
            ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )

    def test_duplicate_handle_and_corrupt_vault_fail_closed(self):
        self.register()
        with self.assertRaisesRegex(SecretVaultError, "already exists"):
            self.register(secret="other")

        self.path.write_text('{"version":1,"records":{"broken":{}}}', encoding="utf-8")
        with self.assertRaises(SecretVaultError):
            ProtectedCredentialVault(
                self.path,
                protector=DeterministicProtector(),
            )

    def test_startup_rejects_tampered_scope_and_empty_ciphertext(self):
        handle = self.register()
        original = json.loads(self.path.read_text(encoding="utf-8"))

        for field, value in (
            ("purpose", "WITHDRAWAL"),
            ("account_id", "   "),
            ("provider", ""),
            ("environment", "PRODUCTION"),
        ):
            with self.subTest(field=field):
                tampered = json.loads(json.dumps(original))
                tampered["records"][handle.handle_id]["handle"][field] = value
                self.path.write_text(json.dumps(tampered), encoding="utf-8")
                with self.assertRaises(SecretVaultError):
                    ProtectedCredentialVault(
                        self.path,
                        protector=DeterministicProtector(),
                    )

        tampered = json.loads(json.dumps(original))
        tampered["records"][handle.handle_id]["owner_identity"] = " "
        self.path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(SecretVaultError):
            ProtectedCredentialVault(self.path, protector=DeterministicProtector())

        tampered = json.loads(json.dumps(original))
        tampered["records"][handle.handle_id]["ciphertext"] = ""
        self.path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaises(SecretVaultError):
            ProtectedCredentialVault(self.path, protector=DeterministicProtector())

    def test_direct_handle_cannot_bypass_scope_or_generation_invariants(self):
        from mvp.autotrade_mvp.windows_secrets import PersistentCredentialHandle

        with self.assertRaises(SecretVaultError):
            PersistentCredentialHandle(
                handle_id="cred-bad",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="WITHDRAWAL",
                generation=1,
            )
        with self.assertRaises(SecretVaultError):
            PersistentCredentialHandle(
                handle_id="cred-bad",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                generation=0,
            )

    def test_float_or_random_payload_is_never_interpreted_as_secret_metadata(self):
        with self.assertRaises(SecretVaultError):
            self.vault.register(
                owner_identity="",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="x",
            )


class CredentialReattachmentManifestTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def test_export_contains_only_non_secret_reattachment_metadata(self):
        trade = self.vault.register(
            handle_id="cred-trade",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="original-trade-secret",
        )
        trade = self.vault.rotate(
            trade,
            execution_identity="windows-user-1",
            new_secret_value="rotated-trade-secret",
        )
        read = self.vault.register(
            handle_id="cred-read",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="READ",
            secret_value="read-secret-value",
        )
        self.vault.revoke(read, execution_identity="windows-user-1")

        manifest = self.vault.export_reattachment_manifest()
        encoded = json.dumps(manifest, sort_keys=True)

        self.assertFalse(manifest["contains_secrets"])
        self.assertEqual(
            manifest["restore_mode"],
            "EXPLICIT_REATTACHMENT_REQUIRED",
        )
        self.assertNotIn("ciphertext", encoded)
        self.assertNotIn("owner_identity", encoded)
        self.assertNotIn("windows-user-1", encoded)
        self.assertNotIn("original-trade-secret", encoded)
        self.assertNotIn("rotated-trade-secret", encoded)
        self.assertNotIn("read-secret-value", encoded)

        requirements = ProtectedCredentialVault.validate_reattachment_manifest(
            manifest
        )
        by_id = {
            item.handle.handle_id: item
            for item in requirements
        }
        self.assertEqual(set(by_id), {"cred-read", "cred-trade"})
        self.assertEqual(by_id["cred-trade"].handle, trade)
        self.assertTrue(by_id["cred-trade"].was_active)
        self.assertEqual(by_id["cred-read"].handle.generation, 1)
        self.assertFalse(by_id["cred-read"].was_active)

    def test_manifest_validation_is_side_effect_free_and_cannot_restore_authority(self):
        handle = self.vault.register(
            handle_id="cred-trade",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="trade-secret",
        )
        manifest = self.vault.export_reattachment_manifest()

        fresh_path = Path(self.directory.name) / "fresh.json"
        fresh = ProtectedCredentialVault(
            fresh_path,
            protector=DeterministicProtector(),
        )
        before = fresh_path.read_bytes()
        requirements = fresh.validate_reattachment_manifest(manifest)
        self.assertEqual(fresh_path.read_bytes(), before)
        self.assertEqual(requirements[0].handle, handle)

        with self.assertRaises(PermissionError):
            fresh.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_manifest_is_deterministic_across_restart_and_identity_change(self):
        self.vault.register(
            handle_id="cred-trade",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="trade-secret",
        )
        expected = self.vault.export_reattachment_manifest()

        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        self.assertEqual(
            restarted.export_reattachment_manifest(),
            expected,
        )

        foreign_identity = ProtectedCredentialVault(
            self.path,
            protector=CannotDecryptProtector(),
        )
        self.assertEqual(
            foreign_identity.export_reattachment_manifest(),
            expected,
        )
        with self.assertRaisesRegex(PermissionError, "cannot be decrypted"):
            foreign_identity.resolve(
                ProtectedCredentialVault.validate_reattachment_manifest(
                    expected
                )[0].handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            )

    def test_validator_rejects_secret_bearing_or_authority_bearing_manifest(self):
        self.vault.register(
            handle_id="cred-trade",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="trade-secret",
        )
        manifest = self.vault.export_reattachment_manifest()

        bad_cases = []

        value = json.loads(json.dumps(manifest))
        value["contains_secrets"] = True
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["restore_mode"] = "RESTORE_CREDENTIALS"
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["source_vault_format_version"] = 1
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["records"][0]["ciphertext"] = "must-not-be-accepted"
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["records"][0]["handle"]["purpose"] = "WITHDRAWAL"
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["records"][0]["handle"]["environment"] = "PRODUCTION"
        bad_cases.append(value)

        value = json.loads(json.dumps(manifest))
        value["records"].append(json.loads(json.dumps(value["records"][0])))
        bad_cases.append(value)

        for bad in bad_cases:
            with self.subTest(bad=bad), self.assertRaises(SecretVaultError):
                ProtectedCredentialVault.validate_reattachment_manifest(bad)


@unittest.skipUnless(sys.platform == "win32", "Windows DPAPI qualification runs on Windows CI")
class WindowsDpapiTests(unittest.TestCase):
    def test_current_user_round_trip_with_scope_entropy(self):
        protector = DpapiCurrentUserProtector()
        plaintext = b"autotrade-dpapi-ci-secret"
        entropy = sha256(b"autotrade-scope").digest()
        ciphertext = protector.protect(plaintext, entropy=entropy)
        self.assertNotEqual(ciphertext, plaintext)
        self.assertEqual(
            protector.unprotect(ciphertext, entropy=entropy),
            plaintext,
        )
        with self.assertRaises(OSError):
            protector.unprotect(
                ciphertext,
                entropy=sha256(b"different-scope").digest(),
            )


if __name__ == "__main__":
    unittest.main()
