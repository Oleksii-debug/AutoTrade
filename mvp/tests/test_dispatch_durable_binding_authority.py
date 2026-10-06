from hashlib import sha256
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    SubmissionResponseBinding,
    _event_id,
    _identity_digest,
    load_submission_response_binding,
    stable_client_order_id,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest


class DurableSubmissionBindingAuthorityTests(unittest.TestCase):
    def _tamper_event_field(
        self,
        path: str,
        event_type: str,
        field: str,
        value: object,
        *,
        envelope_field: bool = False,
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT event_id, payload_json, envelope_json "
                "FROM events WHERE event_type = ?",
                (event_type,),
            ).fetchone()
            self.assertIsNotNone(row)
            event_id, payload_json, envelope_json = row
            payload = json.loads(payload_json)
            envelope = json.loads(envelope_json)
            if envelope_field:
                envelope[field] = value
            else:
                payload[field] = value
            new_payload_json = canonical_json(payload)
            new_payload_hash = payload_digest(payload)
            envelope["payload"] = payload
            envelope["payload_hash"] = new_payload_hash
            new_envelope_json = canonical_json(envelope)
            new_envelope_hash = (
                "sha256:" + sha256(new_envelope_json.encode("utf-8")).hexdigest()
            )
            connection.execute(
                "UPDATE events SET payload_json = ?, payload_hash = ?, "
                "envelope_json = ?, envelope_hash = ? WHERE event_id = ?",
                (
                    new_payload_json,
                    new_payload_hash,
                    new_envelope_json,
                    new_envelope_hash,
                    event_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _tamper_event_aggregate_version(
        self,
        path: str,
        event_type: str,
        aggregate_version: int,
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT event_id, envelope_json "
                "FROM events WHERE event_type = ?",
                (event_type,),
            ).fetchone()
            self.assertIsNotNone(row)
            event_id, envelope_json = row
            envelope = json.loads(envelope_json)
            envelope["aggregate_version"] = str(aggregate_version)
            new_envelope_json = canonical_json(envelope)
            new_envelope_hash = (
                "sha256:" + sha256(new_envelope_json.encode("utf-8")).hexdigest()
            )
            connection.execute(
                "UPDATE events SET aggregate_version = ?, envelope_json = ?, "
                "envelope_hash = ? WHERE event_id = ?",
                (
                    aggregate_version,
                    new_envelope_json,
                    new_envelope_hash,
                    event_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _tamper_event_timestamp_authority(
        self,
        path: str,
        event_type: str,
        timestamp: str,
    ) -> None:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute(
                "SELECT event_id, envelope_json "
                "FROM events WHERE event_type = ?",
                (event_type,),
            ).fetchone()
            self.assertIsNotNone(row)
            event_id, envelope_json = row
            envelope = json.loads(envelope_json)
            for field in ("occurred_at", "observed_at", "committed_at"):
                envelope[field] = timestamp
            new_envelope_json = canonical_json(envelope)
            new_envelope_hash = (
                "sha256:" + sha256(new_envelope_json.encode("utf-8")).hexdigest()
            )
            connection.execute(
                "UPDATE events SET committed_at = ?, envelope_json = ?, "
                "envelope_hash = ? WHERE event_id = ?",
                (
                    timestamp,
                    new_envelope_json,
                    new_envelope_hash,
                    event_id,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _make_exact_response_attempt(self, path: str) -> None:
        store = JournalStore(path)
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="owner",
        )
        result = dispatcher.dispatch(
            attempt_id="binding-type-a1",
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY"},
            now="2026-10-06T14:00:00Z",
            authority_check=lambda _hash, _now: (True, "allowed"),
            transport_send=lambda _cid, _request, guard: (
                guard(),
                ExactJsonTransportResponse(b'{"accepted":true}'),
            )[1],
            submission_scope={"endpoint": "/orders"},
        )
        self.assertEqual(result.status, "SENT")

    def _redispatch_exact_response_attempt(self, path: str):
        store = JournalStore(path)
        dispatcher = GuardedDispatcher(
            store,
            environment="SIMULATION",
            account_id="acct",
            owner_token="restart-owner",
        )

        def unexpected_authority(_intent_hash, _now):
            raise AssertionError("existing durable attempt must not rerun authority")

        def unexpected_transport(_client_order_id, _request, _guard):
            raise AssertionError("existing durable attempt must not rerun transport")

        return dispatcher.dispatch(
            attempt_id="binding-type-a1",
            intent_id="intent-1",
            intent_hash="sha256:" + "1" * 64,
            provider="provider",
            request={"side": "BUY"},
            now="2026-10-06T14:00:02Z",
            authority_check=unexpected_authority,
            transport_send=unexpected_transport,
            submission_scope={"endpoint": "/orders"},
        )

    def test_sending_recovery_race_revalidates_winning_terminal_history(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="restart-owner",
            )
            complete = dispatcher._events("binding-type-a1")
            sending_history = complete[:2]
            corrupt_terminal = json.loads(canonical_json(complete))
            corrupt_terminal[-1]["payload"]["client_order_id"] = "forged-client-order-id"
            reads = [sending_history, corrupt_terminal]

            def raced_events(_attempt_id):
                self.assertTrue(reads)
                return reads.pop(0)

            def lose_terminal_race(**_kwargs):
                raise ValueError("simulated aggregate-version race")

            dispatcher._events = raced_events
            dispatcher._append = lose_terminal_race

            expected_prepared = {
                key: sending_history[0]["payload"][key]
                for key in (
                    "attempt_id",
                    "intent_id",
                    "intent_hash",
                    "provider",
                    "request_hash",
                    "client_order_id",
                    "environment",
                    "account_id",
                    "submission_scope",
                    "submission_scope_hash",
                )
            }
            result = dispatcher._recover_existing(
                attempt_id="binding-type-a1",
                client_order_id=sending_history[0]["payload"]["client_order_id"],
                now="2026-10-06T14:00:02Z",
                expected_prepared=expected_prepared,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")
            self.assertEqual(reads, [])

    def test_sending_recovery_race_revalidates_prepared_authority_snapshot(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="restart-owner",
            )
            complete = dispatcher._events("binding-type-a1")
            sending_history = complete[:2]
            corrupt_terminal = json.loads(canonical_json(complete))
            corrupt_terminal[0]["payload"]["provider"] = "retargeted-provider"
            reads = [sending_history, corrupt_terminal]
            expected_prepared = {
                key: sending_history[0]["payload"][key]
                for key in (
                    "attempt_id",
                    "intent_id",
                    "intent_hash",
                    "provider",
                    "request_hash",
                    "client_order_id",
                    "environment",
                    "account_id",
                    "submission_scope",
                    "submission_scope_hash",
                )
            }

            def raced_events(_attempt_id):
                self.assertTrue(reads)
                return reads.pop(0)

            def lose_terminal_race(**_kwargs):
                raise ValueError("simulated aggregate-version race")

            dispatcher._events = raced_events
            dispatcher._append = lose_terminal_race

            result = dispatcher._recover_existing(
                attempt_id="binding-type-a1",
                client_order_id=sending_history[0]["payload"]["client_order_id"],
                now="2026-10-06T14:00:02Z",
                expected_prepared=expected_prepared,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")
            self.assertEqual(reads, [])

    def test_final_send_barrier_rejects_prepared_authority_retargeting(self):
        for field, bad_value, envelope_field in (
            ("provider", "retargeted-provider", False),
            ("submission_scope", {"endpoint": "/retargeted"}, False),
            ("owner_token", "retargeted-owner", False),
            ("owner_epoch", 77, False),
            ("owner_epoch", True, False),
            ("prepared_at", "2026-10-06T13:59:59Z", False),
            ("environment", "PAPER", True),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                wire_calls = 0

                def transport(_client_order_id, _request, guard):
                    nonlocal wire_calls
                    self._tamper_event_field(
                        path,
                        "SubmissionPrepared",
                        field,
                        bad_value,
                        envelope_field=envelope_field,
                    )
                    guard()
                    wire_calls += 1
                    return ExactJsonTransportResponse(b'{"accepted":true}')

                result = dispatcher.dispatch(
                    attempt_id="final-barrier-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(
                    result.reason,
                    "submission_changed_during_final_send_validation",
                )
                self.assertEqual(wire_calls, 0)

    def test_post_barrier_durable_tamper_never_returns_sent(self):
        for event_type, field, value, envelope_field in (
            ("SubmissionSending", "client_order_id", "retargeted-client", False),
            ("SubmissionSending", "environment", "PAPER", True),
            ("SubmissionPrepared", "provider", "retargeted-provider", False),
            ("SubmissionPrepared", "owner_token", "retargeted-owner", False),
            ("SubmissionPrepared", "owner_epoch", 77, False),
            ("SubmissionPrepared", "owner_epoch", "77", True),
            ("SubmissionSending", "owner_token", "retargeted-owner", False),
            ("SubmissionSending", "owner_epoch", 77, False),
            ("SubmissionSending", "owner_epoch", "77", True),
        ):
            with self.subTest(event_type=event_type, field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )

                def transport(_client_order_id, _request, guard):
                    guard()
                    self._tamper_event_field(
                        path,
                        event_type,
                        field,
                        value,
                        envelope_field=envelope_field,
                    )
                    return ExactJsonTransportResponse(b'{"accepted":true}')

                result = dispatcher.dispatch(
                    attempt_id="post-barrier-tamper-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, "durable_submission_history_invalid")
                events = dispatcher._events("post-barrier-tamper-a1")
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionSending"],
                )

    def test_restart_rejects_invalid_exact_response_http_status(self):
        for bad_status in ("200", True, 99, 600):
            with self.subTest(bad_status=bad_status), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    "SubmissionSent",
                    "http_status",
                    bad_status,
                )
                result = self._redispatch_exact_response_attempt(path)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, "exact_response_invalid")

    def test_response_binding_rejects_sender_ownership_discontinuity(self):
        cases = (
            ("SubmissionSending", "owner_token", "other-owner", False),
            ("SubmissionSending", "owner_epoch", 2, False),
            ("SubmissionSending", "owner_epoch", "2", True),
            ("SubmissionSent", "owner_epoch", "2", True),
        )
        for event_type, field, value, envelope_field in cases:
            with self.subTest(
                event_type=event_type,
                field=field,
            ), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    event_type,
                    field,
                    value,
                    envelope_field=envelope_field,
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "sender ownership is not continuous",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )

    def test_restart_rejects_sent_terminal_owner_epoch_retarget(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionSent",
                "owner_epoch",
                "2",
                envelope_field=True,
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_restart_rejects_prepared_sending_owner_discontinuity(self):
        for event_type, field, value, envelope_field in (
            ("SubmissionPrepared", "owner_token", "", False),
            ("SubmissionPrepared", "owner_epoch", 0, False),
            ("SubmissionPrepared", "owner_epoch", "2", True),
            ("SubmissionSending", "owner_token", "other-owner", False),
            ("SubmissionSending", "owner_epoch", 2, False),
            ("SubmissionSending", "owner_epoch", "2", True),
        ):
            with self.subTest(event_type=event_type, field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    event_type,
                    field,
                    value,
                    envelope_field=envelope_field,
                )
                store = JournalStore(path)
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="restart-owner",
                )

                def unexpected_authority(_intent_hash, _now):
                    raise AssertionError("corrupt durable attempt must not rerun authority")

                def unexpected_transport(_client_order_id, _request, _guard):
                    raise AssertionError("corrupt durable attempt must not rerun transport")

                result = dispatcher.dispatch(
                    attempt_id="binding-type-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:02Z",
                    authority_check=unexpected_authority,
                    transport_send=unexpected_transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_post_barrier_recovery_terminal_race_converges_without_sent_overwrite(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            nested = []

            def authority(_hash, _now):
                return True, "allowed"

            def transport(_client_order_id, _request, guard):
                guard()
                recovery = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="recovery-owner",
                )
                nested.append(
                    recovery.dispatch(
                        attempt_id="post-barrier-race-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:01Z",
                        authority_check=authority,
                        transport_send=lambda *_args: (_ for _ in ()).throw(
                            AssertionError("recovery must not resend")
                        ),
                        submission_scope={"endpoint": "/orders"},
                    )
                )
                return ExactJsonTransportResponse(b'{"accepted":true}')

            result = dispatcher.dispatch(
                attempt_id="post-barrier-race-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=authority,
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(nested[0].status, "UNKNOWN")
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            events = dispatcher._events("post-barrier-race-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )

    def test_post_barrier_exception_converges_with_recovery_terminal_race(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )

            def authority(_hash, _now):
                return True, "allowed"

            def transport(_client_order_id, _request, guard):
                guard()
                recovery = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="recovery-owner",
                )
                recovered = recovery.dispatch(
                    attempt_id="post-barrier-exception-race-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:01Z",
                    authority_check=authority,
                    transport_send=lambda *_args: (_ for _ in ()).throw(
                        AssertionError("recovery must not resend")
                    ),
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(recovered.status, "UNKNOWN")
                raise TimeoutError("provider reply lost")

            result = dispatcher.dispatch(
                attempt_id="post-barrier-exception-race-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=authority,
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            events = dispatcher._events("post-barrier-exception-race-a1")
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending", "SubmissionUnknown"],
            )

    def test_post_barrier_missing_durable_history_fails_unknown(self):
        for transport_raises in (False, True):
            with self.subTest(transport_raises=transport_raises), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )

                def transport(_client_order_id, _request, guard):
                    guard()
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute("DELETE FROM events")
                        connection.commit()
                    finally:
                        connection.close()
                    if transport_raises:
                        raise TimeoutError("provider reply lost after durable history loss")
                    return ExactJsonTransportResponse(b'{"accepted":true}')

                result = dispatcher.dispatch(
                    attempt_id="missing-history-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(
                    result.reason,
                    "durable_submission_history_invalid",
                )

    def test_hash_consistent_unknown_to_sent_retarget_fails_event_identity(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="terminal-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"accepted":false}',
                        http_status=503,
                        requires_reconciliation=True,
                        ambiguity_reason="provider_execution_unknown",
                    ),
                )[1],
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(result.status, "UNKNOWN")

            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT event_id, envelope_json "
                    "FROM events WHERE event_type = 'SubmissionUnknown'"
                ).fetchone()
                self.assertIsNotNone(row)
                event_id, envelope_json = row
                envelope = json.loads(envelope_json)
                envelope["event_type"] = "SubmissionSent"
                new_envelope_json = canonical_json(envelope)
                new_envelope_hash = (
                    "sha256:"
                    + sha256(new_envelope_json.encode("utf-8")).hexdigest()
                )
                connection.execute(
                    "UPDATE events SET event_type = ?, envelope_json = ?, "
                    "envelope_hash = ? WHERE event_id = ?",
                    (
                        "SubmissionSent",
                        new_envelope_json,
                        new_envelope_hash,
                        event_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="restart-owner",
            )

            def unexpected_authority(_intent_hash, _now):
                raise AssertionError("corrupt terminal history must not rerun authority")

            def unexpected_transport(_client_order_id, _request, _guard):
                raise AssertionError("corrupt terminal history must not resend")

            recovered = restarted.dispatch(
                attempt_id="terminal-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:02Z",
                authority_check=unexpected_authority,
                transport_send=unexpected_transport,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "durable_submission_history_invalid",
            )
            with self.assertRaisesRegex(
                ValueError,
                "event_id mismatches canonical submission event identity",
            ):
                load_submission_response_binding(
                    JournalStore(path),
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="terminal-retarget-a1",
                )

    def test_unknown_to_sent_retarget_with_matching_event_id_stays_unknown(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="terminal-semantic-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"accepted":false}',
                        http_status=503,
                        requires_reconciliation=True,
                        ambiguity_reason="provider_execution_unknown",
                    ),
                )[1],
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(result.status, "UNKNOWN")

            scope_key = _identity_digest("SIMULATION", "acct")
            canonical_sent_event_id = _event_id(
                scope_key,
                "terminal-semantic-retarget-a1",
                "SubmissionSent",
                3,
            )
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT event_id, envelope_json "
                    "FROM events WHERE event_type = 'SubmissionUnknown'"
                ).fetchone()
                self.assertIsNotNone(row)
                old_event_id, envelope_json = row
                envelope = json.loads(envelope_json)
                envelope["event_id"] = canonical_sent_event_id
                envelope["event_type"] = "SubmissionSent"
                new_envelope_json = canonical_json(envelope)
                new_envelope_hash = (
                    "sha256:"
                    + sha256(new_envelope_json.encode("utf-8")).hexdigest()
                )
                connection.execute(
                    "UPDATE events SET event_id = ?, event_type = ?, "
                    "envelope_json = ?, envelope_hash = ? WHERE event_id = ?",
                    (
                        canonical_sent_event_id,
                        "SubmissionSent",
                        new_envelope_json,
                        new_envelope_hash,
                        old_event_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            restarted = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="restart-owner",
            )
            recovered = restarted.dispatch(
                attempt_id="terminal-semantic-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:02Z",
                authority_check=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("corrupt terminal history must not rerun authority")
                ),
                transport_send=lambda *_args: (_ for _ in ()).throw(
                    AssertionError("corrupt terminal history must not resend")
                ),
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "durable_submission_history_invalid",
            )
            with self.assertRaisesRegex(
                ValueError,
                "terminal semantics are invalid",
            ):
                load_submission_response_binding(
                    JournalStore(path),
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="terminal-semantic-retarget-a1",
                )

    def test_dispatch_rejects_rebound_journal_class_load_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            rebound_calls = []
            authority_calls = []
            transport_calls = []

            def rebound_load(*_args, **_kwargs):
                rebound_calls.append("load_events")
                raise AssertionError("rebound load_events must not execute")

            original = JournalStore.load_events
            JournalStore.load_events = rebound_load
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "submission journal operation changed: load_events",
                ):
                    dispatcher.dispatch(
                        attempt_id="class-load-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: authority_calls.append(True),
                        transport_send=lambda *_args: transport_calls.append(True),
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                JournalStore.load_events = original

            self.assertEqual(rebound_calls, [])
            self.assertEqual(authority_calls, [])
            self.assertEqual(transport_calls, [])
            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id("class-load-a1"),
                ),
                [],
            )

    def test_final_send_rejects_rebound_journal_append_without_wire_effect(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            authority_calls = 0
            rebound_calls = []
            wire_calls = 0
            original = JournalStore.append_event

            def rebound_append(*_args, **_kwargs):
                rebound_calls.append("append_event")
                raise AssertionError("rebound append_event must not execute")

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    JournalStore.append_event = rebound_append
                return True, "allowed"

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "submission journal operation changed: append_event",
                ):
                    dispatcher.dispatch(
                        attempt_id="class-append-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=authority,
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                JournalStore.append_event = original

            self.assertEqual(authority_calls, 2)
            self.assertEqual(rebound_calls, [])
            self.assertEqual(wire_calls, 0)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("class-append-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_dispatch_rejects_rebound_store_identity_descriptor(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            descriptor_calls = []

            def rebound_identity(_self):
                descriptor_calls.append("store_identity")
                return vars(store)["_store_identity"]

            original = JournalStore.store_identity
            JournalStore.store_identity = property(rebound_identity)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "submission journal identity authority changed",
                ):
                    dispatcher.dispatch(
                        attempt_id="identity-rebind-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=lambda *_args: (
                            _ for _ in ()
                        ).throw(AssertionError("transport must not run")),
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                JournalStore.store_identity = original

            self.assertEqual(descriptor_calls, [])

    def test_dispatch_rejects_rebound_journal_connect_dependency_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            rebound_calls = []
            authority_calls = []
            transport_calls = []
            original = JournalStore._connect

            def rebound_connect(*_args, **_kwargs):
                rebound_calls.append("_connect")
                raise AssertionError("rebound _connect must not execute")

            JournalStore._connect = rebound_connect
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "submission journal class authority changed",
                ):
                    dispatcher.dispatch(
                        attempt_id="class-connect-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: authority_calls.append(True),
                        transport_send=lambda *_args: transport_calls.append(True),
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                JournalStore._connect = original

            self.assertEqual(rebound_calls, [])
            self.assertEqual(authority_calls, [])
            self.assertEqual(transport_calls, [])

    def test_dispatch_rejects_rebound_inherited_decode_dependency_before_callbacks(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            base = JournalStore.__mro__[1]
            original = base.__dict__["_decode_event_row"]
            rebound_calls = []
            authority_calls = []
            transport_calls = []

            def rebound_decode(*_args, **_kwargs):
                rebound_calls.append("_decode_event_row")
                raise AssertionError("rebound _decode_event_row must not execute")

            setattr(base, "_decode_event_row", rebound_decode)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "submission journal class authority changed",
                ):
                    dispatcher.dispatch(
                        attempt_id="class-decode-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: authority_calls.append(True),
                        transport_send=lambda *_args: transport_calls.append(True),
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                setattr(base, "_decode_event_row", original)

            self.assertEqual(rebound_calls, [])
            self.assertEqual(authority_calls, [])
            self.assertEqual(transport_calls, [])

    def test_final_guard_rejects_dispatcher_authority_retargeting_before_wire(self):
        cases = (
            ("environment", "PAPER"),
            ("account_id", "other-account"),
            ("scope_key", "retargeted-scope"),
            ("owner_token", "other-owner"),
            ("owner_epoch", 2),
            ("prepared_lease_seconds", 3600),
        )
        for field_name, replacement in cases:
            with self.subTest(field_name=field_name), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                    owner_epoch=1,
                    prepared_lease_seconds=60,
                )
                original = getattr(dispatcher, field_name)
                wire_calls = 0

                def transport(_client_order_id, _request, guard):
                    nonlocal wire_calls
                    setattr(dispatcher, field_name, replacement)
                    guard()
                    wire_calls += 1
                    return ExactJsonTransportResponse(b'{"accepted":true}')

                try:
                    with self.assertRaisesRegex(
                        PermissionError,
                        "dispatcher authority state changed",
                    ):
                        dispatcher.dispatch(
                            attempt_id="dispatcher-retarget-a1",
                            intent_id="intent-1",
                            intent_hash="sha256:" + "1" * 64,
                            provider="provider",
                            request={"side": "BUY"},
                            now="2026-10-06T14:00:00Z",
                            authority_check=lambda *_args: (True, "allowed"),
                            transport_send=transport,
                            submission_scope={"endpoint": "/orders"},
                        )
                finally:
                    setattr(dispatcher, field_name, original)

                self.assertEqual(wire_calls, 0)
                events = JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id("dispatcher-retarget-a1"),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared"],
                )

    def test_final_guard_uses_per_call_state_even_if_instance_snapshot_is_rewritten(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
                prepared_lease_seconds=60,
            )
            original_state = dispatcher._dispatch_authority_state
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                dispatcher.prepared_lease_seconds = 3600
                dispatcher._dispatch_authority_state = (
                    dispatcher.environment,
                    dispatcher.account_id,
                    dispatcher.scope_key,
                    dispatcher.owner_token,
                    dispatcher.owner_epoch,
                    dispatcher.prepared_lease_seconds,
                )
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority changed during dispatch",
                ):
                    dispatcher.dispatch(
                        attempt_id="per-call-state-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                dispatcher.prepared_lease_seconds = 60
                dispatcher._dispatch_authority_state = original_state

            self.assertEqual(wire_calls, 0)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("per-call-state-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_final_authority_callback_cannot_retarget_sender_state(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )
            authority_calls = 0
            wire_calls = 0

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    dispatcher.owner_epoch = 2
                return True, "allowed"

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority state changed",
                ):
                    dispatcher.dispatch(
                        attempt_id="final-authority-retarget-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=authority,
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                dispatcher.owner_epoch = 1

            self.assertEqual(authority_calls, 2)
            self.assertEqual(wire_calls, 0)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("final-authority-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_initial_authority_cannot_retarget_expected_dispatcher_snapshot(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )
            transport_calls = 0

            def authority(_intent_hash, _now):
                dispatcher.owner_epoch = 2
                dispatcher._dispatch_authority_state = (
                    dispatcher.environment,
                    dispatcher.account_id,
                    dispatcher.scope_key,
                    dispatcher.owner_token,
                    dispatcher.owner_epoch,
                    dispatcher.prepared_lease_seconds,
                )
                return True, "allowed"

            def transport(*_args):
                nonlocal transport_calls
                transport_calls += 1
                raise AssertionError("transport must not execute")

            with self.assertRaisesRegex(
                PermissionError,
                "dispatcher authority changed during dispatch",
            ):
                dispatcher.dispatch(
                    attempt_id="initial-expected-retarget-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=authority,
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )

            self.assertEqual(transport_calls, 0)
            self.assertEqual(dispatcher.owner_epoch, 1)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("initial-expected-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_final_guard_cannot_retarget_expected_store_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            other_store = JournalStore(f"{directory}/other.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                dispatcher.store = other_store
                dispatcher._journal_store_path = other_store.path
                dispatcher._journal_store_identity = other_store.store_identity
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            with self.assertRaisesRegex(
                PermissionError,
                "dispatcher authority changed during dispatch",
            ):
                dispatcher.dispatch(
                    attempt_id="store-expected-retarget-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )

            self.assertEqual(wire_calls, 0)
            self.assertIs(dispatcher.store, store)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("store-expected-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )
            self.assertEqual(
                JournalStore.load_events(
                    other_store,
                    "submission_attempt",
                    dispatcher._aggregate_id("store-expected-retarget-a1"),
                ),
                [],
            )

    def test_final_guard_rejects_callback_time_dispatcher_method_shadow(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def fake_append(**_kwargs):
                raise AssertionError("callback-installed _append must not execute")

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                dispatcher._append = fake_append
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            with self.assertRaisesRegex(
                PermissionError,
                "dispatcher authority changed during dispatch",
            ):
                dispatcher.dispatch(
                    attempt_id="method-shadow-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )

            self.assertEqual(wire_calls, 0)
            self.assertNotIn("_append", vars(dispatcher))
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("method-shadow-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_final_guard_rejects_callback_time_dispatcher_class_rebind(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0
            original_append = GuardedDispatcher._append

            def fake_append(*_args, **_kwargs):
                raise AssertionError("callback-rebound class _append must not execute")

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                GuardedDispatcher._append = fake_append
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority changed during dispatch",
                ):
                    dispatcher.dispatch(
                        attempt_id="class-rebind-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                GuardedDispatcher._append = original_append

            self.assertEqual(wire_calls, 0)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("class-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_authority_callback_cannot_rebind_envelope_helper_before_send(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_envelope = dispatch_module._envelope
            forged_calls = 0
            wire_calls = 0

            def forged_envelope(*_args, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                raise AssertionError("rebound envelope helper executed")

            def authority(_intent_hash, _now):
                dispatch_module._envelope = forged_envelope
                return True, "allowed"

            def transport(*_args):
                nonlocal wire_calls
                wire_calls += 1
                raise AssertionError("wire must not execute")

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority changed during dispatch",
                ):
                    dispatcher.dispatch(
                        attempt_id="module-envelope-rebind-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=authority,
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                dispatch_module._envelope = original_envelope

            self.assertEqual(forged_calls, 0)
            self.assertEqual(wire_calls, 0)
            self.assertIs(dispatch_module._envelope, original_envelope)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("module-envelope-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_final_authority_callback_cannot_rebind_journal_helper_before_send(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_journal_call = dispatch_module._journal_store_call
            authority_calls = 0
            forged_calls = 0
            wire_calls = 0

            def forged_journal_call(*_args, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                raise AssertionError("rebound journal helper executed")

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                if authority_calls == 2:
                    dispatch_module._journal_store_call = forged_journal_call
                return True, "allowed"

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority changed during dispatch",
                ):
                    dispatcher.dispatch(
                        attempt_id="module-journal-rebind-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=authority,
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                dispatch_module._journal_store_call = original_journal_call

            self.assertEqual(authority_calls, 2)
            self.assertEqual(forged_calls, 0)
            self.assertEqual(wire_calls, 0)
            self.assertIs(
                dispatch_module._journal_store_call,
                original_journal_call,
            )
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("module-journal-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_sender_callback_cannot_mutate_append_code_before_send(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            append_method = GuardedDispatcher._append
            original_code = append_method.__code__
            wire_calls = 0

            def forged_append(self, **_kwargs):
                raise AssertionError("mutated _append code executed")

            def sender_check(_owner_token, _owner_epoch):
                append_method.__code__ = forged_append.__code__

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                with self.assertRaisesRegex(
                    PermissionError,
                    "dispatcher authority changed during dispatch",
                ):
                    dispatcher.dispatch(
                        attempt_id="append-code-retarget-a1",
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=transport,
                        sender_check=sender_check,
                        submission_scope={"endpoint": "/orders"},
                    )
            finally:
                append_method.__code__ = original_code

            self.assertEqual(wire_calls, 0)
            self.assertIs(append_method.__code__, original_code)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("append-code-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_sender_exception_cannot_retarget_expected_authority_before_block_record(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
                owner_epoch=1,
            )

            def sender_check(_owner_token, _owner_epoch):
                dispatcher.owner_epoch = 2
                dispatcher._dispatch_authority_state = (
                    dispatcher.environment,
                    dispatcher.account_id,
                    dispatcher.scope_key,
                    dispatcher.owner_token,
                    dispatcher.owner_epoch,
                    dispatcher.prepared_lease_seconds,
                )
                raise RuntimeError("sender failed after retarget")

            def transport(_client_order_id, _request, guard):
                guard()
                raise AssertionError("wire must not execute")

            with self.assertRaisesRegex(
                PermissionError,
                "dispatcher authority changed during dispatch",
            ):
                dispatcher.dispatch(
                    attempt_id="sender-exception-retarget-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    sender_check=sender_check,
                    submission_scope={"endpoint": "/orders"},
                )

            self.assertEqual(dispatcher.owner_epoch, 1)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("sender-exception-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared"],
            )

    def test_post_guard_store_retarget_preserves_original_sending_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            other_store = JournalStore(f"{directory}/other.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                dispatcher.store = other_store
                dispatcher._journal_store_path = other_store.path
                dispatcher._journal_store_identity = other_store.store_identity
                return ExactJsonTransportResponse(b'{"accepted":true}')

            result = dispatcher.dispatch(
                attempt_id="post-guard-store-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(wire_calls, 1)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            self.assertIs(dispatcher.store, store)
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-store-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                ],
            )
            self.assertEqual(
                JournalStore.load_events(
                    other_store,
                    "submission_attempt",
                    dispatcher._aggregate_id("post-guard-store-retarget-a1"),
                ),
                [],
            )

            redispatch_wire_calls = 0

            def must_not_resend(*_args):
                nonlocal redispatch_wire_calls
                redispatch_wire_calls += 1
                raise AssertionError("durable UNKNOWN must not resend")

            recovered = dispatcher.dispatch(
                attempt_id="post-guard-store-retarget-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:01Z",
                authority_check=lambda *_args: (
                    (_ for _ in ()).throw(
                        AssertionError("durable UNKNOWN must not rerun authority")
                    )
                ),
                transport_send=must_not_resend,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(redispatch_wire_calls, 0)
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            recovered_events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-store-retarget-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in recovered_events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

    def test_post_guard_store_retarget_then_transport_error_stays_on_original_journal(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            other_store = JournalStore(f"{directory}/other.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                dispatcher.store = other_store
                dispatcher._journal_store_path = other_store.path
                dispatcher._journal_store_identity = other_store.store_identity
                raise RuntimeError("transport failed after retarget")

            result = dispatcher.dispatch(
                attempt_id="post-guard-store-error-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(wire_calls, 1)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            self.assertIs(dispatcher.store, store)
            self.assertEqual(
                [
                    event["event_type"]
                    for event in JournalStore.load_events(
                        store,
                        "submission_attempt",
                        dispatcher._aggregate_id("post-guard-store-error-a1"),
                    )
                ],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                ],
            )
            self.assertEqual(
                JournalStore.load_events(
                    other_store,
                    "submission_attempt",
                    dispatcher._aggregate_id("post-guard-store-error-a1"),
                ),
                [],
            )

    def test_post_guard_class_rebind_never_calls_rebound_dispatcher_method(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_events = GuardedDispatcher._events
            hostile_calls = 0
            wire_calls = 0

            def hostile_events(*_args, **_kwargs):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("post-barrier rebound _events must not execute")

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                GuardedDispatcher._events = hostile_events
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                result = dispatcher.dispatch(
                    attempt_id="post-guard-class-rebind-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
            finally:
                GuardedDispatcher._events = original_events

            self.assertEqual(wire_calls, 1)
            self.assertEqual(hostile_calls, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-class-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_post_guard_dispatcher_class_rebind_preserves_sealed_sending_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_events = GuardedDispatcher._events
            forged_calls = 0
            wire_calls = 0

            def forged_events(*_args, **_kwargs):
                nonlocal forged_calls
                forged_calls += 1
                return [
                    {
                        "event_type": "SubmissionSent",
                        "payload": {
                            "client_order_id": "forged",
                            "response": {"accepted": True},
                        },
                    }
                ]

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                GuardedDispatcher._events = forged_events
                return ExactJsonTransportResponse(b'{"accepted":true}')

            try:
                result = dispatcher.dispatch(
                    attempt_id="post-guard-class-rebind-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )
            finally:
                GuardedDispatcher._events = original_events

            self.assertEqual(wire_calls, 1)
            self.assertEqual(forged_calls, 0)
            self.assertIs(GuardedDispatcher._events, original_events)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(
                result.reason,
                "dispatcher_authority_changed_after_send_barrier",
            )
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-class-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )

            recovery_wire_calls = 0

            def must_not_resend(*_args):
                nonlocal recovery_wire_calls
                recovery_wire_calls += 1
                raise AssertionError(
                    "post-guard class rebind must recover without resend"
                )

            recovered = dispatcher.dispatch(
                attempt_id="post-guard-class-rebind-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:01Z",
                authority_check=lambda *_args: (
                    (_ for _ in ()).throw(
                        AssertionError("Sending recovery must not rerun authority")
                    )
                ),
                transport_send=must_not_resend,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(recovery_wire_calls, 0)
            self.assertEqual(recovered.status, "UNKNOWN")
            self.assertEqual(
                recovered.reason,
                "recovered_after_send_barrier_without_terminal_result",
            )
            recovered_events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-guard-class-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in recovered_events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )

    def test_post_send_module_helper_rebinding_is_restored_without_execution(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        surfaces = (
            "_canonical_journal_authority_snapshot",
            "_journal_store_call",
            "_envelope",
            "_detach_submission_json",
            "submission_attempt_aggregate_id",
            "_event_id",
            "_identity_digest",
            "_instant",
            "payload_digest",
            "canonical_json",
            "_canonical_submission_event_instant",
            "_exact_response_terminal_semantics_are_canonical",
            "_has_exact_response_markers",
            "uuid5",
            "NAMESPACE_URL",
            "sha256",
            "_DispatchAuthorityChanged",
            "DispatchBlocked",
            "DispatchOutcome",
        )
        for surface in surfaces:
            with self.subTest(surface=surface), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                original = getattr(dispatch_module, surface)
                hostile_calls = 0
                wire_calls = 0

                def forged(*_args, **_kwargs):
                    nonlocal hostile_calls
                    hostile_calls += 1
                    raise AssertionError(f"rebound {surface} executed")

                def transport(_client_order_id, _request, guard):
                    nonlocal wire_calls
                    guard()
                    response = ExactJsonTransportResponse(b'{"accepted":true}')
                    wire_calls += 1
                    setattr(dispatch_module, surface, forged)
                    return response

                attempt_id = f"post-send-module-helper-{surface}"
                try:
                    result = dispatcher.dispatch(
                        attempt_id=attempt_id,
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
                    self.assertIs(getattr(dispatch_module, surface), original)
                finally:
                    setattr(dispatch_module, surface, original)

                self.assertEqual(wire_calls, 1)
                self.assertEqual(hostile_calls, 0)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(
                    result.reason,
                    "dispatcher_authority_changed_after_send_barrier",
                )
                events = JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id(attempt_id),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["SubmissionPrepared", "SubmissionSending"],
                )

                resend_calls = 0

                def must_not_resend(*_args):
                    nonlocal resend_calls
                    resend_calls += 1
                    raise AssertionError("recovered possible-send state must not resend")

                recovered = dispatcher.dispatch(
                    attempt_id=attempt_id,
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:01Z",
                    authority_check=lambda *_args: (
                        (_ for _ in ()).throw(
                            AssertionError(
                                "Sending recovery must not rerun authority"
                            )
                        )
                    ),
                    transport_send=must_not_resend,
                    submission_scope={"endpoint": "/orders"},
                )
                self.assertEqual(resend_calls, 0)
                self.assertEqual(recovered.status, "UNKNOWN")
                self.assertEqual(
                    recovered.reason,
                    "recovered_after_send_barrier_without_terminal_result",
                )
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in JournalStore.load_events(
                            store,
                            "submission_attempt",
                            dispatcher._aggregate_id(attempt_id),
                        )
                    ],
                    [
                        "SubmissionPrepared",
                        "SubmissionSending",
                        "SubmissionUnknown",
                    ],
                )

    def test_post_send_exception_class_rebinding_cannot_escape_unknown(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        for surface in ("_DispatchAuthorityChanged", "DispatchBlocked"):
            with self.subTest(surface=surface), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                original = getattr(dispatch_module, surface)
                hostile_checks = 0
                wire_calls = 0

                class HostileMeta(type):
                    def __instancecheck__(cls, _instance):
                        nonlocal hostile_checks
                        hostile_checks += 1
                        raise AssertionError(
                            f"rebound {surface} instance check executed"
                        )

                    def __subclasscheck__(cls, _subclass):
                        nonlocal hostile_checks
                        hostile_checks += 1
                        raise AssertionError(
                            f"rebound {surface} subclass check executed"
                        )

                class HostileException(Exception, metaclass=HostileMeta):
                    pass

                def transport(_client_order_id, _request, guard):
                    nonlocal wire_calls
                    guard()
                    wire_calls += 1
                    setattr(dispatch_module, surface, HostileException)
                    raise RuntimeError("transport failed after helper retarget")

                attempt_id = f"post-send-exception-class-{surface}"
                try:
                    result = dispatcher.dispatch(
                        attempt_id=attempt_id,
                        intent_id="intent-1",
                        intent_hash="sha256:" + "1" * 64,
                        provider="provider",
                        request={"side": "BUY"},
                        now="2026-10-06T14:00:00Z",
                        authority_check=lambda *_args: (True, "allowed"),
                        transport_send=transport,
                        submission_scope={"endpoint": "/orders"},
                    )
                    self.assertIs(getattr(dispatch_module, surface), original)
                finally:
                    setattr(dispatch_module, surface, original)

                self.assertEqual(wire_calls, 1)
                self.assertEqual(hostile_checks, 0)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(
                    result.reason,
                    "dispatcher_authority_changed_after_send_barrier",
                )
                self.assertEqual(
                    [
                        event["event_type"]
                        for event in JournalStore.load_events(
                            store,
                            "submission_attempt",
                            dispatcher._aggregate_id(attempt_id),
                        )
                    ],
                    ["SubmissionPrepared", "SubmissionSending"],
                )

    def test_exact_response_post_construction_tamper_is_revalidated_without_callbacks(self):
        class TrapBytes(bytes):
            callbacks = 0

            def decode(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("tampered response bytes decode executed")

        cases = (
            ("response_bytes", TrapBytes(b'{"accepted":true}'), TypeError),
            ("http_status", True, ValueError),
            ("requires_reconciliation", 1, TypeError),
        )
        for field, forged_value, expected_error in cases:
            with self.subTest(field=field), TemporaryDirectory() as directory:
                TrapBytes.callbacks = 0
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                wire_calls = 0

                def transport(_client_order_id, _request, guard):
                    nonlocal wire_calls
                    guard()
                    wire_calls += 1
                    response = ExactJsonTransportResponse(
                        b'{"accepted":true}',
                        http_status=200,
                    )
                    object.__setattr__(response, field, forged_value)
                    return response

                result = dispatcher.dispatch(
                    attempt_id=f"exact-response-tamper-{field}",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                    submission_scope={"endpoint": "/orders"},
                )

                self.assertEqual(wire_calls, 1)
                self.assertEqual(TrapBytes.callbacks, 0)
                self.assertEqual(result.status, "UNKNOWN")
                self.assertEqual(result.reason, "sent_response_persistence_failed")
                events = JournalStore.load_events(
                    store,
                    "submission_attempt",
                    dispatcher._aggregate_id(
                        f"exact-response-tamper-{field}"
                    ),
                )
                self.assertEqual(
                    [event["event_type"] for event in events],
                    [
                        "SubmissionPrepared",
                        "SubmissionSending",
                        "SubmissionUnknown",
                    ],
                )
                self.assertEqual(
                    events[-1]["payload"]["reason"],
                    "sent_response_persistence_failed:"
                    + expected_error.__name__,
                )

    def test_post_send_response_class_rebind_never_executes_hostile_instancecheck(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        callbacks = 0

        class HostileMeta(type):
            def __instancecheck__(cls, _instance):
                nonlocal callbacks
                callbacks += 1
                raise AssertionError("rebound response class __instancecheck__ executed")

        class HostileResponse(metaclass=HostileMeta):
            pass

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            response = ExactJsonTransportResponse(b'{"accepted":true}')
            original_response_type = dispatch_module.ExactJsonTransportResponse

            def transport(_client_order_id, _request, guard):
                guard()
                dispatch_module.ExactJsonTransportResponse = HostileResponse
                return response

            try:
                result = dispatcher.dispatch(
                    attempt_id="post-send-response-class-rebind-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                )
            finally:
                dispatch_module.ExactJsonTransportResponse = original_response_type

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-send-response-class-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "sent_response_persistence_failed:ValueError",
            )

    def test_post_send_legacy_response_uses_pretransport_isinstance_builtin(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        callbacks = 0

        def hostile_isinstance(*_args):
            nonlocal callbacks
            callbacks += 1
            raise AssertionError("rebound isinstance executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_isinstance = getattr(dispatch_module, "isinstance", None)
            had_global_isinstance = "isinstance" in vars(dispatch_module)

            def transport(_client_order_id, _request, guard):
                guard()
                dispatch_module.isinstance = hostile_isinstance
                return {"accepted": True}

            try:
                result = dispatcher.dispatch(
                    attempt_id="post-send-isinstance-rebind-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda *_args: (True, "allowed"),
                    transport_send=transport,
                )
            finally:
                if had_global_isinstance:
                    dispatch_module.isinstance = original_isinstance
                else:
                    del dispatch_module.isinstance

            self.assertEqual(callbacks, 0)
            self.assertEqual(result.status, "SENT")
            self.assertEqual(result.reason, "sent_confirmed")
            self.assertEqual(result.response, {"accepted": True})
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-send-isinstance-rebind-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionSent",
                ],
            )

    def test_legacy_response_subclass_is_rejected_without_callback_execution(self):
        class TrapDict(dict):
            callbacks = 0

            def items(self):
                type(self).callbacks += 1
                raise AssertionError("provider response items callback executed")

            def __iter__(self):
                type(self).callbacks += 1
                raise AssertionError("provider response iteration callback executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return TrapDict({"accepted": True})

            result = dispatcher.dispatch(
                attempt_id="legacy-response-subclass-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(wire_calls, 1)
            self.assertEqual(TrapDict.callbacks, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("legacy-response-subclass-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "sent_response_persistence_failed:TypeError",
            )

    def test_terminal_reread_rejects_post_append_tamper(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_append = dispatcher._append
            wire_calls = 0

            def append_then_tamper(**kwargs):
                result = original_append(**kwargs)
                if kwargs["event_type"] == "SubmissionSent":
                    self._tamper_event_field(
                        path,
                        "SubmissionSent",
                        "client_order_id",
                        "retargeted-client",
                    )
                return result

            dispatcher._append = append_then_tamper

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(b'{"accepted":true}')

            result = dispatcher.dispatch(
                attempt_id="terminal-reread-tamper-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(wire_calls, 1)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_terminal_reread_rejects_post_append_disappearance(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            original_append = dispatcher._append

            def append_then_delete(**kwargs):
                result = original_append(**kwargs)
                if kwargs["event_type"] == "SubmissionSent":
                    connection = sqlite3.connect(path)
                    try:
                        connection.execute(
                            "DELETE FROM events WHERE event_type = 'SubmissionSent'"
                        )
                        connection.commit()
                    finally:
                        connection.close()
                return result

            dispatcher._append = append_then_delete
            result = dispatcher.dispatch(
                attempt_id="terminal-reread-delete-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(b'{"accepted":true}'),
                )[1],
                submission_scope={"endpoint": "/orders"},
            )

            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("terminal-reread-delete-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                ["SubmissionPrepared", "SubmissionSending"],
            )

    def test_runtime_redispatch_rejects_submission_scope_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionPrepared",
                "submission_scope",
                {"endpoint": "/retargeted"},
            )

            with self.assertRaisesRegex(
                ValueError,
                "attempt_id conflicts with existing submission content",
            ):
                self._redispatch_exact_response_attempt(path)

    def test_sending_recovery_race_revalidates_submission_scope_snapshot(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="restart-owner",
            )
            complete = dispatcher._events("binding-type-a1")
            sending_history = complete[:2]
            corrupt_terminal = json.loads(canonical_json(complete))
            corrupt_terminal[0]["payload"]["submission_scope"] = {
                "endpoint": "/retargeted"
            }
            reads = [sending_history, corrupt_terminal]
            expected_prepared = {
                key: sending_history[0]["payload"][key]
                for key in (
                    "attempt_id",
                    "intent_id",
                    "intent_hash",
                    "provider",
                    "request_hash",
                    "client_order_id",
                    "environment",
                    "account_id",
                    "submission_scope",
                    "submission_scope_hash",
                )
            }

            def raced_events(_attempt_id):
                self.assertTrue(reads)
                return reads.pop(0)

            def lose_terminal_race(**_kwargs):
                raise ValueError("simulated aggregate-version race")

            dispatcher._events = raced_events
            dispatcher._append = lose_terminal_race

            result = dispatcher._recover_existing(
                attempt_id="binding-type-a1",
                client_order_id=sending_history[0]["payload"]["client_order_id"],
                now="2026-10-06T14:00:02Z",
                expected_prepared=expected_prepared,
            )
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")
            self.assertEqual(reads, [])

    def test_runtime_recovery_rejects_non_contiguous_aggregate_versions(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_aggregate_version(
                path,
                "SubmissionSent",
                4,
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_runtime_recovery_rejects_cross_event_client_order_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionSent",
                "client_order_id",
                "forged-client-order-id",
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_runtime_recovery_rejects_cross_event_environment_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionSending",
                "environment",
                "PAPER",
                envelope_field=True,
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_runtime_recovery_rejects_nonmonotonic_durable_chronology(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_timestamp_authority(
                path,
                "SubmissionSent",
                "2026-10-06T13:59:59Z",
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_runtime_recovery_rejects_prepared_payload_timestamp_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionPrepared",
                "prepared_at",
                "2026-10-06T13:59:59Z",
            )

            result = self._redispatch_exact_response_attempt(path)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "durable_submission_history_invalid")

    def test_restart_rejects_nonmonotonic_durable_chronology(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_timestamp_authority(
                path,
                "SubmissionSent",
                "2026-10-06T13:59:59Z",
            )

            reopened = JournalStore(path)
            with self.assertRaisesRegex(
                ValueError,
                "durable submission chronology is not monotonic",
            ):
                load_submission_response_binding(
                    reopened,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="binding-type-a1",
                )

    def test_restart_rejects_partial_timestamp_authority_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionSent",
                "observed_at",
                "2026-10-06T13:59:59Z",
                envelope_field=True,
            )

            reopened = JournalStore(path)
            with self.assertRaisesRegex(
                ValueError,
                "durable terminal timestamp authority is invalid",
            ):
                load_submission_response_binding(
                    reopened,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="binding-type-a1",
                )

    def test_restart_rejects_prepared_payload_timestamp_retargeting(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_field(
                path,
                "SubmissionPrepared",
                "prepared_at",
                "2026-10-06T13:59:59Z",
            )

            reopened = JournalStore(path)
            with self.assertRaisesRegex(
                ValueError,
                "durable SubmissionPrepared prepared_at mismatches event chronology",
            ):
                load_submission_response_binding(
                    reopened,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="binding-type-a1",
                )

    def test_restart_rejects_non_contiguous_aggregate_versions(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            self._tamper_event_aggregate_version(
                path,
                "SubmissionSent",
                4,
            )

            reopened = JournalStore(path)
            with self.assertRaisesRegex(
                ValueError,
                "durable exact response requires aggregate versions 1 -> 2 -> 3",
            ):
                load_submission_response_binding(
                    reopened,
                    environment="SIMULATION",
                    account_id="acct",
                    attempt_id="binding-type-a1",
                )

    def test_restart_rejects_coerced_identity_fields_in_durable_prepared_event(self):
        for field, bad_value in (
            ("attempt_id", ["attempt"]),
            ("provider", 7),
            ("request_hash", {"digest": "forged"}),
            ("client_order_id", ["client"]),
            ("environment", 1),
            ("account_id", {"id": "acct"}),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    "SubmissionPrepared",
                    field,
                    bad_value,
                )

                reopened = JournalStore(path)
                with self.assertRaisesRegex(
                    ValueError,
                    f"durable SubmissionPrepared {field} must be exact non-empty text",
                ):
                    load_submission_response_binding(
                        reopened,
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )


    def test_restart_rejects_exact_text_identity_retargeting(self):
        for field, bad_value in (
            ("attempt_id", "other-attempt"),
            ("environment", "PAPER"),
            ("account_id", "other-account"),
        ):
            with self.subTest(field=field), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    "SubmissionPrepared",
                    field,
                    bad_value,
                )

                reopened = JournalStore(path)
                with self.assertRaisesRegex(
                    ValueError,
                    f"durable SubmissionPrepared {field} mismatches selected submission identity",
                ):
                    load_submission_response_binding(
                        reopened,
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )

    def test_restart_rejects_cross_event_client_order_identity_retargeting(self):
        for event_type in ("SubmissionSending", "SubmissionSent"):
            with self.subTest(event_type=event_type), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    event_type,
                    "client_order_id",
                    "forged-client-order-id",
                )

                reopened = JournalStore(path)
                expected_event = (
                    "SubmissionSending" if event_type == "SubmissionSending" else "terminal"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    f"durable {expected_event} client_order_id mismatches SubmissionPrepared",
                ):
                    load_submission_response_binding(
                        reopened,
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )

    def test_restart_rejects_cross_event_environment_retargeting(self):
        for event_type in ("SubmissionSending", "SubmissionSent"):
            with self.subTest(event_type=event_type), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                self._make_exact_response_attempt(path)
                self._tamper_event_field(
                    path,
                    event_type,
                    "environment",
                    "PAPER",
                    envelope_field=True,
                )

                reopened = JournalStore(path)
                expected_event = (
                    "SubmissionSending" if event_type == "SubmissionSending" else "terminal"
                )
                with self.assertRaisesRegex(
                    ValueError,
                    f"durable {expected_event} environment mismatches selected submission identity",
                ):
                    load_submission_response_binding(
                        reopened,
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )



    def test_public_identity_boundaries_reject_string_subclasses_without_executing_them(self):
        class TrapText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled strip executed")

            def upper(self):
                raise AssertionError("caller-controlled upper executed")

            def lower(self):
                raise AssertionError("caller-controlled lower executed")

            def replace(self, *args, **kwargs):
                raise AssertionError("caller-controlled replace executed")

        trap = TrapText("SIMULATION")
        with self.assertRaisesRegex(ValueError, "environment must be REPLAY"):
            submission_attempt_aggregate_id(
                environment=trap,
                account_id="acct",
                attempt_id="attempt",
            )
        with self.assertRaisesRegex(ValueError, "account_id is required"):
            submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id=TrapText("acct"),
                attempt_id="attempt",
            )
        with self.assertRaisesRegex(ValueError, "attempt_id is required"):
            submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct",
                attempt_id=TrapText("attempt"),
            )
        with self.assertRaisesRegex(ValueError, "provider is required"):
            stable_client_order_id(
                TrapText("provider"),
                "intent",
                environment="SIMULATION",
                account_id="acct",
            )
        with self.assertRaisesRegex(ValueError, "intent_id is required"):
            stable_client_order_id(
                "provider",
                TrapText("intent"),
                environment="SIMULATION",
                account_id="acct",
            )
        with self.assertRaisesRegex(ValueError, "environment must be REPLAY"):
            stable_client_order_id(
                "provider",
                "intent",
                environment=trap,
                account_id="acct",
            )
        with self.assertRaisesRegex(ValueError, "account_id is required"):
            stable_client_order_id(
                "provider",
                "intent",
                environment="SIMULATION",
                account_id=TrapText("acct"),
            )
        with self.assertRaisesRegex(TypeError, "client_id_format must be text"):
            stable_client_order_id(
                "provider",
                "intent",
                environment="SIMULATION",
                account_id="acct",
                client_id_format=TrapText("TOKEN"),
            )

    def test_dispatcher_constructor_rejects_string_subclass_scope_and_owner(self):
        class TrapText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled strip executed")

            def upper(self):
                raise AssertionError("caller-controlled upper executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "environment must be REPLAY"):
                GuardedDispatcher(
                    store,
                    environment=TrapText("SIMULATION"),
                    account_id="acct",
                )
            with self.assertRaisesRegex(ValueError, "account_id is required"):
                GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id=TrapText("acct"),
                )
            with self.assertRaisesRegex(ValueError, "owner_token must be exact non-empty text"):
                GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token=TrapText("owner"),
                )


    def test_authority_result_rejects_polymorphic_tuple_and_reason_before_transport(self):
        class TrapTuple(tuple):
            def __len__(self):
                raise AssertionError("caller-controlled tuple length executed")

            def __iter__(self):
                raise AssertionError("caller-controlled tuple iteration executed")

        class TrapReason(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled reason strip executed")

        for authority_result, expected_reason in (
            (TrapTuple((True, "allowed")), "authority_check_invalid_result"),
            ((True, TrapReason("allowed")), "authority_check_invalid_reason"),
        ):
            with self.subTest(expected_reason=expected_reason), TemporaryDirectory() as directory:
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                transport_calls = 0

                def transport(_cid, _request, _guard):
                    nonlocal transport_calls
                    transport_calls += 1
                    raise AssertionError("transport must not execute")

                result = dispatcher.dispatch(
                    attempt_id="authority-shape-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now="2026-10-06T14:00:00Z",
                    authority_check=lambda _hash, _now, value=authority_result: value,
                    transport_send=transport,
                )
                self.assertEqual(result.status, "BLOCKED")
                self.assertEqual(result.reason, expected_reason)
                self.assertEqual(transport_calls, 0)

    def test_dispatch_rejects_mapping_subclasses_before_callbacks(self):
        class TrapDict(dict):
            callbacks = 0

            def items(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled items executed")

            def __iter__(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled iteration executed")

        for field_name in ("request", "submission_scope"):
            with self.subTest(field_name=field_name), TemporaryDirectory() as directory:
                TrapDict.callbacks = 0
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                authority_calls = 0
                transport_calls = 0

                def authority(_intent_hash, _now):
                    nonlocal authority_calls
                    authority_calls += 1
                    return True, "allowed"

                def transport(_client_order_id, _request, _guard):
                    nonlocal transport_calls
                    transport_calls += 1
                    raise AssertionError("transport must not execute")

                kwargs = {
                    "attempt_id": "mapping-shape-a1",
                    "intent_id": "intent-1",
                    "intent_hash": "sha256:" + "1" * 64,
                    "provider": "provider",
                    "request": {"side": "BUY"},
                    "now": "2026-10-06T14:00:00Z",
                    "authority_check": authority,
                    "transport_send": transport,
                    "submission_scope": {"endpoint": "/orders"},
                }
                kwargs[field_name] = TrapDict(kwargs[field_name])
                with self.assertRaisesRegex(
                    TypeError,
                    f"{field_name} must be an exact dict",
                ):
                    dispatcher.dispatch(**kwargs)

                self.assertEqual(TrapDict.callbacks, 0)
                self.assertEqual(authority_calls, 0)
                self.assertEqual(transport_calls, 0)
                self.assertEqual(
                    JournalStore.load_events(
                        store,
                        "submission_attempt",
                        dispatcher._aggregate_id("mapping-shape-a1"),
                    ),
                    [],
                )

    def test_dispatch_rejects_nested_polymorphic_json_before_callbacks(self):
        class TrapText(str):
            callbacks = 0

            def encode(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled encode executed")

            def __str__(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled string conversion executed")

        for field_name in ("request", "submission_scope"):
            with self.subTest(field_name=field_name), TemporaryDirectory() as directory:
                TrapText.callbacks = 0
                store = JournalStore(f"{directory}/journal.sqlite3")
                dispatcher = GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_token="owner",
                )
                kwargs = {
                    "attempt_id": "nested-json-a1",
                    "intent_id": "intent-1",
                    "intent_hash": "sha256:" + "1" * 64,
                    "provider": "provider",
                    "request": {"side": "BUY"},
                    "now": "2026-10-06T14:00:00Z",
                    "authority_check": lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("authority must not execute")),
                    "transport_send": lambda *_args: (
                        _ for _ in ()
                    ).throw(AssertionError("transport must not execute")),
                    "submission_scope": {"endpoint": "/orders"},
                }
                kwargs[field_name] = {"nested": [TrapText("hostile")]}
                with self.assertRaisesRegex(
                    TypeError,
                    "submission JSON values must use exact built-in",
                ):
                    dispatcher.dispatch(**kwargs)
                self.assertEqual(TrapText.callbacks, 0)

    def test_dispatch_preserves_exact_tuple_json_array_compatibility(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            seen_request = []

            def transport(_client_order_id, request, guard):
                seen_request.append(request)
                guard()
                return ExactJsonTransportResponse(b'{"accepted":true}')

            result = dispatcher.dispatch(
                attempt_id="tuple-json-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"legs": ("A", "B")},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"axes": ("x", "y")},
            )

            self.assertEqual(result.status, "SENT")
            self.assertEqual(tuple(seen_request[0]["legs"]), ("A", "B"))
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("tuple-json-a1"),
            )
            self.assertEqual(
                events[0]["payload"]["submission_scope"]["axes"],
                ["x", "y"],
            )

    def test_post_send_legacy_response_rejects_polymorphic_json_without_callbacks(self):
        class TrapDict(dict):
            callbacks = 0

            def items(self):
                type(self).callbacks += 1
                raise AssertionError("provider response items executed")

            def __iter__(self):
                type(self).callbacks += 1
                raise AssertionError("provider response iteration executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )

            def transport(_client_order_id, _request, guard):
                guard()
                return {"provider": TrapDict({"accepted": True})}

            result = dispatcher.dispatch(
                attempt_id="post-send-response-callback-a1",
                intent_id="intent-1",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
            )

            self.assertEqual(TrapDict.callbacks, 0)
            self.assertEqual(result.status, "UNKNOWN")
            self.assertEqual(result.reason, "sent_response_persistence_failed")
            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("post-send-response-callback-a1"),
            )
            self.assertEqual(
                [event["event_type"] for event in events],
                [
                    "SubmissionPrepared",
                    "SubmissionSending",
                    "SubmissionUnknown",
                ],
            )
            self.assertEqual(
                events[-1]["payload"]["reason"],
                "sent_response_persistence_failed:TypeError",
            )

    def test_dispatch_rejects_polymorphic_timestamp_before_timestamp_methods_execute(self):
        class TrapTimestamp(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled timestamp strip executed")

            def replace(self, *args, **kwargs):
                raise AssertionError("caller-controlled timestamp replace executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            with self.assertRaisesRegex(ValueError, "now must be an ISO timestamp"):
                dispatcher.dispatch(
                    attempt_id="timestamp-shape-a1",
                    intent_id="intent-1",
                    intent_hash="sha256:" + "1" * 64,
                    provider="provider",
                    request={"side": "BUY"},
                    now=TrapTimestamp("2026-10-06T14:00:00Z"),
                    authority_check=lambda _hash, _now: (True, "allowed"),
                    transport_send=lambda _cid, _request, _guard: None,
                )


    def test_public_integer_boundaries_reject_int_subclasses_without_executing_them(self):
        class TrapInt(int):
            def __lt__(self, other):
                raise AssertionError("caller-controlled comparison executed")

            def __str__(self):
                raise AssertionError("caller-controlled string conversion executed")

        with self.assertRaisesRegex(ValueError, "max_length must be an integer"):
            stable_client_order_id(
                "provider",
                "intent",
                environment="SIMULATION",
                account_id="acct",
                max_length=TrapInt(32),
            )
        with self.assertRaisesRegex(
            ValueError,
            "UUID client-order identity requires max_length",
        ):
            stable_client_order_id(
                "provider",
                "intent",
                environment="SIMULATION",
                account_id="acct",
                max_length=TrapInt(36),
                client_id_format="UUID",
            )

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            with self.assertRaisesRegex(ValueError, "owner_epoch must be a positive integer"):
                GuardedDispatcher(
                    store,
                    environment="SIMULATION",
                    account_id="acct",
                    owner_epoch=TrapInt(1),
                )



    def test_dispatch_canonicalizes_identity_text_before_durable_send_and_replay(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            wire_calls = 0

            def transport(_client_order_id, _request, guard):
                nonlocal wire_calls
                guard()
                wire_calls += 1
                return ExactJsonTransportResponse(
                    b'{"accepted":true}',
                    http_status=200,
                )

            first = dispatcher.dispatch(
                attempt_id="  canonical-attempt  ",
                intent_id="  canonical-intent  ",
                intent_hash="sha256:" + "1" * 64,
                provider="  provider  ",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda *_args: (True, "allowed"),
                transport_send=transport,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(first.status, "SENT")
            self.assertEqual(wire_calls, 1)

            events = JournalStore.load_events(
                store,
                "submission_attempt",
                dispatcher._aggregate_id("canonical-attempt"),
            )
            prepared = events[0]["payload"]
            self.assertEqual(prepared["attempt_id"], "canonical-attempt")
            self.assertEqual(prepared["intent_id"], "canonical-intent")
            self.assertEqual(prepared["provider"], "provider")

            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="canonical-attempt",
            )
            self.assertEqual(binding.attempt_id, "canonical-attempt")
            self.assertEqual(binding.provider, "provider")

            def must_not_resend(*_args):
                raise AssertionError("canonical replay must not resend")

            replay = dispatcher.dispatch(
                attempt_id="canonical-attempt",
                intent_id="canonical-intent",
                intent_hash="sha256:" + "1" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:01Z",
                authority_check=lambda *_args: (
                    (_ for _ in ()).throw(
                        AssertionError("canonical replay must not rerun authority")
                    )
                ),
                transport_send=must_not_resend,
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(replay.status, "SENT")
            self.assertEqual(replay.client_order_id, first.client_order_id)
            self.assertEqual(wire_calls, 1)

    def test_dispatch_rejects_polymorphic_text_before_caller_methods_execute(self):
        class TrapText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("caller-controlled strip executed")

        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            authority_calls = 0
            transport_calls = 0

            def authority(_intent_hash, _now):
                nonlocal authority_calls
                authority_calls += 1
                return True, "allowed"

            def transport(_client_order_id, _request, _guard):
                nonlocal transport_calls
                transport_calls += 1
                raise AssertionError("transport must not execute")

            base_values = {
                "attempt_id": "attempt-a1",
                "intent_id": "intent-1",
                "intent_hash": "sha256:" + "1" * 64,
                "provider": "provider",
            }
            for field_name in (
                "attempt_id",
                "intent_id",
                "intent_hash",
                "provider",
            ):
                values = dict(base_values)
                values[field_name] = TrapText(values[field_name])
                with self.subTest(field_name=field_name):
                    with self.assertRaisesRegex(
                        ValueError,
                        f"{field_name} is required",
                    ):
                        dispatcher.dispatch(
                            **values,
                            request={"side": "BUY"},
                            now="2026-10-06T14:00:00Z",
                            authority_check=authority,
                            transport_send=transport,
                        )

            self.assertEqual(authority_calls, 0)
            self.assertEqual(transport_calls, 0)


    def test_binding_loader_rejects_rebound_journal_call_before_callback(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            callbacks = 0
            original = dispatch_module._journal_store_call

            def forged_journal_call(*_args, **_kwargs):
                nonlocal callbacks
                callbacks += 1
                raise AssertionError("rebound journal call executed")

            try:
                dispatch_module._journal_store_call = forged_journal_call
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                dispatch_module._journal_store_call = original

            self.assertEqual(callbacks, 0)
            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_constructor_and_hash_authority_rebinding_before_callbacks(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            original_post_init = SubmissionResponseBinding.__post_init__
            post_init_callbacks = 0

            def forged_post_init(_self):
                nonlocal post_init_callbacks
                post_init_callbacks += 1

            try:
                SubmissionResponseBinding.__post_init__ = forged_post_init
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                SubmissionResponseBinding.__post_init__ = original_post_init
            self.assertEqual(post_init_callbacks, 0)

            original_canonical_json = dispatch_module.canonical_json
            json_callbacks = 0

            def forged_canonical_json(_value):
                nonlocal json_callbacks
                json_callbacks += 1
                raise AssertionError("rebound canonical_json executed")

            try:
                dispatch_module.canonical_json = forged_canonical_json
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                dispatch_module.canonical_json = original_canonical_json
            self.assertEqual(json_callbacks, 0)

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_late_helper_rebinding_before_callbacks(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            surfaces = (
                "_canonical_submission_event_instant",
                "_event_id",
                "_identity_digest",
                "_exact_response_terminal_semantics_are_canonical",
            )
            for surface in surfaces:
                callbacks = 0
                original = getattr(dispatch_module, surface)

                def forged(*_args, **_kwargs):
                    nonlocal callbacks
                    callbacks += 1
                    raise AssertionError(f"rebound {surface} executed")

                try:
                    setattr(dispatch_module, surface, forged)
                    with self.subTest(surface=surface), self.assertRaisesRegex(
                        ValueError,
                        "submission response binding authority is unavailable",
                    ):
                        load_submission_response_binding(
                            JournalStore(path),
                            environment="SIMULATION",
                            account_id="acct",
                            attempt_id="binding-type-a1",
                        )
                finally:
                    setattr(dispatch_module, surface, original)
                self.assertEqual(callbacks, 0)

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_transitive_decoder_rebinding_before_callbacks(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            callbacks = 0

            def forged(*_args, **_kwargs):
                nonlocal callbacks
                callbacks += 1
                raise AssertionError("rebound decoder authority executed")

            original_loads = dispatch_module.json.loads
            original_decoder = dispatch_module.json.JSONDecoder
            original_number_parser = dispatch_module.parse_bounded_json_number_token
            original_integer_parser = dispatch_module.parse_bounded_json_integer_token
            original_exact_error = dispatch_module.ExactDecimalError

            class ForgedDecoder:
                def __init__(self, **_kwargs):
                    forged()

            cases = (
                (
                    "json.loads",
                    lambda: setattr(dispatch_module.json, "loads", forged),
                    lambda: setattr(dispatch_module.json, "loads", original_loads),
                ),
                (
                    "json.JSONDecoder",
                    lambda: setattr(dispatch_module.json, "JSONDecoder", ForgedDecoder),
                    lambda: setattr(dispatch_module.json, "JSONDecoder", original_decoder),
                ),
                (
                    "parse_bounded_json_number_token",
                    lambda: setattr(
                        dispatch_module,
                        "parse_bounded_json_number_token",
                        forged,
                    ),
                    lambda: setattr(
                        dispatch_module,
                        "parse_bounded_json_number_token",
                        original_number_parser,
                    ),
                ),
                (
                    "parse_bounded_json_integer_token",
                    lambda: setattr(
                        dispatch_module,
                        "parse_bounded_json_integer_token",
                        forged,
                    ),
                    lambda: setattr(
                        dispatch_module,
                        "parse_bounded_json_integer_token",
                        original_integer_parser,
                    ),
                ),
                (
                    "ExactDecimalError",
                    lambda: setattr(dispatch_module, "ExactDecimalError", RuntimeError),
                    lambda: setattr(
                        dispatch_module,
                        "ExactDecimalError",
                        original_exact_error,
                    ),
                ),
            )
            for surface, mutate, restore in cases:
                try:
                    mutate()
                    with self.subTest(surface=surface), self.assertRaisesRegex(
                        ValueError,
                        "submission response binding authority is unavailable",
                    ):
                        load_submission_response_binding(
                            JournalStore(path),
                            environment="SIMULATION",
                            account_id="acct",
                            attempt_id="binding-type-a1",
                        )
                finally:
                    restore()
                self.assertEqual(callbacks, 0)

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_in_place_json_loads_kwdefault_retarget(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            loads = dispatch_module.json.loads
            kwdefaults = loads.__kwdefaults__
            self.assertIs(type(kwdefaults), dict)
            original_items = tuple(kwdefaults.items())

            class ForgedDecoder:
                calls = 0

                def __init__(self, **_kwargs):
                    type(self).calls += 1
                    raise AssertionError("forged JSON decoder executed")

            try:
                kwdefaults["cls"] = ForgedDecoder
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                kwdefaults.clear()
                kwdefaults.update(dict(original_items))

            self.assertEqual(ForgedDecoder.calls, 0)
            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_in_place_json_decoder_and_scanner_mutation(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            decoder = dispatch_module.json.JSONDecoder
            decode = decoder.__dict__["decode"]
            original_decode_code = decode.__code__

            def forged_decode(self, text):
                raise AssertionError("forged JSONDecoder.decode executed")

            try:
                decode.__code__ = forged_decode.__code__
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                decode.__code__ = original_decode_code

            decoder_init = decoder.__dict__["__init__"]
            scanner = decoder_init.__globals__["scanner"]
            scanner_namespace = vars(scanner)
            original_make_scanner = scanner_namespace["make_scanner"]
            scanner_callbacks = 0

            def forged_make_scanner(_context):
                nonlocal scanner_callbacks
                scanner_callbacks += 1
                raise AssertionError("forged scanner executed")

            try:
                scanner_namespace["make_scanner"] = forged_make_scanner
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                scanner_namespace["make_scanner"] = original_make_scanner

            self.assertEqual(scanner_callbacks, 0)
            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_in_place_json_decoder_kwdefault_retarget(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            decoder_init = dispatch_module.json.JSONDecoder.__dict__["__init__"]
            kwdefaults = decoder_init.__kwdefaults__
            self.assertIs(type(kwdefaults), dict)
            original_items = tuple(kwdefaults.items())
            try:
                kwdefaults["strict"] = False
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                kwdefaults.clear()
                kwdefaults.update(dict(original_items))

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_canonical_json_code_and_global_retarget(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)

            canonical = dispatch_module.canonical_json
            original_code = canonical.__code__

            def forged_canonical_json(_value):
                raise AssertionError("forged canonical JSON executed")

            try:
                canonical.__code__ = forged_canonical_json.__code__
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                canonical.__code__ = original_code

            canonical_globals = canonical.__globals__
            original_json = canonical_globals["json"]

            class ForgedJson:
                @staticmethod
                def dumps(*_args, **_kwargs):
                    raise AssertionError("forged canonical JSON module executed")

            try:
                canonical_globals["json"] = ForgedJson
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    load_submission_response_binding(
                        JournalStore(path),
                        environment="SIMULATION",
                        account_id="acct",
                        attempt_id="binding-type-a1",
                    )
            finally:
                canonical_globals["json"] = original_json

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")

    def test_binding_loader_rejects_json_encoder_rebinding_before_callbacks(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            callbacks = 0

            def forged(*_args, **_kwargs):
                nonlocal callbacks
                callbacks += 1
                raise AssertionError("rebound canonicalization authority executed")

            original_dumps = dispatch_module.json.dumps
            original_encoder = dispatch_module.json.JSONEncoder

            class ForgedEncoder:
                def __init__(self, **_kwargs):
                    forged()

            cases = (
                (
                    "json.dumps",
                    lambda: setattr(dispatch_module.json, "dumps", forged),
                    lambda: setattr(dispatch_module.json, "dumps", original_dumps),
                ),
                (
                    "json.JSONEncoder",
                    lambda: setattr(
                        dispatch_module.json,
                        "JSONEncoder",
                        ForgedEncoder,
                    ),
                    lambda: setattr(
                        dispatch_module.json,
                        "JSONEncoder",
                        original_encoder,
                    ),
                ),
            )
            for surface, mutate, restore in cases:
                try:
                    mutate()
                    with self.subTest(surface=surface), self.assertRaisesRegex(
                        ValueError,
                        "submission response binding authority is unavailable",
                    ):
                        load_submission_response_binding(
                            JournalStore(path),
                            environment="SIMULATION",
                            account_id="acct",
                            attempt_id="binding-type-a1",
                        )
                finally:
                    restore()
                self.assertEqual(callbacks, 0)

            restored = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            self.assertEqual(restored.attempt_id, "binding-type-a1")
            self.assertNotIn("re", vars(dispatch_module))

    def test_response_binding_constructor_rejects_polymorphic_authority_inputs(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        class TrapText(str):
            callbacks = 0

            def strip(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled strip executed")

            def upper(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled upper executed")

            def encode(self, *args, **kwargs):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled encode executed")

        scope = {}
        response = b"{}"
        kwargs = {
            "attempt_id": "attempt-constructor",
            "aggregate_id": submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct-constructor",
                attempt_id="attempt-constructor",
            ),
            "provider": "BYBIT",
            "request_hash": "sha256:" + "1" * 64,
            "client_order_id": "client-constructor",
            "environment": "SIMULATION",
            "account_id": "acct-constructor",
            "prepared_at": "2026-10-06T14:00:00Z",
            "sent_at": "2026-10-06T14:00:01Z",
            "submission_scope": scope,
            "submission_scope_hash": "sha256:"
            + sha256(canonical_json(scope).encode("utf-8")).hexdigest(),
            "response_bytes": response,
            "response_sha256": "sha256:" + sha256(response).hexdigest(),
            "_factory_token": dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN,
        }
        for field in (
            "attempt_id",
            "aggregate_id",
            "provider",
            "request_hash",
            "client_order_id",
            "environment",
            "account_id",
            "submission_scope_hash",
            "response_sha256",
        ):
            TrapText.callbacks = 0
            forged = dict(kwargs)
            forged[field] = TrapText(forged[field])
            with self.subTest(field=field):
                with self.assertRaises((TypeError, ValueError)):
                    SubmissionResponseBinding(**forged)
                self.assertEqual(TrapText.callbacks, 0)

    def test_response_binding_constructor_cross_binds_identity_and_chronology(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        scope = {}
        response = b"{}"
        kwargs = {
            "attempt_id": "attempt-constructor",
            "aggregate_id": submission_attempt_aggregate_id(
                environment="SIMULATION",
                account_id="acct-constructor",
                attempt_id="attempt-constructor",
            ),
            "provider": "BYBIT",
            "request_hash": "sha256:" + "1" * 64,
            "client_order_id": "client-constructor",
            "environment": "SIMULATION",
            "account_id": "acct-constructor",
            "prepared_at": "2026-10-06T14:00:00Z",
            "sent_at": "2026-10-06T14:00:01Z",
            "submission_scope": scope,
            "submission_scope_hash": "sha256:"
            + sha256(canonical_json(scope).encode("utf-8")).hexdigest(),
            "response_bytes": response,
            "response_sha256": "sha256:" + sha256(response).hexdigest(),
            "_factory_token": dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN,
        }

        forged_identity = dict(kwargs)
        forged_identity["aggregate_id"] = "submission-attempt:" + "0" * 64
        with self.assertRaisesRegex(
            ValueError,
            "aggregate_id mismatches durable submission identity",
        ):
            SubmissionResponseBinding(**forged_identity)

        reversed_chronology = dict(kwargs)
        reversed_chronology["sent_at"] = "2026-10-06T13:59:59Z"
        with self.assertRaisesRegex(
            ValueError,
            "sent_at must not precede prepared_at",
        ):
            SubmissionResponseBinding(**reversed_chronology)

    def test_response_binding_constructor_rejects_mapping_subclass_before_callbacks(self):
        from mvp.autotrade_mvp import dispatch as dispatch_module

        class TrapDict(dict):
            callbacks = 0

            def items(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled items executed")

            def __iter__(self):
                type(self).callbacks += 1
                raise AssertionError("caller-controlled iteration executed")

        response = b"{}"
        scope = TrapDict()
        with self.assertRaisesRegex(TypeError, "submission_scope must be an exact dict"):
            SubmissionResponseBinding(
                attempt_id="attempt-constructor",
                aggregate_id=submission_attempt_aggregate_id(
                    environment="SIMULATION",
                    account_id="acct-constructor",
                    attempt_id="attempt-constructor",
                ),
                provider="BYBIT",
                request_hash="sha256:" + "1" * 64,
                client_order_id="client-constructor",
                environment="SIMULATION",
                account_id="acct-constructor",
                prepared_at="2026-10-06T14:00:00Z",
                sent_at="2026-10-06T14:00:01Z",
                submission_scope=scope,
                submission_scope_hash="sha256:" + sha256(b"{}").hexdigest(),
                response_bytes=response,
                response_sha256="sha256:" + sha256(response).hexdigest(),
                _factory_token=dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN,
            )
        self.assertEqual(TrapDict.callbacks, 0)

if __name__ == "__main__":
    unittest.main()
