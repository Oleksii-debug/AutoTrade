from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.dispatch import GuardedDispatcher
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.recovery_takeover import (
    DurableTakeoverError,
    _latest_effectful_submission_sequence,
)


class TakeoverSubmissionScopeValidationTests(unittest.TestCase):
    def test_malformed_effectful_prepared_scope_cannot_be_skipped_by_takeover_scan(self):
        """Malformed durable send state is a blocker, not an unrelated account."""

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            dispatcher = GuardedDispatcher(
                store,
                environment="PAPER",
                account_id="paper-1",
                owner_token="host-a",
                owner_epoch=1,
            )
            attempt_id = "malformed-scope-effectful-send"
            now = "2026-10-04T00:32:00Z"

            # The low-level journal writer can preserve historical/corrupt rows
            # whose Prepared payload no longer satisfies the current contract.
            # A takeover conservation scan must reject such an aggregate rather
            # than coerce the malformed value with str(...) and silently skip a
            # durable SubmissionSending that may have crossed the wire boundary.
            dispatcher._append(
                attempt_id=attempt_id,
                event_type="SubmissionPrepared",
                version=1,
                payload={
                    "attempt_id": attempt_id,
                    "intent_id": "intent-malformed-scope",
                    "intent_hash": "intent-hash-malformed-scope",
                    "provider": "SIMULATED",
                    "request_hash": "sha256:" + "0" * 64,
                    "client_order_id": "client-malformed-scope",
                    "environment": 123,
                    "account_id": "paper-1",
                    "owner_token": "host-a",
                    "owner_epoch": 1,
                    "prepared_at": now,
                },
                now=now,
            )
            dispatcher._append(
                attempt_id=attempt_id,
                event_type="SubmissionSending",
                version=2,
                payload={
                    "client_order_id": "client-malformed-scope",
                    "owner_token": "host-a",
                    "owner_epoch": 1,
                },
                now=now,
            )

            with self.assertRaisesRegex(
                DurableTakeoverError,
                "scope|environment|account|SubmissionPrepared",
            ):
                _latest_effectful_submission_sequence(
                    store,
                    environment="PAPER",
                    account_id="paper-1",
                )


if __name__ == "__main__":
    unittest.main()
