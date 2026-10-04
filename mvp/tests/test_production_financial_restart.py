from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_restart import (
    resume_production_financial_host,
)
from mvp.autotrade_mvp.production_host import ProductionHostConfig, ProductionHostRuntime
from mvp.autotrade_mvp.recovery import HostState, RecoveryController
from mvp.autotrade_mvp.sender_authority import SenderAuthorityError


class ProductionFinancialRestartTests(unittest.TestCase):
    def _config(self, root: str, *, host_id: str = "host-a") -> ProductionHostConfig:
        return ProductionHostConfig(
            journal_path=Path(root) / "financial-host.sqlite",
            account_id="account-1",
            environment="PAPER",
            host_id=host_id,
            bind_host="127.0.0.1",
            bind_port=18766,
            public_origin="http://127.0.0.1:18766",
        )

    def _host(
        self,
        config: ProductionHostConfig,
        *,
        journal: JournalStore | None = None,
    ) -> ProductionHostRuntime:
        store = journal or JournalStore(config.journal_path)
        return ProductionHostRuntime(
            config=config,
            journal=store,
            application=Mock(),
            server=Mock(),
            instance_fence=Mock(),
            admission_gate=Mock(),
        )

    @staticmethod
    def _seed_owner(journal: JournalStore, owner_id: str = "host-a"):
        controller = RecoveryController(
            owner_store=journal,
            owner_scope="PAPER:account-1",
        )
        owner = controller.start(owner_id)
        controller.stop()
        return owner

    def test_same_owner_restart_reuses_no_epoch_and_starts_recovering(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            journal = JournalStore(config.journal_path)
            owner = self._seed_owner(journal)
            host = self._host(config, journal=journal)

            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ):
                runtime = resume_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            self.assertEqual(runtime.owner, owner)
            self.assertEqual(runtime.owner.epoch, 1)
            self.assertEqual(runtime.recovery_controller.owner, owner)
            self.assertIs(runtime.recovery_controller.state, HostState.RECOVERING)
            self.assertFalse(runtime.recovery_controller.provider_reconciled)
            self.assertIn(
                "startup_reconciliation_required",
                runtime.recovery_controller.reason_codes,
            )
            self.assertEqual(
                runtime.recovery_controller.durable_owner_chain(),
                (owner,),
            )
            self.assertEqual(
                len(journal.load_events("recovery_owner", "PAPER:account-1")),
                1,
            )
            self.assertEqual(runtime.dispatcher.owner, owner)

    def test_same_owner_restart_cannot_send_before_fresh_reconciliation(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            journal = JournalStore(config.journal_path)
            self._seed_owner(journal)
            host = self._host(config, journal=journal)
            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ):
                runtime = resume_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            wire_calls: list[str] = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"status": "accepted"}

            outcome = runtime.dispatcher.dispatch(
                attempt_id="restart-blocked",
                intent_id="intent-restart",
                intent_hash="hash-restart",
                provider="BYBIT",
                request={},
                now="2026-10-04T02:10:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(wire_calls, [])
            self.assertEqual(
                [
                    event["event_type"]
                    for event in journal.load_events(
                        "submission_attempt",
                        runtime.dispatcher._dispatcher._aggregate_id("restart-blocked"),
                    )
                ],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )

    def test_restart_recovers_durable_possible_send_uncertainty(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            journal = JournalStore(config.journal_path)
            owner = self._seed_owner(journal)
            prior_dispatcher = GuardedDispatcher(
                journal,
                environment="PAPER",
                account_id="account-1",
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )

            def ambiguous_transport(_client_order_id, _request, final_guard):
                final_guard()
                raise TimeoutError("provider outcome unknown")

            prior = prior_dispatcher.dispatch(
                attempt_id="restart-unknown",
                intent_id="intent-unknown",
                intent_hash="hash-unknown",
                provider="BYBIT",
                request={},
                now="2026-10-04T02:11:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                sender_check=lambda _owner_id, _owner_epoch: None,
                transport_send=ambiguous_transport,
            )
            self.assertEqual(prior.status, "UNKNOWN")

            host = self._host(config, journal=journal)
            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ):
                runtime = resume_production_financial_host(
                    config,
                    security_boundary=Mock(),
                    principal_resolver=Mock(),
                    snapshot_provider=Mock(),
                )

            self.assertIn("restart-unknown", runtime.recovery_controller.unresolved_attempts)
            self.assertIs(runtime.recovery_controller.state, HostState.RECOVERING)

    def test_different_durable_owner_requires_explicit_takeover_and_releases_host(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root, host_id="host-b")
            journal = JournalStore(config.journal_path)
            self._seed_owner(journal, owner_id="host-a")
            host = self._host(config, journal=journal)
            host.close = Mock()  # type: ignore[method-assign]

            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "different host; explicit takeover is required",
                ):
                    resume_production_financial_host(
                        config,
                        security_boundary=Mock(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )

            host.close.assert_called_once_with()
            self.assertEqual(
                [(item.owner_id, item.epoch) for item in RecoveryController(
                    owner_store=journal,
                    owner_scope="PAPER:account-1",
                ).durable_owner_chain()],
                [("host-a", 1)],
            )

    def test_empty_owner_journal_requires_first_owner_builder(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            journal = JournalStore(config.journal_path)
            host = self._host(config, journal=journal)
            host.close = Mock()  # type: ignore[method-assign]

            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ):
                with self.assertRaisesRegex(
                    PermissionError,
                    "use first-owner builder",
                ):
                    resume_production_financial_host(
                        config,
                        security_boundary=Mock(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )

            host.close.assert_called_once_with()
            self.assertEqual(
                journal.load_events("recovery_owner", "PAPER:account-1"),
                [],
            )

    def test_pending_takeover_freeze_blocks_same_owner_restart_attachment(self) -> None:
        with TemporaryDirectory() as root:
            config = self._config(root)
            journal = JournalStore(config.journal_path)
            self._seed_owner(journal)
            host = self._host(config, journal=journal)
            host.close = Mock()  # type: ignore[method-assign]

            with patch(
                "mvp.autotrade_mvp.production_financial_restart.build_production_host",
                return_value=host,
            ), patch(
                "mvp.autotrade_mvp.production_financial_restart.sender_authority_window",
                side_effect=SenderAuthorityError("pending durable takeover"),
            ):
                with self.assertRaisesRegex(
                    SenderAuthorityError,
                    "pending durable takeover",
                ):
                    resume_production_financial_host(
                        config,
                        security_boundary=Mock(),
                        principal_resolver=Mock(),
                        snapshot_provider=Mock(),
                    )

            host.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
