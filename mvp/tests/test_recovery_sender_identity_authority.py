from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.recovery import (
    HostState,
    OutboundAttempt,
    RecoveryController,
)


class RecoverySenderIdentityAuthorityTests(unittest.TestCase):
    @staticmethod
    def _append(
        store: JournalStore,
        *,
        aggregate_id: str,
        event_type: str,
        version: int,
        suffix: str,
        payload: dict[str, object],
        owner_epoch: str,
    ) -> None:
        store.append_event(
            {
                "event_id": "sender-authority-" + suffix,
                "event_type": event_type,
                "aggregate_type": "submission_attempt",
                "aggregate_id": aggregate_id,
                "aggregate_version": str(version),
                "payload": payload,
                "payload_hash": payload_digest(payload),
                "committed_at": "2026-10-02T05:52:00Z",
                "owner_epoch": owner_epoch,
            }
        )

    def _prepared_only(
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
                now="2026-10-02T05:51:00Z",
                authority_check=lambda _intent_hash, _now: (True, "allowed"),
                transport_send=stop_before_send,
            )
        events = store.load_events_by_aggregate_type("submission_attempt")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "SubmissionPrepared")
        return events[0]

    def _unknown(self, store: JournalStore, *, attempt_id: str) -> None:
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="sender-a",
            owner_epoch=1,
        )

        def ambiguous(_client_id, _request, final_guard):
            final_guard()
            raise TimeoutError("ambiguous provider result")

        outcome = dispatcher.dispatch(
            attempt_id=attempt_id,
            intent_id="intent-1",
            intent_hash="intent-hash-1",
            provider="sim",
            request={"side": "BUY"},
            now="2026-10-02T05:51:00Z",
            authority_check=lambda _intent_hash, _now: (True, "allowed"),
            transport_send=ambiguous,
        )
        self.assertEqual(outcome.status, "UNKNOWN")

    def test_restart_rejects_sender_identity_mutation_before_state_mutation(self):
        cases = (
            ("sending-envelope-epoch", "2", None, None, None, "1", None),
            ("sending-payload-epoch", "1", 2, None, None, "1", None),
            ("sending-owner-token", "1", None, "sender-b", None, "1", None),
            ("sending-client-order", "1", None, None, "swapped-client", "1", None),
            ("unknown-envelope-epoch", "1", None, None, None, "2", None),
            ("unknown-client-order", "1", None, None, None, "1", "swapped-client"),
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
                prepared = self._prepared_only(store, attempt_id="attempt-" + name)
                aggregate_id = prepared["aggregate_id"]
                prepared_payload = prepared["payload"]
                self.assertIsInstance(prepared_payload, dict)
                client_order_id = prepared_payload["client_order_id"]
                owner_token = prepared_payload["owner_token"]
                owner_epoch = prepared_payload["owner_epoch"]

                self._append(
                    store,
                    aggregate_id=aggregate_id,
                    event_type="SubmissionSending",
                    version=2,
                    suffix=name + "-sending",
                    payload={
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
                    },
                    owner_epoch=sending_envelope_epoch,
                )
                self._append(
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

    def test_durable_unknown_rejects_caller_terminalization(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            self._unknown(store, attempt_id="durable-caller-resolution")
            recovery = RecoveryController(
                owner_store=store,
                owner_scope="SIMULATION:acct",
            )
            owner = recovery.start("host-a")
            self.assertEqual(
                recovery.unresolved_attempts,
                {"durable-caller-resolution"},
            )

            forged = OutboundAttempt(
                "durable-caller-resolution",
                "intent-1",
                owner.epoch,
            )
            forged.persist()
            forged.mark_send_started("caller:forged-send")
            forged.acknowledge(
                "caller-provider-order",
                "caller:forged-terminal",
            )
            with self.assertRaisesRegex(
                PermissionError,
                "journal-issued reconciliation checkpoint",
            ):
                recovery.resolve_attempt(forged)

            self.assertEqual(
                recovery.unresolved_attempts,
                {"durable-caller-resolution"},
            )
            self.assertEqual(recovery.state, HostState.DEGRADED)


if __name__ == "__main__":
    unittest.main()
