from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation_journal import (
    _reconciliation_aggregate_id,
    load_latest_reconciliation_checkpoint,
)


class ReconciliationDirectNegativeAuthorityTests(unittest.TestCase):
    def test_direct_loader_rejects_legacy_real_provider_proven_absent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reconciliation_id = "legacy-direct-negative"
            provider_id = "BYBIT"
            account_id = "paper-account"
            environment = "PAPER"
            aggregate_id = _reconciliation_aggregate_id(
                reconciliation_id=reconciliation_id,
                provider_id=provider_id,
                account_id=account_id,
                environment=environment,
            )
            payload = {
                "provider_id": provider_id,
                "account_id": account_id,
                "environment": environment,
                "submission_resolutions": [
                    {
                        "attempt_id": "attempt-legacy-negative",
                        "intent_id": "intent-legacy-negative",
                        "client_order_id": "client-legacy-negative",
                        "outcome": "PROVEN_ABSENT",
                    }
                ],
            }
            event_id = "legacy-direct-negative-event"
            store.append_event(
                {
                    "event_id": event_id,
                    "event_type": "AccountReconciled",
                    "schema_version": "1.0.0",
                    "aggregate_type": "account_reconciliation",
                    "aggregate_id": aggregate_id,
                    "aggregate_version": "1",
                    "host_id": "legacy-host",
                    "owner_epoch": "1",
                    "environment": environment,
                    "occurred_at": "2026-10-05T08:10:00Z",
                    "observed_at": "2026-10-05T08:10:00Z",
                    "committed_at": "2026-10-05T08:10:00Z",
                    "correlation_id": event_id,
                    "causation_id": None,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                    "evidence_refs": [],
                }
            )

            with self.assertRaisesRegex(
                ValueError,
                "PROVEN_ABSENT|coverage authority",
            ):
                load_latest_reconciliation_checkpoint(
                    store,
                    reconciliation_id=reconciliation_id,
                    provider_id=provider_id,
                    account_id=account_id,
                    environment=environment,
                )


if __name__ == "__main__":
    unittest.main()
