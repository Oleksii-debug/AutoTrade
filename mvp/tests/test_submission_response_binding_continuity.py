"""Durable submission-response bindings require one continuous journal identity."""

from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import (
    _envelope,
    load_submission_response_binding,
    submission_attempt_aggregate_id,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json


class SubmissionResponseBindingContinuityTests(unittest.TestCase):
    ENVIRONMENT = "SIMULATION"
    ACCOUNT_ID = "acct"
    ATTEMPT_ID = "attempt-continuity"
    CLIENT_ORDER_ID = "client-continuity"

    def _build_store(
        self,
        directory,
        *,
        prepared_overrides=None,
        sending_overrides=None,
        terminal_overrides=None,
        sending_envelope_environment=None,
    ):
        store = JournalStore(directory + "/journal.sqlite3")
        aggregate_id = submission_attempt_aggregate_id(
            environment=self.ENVIRONMENT,
            account_id=self.ACCOUNT_ID,
            attempt_id=self.ATTEMPT_ID,
        )
        raw = b'{"ok":true}'
        scope = {}
        prepared = {
            "attempt_id": self.ATTEMPT_ID,
            "intent_id": "intent-continuity",
            "intent_hash": "intent-hash-continuity",
            "provider": "BYBIT",
            "request_hash": "sha256:" + "1" * 64,
            "client_order_id": self.CLIENT_ORDER_ID,
            "environment": self.ENVIRONMENT,
            "account_id": self.ACCOUNT_ID,
            "owner_token": "owner",
            "owner_epoch": 1,
            "prepared_at": "2026-10-06T00:30:00Z",
            "submission_scope": scope,
            "submission_scope_hash": (
                "sha256:" + sha256(canonical_json(scope).encode("utf-8")).hexdigest()
            ),
        }
        sending = {
            "client_order_id": self.CLIENT_ORDER_ID,
            "owner_token": "owner",
            "owner_epoch": 1,
            "reason": "final_send_barrier_passed",
        }
        terminal = {
            "client_order_id": self.CLIENT_ORDER_ID,
            "response_text": raw.decode("utf-8"),
            "response_sha256": "sha256:" + sha256(raw).hexdigest(),
            "response_encoding": "utf-8-json",
            "http_status": 200,
        }
        prepared.update(prepared_overrides or {})
        sending.update(sending_overrides or {})
        terminal.update(terminal_overrides or {})

        records = (
            ("SubmissionPrepared", prepared, self.ENVIRONMENT),
            (
                "SubmissionSending",
                sending,
                sending_envelope_environment or self.ENVIRONMENT,
            ),
            ("SubmissionSent", terminal, self.ENVIRONMENT),
        )
        for version, (event_type, payload, envelope_environment) in enumerate(
            records,
            start=1,
        ):
            envelope = _envelope(
                scope_key="continuity-test-scope",
                aggregate_id=aggregate_id,
                environment=envelope_environment,
                attempt_id=self.ATTEMPT_ID,
                event_type=event_type,
                version=version,
                payload=payload,
                now=f"2026-10-06T00:30:0{version}Z",
                owner_epoch=1,
            )
            JournalStore.append_event(store, envelope)
        return store

    def _load(self, store):
        return load_submission_response_binding(
            store,
            environment=self.ENVIRONMENT,
            account_id=self.ACCOUNT_ID,
            attempt_id=self.ATTEMPT_ID,
        )

    def test_valid_hash_consistent_sequence_loads_one_exact_identity(self):
        with TemporaryDirectory() as directory:
            binding = self._load(self._build_store(directory))
            self.assertEqual(binding.attempt_id, self.ATTEMPT_ID)
            self.assertEqual(binding.environment, self.ENVIRONMENT)
            self.assertEqual(binding.account_id, self.ACCOUNT_ID)
            self.assertEqual(binding.client_order_id, self.CLIENT_ORDER_ID)
            self.assertEqual(binding.provider, "BYBIT")
            self.assertEqual(binding.terminal_state, "SENT")

    def test_prepared_scope_identity_must_match_aggregate_selector(self):
        cases = (
            ("attempt", {"attempt_id": "different-attempt"}),
            ("environment", {"environment": "PAPER"}),
            ("account", {"account_id": "different-account"}),
        )
        for label, mutation in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                store = self._build_store(directory, prepared_overrides=mutation)
                with self.assertRaisesRegex(
                    ValueError,
                    "SubmissionPrepared identity",
                ):
                    self._load(store)

    def test_non_text_prepared_identity_is_not_string_coerced_into_authority(self):
        cases = (
            ("provider", {"provider": 7}),
            ("client", {"client_order_id": 7}),
            ("request_hash", {"request_hash": 7}),
        )
        for label, mutation in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                store = self._build_store(directory, prepared_overrides=mutation)
                with self.assertRaisesRegex(
                    ValueError,
                    "SubmissionPrepared identity",
                ):
                    self._load(store)

    def test_client_order_id_must_be_continuous_through_send_and_terminal(self):
        cases = (
            ("sending", {"sending_overrides": {"client_order_id": "other"}}),
            ("terminal", {"terminal_overrides": {"client_order_id": "other"}}),
        )
        for label, kwargs in cases:
            with self.subTest(label=label), TemporaryDirectory() as directory:
                store = self._build_store(directory, **kwargs)
                with self.assertRaisesRegex(
                    ValueError,
                    "client-order identity changed",
                ):
                    self._load(store)

    def test_event_envelope_environment_must_match_selected_scope(self):
        with TemporaryDirectory() as directory:
            store = self._build_store(
                directory,
                sending_envelope_environment="PAPER",
            )
            with self.assertRaisesRegex(ValueError, "event chronology"):
                self._load(store)


if __name__ == "__main__":
    unittest.main()
