from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.production_financial_host import HostLifetimeGuardedDispatcher
from mvp.autotrade_mvp.recovery import RecoveryController


class ProductionFinancialHostSenderBindingTests(unittest.TestCase):
    def test_instance_validator_replacement_cannot_retarget_final_sender_authority(self) -> None:
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="PAPER:acct",
            )
            owner = recovery.start("host-a")
            core = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="acct",
                owner_token=owner.owner_id,
                owner_epoch=owner.epoch,
            )
            dispatcher = HostLifetimeGuardedDispatcher(
                core,
                recovery_controller=recovery,
                owner=owner,
            )

            replacement_calls: list[tuple[str, int]] = []

            def allow_all(owner_id: str, owner_epoch: int) -> None:
                replacement_calls.append((owner_id, owner_epoch))

            # The product runtime exposes RecoveryController for reconciliation
            # and takeover orchestration. Replacing an instance attribute must
            # not replace the authority callback captured by the host sender.
            recovery.validate_sender = allow_all  # type: ignore[method-assign]
            wire_calls: list[str] = []

            def transport_send(_client_order_id, _request, final_guard):
                final_guard()
                wire_calls.append("wire")
                return {"status": "accepted"}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-mutated-validator",
                intent_id="intent-mutated-validator",
                intent_hash="hash-mutated-validator",
                provider="BYBIT",
                request={},
                now="2026-10-04T02:05:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport_send,
            )

            self.assertEqual(outcome.status, "BLOCKED")
            self.assertEqual(outcome.reason, "sender_fence_rejected:PermissionError")
            self.assertEqual(replacement_calls, [])
            self.assertEqual(wire_calls, [])
            events = store.load_events(
                "submission_attempt",
                core._aggregate_id("attempt-mutated-validator"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionBlocked"],
            )


if __name__ == "__main__":
    unittest.main()
