from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp import production_trading_host as trading_host
from mvp.autotrade_mvp import recovery_takeover
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_host import ProductionHostConfig
from mvp.autotrade_mvp.reconciliation import (
    ProviderFillEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import record_reconciliation_checkpoint
from mvp.autotrade_mvp.recovery import HostState, OwnerFence, RecoveryController
from mvp.autotrade_mvp.windows_secrets import ProtectedCredentialVault


class _DeterministicProtector:
    PREFIX = b"production-host-takeover-v1:"

    def protect(self, plaintext: bytes, *, entropy: bytes) -> bytes:
        return self.PREFIX + sha256(entropy).digest() + plaintext[::-1]

    def unprotect(self, ciphertext: bytes, *, entropy: bytes) -> bytes:
        expected = self.PREFIX + sha256(entropy).digest()
        if not ciphertext.startswith(expected):
            raise OSError("scope entropy mismatch")
        return ciphertext[len(expected):][::-1]


class _Fence:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.released = False

    def release(self) -> None:
        self.released = True


class _Host:
    def __init__(self, config: ProductionHostConfig, store: JournalStore) -> None:
        self.config = config
        self.journal = store
        self.store_identity = store.store_identity
        self.application = Mock()
        self.server = Mock()
        self._instance_fence = _Fence(Path(str(store.path) + ".host.lock"))
        self.closed = False
        self.shutdown_requested = False
        self.serving = False
        self._terminal_finalizer = None

    def bind_terminal_finalizer(self, finalizer) -> None:
        if self._terminal_finalizer is not None:
            raise RuntimeError("terminal finalizer is already bound")
        self._terminal_finalizer = finalizer

    def close(self) -> None:
        if not self.closed:
            if self._terminal_finalizer is not None:
                self._terminal_finalizer()
            self._instance_fence.release()
            self.closed = True
            self.shutdown_requested = True

    def serve_forever(self, *, poll_interval: float = 0.5) -> None:
        del poll_interval
        self.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        del exc_type, exc, traceback
        self.close()


def _reconciliation():
    snapshot = SnapshotConsistencyEvidence(
        provider_id="SIMULATED",
        account_id="acct",
        environment="PAPER",
        mode="ATOMIC",
        query_started_at="2026-10-04T03:10:00Z",
        query_completed_at="2026-10-04T03:11:00Z",
    )
    fill = ProviderFillEvidence.create(
        provider_id="SIMULATED",
        account_id="acct",
        environment="PAPER",
        provider_execution_id="exec-1",
        client_order_id="client-1",
        instrument="ABC",
        quantity="1",
        price="100",
        fee_currency="USD",
        trade_time="2026-10-04T03:10:30Z",
    )
    return reconcile_account(
        provider_id="SIMULATED",
        account_id="acct",
        environment="PAPER",
        local_cash={"USD": "900"},
        provider_cash={"USD": "900"},
        local_positions={"ABC": "1"},
        provider_positions={"ABC": "1"},
        local_execution_ids=["exec-1"],
        provider_fills=[fill],
        snapshot_consistency=snapshot,
        coverage_start="2026-10-04T03:10:00Z",
        coverage_end="2026-10-04T03:11:00Z",
        pagination_complete=True,
        provider_activity_provider_id="SIMULATED",
        provider_activity_account_id="acct",
    )


class ProductionTradingHostDurableTakeoverTests(unittest.TestCase):
    def test_clean_restart_executes_real_takeover_before_dispatcher_exposure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = ProductionHostConfig(
                journal_path=root / "journal.sqlite3",
                account_id="acct",
                environment="PAPER",
                host_id="host-process-b",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            store = JournalStore(config.journal_path)
            prior = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            prior.start("owner-a")
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="restart-ready",
                result=_reconciliation(),
                observed_at="2026-10-04T03:11:00Z",
                host_id="owner-a",
                owner_epoch="1",
            )
            prior.record_reconciliation_checkpoint(
                reconciliation_id="restart-ready",
                provider_id="SIMULATED",
                account_id="acct",
                environment="PAPER",
            )
            self.assertEqual(prior.state, HostState.READY)
            prior.stop()

            vault = ProtectedCredentialVault(
                root / "credentials.json",
                protector=_DeterministicProtector(),
            )
            handle = vault.register(
                handle_id="trade-credential",
                owner_identity="windows-user",
                account_id="acct",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="secret-v1",
            )
            host = _Host(config, store)
            original_fence = host._instance_fence

            with patch.object(
                trading_host,
                "build_production_host",
                return_value=host,
            ):
                runtime = trading_host.build_production_trading_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                    recovery_owner_id="owner-b",
                    takeover=trading_host.DurableTakeoverInputs(
                        vault=vault,
                        handle=handle,
                        execution_identity="windows-user",
                        reconciliation_id="restart-ready",
                        provider_id="SIMULATED",
                    ),
                )
            try:
                self.assertEqual(runtime.recovery.owner.owner_id, "owner-b")
                self.assertEqual(runtime.recovery.owner.epoch, 2)
                self.assertEqual(runtime.recovery.state, HostState.RECOVERING)
                self.assertEqual(runtime.dispatcher.owner_id, "owner-b")
                self.assertEqual(runtime.dispatcher.owner_epoch, 2)
                self.assertEqual(
                    [
                        (owner.owner_id, owner.epoch)
                        for owner in runtime.recovery.durable_owner_chain()
                    ],
                    [("owner-a", 1), ("owner-b", 2)],
                )
                with self.assertRaisesRegex(PermissionError, "unavailable"):
                    vault.resolve(
                        handle,
                        execution_identity="windows-user",
                        account_id="acct",
                        provider="SIMULATED",
                        environment="PAPER",
                        purpose="TRADE",
                    )
                self.assertFalse(original_fence.released)
            finally:
                runtime.close()

            self.assertTrue(original_fence.released)
            self.assertIsNone(runtime.recovery.owner)
            verifier = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            self.assertEqual(
                [(owner.owner_id, owner.epoch) for owner in verifier.durable_owner_chain()],
                [("owner-a", 1), ("owner-b", 2)],
            )


    def test_restart_resumes_after_target_owner_commit_before_takeover_completion(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config = ProductionHostConfig(
                journal_path=root / "journal.sqlite3",
                account_id="acct",
                environment="PAPER",
                host_id="host-process-c",
                bind_host="127.0.0.1",
                bind_port=8765,
                public_origin="http://127.0.0.1:8765",
            )
            store = JournalStore(config.journal_path)
            prior = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            prior.start("owner-a")
            record_reconciliation_checkpoint(
                store,
                reconciliation_id="restart-ready",
                result=_reconciliation(),
                observed_at="2026-10-04T03:11:00Z",
                host_id="owner-a",
                owner_epoch="1",
            )
            prior.record_reconciliation_checkpoint(
                reconciliation_id="restart-ready",
                provider_id="SIMULATED",
                account_id="acct",
                environment="PAPER",
            )
            self.assertEqual(prior.state, HostState.READY)
            prior.stop()

            vault = ProtectedCredentialVault(
                root / "credentials.json",
                protector=_DeterministicProtector(),
            )
            handle = vault.register(
                handle_id="trade-credential",
                owner_identity="windows-user",
                account_id="acct",
                provider="SIMULATED",
                environment="PAPER",
                purpose="TRADE",
                secret_value="secret-v1",
            )

            interrupted = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            trading_host._stage_durable_source_for_immediate_takeover(
                interrupted,
                OwnerFence("owner-a", 1),
            )
            original_append = recovery_takeover._append_takeover_event

            def crash_before_completion(
                event_store,
                *,
                takeover_id,
                version,
                event_type,
                payload,
            ):
                if event_type == "RecoveryTakeoverOwnerCommitted":
                    raise RuntimeError("simulated crash before takeover completion")
                return original_append(
                    event_store,
                    takeover_id=takeover_id,
                    version=version,
                    event_type=event_type,
                    payload=payload,
                )

            with patch.object(
                recovery_takeover,
                "_append_takeover_event",
                side_effect=crash_before_completion,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "simulated crash before takeover completion",
                ):
                    recovery_takeover.execute_durable_takeover(
                        interrupted,
                        new_owner_id="owner-b",
                        vault=vault,
                        handle=handle,
                        execution_identity="windows-user",
                        reconciliation_id="restart-ready",
                        provider_id="SIMULATED",
                    )

            verifier = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            self.assertEqual(
                [(owner.owner_id, owner.epoch) for owner in verifier.durable_owner_chain()],
                [("owner-a", 1), ("owner-b", 2)],
            )
            takeover_events = store.load_events_by_aggregate_type("recovery_takeover")
            self.assertEqual(
                [event["event_type"] for event in takeover_events],
                ["RecoveryTakeoverStarted", "RecoveryTakeoverEvidenceIssued"],
            )

            host = _Host(config, store)
            original_fence = host._instance_fence
            with patch.object(
                trading_host,
                "build_production_host",
                return_value=host,
            ):
                runtime = trading_host.build_production_trading_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                    recovery_owner_id="owner-b",
                    takeover=trading_host.DurableTakeoverInputs(
                        vault=vault,
                        handle=handle,
                        execution_identity="windows-user",
                        reconciliation_id="restart-ready",
                        provider_id="SIMULATED",
                    ),
                )
            try:
                self.assertEqual(runtime.recovery.owner, OwnerFence("owner-b", 2))
                self.assertEqual(runtime.recovery.state, HostState.RECOVERING)
                self.assertEqual(runtime.dispatcher.owner_id, "owner-b")
                self.assertEqual(runtime.dispatcher.owner_epoch, 2)
                self.assertEqual(
                    [(owner.owner_id, owner.epoch) for owner in runtime.recovery.durable_owner_chain()],
                    [("owner-a", 1), ("owner-b", 2)],
                )
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in store.load_events_by_aggregate_type("recovery_takeover")
                    ],
                    [
                        "RecoveryTakeoverStarted",
                        "RecoveryTakeoverEvidenceIssued",
                        "RecoveryTakeoverOwnerCommitted",
                    ],
                )
                self.assertFalse(original_fence.released)
            finally:
                runtime.close()

            self.assertTrue(original_fence.released)


if __name__ == "__main__":
    unittest.main()
