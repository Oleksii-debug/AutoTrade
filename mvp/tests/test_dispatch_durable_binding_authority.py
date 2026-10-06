from hashlib import sha256
import json
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    ExactJsonTransportResponse,
    GuardedDispatcher,
    load_submission_response_binding,
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

    def test_restart_rejects_coerced_identity_fields_in_durable_prepared_event(self):
        for field, bad_value in (
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



if __name__ == "__main__":
    unittest.main()
