from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.credential_transition_journal as journal_module
from mvp.autotrade_mvp.credential_transition_journal import (
    CredentialTransitionAnchorError,
    record_current_trade_credential_transition_anchor,
    require_current_trade_credential_transition_anchor,
)
from mvp.autotrade_mvp.credential_transition_receipt import (
    CredentialTransitionReceiptError,
    rotate_trade_credential_with_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"anchor-rebinding-authority-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CredentialTransitionJournalRebindingAuthorityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.vault_path = root / "credentials.json"
        self.vault = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )
        self.store = JournalStore(root / "journal.sqlite3")
        self.first = self.vault.register(
            handle_id="cred-anchor-rebind",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="test-value-v1",
        )

    def _rotate(self, vault, handle, value):
        return rotate_trade_credential_with_receipt(
            vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value=value,
        )

    def _stale_anchor_fixture(self):
        second, receipt_one = self._rotate(self.vault, self.first, "test-value-v2")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )
        _third, _receipt_two = self._rotate(self.vault, second, "test-value-v3")
        return receipt_one

    def _rollback_fixture(self):
        second, receipt_one = self._rotate(self.vault, self.first, "test-value-v2")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )
        rolled_back_bytes = self.vault_path.read_bytes()
        _third, receipt_two = self._rotate(self.vault, second, "test-value-v3")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_two,
        )
        self.vault_path.write_bytes(rolled_back_bytes)
        rolled_back = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )
        return rolled_back, receipt_one

    def test_public_receipt_verifier_rebind_cannot_authorize_stale_anchor(self) -> None:
        receipt_one = self._stale_anchor_fixture()
        calls = []

        def rebound_verifier(vault, receipt):
            calls.append(receipt.receipt_id)
            return receipt

        with patch.object(
            journal_module,
            "verify_trade_credential_transition_receipt",
            new=rebound_verifier,
        ):
            with self.assertRaises(CredentialTransitionReceiptError):
                require_current_trade_credential_transition_anchor(
                    self.store,
                    self.vault,
                    receipt_one,
                )

        self.assertEqual(
            calls,
            [],
            "anchor authority must not dispatch through a rebound public receipt verifier",
        )

    def test_private_canonical_verifier_rebind_before_call_is_never_executed(self) -> None:
        receipt_one = self._stale_anchor_fixture()
        calls = []

        def rebound_verifier(vault, receipt):
            calls.append(receipt.receipt_id)
            return receipt

        with patch.object(
            journal_module,
            "_CANONICAL_VERIFY_TRADE_CREDENTIAL_TRANSITION_RECEIPT",
            new=rebound_verifier,
        ):
            with self.assertRaises(CredentialTransitionReceiptError):
                require_current_trade_credential_transition_anchor(
                    self.store,
                    self.vault,
                    receipt_one,
                )

        self.assertEqual(
            calls,
            [],
            "module-private canonical names must not remain pre-call authority",
        )

    def test_public_load_events_rebind_cannot_hide_newer_durable_anchor(self) -> None:
        rolled_back, receipt_one = self._rollback_fixture()
        installed_load_events = JournalStore.load_events
        calls = []

        def rebound_load_events(store, aggregate_type, aggregate_id):
            calls.append((aggregate_type, aggregate_id))
            events = installed_load_events(store, aggregate_type, aggregate_id)
            if aggregate_type == "credential_transition_anchor":
                return events[:1]
            return events

        with patch.object(JournalStore, "load_events", new=rebound_load_events):
            with self.assertRaisesRegex(
                CredentialTransitionAnchorError,
                "not the next durable receipt sequence",
            ):
                record_current_trade_credential_transition_anchor(
                    self.store,
                    rolled_back,
                    receipt_one,
                )

        self.assertEqual(
            calls,
            [],
            "durable anchor lineage must not dispatch through rebound JournalStore.load_events",
        )

    def test_private_canonical_load_rebind_before_call_is_never_executed(self) -> None:
        rolled_back, receipt_one = self._rollback_fixture()
        installed_load_events = JournalStore.load_events
        calls = []

        def rebound_load_events(store, aggregate_type, aggregate_id):
            calls.append((aggregate_type, aggregate_id))
            events = installed_load_events(store, aggregate_type, aggregate_id)
            if aggregate_type == "credential_transition_anchor":
                return events[:1]
            return events

        with patch.object(
            journal_module,
            "_CANONICAL_JOURNAL_LOAD_EVENTS",
            new=rebound_load_events,
        ):
            with self.assertRaisesRegex(
                CredentialTransitionAnchorError,
                "not the next durable receipt sequence",
            ):
                record_current_trade_credential_transition_anchor(
                    self.store,
                    rolled_back,
                    receipt_one,
                )

        self.assertEqual(
            calls,
            [],
            "module-private canonical JournalStore names must not remain pre-call authority",
        )


if __name__ == "__main__":
    unittest.main()
