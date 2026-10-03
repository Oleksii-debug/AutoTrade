from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp import credential_transition_receipt as transition
from mvp.autotrade_mvp.credential_transition_receipt import (
    CredentialTransitionReceiptError,
    rotate_trade_credential_with_receipt,
    verify_trade_credential_transition_receipt,
)
from mvp.autotrade_mvp.security import SecurityBoundary
from mvp.autotrade_mvp.windows_secrets import (
    PersistentCredentialHandle,
    ProtectedCredentialVault,
    SecretVaultError,
    _scope_entropy,
)


class DeterministicProtector:
    PREFIX = b"provider-domain-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class _HostileText(str):
    def strip(self):
        raise AssertionError("hostile string callback executed")

    def upper(self):
        raise AssertionError("hostile string callback executed")

    def encode(self, *args, **kwargs):
        raise AssertionError("hostile string callback executed")


class ProviderEnvironmentCredentialScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def _bybit(self, *, domain: str = "TESTNET"):
        return self.vault.register(
            handle_id="cred-bybit-paper",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment=domain,
            purpose="TRADE",
            secret_value="scoped-secret",
        )

    def test_vault_v3_persists_and_cryptographically_binds_exact_domain(self):
        handle = self._bybit(domain="TESTNET")
        raw = json.loads(self.path.read_text(encoding="utf-8"))

        self.assertEqual(raw["version"], 3)
        self.assertEqual(handle.provider_environment, "TESTNET")
        self.assertEqual(
            raw["records"][handle.handle_id]["handle"]["provider_environment"],
            "TESTNET",
        )
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
            "scoped-secret",
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

    def test_bybit_domain_is_required_before_vault_mutation(self):
        before = self.path.read_bytes()
        for domain in (None, "MAINNET"):
            with self.subTest(domain=domain), self.assertRaises(SecretVaultError):
                self.vault.register(
                    handle_id=f"cred-invalid-{domain}",
                    owner_identity="windows-user-1",
                    account_id="paper-1",
                    provider="BYBIT",
                    environment="PAPER",
                    provider_environment=domain,
                    purpose="TRADE",
                    secret_value="must-not-store",
                )
            self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_v1_and_v2_vaults_require_explicit_reattachment(self):
        for version in (1, 2):
            path = Path(self.directory.name) / f"legacy-{version}.json"
            path.write_text(
                json.dumps({"version": version, "records": {}}),
                encoding="utf-8",
            )
            with self.subTest(version=version), self.assertRaisesRegex(
                SecretVaultError,
                "provider-environment binding.*reattachment",
            ):
                ProtectedCredentialVault(
                    path,
                    protector=DeterministicProtector(),
                )

    def test_reattachment_manifest_preserves_provider_environment_without_authority(self):
        handle = self._bybit(domain="TESTNET")
        manifest = self.vault.export_reattachment_manifest()
        requirements = ProtectedCredentialVault.validate_reattachment_manifest(manifest)

        self.assertEqual(manifest["source_vault_format_version"], 3)
        self.assertEqual(len(requirements), 1)
        self.assertEqual(requirements[0].handle, handle)
        self.assertEqual(requirements[0].handle.provider_environment, "TESTNET")
        self.assertTrue(requirements[0].was_active)
        self.assertFalse(manifest["contains_secrets"])

        legacy = json.loads(json.dumps(manifest))
        legacy["source_vault_format_version"] = 2
        legacy["records"][0]["handle"].pop("provider_environment")
        with self.assertRaises(SecretVaultError):
            ProtectedCredentialVault.validate_reattachment_manifest(legacy)

    def test_security_boundary_preserves_domain_across_register_lease_rotate_and_revoke(self):
        boundary = SecurityBoundary(
            allowed_origins={"https://localhost"},
            credential_vault=self.vault,
            session_authorizer=lambda _subject, _role, _origin: True,
            now=lambda: 100.0,
        )
        session = boundary.create_session(
            subject="owner-1",
            role="OWNER",
            origin="https://localhost",
        )
        handle = boundary.register_secret(
            session.token,
            origin="https://localhost",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="scoped-secret",
        )
        self.assertEqual(handle.provider_environment, "TESTNET")

        with boundary.lease_for_execution(
            session.token,
            origin="https://localhost",
            handle=handle,
            execution_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            purpose="TRADE",
            provider_environment="TESTNET",
        ) as plaintext:
            self.assertEqual(plaintext, "scoped-secret")

        with self.assertRaisesRegex(PermissionError, "scope mismatch"):
            with boundary.lease_for_execution(
                session.token,
                origin="https://localhost",
                handle=handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                purpose="TRADE",
                provider_environment="DEMO",
            ):
                self.fail("wrong provider domain must not lease plaintext")

        rotated = boundary.rotate_secret(
            session.token,
            origin="https://localhost",
            handle_id=handle.handle_id,
            owner_identity="windows-user-1",
            new_secret_value="scoped-secret-v2",
        )
        self.assertEqual(rotated.provider_environment, "TESTNET")
        self.assertEqual(
            boundary.describe_handle(rotated.handle_id)["provider_environment"],
            "TESTNET",
        )
        boundary.revoke_secret(
            session.token,
            origin="https://localhost",
            handle_id=rotated.handle_id,
            owner_identity="windows-user-1",
        )
        with self.assertRaises(PermissionError):
            boundary.describe_handle(rotated.handle_id)

    def test_security_boundary_cannot_guess_bybit_provider_domain(self):
        boundary = SecurityBoundary(
            allowed_origins={"https://localhost"},
            credential_vault=self.vault,
            session_authorizer=lambda _subject, _role, _origin: True,
            now=lambda: 100.0,
        )
        session = boundary.create_session(
            subject="owner-1",
            role="OWNER",
            origin="https://localhost",
        )
        before = self.path.read_bytes()
        with self.assertRaises(SecretVaultError):
            boundary.register_secret(
                session.token,
                origin="https://localhost",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                purpose="TRADE",
                secret_value="must-not-store",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_transition_receipt_retains_exact_provider_environment(self):
        old = self._bybit(domain="TESTNET")
        current, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            old,
            execution_identity="windows-user-1",
            new_secret_value="rotated-secret",
        )

        self.assertEqual(current.provider_environment, "TESTNET")
        self.assertEqual(receipt.schema_version, "2.0.0")
        self.assertEqual(receipt.provider_environment, "TESTNET")
        self.assertEqual(
            verify_trade_credential_transition_receipt(self.vault, receipt),
            receipt,
        )
        self.assertEqual(
            self.vault.resolve(
                current,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment="TESTNET",
                purpose="TRADE",
            ),
            "rotated-secret",
        )

    def test_rehashed_cross_domain_receipt_is_not_vault_issued(self):
        old = self._bybit(domain="TESTNET")
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            old,
            execution_identity="windows-user-1",
            new_secret_value="rotated-secret",
        )
        forged = replace(
            receipt,
            provider_environment="DEMO",
            receipt_id="credential-transition/sha256:" + "0" * 64,
        )
        forged = replace(
            forged,
            receipt_id=transition._receipt_id_from_subject(
                transition._receipt_subject(forged)
            ),
        )
        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "stale or not vault-issued|scope mismatches",
        ):
            verify_trade_credential_transition_receipt(self.vault, forged)

    def test_coherent_vault_fork_cannot_splice_testnet_predecessor_into_demo(self):
        seed = self.vault.register(
            handle_id="cred-seed",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="seed-v1",
        )
        rotate_trade_credential_with_receipt(
            self.vault,
            seed,
            execution_identity="windows-user-1",
            new_secret_value="seed-v2",
        )
        common_fork = self.path.read_bytes()

        testnet_first = self.vault.register(
            handle_id="cred-forked",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="testnet-v1",
        )
        rotate_trade_credential_with_receipt(
            self.vault,
            testnet_first,
            execution_identity="windows-user-1",
            new_secret_value="testnet-v2",
        )
        testnet_state = json.loads(self.path.read_text(encoding="utf-8"))
        testnet_latest = testnet_state["credential_transition_authority"][
            "latest_by_handle"
        ]["cred-forked"]

        self.path.write_bytes(common_fork)
        demo_vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        demo_first = demo_vault.register(
            handle_id="cred-forked",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
            purpose="TRADE",
            secret_value="demo-v1",
        )
        demo_current, _demo_receipt = rotate_trade_credential_with_receipt(
            demo_vault,
            demo_first,
            execution_identity="windows-user-1",
            new_secret_value="demo-v2",
        )
        spliced_state = json.loads(self.path.read_text(encoding="utf-8"))
        spliced_state["credential_transition_authority"]["latest_by_handle"][
            "cred-forked"
        ] = testnet_latest
        self.path.write_text(
            json.dumps(spliced_state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        spliced = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "scope does not match current credential",
        ):
            rotate_trade_credential_with_receipt(
                spliced,
                demo_current,
                execution_identity="windows-user-1",
                new_secret_value="demo-v3",
            )

    def test_entropy_separates_testnet_and_demo_for_same_logical_handle(self):
        common = dict(
            handle_id="cred-same",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            purpose="TRADE",
            generation=1,
        )
        self.assertNotEqual(
            _scope_entropy(provider_environment="TESTNET", **common),
            _scope_entropy(provider_environment="DEMO", **common),
        )

    def test_direct_bybit_handle_cannot_omit_domain(self):
        with self.assertRaises(SecretVaultError):
            PersistentCredentialHandle(
                handle_id="cred-direct",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                purpose="TRADE",
                generation=1,
            )

    def test_non_bybit_default_remains_explicit_in_persisted_scope(self):
        handle = self.vault.register(
            handle_id="cred-simulated",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="sim-secret",
        )
        self.assertEqual(handle.provider_environment, "PAPER")
        self.assertEqual(
            self.vault.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
            ),
            "sim-secret",
        )

    def test_hostile_secret_text_is_rejected_before_callbacks_or_mutation(self):
        before = self.path.read_bytes()
        with self.assertRaisesRegex(SecretVaultError, "exact non-empty text"):
            self.vault.register(
                handle_id="cred-hostile-secret",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value=_HostileText("secret"),
            )
        self.assertEqual(self.path.read_bytes(), before)

        handle = self.vault.register(
            handle_id="cred-rotate-hostile-secret",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="secret-v1",
        )
        before_rotate = self.path.read_bytes()
        with self.assertRaisesRegex(SecretVaultError, "exact non-empty text"):
            self.vault.rotate(
                handle,
                execution_identity="windows-user-1",
                new_secret_value=_HostileText("secret-v2"),
            )
        self.assertEqual(self.path.read_bytes(), before_rotate)

    def test_hostile_provider_domain_text_is_rejected_before_callbacks(self):
        before = self.path.read_bytes()
        with self.assertRaises(SecretVaultError):
            self.vault.register(
                handle_id="cred-hostile",
                owner_identity="windows-user-1",
                account_id="paper-1",
                provider="BYBIT",
                environment="PAPER",
                provider_environment=_HostileText("TESTNET"),
                purpose="TRADE",
                secret_value="must-not-store",
            )
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
