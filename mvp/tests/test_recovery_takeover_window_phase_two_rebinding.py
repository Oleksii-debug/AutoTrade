from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp import recovery_takeover as takeover_module
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.reconciliation import ProviderFillEvidence, SnapshotConsistencyEvidence, reconcile_account
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import RecoveryController
from mvp.autotrade_mvp.recovery_takeover import execute_durable_takeover
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class PhaseTwoRebindingProtector:
    PREFIX = b"takeover-window-phase-two-v1:"

    def __init__(self) -> None:
        self.on_next_protect = None

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        callback = self.on_next_protect
        if callback is not None:
            self.on_next_protect = None
            callback()
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


def _reconciliation():
    snapshot = SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-10-04T00:30:00Z",
        query_completed_at="2026-10-04T00:31:00Z",
    )
    fill = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        provider_execution_id="exec-window-phase-two",
        client_order_id="client-window-phase-two",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-10-04T00:30:30Z",
    )
    return reconcile_account(
        provider_id="SIMULATED",
        account_id="paper-1",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["exec-window-phase-two"],
        provider_fills=[fill],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-04T00:30:00Z",
        coverage_end="2026-10-04T00:31:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="paper-1",
    )


class RecoveryTakeoverWindowPhaseTwoRebindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = JournalStore(root / "journal.sqlite3")
        self.controller = RecoveryController(owner_store=self.store, owner_scope="PAPER:paper-1")
        self.controller.start("host-a")
        self.protector = PhaseTwoRebindingProtector()
        self.vault = ProtectedCredentialVault(root / "credentials.json", protector=self.protector)
        self.handle = self.vault.register(
            handle_id="trade-credential",
            owner_identity="windows-user-1",
            account_id="paper-1",
            provider="SIMULATED",
            environment="PAPER",
            purpose="TRADE",
            secret_value="test-value-v1",
        )
        record_reconciliation_checkpoint(
            self.store,
            reconciliation_id="takeover-ready",
            result=_reconciliation(),
            observed_at="2026-10-04T00:31:00Z",
            host_id="host-a",
            owner_epoch="1",
        )
        self.controller.record_reconciliation_checkpoint(
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
            account_id="paper-1",
            environment="PAPER",
        )

    def _execute(self):
        return execute_durable_takeover(
            self.controller,
            new_owner_id="host-b",
            vault=self.vault,
            handle=self.handle,
            execution_identity="windows-user-1",
            reconciliation_id="takeover-ready",
            provider_id="SIMULATED",
        )

    def _assert_private_authority_rebind_is_ignored(self, attribute: str) -> None:
        calls = []

        def rebound(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(f"pre-call private authority executed: {attribute}")

        with patch.object(takeover_module, attribute, new=rebound):
            result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(calls, [])

    def test_private_canonical_window_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_TAKEOVER_AUTHORITY_WINDOW"
        )

    def test_private_record_anchor_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_RECORD_CREDENTIAL_TRANSITION_ANCHOR"
        )

    def test_private_require_anchor_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_REQUIRE_CREDENTIAL_TRANSITION_ANCHOR"
        )

    def test_private_current_sequence_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_JOURNAL_CURRENT_SEQUENCE"
        )

    def test_private_append_event_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_JOURNAL_APPEND_EVENT"
        )

    def test_private_load_events_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_JOURNAL_LOAD_EVENTS"
        )

    def test_private_load_by_type_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_JOURNAL_LOAD_EVENTS_BY_AGGREGATE_TYPE"
        )

    def test_private_get_event_rebind_before_call_is_never_executed(self) -> None:
        self._assert_private_authority_rebind_is_ignored(
            "_CANONICAL_JOURNAL_GET_EVENT"
        )

    def test_phase_two_callback_cannot_retarget_phase_three_takeover_window(self) -> None:
        calls = []

        @contextmanager
        def rebound_window(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("phase-three rebound takeover window executed")
            yield

        patcher = patch.object(takeover_module, "takeover_authority_window", new=rebound_window)

        def rebind_during_credential_transition() -> None:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.protector.on_next_protect = rebind_during_credential_transition
        result = self._execute()

        self.assertEqual(result.target_owner.owner_id, "host-b")
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
