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
