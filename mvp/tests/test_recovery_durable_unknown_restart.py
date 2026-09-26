from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.dispatch import (
    DispatchBlocked,
    ExactJsonTransportResponse,
    GuardedDispatcher,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import HostState, RecoveryController


class DurableUnknownRestartTests(unittest.TestCase):
    @staticmethod
    def _append_submission_event(
        store: JournalStore,
        *,
        aggregate_id: str,
        event_type: str,
        version: int,
        suffix: str,
        payload: dict[str, object] | None = None,
        owner_epoch: str = "1",
    ) -> None:
        if payload is None:
            payload = {"client_order_id": "manual-" + suffix}
        store.append_event(
            {
                "event_id": "manual-" + suffix,
                "event_type": event_type,
                "aggregate_type": "submission_attempt",
                "aggregate_id": aggregate_id,
                "aggregate_version": str(version),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-09-25T20:01:00Z",
                "owner_epoch": owner_epoch,
            }
        )

    def _prepared_only_dispatch(
        self,
        store: JournalStore,
        *,
        attempt_id: str,
    ) -> dict[str, object]:
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="sender-a",
            owner_epoch=1,
        )

        def stop_before_send(_client_id, _request, _final_guard):
            raise SystemExit("stop after durable Prepared")

        with self.assertRaises(SystemExit):
            dispatcher.dispatch(
                attempt_id=attempt_id,
                intent_id="intent-1",
                intent_hash="intent-hash-1",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-25T20:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=stop_before_send,
            )
        events = store.load_events_by_aggregate_type("submission_attempt")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "SubmissionPrepared")
        return events[0]

    def _unknown_dispatch(self, store: JournalStore, *, attempt_id: str = "attempt-1"):
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="sender-a",
            owner_epoch=1,
        )

        def transport(_client_id, _request, final_guard):
            final_guard()
            raise TimeoutError("ambiguous provider result")

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            provider="sim",
            request={"side": "BUY"},
            now="2026-09-25T20:00:00Z",
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=transport,
        )
        self.assertEqual(outcome.status, "UNKNOWN")
        return outcome

    def test_restart_rejects_sender_identity_mutations_before_state_mutation(self):
        cases = (
            ("sending-envelope-epoch", "2", None, None, None, "1", None),
            ("sending-payload-epoch", "1", 2, None, None, "1", None),
            ("sending-owner-token", "1", None, "sender-b", None, "1", None),
            ("sending-client-order-id", "1", None, None, "swapped-client", "1", None),
            ("unknown-envelope-epoch", "1", None, None, None, "2", None),
            ("unknown-client-order-id", "1", None, None, None, "1", "swapped-client"),
        )
        for (
            name,
            sending_envelope_epoch,
            sending_payload_epoch,
            sending_owner_token,
            sending_client_order_id,
            unknown_envelope_epoch,
            unknown_client_order_id,
        ) in cases:
            with self.subTest(case=name), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                prepared = self._prepared_only_dispatch(
                    store,
                    attempt_id="attempt-" + name,
                )
                aggregate_id = prepared["aggregate_id"]
                prepared_payload = prepared["payload"]
                self.assertIsInstance(prepared_payload, dict)
                client_order_id = prepared_payload["client_order_id"]
                owner_token = prepared_payload["owner_token"]
                owner_epoch = prepared_payload["owner_epoch"]

                sending_payload = {
                    "client_order_id": (
                        client_order_id
                        if sending_client_order_id is None
                        else sending_client_order_id
                    ),
                    "owner_token": (
                        owner_token
                        if sending_owner_token is None
                        else sending_owner_token
                    ),
                    "owner_epoch": (
                        owner_epoch
                        if sending_payload_epoch is None
                        else sending_payload_epoch
                    ),
                    "reason": "final_send_barrier_passed",
                }
                self._append_submission_event(
                    store,
                    aggregate_id=aggregate_id,
                    event_type="SubmissionSending",
                    version=2,
                    suffix=name + "-sending",
                    payload=sending_payload,
                    owner_epoch=sending_envelope_epoch,
                )
                self._append_submission_event(
                    store,
                    aggregate_id=aggregate_id,
                    event_type="SubmissionUnknown",
                    version=3,
                    suffix=name + "-unknown",
                    payload={
                        "client_order_id": (
                            client_order_id
                            if unknown_client_order_id is None
                            else unknown_client_order_id
                        ),
                        "reason": "ambiguous",
                    },
                    owner_epoch=unknown_envelope_epoch,
                )

                recovery = RecoveryController(
                    owner_store=store,
                    owner_scope="SIMULATION:acct",
                )
                with self.assertRaises(RuntimeError):
                    recovery.recover_durable_submission_uncertainty(
                        environment="SIMULATION",
                        account_id="acct",
                    )

                self.assertEqual(recovery.state, HostState.STOPPED)
                self.assertEqual(recovery.reason_codes, set())
                self.assertEqual(recovery.unresolved_attempts, set())
                self.assertEqual(recovery._unresolved_send_attempts, set())
                self.assertEqual(recovery._unresolved_send_bindings, {})
                self.assertEqual(recovery._recovered_unknown_identities, {})

    def test_restart_rebuilds_durable_unknown_before_ready(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._unknown_dispatch(store)

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-restarted")

            self.assertEqual(owner.epoch, 1)
            self.assertEqual(recovery.state, HostState.DEGRADED)
            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})
            self.assertIn("provider_uncertainty", recovery.reason_codes)
            with self.assertRaisesRegex(PermissionError, "not ready|unresolved"):
                recovery.validate_admission(owner.epoch)

    def test_completed_send_is_not_recovered_as_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )

            def transport(_client_id, _request, final_guard):
                final_guard()
                return {"provider_order_id": "provider-order-1"}

            outcome = dispatcher.dispatch(
                attempt_id="attempt-sent",
                intent_id="intent-sent",
                intent_hash="intent-hash-sent",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-25T20:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
            )
            self.assertEqual(outcome.status, "SENT")

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")

            self.assertEqual(recovery.unresolved_attempts, set())
            self.assertEqual(recovery.state, HostState.RECOVERING)

    def test_unknown_event_tail_after_send_barrier_blocks_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )

            def crash_after_barrier(_client_id, _request, final_guard):
                final_guard()
                raise SystemExit("simulated process death")

            with self.assertRaises(SystemExit):
                dispatcher.dispatch(
                    attempt_id="attempt-invalid-tail",
                    intent_id="intent-invalid-tail",
                    intent_hash="intent-hash-invalid-tail",
                    provider="sim",
                    request={"side": "BUY"},
                    now="2026-09-25T20:00:00Z",
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=crash_after_barrier,
                )

            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )
            aggregate_id = events[0]["aggregate_id"]
            self._append_submission_event(
                store,
                aggregate_id=aggregate_id,
                event_type="SubmissionUnexpected",
                version=3,
                suffix="invalid-tail",
            )

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            with self.assertRaisesRegex(RuntimeError, "transition sequence is invalid"):
                recovery.start("host-restarted")
            self.assertNotEqual(recovery.state, HostState.READY)

    def test_sent_without_send_barrier_blocks_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )

            def crash_before_barrier(_client_id, _request, _final_guard):
                raise SystemExit("simulated process death")

            with self.assertRaises(SystemExit):
                dispatcher.dispatch(
                    attempt_id="attempt-missing-barrier",
                    intent_id="intent-missing-barrier",
                    intent_hash="intent-hash-missing-barrier",
                    provider="sim",
                    request={"side": "BUY"},
                    now="2026-09-25T20:00:00Z",
                    authority_check=lambda _intent_hash, _now: (True, "allowed"),
                    transport_send=crash_before_barrier,
                )

            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )
            aggregate_id = events[0]["aggregate_id"]
            self._append_submission_event(
                store,
                aggregate_id=aggregate_id,
                event_type="SubmissionSent",
                version=2,
                suffix="missing-barrier",
            )

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            with self.assertRaisesRegex(RuntimeError, "transition sequence is invalid"):
                recovery.start("host-restarted")
            self.assertNotEqual(recovery.state, HostState.READY)

    def test_terminal_event_after_unknown_blocks_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._unknown_dispatch(store, attempt_id="attempt-terminal-after-unknown")
            events = store.load_events_by_aggregate_type("submission_attempt")
            aggregate_id = events[0]["aggregate_id"]
            self._append_submission_event(
                store,
                aggregate_id=aggregate_id,
                event_type="SubmissionSent",
                version=4,
                suffix="terminal-after-unknown",
            )

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            with self.assertRaisesRegex(RuntimeError, "transition sequence is invalid"):
                recovery.start("host-restarted")
            self.assertNotEqual(recovery.state, HostState.READY)

    def test_blocked_then_unknown_contract_violation_remains_ambiguous(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )
            authority_calls = 0

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 1:
                    return True, "allowed"
                return False, "revoked_at_final_barrier"

            def swallow_block(_client_id, _request, final_guard):
                try:
                    final_guard()
                except DispatchBlocked:
                    return {"provider_order_id": "unsafe-wrapper-result"}
                raise AssertionError("final guard should have blocked")

            outcome = dispatcher.dispatch(
                attempt_id="attempt-blocked-unknown",
                intent_id="intent-blocked-unknown",
                intent_hash="intent-hash-blocked-unknown",
                provider="sim",
                request={"side": "BUY"},
                now="2026-09-25T20:00:00Z",
                authority_check=authority,
                transport_send=swallow_block,
            )
            self.assertEqual(outcome.status, "UNKNOWN")
            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionBlocked",
                    "SubmissionUnknown",
                ],
            )

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")
            self.assertEqual(
                recovery.unresolved_attempts,
                {"attempt-blocked-unknown"},
            )
            self.assertEqual(recovery.state, HostState.DEGRADED)

    def test_exact_response_requiring_reconciliation_is_durable_unknown_after_restart(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="LIVE",
                account_id="acct",
                owner_token="sender-a",
                owner_epoch=1,
            )
            exact_bytes = b'{"error":["provider-deadline"],"result":null}'

            def transport(_client_id, _request, final_guard):
                final_guard()
                return ExactJsonTransportResponse(
                    exact_bytes,
                    http_status=200,
                    requires_reconciliation=True,
                    ambiguity_reason="provider-deadline",
                )

            outcome = dispatcher.dispatch(
                attempt_id="attempt-response-unknown",
                intent_id="intent-response-unknown",
                intent_hash="intent-hash-response-unknown",
                provider="provider",
                request={"side": "BUY"},
                now="2026-09-25T20:00:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=transport,
                sender_check=lambda _owner_token, _owner_epoch: None,
            )

            self.assertEqual(outcome.status, "UNKNOWN")
            self.assertEqual(
                outcome.reason,
                "provider-deadline",
            )
            events = store.load_events_by_aggregate_type("submission_attempt")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )
            unknown_payload = events[-1]["payload"]
            self.assertEqual(
                unknown_payload["response_text"],
                exact_bytes.decode("utf-8"),
            )
            self.assertEqual(
                unknown_payload["response_sha256"],
                "sha256:"
                + sha256(exact_bytes).hexdigest(),
            )
            self.assertEqual(unknown_payload["http_status"], 200)
            self.assertEqual(
                unknown_payload["reason"],
                "provider-deadline",
            )

            recovery = RecoveryController(
                owner_store=store,
                owner_scope="LIVE:acct",
            )
            recovery.start("host-restarted")
            self.assertEqual(
                recovery.unresolved_attempts,
                {"attempt-response-unknown"},
            )
            self.assertEqual(recovery.state, HostState.DEGRADED)
            with self.assertRaisesRegex(PermissionError, "not ready|unresolved"):
                recovery.validate_admission(recovery.owner.epoch)

    def test_exact_reconciliation_terminal_verdict_clears_recovered_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            outcome = self._unknown_dispatch(store)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-restarted")
            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})

            checkpoint = {
                "event_id": "reconciliation-event-1",
                "payload_hash": "sha256:" + "a" * 64,
                "journal_sequence": 99,
                "payload": {
                    "provider_id": "SIM",
                    "account_id": "acct",
                    "environment": "SIMULATION",
                    "complete": True,
                    "snapshot_consistent": True,
                    "activity_coverage_complete": True,
                    "blocking_resources": [],
                    "submission_resolutions": [
                        {
                            "attempt_id": "attempt-1",
                            "intent_id": "intent-1",
                            "client_order_id": outcome.client_order_id,
                            "outcome": "PROVEN_ABSENT",
                            "evidence_reason": "complete provider absence proof",
                            "provider_order_ids": [],
                            "provider_execution_ids": [],
                        }
                    ],
                },
            }
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
                return_value=checkpoint,
            ):
                evidence = recovery.record_reconciliation_checkpoint(
                    reconciliation_id="recon-1",
                    provider_id="SIM",
                    account_id="acct",
                    environment="SIMULATION",
                )

            self.assertEqual(evidence["event_id"], "reconciliation-event-1")
            self.assertEqual(recovery.unresolved_attempts, set())
            self.assertEqual(recovery.state, HostState.READY)
            recovery.validate_admission(owner.epoch)

    def test_observed_execution_without_execution_identity_cannot_clear_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            outcome = self._unknown_dispatch(store)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")

            checkpoint = {
                "event_id": "reconciliation-event-no-execution-id",
                "payload_hash": "sha256:" + "c" * 64,
                "journal_sequence": 101,
                "payload": {
                    "provider_id": "SIM",
                    "account_id": "acct",
                    "environment": "SIMULATION",
                    "complete": True,
                    "snapshot_consistent": True,
                    "activity_coverage_complete": True,
                    "blocking_resources": [],
                    "submission_resolutions": [
                        {
                            "attempt_id": "attempt-1",
                            "intent_id": "intent-1",
                            "client_order_id": outcome.client_order_id,
                            "outcome": "OBSERVED_EXECUTION",
                            "evidence_reason": "malformed execution verdict",
                            "provider_order_ids": [],
                            "provider_execution_ids": [],
                        }
                    ],
                },
            }
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
                return_value=checkpoint,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "lacks execution identity",
                ):
                    recovery.record_reconciliation_checkpoint(
                        reconciliation_id="recon-no-execution-id",
                        provider_id="SIM",
                        account_id="acct",
                        environment="SIMULATION",
                    )

            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})
            self.assertEqual(recovery.state, HostState.DEGRADED)

    def test_reconciliation_identity_mismatch_cannot_clear_recovered_unknown(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            outcome = self._unknown_dispatch(store)
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            recovery.start("host-restarted")

            checkpoint = {
                "event_id": "reconciliation-event-bad",
                "payload_hash": "sha256:" + "b" * 64,
                "journal_sequence": 100,
                "payload": {
                    "provider_id": "SIM",
                    "account_id": "acct",
                    "environment": "SIMULATION",
                    "complete": True,
                    "snapshot_consistent": True,
                    "activity_coverage_complete": True,
                    "blocking_resources": [],
                    "submission_resolutions": [
                        {
                            "attempt_id": "attempt-1",
                            "intent_id": "different-intent",
                            "client_order_id": outcome.client_order_id,
                            "outcome": "PROVEN_ABSENT",
                            "evidence_reason": "forged identity",
                            "provider_order_ids": [],
                            "provider_execution_ids": [],
                        }
                    ],
                },
            }
            with patch(
                "mvp.autotrade_mvp.recovery.load_reconciliation_checkpoint_for_readiness",
                return_value=checkpoint,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "identity conflicts with recovered submission",
                ):
                    recovery.record_reconciliation_checkpoint(
                        reconciliation_id="recon-bad",
                        provider_id="SIM",
                        account_id="acct",
                        environment="SIMULATION",
                    )

            self.assertEqual(recovery.unresolved_attempts, {"attempt-1"})
            self.assertEqual(recovery.state, HostState.DEGRADED)


if __name__ == "__main__":
    unittest.main()
