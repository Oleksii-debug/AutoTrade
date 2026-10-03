from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.credential_transition_journal as journal_module
from mvp.autotrade_mvp.credential_transition_journal import (
    CredentialTransitionAnchorError,
    record_current_trade_credential_transition_anchor,
    require_current_trade_credential_transition_anchor,
)
from mvp.autotrade_mvp.credential_transition_receipt import (
    CredentialTransitionReceiptError,
    rotate_trade_credential_with_receipt,
    verify_trade_credential_transition_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class DeterministicProtector:
    PREFIX = b"anchor-test-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class CredentialTransitionJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.vault_path = root / "credentials.json"
        self.journal_path = root / "journal.sqlite3"
        self.vault = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )
        self.store = JournalStore(self.journal_path)
        self.first = self.vault.register(
            handle_id="cred-wp49-anchor",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="TESTNET",
            purpose="TRADE",
            secret_value="secret-v1",
        )

    def _rotate(self, handle, secret):
        return rotate_trade_credential_with_receipt(
            self.vault,
            handle,
            execution_identity="windows-user-1",
            new_secret_value=secret,
        )

    def test_current_receipt_anchor_is_durable_and_idempotent(self) -> None:
        second, receipt = self._rotate(self.first, "secret-v2")

        first_witness = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt,
        )
        second_witness = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt,
        )
        required = require_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt,
        )

        self.assertEqual(first_witness, second_witness)
        self.assertEqual(required, first_witness)
        self.assertEqual(first_witness.receipt_id, receipt.receipt_id)
        self.assertEqual(
            first_witness.transition_sequence,
            receipt.transition_sequence,
        )
        self.assertEqual(second.generation, 2)

        events = self.store.load_events(
            "credential_transition_anchor",
            first_witness.aggregate_id,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_id"], first_witness.event_id)
        self.assertEqual(events[0]["payload"]["receipt_schema_version"], "2.0.0")
        self.assertEqual(events[0]["payload"]["provider_environment"], "TESTNET")
        self.assertEqual(
            events[0]["journal_sequence"],
            first_witness.journal_sequence,
        )
        self.assertGreaterEqual(
            first_witness.verified_journal_cut,
            first_witness.journal_sequence,
        )
        self.assertEqual(
            required.verified_journal_cut,
            self.store.current_journal_sequence(),
        )

    def test_provider_domains_have_distinct_durable_aggregate_identity(self) -> None:
        _second, testnet_receipt = self._rotate(self.first, "secret-v2")
        testnet_witness = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            testnet_receipt,
        )

        demo_path = self.vault_path.with_name("demo-credentials.json")
        demo_vault = ProtectedCredentialVault(
            demo_path,
            protector=DeterministicProtector(),
        )
        demo_first = demo_vault.register(
            handle_id="cred-wp49-anchor",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="BYBIT",
            environment="PAPER",
            provider_environment="DEMO",
            purpose="TRADE",
            secret_value="demo-secret-v1",
        )
        _demo_second, demo_receipt = rotate_trade_credential_with_receipt(
            demo_vault,
            demo_first,
            execution_identity="windows-user-1",
            new_secret_value="demo-secret-v2",
        )
        demo_witness = record_current_trade_credential_transition_anchor(
            self.store,
            demo_vault,
            demo_receipt,
        )

        self.assertNotEqual(testnet_witness.aggregate_id, demo_witness.aggregate_id)
        self.assertEqual(testnet_receipt.provider_environment, "TESTNET")
        self.assertEqual(demo_receipt.provider_environment, "DEMO")

    def test_historical_receipt_content_identity_is_revalidated(self) -> None:
        _second, receipt = self._rotate(self.first, "secret-v2")
        forged = replace(
            receipt,
            completed_time_ns=receipt.completed_time_ns + 1,
        )
        self.assertEqual(forged.receipt_id, receipt.receipt_id)
        aggregate_id = journal_module._aggregate_id(forged)
        payload = journal_module._event_payload(forged)
        event = {
            "event_id": journal_module._event_id(aggregate_id, forged),
            "event_type": "CredentialTransitionAnchored",
            "aggregate_type": "credential_transition_anchor",
            "aggregate_id": aggregate_id,
            "aggregate_version": 1,
            "payload": payload,
            "payload_hash": journal_module.payload_digest(payload),
            "journal_sequence": 1,
        }

        with self.assertRaisesRegex(
            CredentialTransitionAnchorError,
            "receipt content identity mismatch",
        ):
            journal_module._validate_chain(aggregate_id, [event])

    def test_vault_only_rollback_cannot_roll_back_newer_journal_anchor(self) -> None:
        second, receipt_one = self._rotate(self.first, "secret-v2")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )
        rolled_back_bytes = self.vault_path.read_bytes()

        third, receipt_two = self._rotate(second, "secret-v3")
        witness_two = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_two,
        )
        self.assertEqual(third.generation, 3)
        self.assertEqual(witness_two.transition_sequence, 2)

        # Restore the complete older vault file, including its old valid receipt,
        # authority instance, protector seal and credential generation.
        self.vault_path.write_bytes(rolled_back_bytes)
        rolled_back = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )

        # Vault-only verification cannot distinguish this coherent rollback.
        self.assertEqual(
            verify_trade_credential_transition_receipt(
                rolled_back,
                receipt_one,
            ),
            receipt_one,
        )

        # The independent monotonic JournalStore history can.
        with self.assertRaisesRegex(
            CredentialTransitionAnchorError,
            "not the latest durable anchor",
        ):
            require_current_trade_credential_transition_anchor(
                self.store,
                rolled_back,
                receipt_one,
            )
        with self.assertRaisesRegex(
            CredentialTransitionAnchorError,
            "not the next durable receipt sequence",
        ):
            record_current_trade_credential_transition_anchor(
                self.store,
                rolled_back,
                receipt_one,
            )

    def test_divergent_rollback_branch_cannot_rejoin_durable_lineage(self) -> None:
        second, receipt_one = self._rotate(self.first, "secret-v2")
        first_witness = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )
        rolled_back_bytes = self.vault_path.read_bytes()

        _third, receipt_two = self._rotate(second, "secret-v3")
        second_witness = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_two,
        )
        self.assertEqual(receipt_two.previous_receipt_id, receipt_one.receipt_id)

        self.vault_path.write_bytes(rolled_back_bytes)
        rolled_back = ProtectedCredentialVault(
            self.vault_path,
            protector=DeterministicProtector(),
        )
        alternative_third, alternative_two = rotate_trade_credential_with_receipt(
            rolled_back,
            second,
            execution_identity="windows-user-1",
            new_secret_value="alternative-v3",
        )
        _alternative_fourth, alternative_three = (
            rotate_trade_credential_with_receipt(
                rolled_back,
                alternative_third,
                execution_identity="windows-user-1",
                new_secret_value="alternative-v4",
            )
        )

        self.assertEqual(alternative_two.transition_sequence, 2)
        self.assertNotEqual(alternative_two.receipt_id, receipt_two.receipt_id)
        self.assertEqual(
            alternative_three.previous_receipt_id,
            alternative_two.receipt_id,
        )
        self.assertEqual(
            alternative_three.transition_sequence,
            receipt_two.transition_sequence + 1,
        )

        with self.assertRaisesRegex(
            CredentialTransitionAnchorError,
            "does not descend from the latest durable receipt",
        ):
            record_current_trade_credential_transition_anchor(
                self.store,
                rolled_back,
                alternative_three,
            )

        events = self.store.load_events(
            "credential_transition_anchor",
            first_witness.aggregate_id,
        )
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1]["event_id"], second_witness.event_id)

    def test_missing_intermediate_anchor_fails_closed(self) -> None:
        second, receipt_one = self._rotate(self.first, "secret-v2")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )

        third, _receipt_two = self._rotate(second, "secret-v3")
        _fourth, receipt_three = self._rotate(third, "secret-v4")

        with self.assertRaisesRegex(
            CredentialTransitionAnchorError,
            "not the next durable receipt sequence",
        ):
            record_current_trade_credential_transition_anchor(
                self.store,
                self.vault,
                receipt_three,
            )

    def test_newer_anchor_supersedes_older_receipt(self) -> None:
        second, receipt_one = self._rotate(self.first, "secret-v2")
        record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_one,
        )
        _third, receipt_two = self._rotate(second, "secret-v3")
        witness_two = record_current_trade_credential_transition_anchor(
            self.store,
            self.vault,
            receipt_two,
        )

        with self.assertRaises(CredentialTransitionReceiptError):
            require_current_trade_credential_transition_anchor(
                self.store,
                self.vault,
                receipt_one,
            )
        self.assertEqual(
            require_current_trade_credential_transition_anchor(
                self.store,
                self.vault,
                receipt_two,
            ),
            witness_two,
        )

    def test_exact_authority_types_are_required(self) -> None:
        _second, receipt = self._rotate(self.first, "secret-v2")

        class JournalSubclass(JournalStore):
            pass

        with self.assertRaises(TypeError):
            record_current_trade_credential_transition_anchor(
                JournalSubclass(self.journal_path.with_name("other.sqlite3")),
                self.vault,
                receipt,
            )


if __name__ == "__main__":
    unittest.main()
