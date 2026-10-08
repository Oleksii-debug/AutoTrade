from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.reconciliation_journal import (
    _reconciliation_aggregate_id,
    load_latest_reconciliation_checkpoint,
    unresolved_provider_activity_ids_from_checkpoint,
)


class ReconciliationDirectNegativeAuthorityTests(unittest.TestCase):
    def _append_checkpoint(
        self,
        store: JournalStore,
        *,
        reconciliation_id: str,
        provider_id: str,
        environment: str,
        outcome: str,
    ) -> str:
        account_id = "paper-account"
        provider_environment = (
            "TESTNET" if environment == "PAPER" else "MAINNET"
        ) if provider_id == "BYBIT" else environment
        aggregate_id = _reconciliation_aggregate_id(
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id=account_id,
            environment=environment,
            provider_environment=provider_environment,
        )
        payload = {
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
            "provider_environment": provider_environment,
            "submission_resolutions": [
                {
                    "attempt_id": "attempt-" + reconciliation_id,
                    "intent_id": "intent-" + reconciliation_id,
                    "client_order_id": "client-" + reconciliation_id,
                    "outcome": outcome,
                }
            ],
        }
        event_id = str(uuid5(NAMESPACE_URL, "plan6-negative-fixture:" + reconciliation_id))
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
        return event_id

    def _load(
        self,
        store: JournalStore,
        *,
        reconciliation_id: str,
        provider_id: str,
        environment: str,
    ):
        return load_latest_reconciliation_checkpoint(
            store,
            reconciliation_id=reconciliation_id,
            provider_id=provider_id,
            account_id="paper-account",
            environment=environment,
            provider_environment=(
                "TESTNET" if environment == "PAPER" else "MAINNET"
            ) if provider_id == "BYBIT" else None,
        )

    def test_direct_loader_rejects_legacy_real_provider_proven_absent(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment):
                with TemporaryDirectory() as directory:
                    store = JournalStore(Path(directory) / "journal.sqlite3")
                    reconciliation_id = "legacy-direct-negative-" + environment.lower()
                    self._append_checkpoint(
                        store,
                        reconciliation_id=reconciliation_id,
                        provider_id="BYBIT",
                        environment=environment,
                        outcome="PROVEN_ABSENT",
                    )

                    with self.assertRaisesRegex(
                        ValueError,
                        "PROVEN_ABSENT|coverage authority",
                    ):
                        self._load(
                            store,
                            reconciliation_id=reconciliation_id,
                            provider_id="BYBIT",
                            environment=environment,
                        )

    def test_activity_helper_rejects_legacy_real_provider_proven_absent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reconciliation_id = "legacy-activity-negative"
            event_id = self._append_checkpoint(
                store,
                reconciliation_id=reconciliation_id,
                provider_id="BYBIT",
                environment="PAPER",
                outcome="PROVEN_ABSENT",
            )
            checkpoint = store.get_event(event_id)

            with self.assertRaisesRegex(
                ValueError,
                "PROVEN_ABSENT|coverage authority",
            ):
                unresolved_provider_activity_ids_from_checkpoint(
                    checkpoint,
                    provider_id="BYBIT",
                    account_id="paper-account",
                    environment="PAPER",
                    provider_environment="TESTNET",
                )

    def test_direct_loader_keeps_simulated_paper_exception(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reconciliation_id = "simulated-direct-negative"
            event_id = self._append_checkpoint(
                store,
                reconciliation_id=reconciliation_id,
                provider_id="SIMULATED",
                environment="PAPER",
                outcome="PROVEN_ABSENT",
            )

            loaded = self._load(
                store,
                reconciliation_id=reconciliation_id,
                provider_id="SIMULATED",
                environment="PAPER",
            )
            self.assertEqual(loaded["event_id"], event_id)
            self.assertEqual(
                loaded["payload"]["submission_resolutions"][0]["outcome"],
                "PROVEN_ABSENT",
            )

    def test_direct_loader_keeps_non_negative_real_provider_checkpoint(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            reconciliation_id = "real-provider-unknown"
            event_id = self._append_checkpoint(
                store,
                reconciliation_id=reconciliation_id,
                provider_id="BYBIT",
                environment="PAPER",
                outcome="UNKNOWN",
            )

            loaded = self._load(
                store,
                reconciliation_id=reconciliation_id,
                provider_id="BYBIT",
                environment="PAPER",
            )
            self.assertEqual(loaded["event_id"], event_id)
            self.assertEqual(
                loaded["payload"]["submission_resolutions"][0]["outcome"],
                "UNKNOWN",
            )


if __name__ == "__main__":
    unittest.main()
