from __future__ import annotations

from hashlib import sha256
import inspect
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import credential_transition_receipt as transition
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"receipt-transitive-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CredentialTransitionReceiptTransitiveAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )
        self.handle = self.vault.register(
            handle_id="cred-transitive-authority",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="secret-v1",
        )

    def _rebound(self, name: str, calls: list[object]):
        def rebound(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(f"pre-call rebound authority executed: {name}")

        return rebound

    def _assert_rotate_ignores_module_rebind(self, name: str) -> None:
        calls: list[object] = []
        with patch.object(transition, name, new=self._rebound(name, calls)):
            current, receipt = transition.rotate_trade_credential_with_receipt(
                self.vault,
                self.handle,
                execution_identity="windows-user-1",
                new_secret_value="secret-v2",
            )
        self.assertEqual(current.generation, self.handle.generation + 1)
        self.assertEqual(receipt.operation, "ROTATED")
        self.assertEqual(calls, [])

    def _assert_rotate_ignores_vault_method_rebind(self, name: str) -> None:
        calls: list[object] = []
        with patch.object(
            ProtectedCredentialVault,
            name,
            new=self._rebound(name, calls),
        ):
            current, receipt = transition.rotate_trade_credential_with_receipt(
                self.vault,
                self.handle,
                execution_identity="windows-user-1",
                new_secret_value="secret-v2",
            )
        self.assertEqual(current.generation, self.handle.generation + 1)
        self.assertEqual(receipt.operation, "ROTATED")
        self.assertEqual(calls, [])

    def _assert_revoke_ignores_module_rebind(self, name: str) -> None:
        calls: list[object] = []
        with patch.object(transition, name, new=self._rebound(name, calls)):
            receipt = transition.revoke_trade_credential_with_receipt(
                self.vault,
                self.handle,
                execution_identity="windows-user-1",
            )
        self.assertEqual(receipt.operation, "REVOKED")
        self.assertEqual(calls, [])

    def _assert_revoke_ignores_vault_method_rebind(self, name: str) -> None:
        calls: list[object] = []
        with patch.object(
            ProtectedCredentialVault,
            name,
            new=self._rebound(name, calls),
        ):
            receipt = transition.revoke_trade_credential_with_receipt(
                self.vault,
                self.handle,
                execution_identity="windows-user-1",
            )
        self.assertEqual(receipt.operation, "REVOKED")
        self.assertEqual(calls, [])

    def _issued_receipt(self):
        return transition.revoke_trade_credential_with_receipt(
            self.vault,
            self.handle,
            execution_identity="windows-user-1",
        )

    def _assert_verify_ignores_module_rebind(self, name: str) -> None:
        receipt = self._issued_receipt()
        calls: list[object] = []
        with patch.object(transition, name, new=self._rebound(name, calls)):
            verified = transition.verify_trade_credential_transition_receipt(
                self.vault,
                receipt,
            )
        self.assertEqual(verified, receipt)
        self.assertEqual(calls, [])

    def _assert_verify_ignores_vault_method_rebind(self, name: str) -> None:
        receipt = self._issued_receipt()
        calls: list[object] = []
        with patch.object(
            ProtectedCredentialVault,
            name,
            new=self._rebound(name, calls),
        ):
            verified = transition.verify_trade_credential_transition_receipt(
                self.vault,
                receipt,
            )
        self.assertEqual(verified, receipt)
        self.assertEqual(calls, [])

    def test_public_entrypoints_expose_no_authority_override_parameters(self) -> None:
        for operation in (
            transition.rotate_trade_credential_with_receipt,
            transition.revoke_trade_credential_with_receipt,
            transition.verify_trade_credential_transition_receipt,
        ):
            with self.subTest(operation=operation.__name__):
                parameters = inspect.signature(operation).parameters
                self.assertFalse(
                    any(name.startswith("_") for name in parameters),
                    parameters,
                )

    def test_revoke_rejects_caller_authority_override_before_mutation(self) -> None:
        before = self.path.read_bytes()
        with self.assertRaises(TypeError):
            transition.revoke_trade_credential_with_receipt(
                self.vault,
                self.handle,
                execution_identity="windows-user-1",
                _file_lock=lambda *args, **kwargs: None,
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_verify_rejects_caller_authority_override(self) -> None:
        receipt = self._issued_receipt()
        with self.assertRaises(TypeError):
            transition.verify_trade_credential_transition_receipt(
                self.vault,
                receipt,
                _vault_load=lambda vault: {},
            )

    def test_rotate_ignores_pre_call_lock_rebind(self) -> None:
        self._assert_rotate_ignores_module_rebind("_exclusive_file_lock")

    def test_rotate_ignores_pre_call_text_rebind(self) -> None:
        self._assert_rotate_ignores_module_rebind("_text")

    def test_rotate_ignores_pre_call_scope_entropy_rebind(self) -> None:
        self._assert_rotate_ignores_module_rebind("_scope_entropy")

    def test_rotate_ignores_pre_call_issue_helper_rebind(self) -> None:
        self._assert_rotate_ignores_module_rebind("_issue_locked")

    def test_rotate_ignores_pre_call_vault_load_rebind(self) -> None:
        self._assert_rotate_ignores_vault_method_rebind("_load")

    def test_rotate_ignores_pre_call_vault_handle_rebind(self) -> None:
        self._assert_rotate_ignores_vault_method_rebind("_handle")

    def test_rotate_ignores_pre_call_identity_proof_rebind(self) -> None:
        self._assert_rotate_ignores_vault_method_rebind(
            "_prove_current_identity_can_decrypt"
        )

    def test_rotate_ignores_pre_call_vault_write_rebind(self) -> None:
        self._assert_rotate_ignores_vault_method_rebind("_write")

    def test_revoke_ignores_pre_call_lock_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_exclusive_file_lock")

    def test_revoke_ignores_pre_call_text_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_text")

    def test_revoke_ignores_pre_call_issue_helper_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_issue_locked")

    def test_revoke_ignores_pre_call_authority_section_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_authority_section")

    def test_revoke_ignores_pre_call_next_sequence_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_next_sequence")

    def test_revoke_ignores_pre_call_vault_authority_digest_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_vault_authority_digest")

    def test_revoke_ignores_pre_call_record_state_digest_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_record_state_digest")

    def test_revoke_ignores_pre_call_receipt_id_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_receipt_id_from_subject")

    def test_revoke_ignores_pre_call_seal_entropy_rebind(self) -> None:
        self._assert_revoke_ignores_module_rebind("_seal_entropy")

    def test_revoke_ignores_pre_call_vault_load_rebind(self) -> None:
        self._assert_revoke_ignores_vault_method_rebind("_load")

    def test_revoke_ignores_pre_call_vault_handle_rebind(self) -> None:
        self._assert_revoke_ignores_vault_method_rebind("_handle")

    def test_revoke_ignores_pre_call_identity_proof_rebind(self) -> None:
        self._assert_revoke_ignores_vault_method_rebind(
            "_prove_current_identity_can_decrypt"
        )

    def test_revoke_ignores_pre_call_vault_write_rebind(self) -> None:
        self._assert_revoke_ignores_vault_method_rebind("_write")

    def test_verify_ignores_pre_call_lock_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_exclusive_file_lock")

    def test_verify_ignores_pre_call_vault_load_rebind(self) -> None:
        self._assert_verify_ignores_vault_method_rebind("_load")

    def test_verify_ignores_pre_call_vault_handle_rebind(self) -> None:
        self._assert_verify_ignores_vault_method_rebind("_handle")

    def test_verify_ignores_pre_call_authority_section_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_authority_section")

    def test_verify_ignores_pre_call_parse_receipt_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_parse_receipt")

    def test_verify_ignores_pre_call_receipt_id_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_receipt_id_from_subject")

    def test_verify_ignores_pre_call_receipt_subject_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_receipt_subject")

    def test_verify_ignores_pre_call_vault_authority_digest_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_vault_authority_digest")

    def test_verify_ignores_pre_call_text_digest_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_text_digest")

    def test_verify_ignores_pre_call_record_state_digest_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_record_state_digest")

    def test_verify_ignores_pre_call_seal_entropy_rebind(self) -> None:
        self._assert_verify_ignores_module_rebind("_seal_entropy")


if __name__ == "__main__":
    unittest.main()
