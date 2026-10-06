from hashlib import sha256
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

import mvp.autotrade_mvp.dispatch as dispatch_module

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    SubmissionResponseBinding,
    _event_id,
    _identity_digest,
    load_submission_response_binding,
    require_canonical_submission_response_binding,
    stable_client_order_id,
    submission_attempt_aggregate_id,
    submission_response_binding_projection,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json, payload_digest
from mvp.autotrade_mvp.provider_core import (
    ProviderCoreError,
    observe_submission_json_response,
    provider_submission_observation_projection,
)


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

    def test_importable_binding_token_cannot_mint_response_authority(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            projected = submission_response_binding_projection(binding)
            require_canonical_submission_response_binding(binding)
            self.assertEqual(projected["terminal_state"], "SENT")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertIsNone(projected["ambiguity_reason"])
            self.assertIsNone(projected["retry_disposition"])

            forged = SubmissionResponseBinding(
                attempt_id=projected["attempt_id"],
                aggregate_id=projected["aggregate_id"],
                provider=projected["provider"],
                request_hash=projected["request_hash"],
                client_order_id=projected["client_order_id"],
                environment=projected["environment"],
                account_id=projected["account_id"],
                prepared_at=projected["prepared_at"],
                sent_at=projected["sent_at"],
                submission_scope=dict(projected["submission_scope"]),
                submission_scope_hash=projected["submission_scope_hash"],
                response_bytes=projected["response_bytes"],
                response_sha256=projected["response_sha256"],
                http_status=projected["http_status"],
                _factory_token=dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN,
            )
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                submission_response_binding_projection(forged)

            clone = object.__new__(SubmissionResponseBinding)
            for name in (
                "attempt_id",
                "aggregate_id",
                "provider",
                "request_hash",
                "client_order_id",
                "environment",
                "account_id",
                "prepared_at",
                "sent_at",
                "submission_scope",
                "submission_scope_hash",
                "response_bytes",
                "response_sha256",
                "http_status",
                "terminal_state",
                "response_encoding",
                "ambiguity_reason",
                "retry_disposition",
                "_factory_token",
            ):
                object.__setattr__(
                    clone,
                    name,
                    object.__getattribute__(binding, name),
                )
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                require_canonical_submission_response_binding(clone)

    def test_binding_projection_preserves_durable_unknown_reconciliation_metadata(self):
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
                attempt_id="binding-unknown-a1",
                intent_id="intent-unknown-1",
                intent_hash="sha256:" + "2" * 64,
                provider="provider",
                request={"side": "BUY"},
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"accepted":false}',
                        http_status=200,
                        requires_reconciliation=True,
                        ambiguity_reason="provider_response_ambiguous",
                    ),
                )[1],
                submission_scope={"endpoint": "/orders"},
            )
            self.assertEqual(result.status, "UNKNOWN")

            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-unknown-a1",
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["terminal_state"], "UNKNOWN")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertEqual(
                projected["ambiguity_reason"],
                "provider_response_ambiguous",
            )
            self.assertEqual(
                projected["retry_disposition"],
                "RECONCILE_FIRST",
            )

    def test_binding_projection_rejects_post_load_retargeting_and_restart_remints(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            first = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            first_projection = submission_response_binding_projection(first)

            restarted = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            restarted_projection = submission_response_binding_projection(restarted)
            self.assertEqual(
                restarted_projection["response_sha256"],
                first_projection["response_sha256"],
            )
            self.assertIsNot(restarted, first)

            object.__setattr__(first, "provider", "forged-provider")
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                submission_response_binding_projection(first)

            object.__setattr__(restarted, "shadow_authority", "forged")
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                require_canonical_submission_response_binding(restarted)

    def test_restart_binding_composes_into_authenticated_provider_observation(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            request = {"side": "BUY", "symbol": "BTCUSDT"}
            request_sha = (
                "sha256:"
                + sha256(canonical_json(request).encode("utf-8")).hexdigest()
            )
            scope = {
                "endpoint": "/v5/order/create",
                "prepared_request_sha256": request_sha,
                "capability_snapshot_ids": ["cap-1"],
                "instrument_versions": ["BTCUSDT:v1"],
                "provider_environment": "TESTNET",
            }
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="provider-observation-sent-a1",
                intent_id="intent-provider-observation-sent",
                intent_hash="sha256:" + "7" * 64,
                provider="BYBIT",
                request=request,
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"retCode":0,"result":{"orderId":"provider-1"}}',
                        http_status=200,
                    ),
                )[1],
                submission_scope=scope,
            )
            self.assertEqual(result.status, "SENT")

            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="provider-observation-sent-a1",
            )
            observation = observe_submission_json_response(
                response_binding=binding,
                provider_id="BYBIT",
                endpoint="/v5/order/create",
                prepared_request_sha256=request_sha,
                capability_snapshot_ids=("cap-1",),
                instrument_versions=("BTCUSDT:v1",),
            )
            projected = provider_submission_observation_projection(observation)
            self.assertEqual(projected["terminal_state"], "SENT")
            self.assertEqual(projected["provider_id"], "BYBIT")
            self.assertEqual(projected["attempt_id"], "provider-observation-sent-a1")
            self.assertEqual(projected["response_encoding"], "utf-8-json")
            self.assertEqual(projected["payload"]["retCode"], 0)
            self.assertEqual(
                projected["payload"]["result"]["orderId"],
                "provider-1",
            )
            self.assertEqual(
                projected["submission_scope_hash"],
                submission_response_binding_projection(binding)[
                    "submission_scope_hash"
                ],
            )

    def test_restart_unknown_binding_cannot_be_promoted_to_provider_observation(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            request = {"side": "BUY", "symbol": "BTCUSDT"}
            request_sha = (
                "sha256:"
                + sha256(canonical_json(request).encode("utf-8")).hexdigest()
            )
            scope = {
                "endpoint": "/v5/order/create",
                "prepared_request_sha256": request_sha,
                "capability_snapshot_ids": ["cap-1"],
                "instrument_versions": ["BTCUSDT:v1"],
                "provider_environment": "TESTNET",
            }
            store = JournalStore(path)
            dispatcher = GuardedDispatcher(
                store,
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            result = dispatcher.dispatch(
                attempt_id="provider-observation-unknown-a1",
                intent_id="intent-provider-observation-unknown",
                intent_hash="sha256:" + "8" * 64,
                provider="BYBIT",
                request=request,
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"retCode":0,"result":{"orderId":"ambiguous"}}',
                        http_status=200,
                        requires_reconciliation=True,
                        ambiguity_reason="provider_response_ambiguous",
                    ),
                )[1],
                submission_scope=scope,
            )
            self.assertEqual(result.status, "UNKNOWN")

            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="provider-observation-unknown-a1",
            )
            projected = submission_response_binding_projection(binding)
            self.assertEqual(projected["terminal_state"], "UNKNOWN")
            self.assertEqual(
                projected["ambiguity_reason"],
                "provider_response_ambiguous",
            )
            self.assertEqual(
                projected["retry_disposition"],
                "RECONCILE_FIRST",
            )
            with self.assertRaisesRegex(
                ProviderCoreError,
                "requires definitive SENT response",
            ):
                observe_submission_json_response(
                    response_binding=binding,
                    provider_id="BYBIT",
                    endpoint="/v5/order/create",
                    prepared_request_sha256=request_sha,
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSDT:v1",),
                )

    def test_importable_binding_token_forgery_cannot_mint_provider_observation(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            request = {"side": "BUY", "symbol": "BTCUSDT"}
            request_sha = (
                "sha256:"
                + sha256(canonical_json(request).encode("utf-8")).hexdigest()
            )
            scope = {
                "endpoint": "/v5/order/create",
                "prepared_request_sha256": request_sha,
                "capability_snapshot_ids": ["cap-1"],
                "instrument_versions": ["BTCUSDT:v1"],
                "provider_environment": "TESTNET",
            }
            dispatcher = GuardedDispatcher(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                owner_token="owner",
            )
            dispatcher.dispatch(
                attempt_id="provider-observation-forgery-a1",
                intent_id="intent-provider-observation-forgery",
                intent_hash="sha256:" + "9" * 64,
                provider="BYBIT",
                request=request,
                now="2026-10-06T14:00:00Z",
                authority_check=lambda _hash, _now: (True, "allowed"),
                transport_send=lambda _cid, _request, guard: (
                    guard(),
                    ExactJsonTransportResponse(
                        b'{"retCode":0,"result":{"orderId":"provider-1"}}',
                        http_status=200,
                    ),
                )[1],
                submission_scope=scope,
            )
            canonical = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="provider-observation-forgery-a1",
            )
            projected = submission_response_binding_projection(canonical)
            forged = SubmissionResponseBinding(
                attempt_id=projected["attempt_id"],
                aggregate_id=projected["aggregate_id"],
                provider=projected["provider"],
                request_hash=projected["request_hash"],
                client_order_id=projected["client_order_id"],
                environment=projected["environment"],
                account_id=projected["account_id"],
                prepared_at=projected["prepared_at"],
                sent_at=projected["sent_at"],
                submission_scope=dict(projected["submission_scope"]),
                submission_scope_hash=projected["submission_scope_hash"],
                response_bytes=projected["response_bytes"],
                response_sha256=projected["response_sha256"],
                http_status=projected["http_status"],
                terminal_state=projected["terminal_state"],
                response_encoding=projected["response_encoding"],
                ambiguity_reason=projected["ambiguity_reason"],
                retry_disposition=projected["retry_disposition"],
                _factory_token=dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN,
            )
            with self.assertRaisesRegex(
                ValueError,
                "submission response binding authority is unavailable",
            ):
                observe_submission_json_response(
                    response_binding=forged,
                    provider_id="BYBIT",
                    endpoint="/v5/order/create",
                    prepared_request_sha256=request_sha,
                    capability_snapshot_ids=("cap-1",),
                    instrument_versions=("BTCUSDT:v1",),
                )

    def test_binding_authority_rejects_runtime_token_rebinding_and_recovers(self):
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            self._make_exact_response_attempt(path)
            binding = load_submission_response_binding(
                JournalStore(path),
                environment="SIMULATION",
                account_id="acct",
                attempt_id="binding-type-a1",
            )
            before = submission_response_binding_projection(binding)
            original = dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN
            try:
                dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN = object()
                with self.assertRaisesRegex(
                    ValueError,
                    "submission response binding authority is unavailable",
                ):
                    submission_response_binding_projection(binding)
            finally:
                dispatch_module._SUBMISSION_RESPONSE_BINDING_TOKEN = original
            after = submission_response_binding_projection(binding)
            self.assertEqual(
                after["response_sha256"],
                before["response_sha256"],
            )


if __name__ == "__main__":
    unittest.main()
