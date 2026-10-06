from hashlib import sha256
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
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


if __name__ == "__main__":
    unittest.main()
