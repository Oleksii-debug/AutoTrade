"""Current-parent response binding must preserve canonical submission scope."""

from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    _envelope,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json


class ResponseScopeResidualCurrentTests(unittest.TestCase):
    ENVIRONMENT = "SIMULATION"
    ACCOUNT_ID = "acct"
    ATTEMPT_ID = "attempt-scope-current"
    CLIENT_ORDER_ID = "client-scope-current"

    def _store(
        self,
        directory,
        *,
        prepared_overrides=None,
        sending_envelope_environment=None,
    ):
        store = JournalStore(directory + "/journal.sqlite3")
        aggregate_id = submission_attempt_aggregate_id(
            environment=self.ENVIRONMENT,
            account_id=self.ACCOUNT_ID,
            attempt_id=self.ATTEMPT_ID,
        )
        scope = {}
        raw = b'{"ok":true}'
        prepared = {
            "attempt_id": self.ATTEMPT_ID,
            "intent_id": "intent-scope-current",
            "intent_hash": "intent-hash-scope-current",
            "provider": "BYBIT",
            "request_hash": "sha256:" + "1" * 64,
            "client_order_id": self.CLIENT_ORDER_ID,
            "environment": self.ENVIRONMENT,
            "account_id": self.ACCOUNT_ID,
            "owner_token": "owner",
            "owner_epoch": 1,
            "prepared_at": "2026-10-06T00:41:00Z",
            "submission_scope": scope,
            "submission_scope_hash": (
                "sha256:" + sha256(canonical_json(scope).encode("utf-8")).hexdigest()
            ),
        }
        prepared.update(prepared_overrides or {})
        records = (
            ("SubmissionPrepared", prepared, self.ENVIRONMENT),
            (
                "SubmissionSending",
                {
                    "client_order_id": self.CLIENT_ORDER_ID,
                    "owner_token": "owner",
                    "owner_epoch": 1,
                    "reason": "final_send_barrier_passed",
                },
                sending_envelope_environment or self.ENVIRONMENT,
            ),
            (
                "SubmissionSent",
                {
                    "client_order_id": self.CLIENT_ORDER_ID,
                    "response_text": raw.decode("utf-8"),
                    "response_sha256": "sha256:" + sha256(raw).hexdigest(),
                    "response_encoding": "utf-8-json",
                    "http_status": 200,
                },
                self.ENVIRONMENT,
            ),
        )
        for version, (event_type, payload, event_environment) in enumerate(
            records,
            start=1,
        ):
            JournalStore.append_event(
                store,
                _envelope(
                    scope_key="response-scope-current",
                    aggregate_id=aggregate_id,
                    environment=event_environment,
                    attempt_id=self.ATTEMPT_ID,
                    event_type=event_type,
                    version=version,
                    payload=payload,
                    now=f"2026-10-06T00:41:0{version}Z",
                    owner_epoch=1,
                ),
            )
        return store

    def _load(self, store, *, environment=None, account_id=None):
        return load_submission_response_binding(
            store,
            environment=environment or self.ENVIRONMENT,
            account_id=account_id or self.ACCOUNT_ID,
            attempt_id=self.ATTEMPT_ID,
        )

    def test_canonical_sequence_and_normalized_selector_load(self):
        with TemporaryDirectory() as directory:
            store = self._store(directory)
            binding = self._load(
                store,
                environment=" simulation ",
                account_id=" acct ",
            )
            self.assertEqual(binding.provider, "BYBIT")
            self.assertEqual(binding.environment, self.ENVIRONMENT)
            self.assertEqual(binding.account_id, self.ACCOUNT_ID)
            self.assertEqual(binding.client_order_id, self.CLIENT_ORDER_ID)

    def test_provider_must_be_exact_text_not_string_coerced(self):
        with TemporaryDirectory() as directory:
            store = self._store(
                directory,
                prepared_overrides={"provider": 7},
            )
            with self.assertRaisesRegex(
                ValueError,
                "prepared submission identity",
            ):
                self._load(store)

    def test_prepared_environment_and_account_must_be_canonical_scope(self):
        cases = (
            {"environment": "simulation"},
            {"account_id": " acct "},
        )
        for mutation in cases:
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                store = self._store(
                    directory,
                    prepared_overrides=mutation,
                )
                with self.assertRaisesRegex(
                    ValueError,
                    "prepared submission identity",
                ):
                    self._load(store)

    def test_event_envelope_environment_cannot_drift_from_selected_scope(self):
        with TemporaryDirectory() as directory:
            store = self._store(
                directory,
                sending_envelope_environment="PAPER",
            )
            with self.assertRaisesRegex(
                ValueError,
                "aggregate identity mismatch",
            ):
                self._load(store)


if __name__ == "__main__":
    unittest.main()
