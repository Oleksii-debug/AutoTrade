from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from autotrade_mvp.durable_financing import DurableFinancingBook
from autotrade_mvp.financing import FinancingConflict, FinancingError
from autotrade_mvp.persistence import JournalStore
from autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


BASE = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)


class FakeArtifactStore:
    def __init__(self):
        self.items: dict[str, bytes] = {}

    def put(
        self,
        artifact_id: str,
        *,
        revision: int,
        kind: str = "FINAL",
        amount: str = "1.20",
        available_at: datetime | None = None,
        provider_id: str = "BYBIT",
        account_id: str = "acct-1",
        environment: str = "PAPER",
        unit: str = "BTC",
        source_account: str = "BORROW_LIABILITY:BTC",
    ) -> None:
        payload = {
            "schema_version": "1.0.0",
            "provider_id": provider_id,
            "account_id": account_id,
            "environment": environment,
            "charge_id": "borrow-btc-2026-09-28",
            "revision": revision,
            "kind": kind,
            "effective_at": BASE.isoformat().replace("+00:00", "Z"),
            "available_at": (available_at or BASE)
            .isoformat()
            .replace("+00:00", "Z"),
            "unit": unit,
            "amount": amount,
            "source_account": source_account,
        }
        self.items[artifact_id] = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def read_authenticated_snapshot(self, artifact_id: str):
        data = self.items[artifact_id]
        return (
            {
                "artifact_id": artifact_id,
                "sha256": "sha256:" + sha256(data).hexdigest(),
            },
            data,
        )


class RejectingArtifactStore:
    def read_authenticated_snapshot(self, artifact_id: str):
        raise ValueError("integrity failure")


class DurableFinancingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JournalStore(Path(self.temp.name) / "journal.sqlite3")
        self.economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        self.financing = DurableFinancingBook(
            self.store,
            self.economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        self.artifacts = FakeArtifactStore()

    def tearDown(self):
        self.temp.cleanup()

    def test_final_revision_and_economics_commit_atomically_and_restart(self):
        self.artifacts.put("00000000-0000-0000-0000-000000000001", revision=1)
        result = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id="00000000-0000-0000-0000-000000000001",
            committed_at=BASE.isoformat(),
        )
        self.assertTrue(result.inserted)
        self.assertEqual(str(result.update.economic_delta), "1.20")
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.update.economic_delta,
        )

        restarted_economic = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        restarted = DurableFinancingBook(
            self.store,
            restarted_economic,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
        )
        latest = restarted.latest("borrow-btc-2026-09-28")
        self.assertIsNotNone(latest)
        self.assertEqual(latest.revision, 1)
        self.assertEqual(str(latest.amount), "1.20")
        self.assertEqual(
            restarted_economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.update.economic_delta,
        )

    def test_later_final_revision_posts_only_durable_delta(self):
        first = "00000000-0000-0000-0000-000000000011"
        second = "00000000-0000-0000-0000-000000000012"
        self.artifacts.put(first, revision=1, amount="1.20")
        self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=first, committed_at=BASE.isoformat()
        )
        self.artifacts.put(
            second,
            revision=2,
            amount="1.10",
            available_at=BASE + timedelta(minutes=1),
        )
        result = self.financing.record_authenticated_artifact(
            self.artifacts,
            artifact_id=second,
            committed_at=(BASE + timedelta(minutes=1)).isoformat(),
        )
        self.assertEqual(result.update.economic_delta, result.event.amount - 1.20)
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            result.event.amount,
        )

    def test_exact_response_loss_retry_is_idempotent(self):
        artifact = "00000000-0000-0000-0000-000000000021"
        self.artifacts.put(artifact, revision=1)
        self.assertTrue(
            self.financing.record_authenticated_artifact(
                self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
            ).inserted
        )
        retried = self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.assertFalse(retried.inserted)
        self.assertEqual(retried.update.economic_delta, 0)
        self.assertEqual(
            len(
                self.store.load_events(
                    "provider_financing_charge",
                    self.financing._aggregate_id("borrow-btc-2026-09-28"),
                )
            ),
            1,
        )

    def test_conflicting_same_revision_fails_closed(self):
        artifact = "00000000-0000-0000-0000-000000000031"
        self.artifacts.put(artifact, revision=1, amount="1.20")
        self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.artifacts.put(artifact, revision=1, amount="9.00")
        with self.assertRaises(FinancingConflict):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            str(self.economic.balance("FINANCING_EXPENSE:BTC", "BTC")),
            "1.20",
        )

    def test_indicated_revision_is_durable_but_non_economic(self):
        artifact = "00000000-0000-0000-0000-000000000041"
        self.artifacts.put(artifact, revision=1, kind="INDICATED", amount="3.25")
        result = self.financing.record_authenticated_artifact(
            self.artifacts, artifact_id=artifact, committed_at=BASE.isoformat()
        )
        self.assertTrue(result.inserted)
        self.assertIsNone(result.economic_transaction)
        self.assertEqual(result.update.economic_delta, 0)
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_forged_or_unreadable_artifact_cannot_grant_economics(self):
        with self.assertRaises(FinancingError):
            self.financing.record_authenticated_artifact(
                RejectingArtifactStore(),
                artifact_id="00000000-0000-0000-0000-000000000051",
                committed_at=BASE.isoformat(),
            )
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_command_failure_exposes_neither_revision_nor_economics(self):
        artifact = "00000000-0000-0000-0000-000000000061"
        self.artifacts.put(artifact, revision=1)
        original = self.store.commit_command

        def fail_commit(**kwargs):
            raise RuntimeError("injected SQL/commit failure")

        self.store.commit_command = fail_commit
        try:
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.financing.record_authenticated_artifact(
                    self.artifacts,
                    artifact_id=artifact,
                    committed_at=BASE.isoformat(),
                )
        finally:
            self.store.commit_command = original

        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))
        self.assertEqual(
            self.economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            0,
        )

    def test_authenticated_scope_mismatch_fails_before_journal_mutation(self):
        artifact = "00000000-0000-0000-0000-000000000071"
        self.artifacts.put(artifact, revision=1, account_id="other-account")
        with self.assertRaisesRegex(FinancingError, "account"):
            self.financing.record_authenticated_artifact(
                self.artifacts,
                artifact_id=artifact,
                committed_at=BASE.isoformat(),
            )
        self.assertIsNone(self.financing.latest("borrow-btc-2026-09-28"))


if __name__ == "__main__":
    unittest.main()
