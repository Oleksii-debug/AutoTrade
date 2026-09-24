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
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-2",
                provider="SIMULATED",
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="OTHER",
                purpose="TRADE",
            ),
            dict(
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                purpose="READ",
            ),
        )
        for values in cases:
            with self.subTest(values=values), self.assertRaises(PermissionError):
                self.vault.resolve(handle, **values)

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
                    purpose=purpose,
                    secret_value="must-not-store",
                )

        read_handle = self.vault.register(
            handle_id="cred-read",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
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
                    purpose="TRADE",
                ),
                f"secret-{index}",
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

    def test_float_or_random_payload_is_never_interpreted_as_secret_metadata(self):
        with self.assertRaises(SecretVaultError):
            self.vault.register(
                owner_identity="",
                account_id="paper-1",
                provider="SIMULATED",
                purpose="TRADE",
                secret_value="x",
            )


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
