from __future__ import annotations

from base64 import b64decode, b64encode
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from mvp.autotrade_mvp import credential_transition_receipt as transition
from mvp.autotrade_mvp.credential_transition_receipt import (
    CredentialTransitionReceipt,
    CredentialTransitionReceiptError,
    revoke_trade_credential_with_receipt,
    rotate_trade_credential_with_receipt,
    verify_trade_credential_transition_receipt,
)
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"receipt-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class DifferentProtector(DeterministicProtector):
    PREFIX = b"different-identity:"


class CredentialTransitionReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

    def _register(self, *, purpose: str = "TRADE"):
        return self.vault.register(
            handle_id="cred-wp49-receipt",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose=purpose,
            secret_value="secret-v1",
        )

    def test_rotation_receipt_survives_restart_and_old_generation_is_stale(self) -> None:
        old = self._register()
        current, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            old,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

        verified = verify_trade_credential_transition_receipt(restarted, receipt)

        self.assertEqual(verified, receipt)
        self.assertEqual(receipt.operation, "ROTATED")
        self.assertIsNone(receipt.previous_receipt_id)
        self.assertEqual(receipt.prior_generation, old.generation)
        self.assertEqual(receipt.successor_generation, current.generation)
        self.assertTrue(receipt.active_after)
        with self.assertRaisesRegex(PermissionError, "generation is stale"):
            restarted.resolve(
                old,
                execution_identity="windows-user-1",
                account_id=old.account_id,
                provider=old.provider,
                environment=old.environment,
                purpose=old.purpose,
            )
        self.assertEqual(
            restarted.resolve(
                current,
                execution_identity="windows-user-1",
                account_id=current.account_id,
                provider=current.provider,
                environment=current.environment,
                purpose=current.purpose,
            ),
            "secret-v2",
        )

    def test_revoke_receipt_proves_current_inactive_terminal_state(self) -> None:
        handle = self._register()
        receipt = revoke_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
        )

        self.assertEqual(
            verify_trade_credential_transition_receipt(self.vault, receipt),
            receipt,
        )
        self.assertEqual(receipt.operation, "REVOKED")
        self.assertIsNone(receipt.successor_generation)
        self.assertFalse(receipt.active_after)
        with self.assertRaisesRegex(PermissionError, "unavailable"):
            self.vault.resolve(
                handle,
                execution_identity="windows-user-1",
                account_id=handle.account_id,
                provider=handle.provider,
                environment=handle.environment,
                purpose=handle.purpose,
            )
        with self.assertRaisesRegex(PermissionError, "unavailable"):
            self.vault.describe(handle.handle_id)

    def test_public_receipt_exposes_no_secret_ciphertext_or_raw_owner_identity(self) -> None:
        handle = self._register()
        current, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        public = json.dumps(asdict(receipt), sort_keys=True)

        self.assertNotIn("secret-v1", public)
        self.assertNotIn("secret-v2", public)
        self.assertNotIn("windows-user-1", public)
        self.assertNotIn("ciphertext", public.lower())
        self.assertEqual(receipt.handle_id, current.handle_id)

    def test_receipt_digest_fields_require_canonical_lowercase_hex(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )

        for field_name in (
            "owner_identity_sha256",
            "vault_authority_sha256",
            "record_state_sha256",
        ):
            with self.subTest(field_name=field_name), self.assertRaises(
                CredentialTransitionReceiptError
            ):
                replace(receipt, **{field_name: "sha256:" + "g" * 64})

        with self.assertRaises(CredentialTransitionReceiptError):
            replace(
                receipt,
                receipt_id="credential-transition/sha256:" + "G" * 64,
            )

    def test_self_authored_rehashed_receipt_is_not_vault_issued(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        forged = replace(
            receipt,
            completed_time_ns=receipt.completed_time_ns + 1,
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
            "stale or not vault-issued",
        ):
            verify_trade_credential_transition_receipt(self.vault, forged)

    def test_different_protector_identity_cannot_verify_issuer_seal(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        foreign_identity = ProtectedCredentialVault(
            self.path,
            protector=DifferentProtector(),
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "issuer seal cannot be verified",
        ):
            verify_trade_credential_transition_receipt(foreign_identity, receipt)

    def test_copied_vault_at_another_path_cannot_revalidate_receipt(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        copied_path = Path(self.directory.name) / "copied-credentials.json"
        copied_path.write_bytes(self.path.read_bytes())
        copied = ProtectedCredentialVault(
            copied_path,
            protector=DeterministicProtector(),
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "different vault authority",
        ):
            verify_trade_credential_transition_receipt(copied, receipt)

    def test_older_receipt_is_superseded_by_next_same_handle_transition(self) -> None:
        first = self._register()
        second, first_receipt = rotate_trade_credential_with_receipt(
            self.vault,
            first,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        third, second_receipt = rotate_trade_credential_with_receipt(
            self.vault,
            second,
            execution_identity="windows-user-1",
            new_secret_value="secret-v3",
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "stale or not vault-issued",
        ):
            verify_trade_credential_transition_receipt(self.vault, first_receipt)
        self.assertEqual(
            verify_trade_credential_transition_receipt(self.vault, second_receipt),
            second_receipt,
        )
        self.assertEqual(second_receipt.transition_sequence, 2)
        self.assertEqual(second_receipt.previous_receipt_id, first_receipt.receipt_id)
        self.assertEqual(second_receipt.successor_generation, third.generation)

    def test_transition_lineage_is_content_linked_to_exact_prior_receipt(self) -> None:
        first = self._register()
        second, first_receipt = rotate_trade_credential_with_receipt(
            self.vault,
            first,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        _third, second_receipt = rotate_trade_credential_with_receipt(
            self.vault,
            second,
            execution_identity="windows-user-1",
            new_secret_value="secret-v3",
        )

        self.assertIsNone(first_receipt.previous_receipt_id)
        self.assertEqual(
            second_receipt.previous_receipt_id,
            first_receipt.receipt_id,
        )
        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "predecessor receipt id",
        ):
            replace(
                second_receipt,
                previous_receipt_id="credential-transition/sha256:" + "g" * 64,
            )
        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "first credential transition",
        ):
            replace(
                first_receipt,
                previous_receipt_id=second_receipt.receipt_id,
            )

    def test_tampered_prior_receipt_cannot_be_laundered_by_next_rotation(self) -> None:
        first = self._register()
        second, _receipt = rotate_trade_credential_with_receipt(
            self.vault,
            first,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        stored = state["credential_transition_authority"]["latest_by_handle"][first.handle_id]
        stored["receipt"]["transition_sequence"] += 7
        self.path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "credential transition receipt content identity",
        ):
            rotate_trade_credential_with_receipt(
                self.vault,
                second,
                execution_identity="windows-user-1",
                new_secret_value="secret-v3",
            )

        self.assertEqual(
            self.vault.resolve(
                second,
                execution_identity="windows-user-1",
                account_id=second.account_id,
                provider=second.provider,
                environment=second.environment,
                purpose=second.purpose,
            ),
            "secret-v2",
        )

    def test_tampered_vault_instance_cannot_be_laundered_by_next_transition(self) -> None:
        first = self._register()
        second, _receipt = rotate_trade_credential_with_receipt(
            self.vault,
            first,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        state["credential_transition_authority"]["instance_id"] = "f" * 32
        self.path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "prior credential transition receipt vault authority",
        ):
            revoke_trade_credential_with_receipt(
                self.vault,
                second,
                execution_identity="windows-user-1",
            )

    def test_later_legacy_mutation_invalidates_prior_receipt(self) -> None:
        first = self._register()
        second, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            first,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        self.vault.rotate(
            second,
            execution_identity="windows-user-1",
            new_secret_value="secret-v3",
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "no longer current",
        ):
            verify_trade_credential_transition_receipt(self.vault, receipt)

    def test_read_credentials_cannot_mint_sender_transition_receipts(self) -> None:
        handle = self._register(purpose="READ")
        before = self.path.read_bytes()

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "READ credentials cannot issue",
        ):
            revoke_trade_credential_with_receipt(
                self.vault,
                handle,
                execution_identity="windows-user-1",
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_persisted_receipt_metadata_tamper_fails_after_restart(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        stored = state["credential_transition_authority"]["latest_by_handle"][handle.handle_id]
        stored["receipt"]["completed_time_ns"] += 1
        self.path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        restarted = ProtectedCredentialVault(
            self.path,
            protector=DeterministicProtector(),
        )

        with self.assertRaises(CredentialTransitionReceiptError):
            verify_trade_credential_transition_receipt(restarted, receipt)

    def test_authority_seal_tamper_fails_read_only_verification(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )
        state = json.loads(self.path.read_text(encoding="utf-8"))
        stored = state["credential_transition_authority"]["latest_by_handle"][handle.handle_id]
        seal = bytearray(b64decode(stored["seal_b64"], validate=True))
        seal[-1] ^= 1
        stored["seal_b64"] = b64encode(bytes(seal)).decode("ascii")
        self.path.write_text(
            json.dumps(state, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            CredentialTransitionReceiptError,
            "issuer seal",
        ):
            verify_trade_credential_transition_receipt(self.vault, receipt)

    def test_receipt_subclass_is_not_accepted_as_authority(self) -> None:
        handle = self._register()
        _, receipt = rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value="secret-v2",
        )

        class ForgedReceipt(CredentialTransitionReceipt):
            pass

        forged = ForgedReceipt(**asdict(receipt))
        with self.assertRaisesRegex(TypeError, "exact CredentialTransitionReceipt"):
            verify_trade_credential_transition_receipt(self.vault, forged)

    def test_transition_waits_for_active_execution_lease(self) -> None:
        handle = self._register()
        entered = Event()
        release = Event()

        def hold_lease() -> None:
            with self.vault.lease(
                handle,
                execution_identity="windows-user-1",
                account_id=handle.account_id,
                provider=handle.provider,
                environment=handle.environment,
                purpose=handle.purpose,
            ):
                entered.set()
                self.assertTrue(release.wait(timeout=5))

        with ThreadPoolExecutor(max_workers=2) as executor:
            lease_future = executor.submit(hold_lease)
            self.assertTrue(entered.wait(timeout=2))
            transition_future = executor.submit(
                revoke_trade_credential_with_receipt,
                self.vault,
                handle,
                execution_identity="windows-user-1",
            )
            with self.assertRaises(FuturesTimeoutError):
                transition_future.result(timeout=0.2)
            release.set()
            self.assertIsNone(lease_future.result(timeout=2))
            receipt = transition_future.result(timeout=2)

        self.assertEqual(receipt.operation, "REVOKED")
        self.assertEqual(
            verify_trade_credential_transition_receipt(self.vault, receipt),
            receipt,
        )


if __name__ == "__main__":
    unittest.main()