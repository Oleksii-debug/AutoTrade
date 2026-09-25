from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.accounting import (
    book_equity_fill,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SETTLEMENT_EVIDENCE_MEDIA_TYPE,
    settlement_completion_evidence_metadata,
    settlement_completion_evidence_receipt,
    settlement_rule_evidence_metadata,
    settlement_rule_evidence_receipt,
)
from mvp.autotrade_mvp.persistence import JournalStore, canonical_json
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)
from research.autotrade_research.artifacts.store import ArtifactStore


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def artifact_store_for(store: JournalStore) -> ArtifactStore:
    return ArtifactStore(Path(store.path).parent / "settlement-evidence")


def raw_rule() -> SettlementRuleBinding:
    return SettlementRuleBinding(
        rule_id="equity-cash",
        rule_version="1",
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        instrument_version="ABC",
        settlement_currency="USD",
        effective_from=date(2026, 9, 1),
        effective_to=None,
        evidence_refs=("instrument:ABC", "rule:equity-cash:1"),
    )


def bind_rule(store: JournalStore, value=None) -> SettlementRuleBinding:
    value = raw_rule() if value is None else value
    receipt = settlement_rule_evidence_receipt(
        value,
        trade_date=date(2026, 9, 25),
        expected_settlement_date=date(2026, 9, 26),
    )
    raw = canonical_json(receipt).encode("utf-8")
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/settlement-rule/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store_for(store).publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=["provider-doc:test-settlement-rule"],
        metadata=settlement_rule_evidence_metadata(
            value,
            trade_date=date(2026, 9, 25),
            expected_settlement_date=date(2026, 9, 26),
        ),
    )
    return replace(
        value,
        evidence_refs=(
            *value.evidence_refs,
            f"artifact:{artifact_id}@{manifest['sha256']}",
        ),
    )


def sell_transaction():
    return book_equity_fill(
        transaction_id="sell-1",
        cause_event_id="provider-execution-sell-1",
        instrument="ABC",
        settlement_currency="USD",
        side="SELL",
        quantity="1",
        price="100",
        economic_effective_at="2026-09-25T09:00:00Z",
        economic_order_key="provider:PROVIDER-A:execution:sell-1",
        observed_at="2026-09-25T09:00:01Z",
    )


def obligation(store: JournalStore, transaction=None) -> SettlementObligation:
    transaction = sell_transaction() if transaction is None else transaction
    return equity_cash_obligation_from_transaction(
        transaction,
        obligation_id="settlement-sell-1",
        instrument="ABC",
        settlement_currency="USD",
        settlement_date=date(2026, 9, 26),
        rule_binding=bind_rule(store),
    )


def durable(store: JournalStore) -> DurableSettlementBook:
    return DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        evidence_artifact_store=artifact_store_for(store),
    )


def economics(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def raw_evidence(ref="provider-read:sha256:" + "b" * 64, *, minute=0) -> SettlementEvidence:
    return SettlementEvidence(
        obligation_id="settlement-sell-1",
        evidence_ref=ref,
        observed_at=datetime(2026, 9, 26, 15, minute, tzinfo=timezone.utc),
    )


def bind_evidence(
    store: JournalStore,
    obligation_value: SettlementObligation,
    value=None,
) -> SettlementEvidence:
    value = raw_evidence() if value is None else value
    receipt = settlement_completion_evidence_receipt(
        scope=SettlementAccountScope(
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        ),
        obligation=obligation_value,
        evidence=value,
    )
    raw = canonical_json(receipt).encode("utf-8")
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "https://evidence.autotrade.local/settlement-completion/"
            + canonical_json(receipt),
        )
    )
    manifest = artifact_store_for(store).publish_bytes(
        artifact_id=artifact_id,
        data=raw,
        media_type=SETTLEMENT_EVIDENCE_MEDIA_TYPE,
        rights={"storage": True, "export": False},
        source_refs=["provider-read:test-settlement"],
        metadata=settlement_completion_evidence_metadata(
            scope=SettlementAccountScope(
                provider_id=PROVIDER,
                account_id=ACCOUNT,
                environment=ENVIRONMENT,
            ),
            obligation=obligation_value,
            evidence=value,
        ),
    )
    return replace(
        value,
        evidence_ref=f"artifact:{artifact_id}@{manifest['sha256']}",
    )


class DurableSettlementBookTests(unittest.TestCase):
    def test_registration_restarts_and_exact_retry_is_noop(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            settlements = durable(store)
            item = obligation(store)

            self.assertTrue(
                settlements.register_obligations(
                    (item,),
                    command_id="register-1",
                    idempotency_key="register-1",
                    committed_at="2026-09-25T09:00:02Z",
                )
            )
            reopened = durable(JournalStore(path))
            self.assertEqual(reopened.obligations, (item,))
            self.assertFalse(
                reopened.register_obligations(
                    (item,),
                    command_id="register-retry",
                    idempotency_key="register-retry",
                    committed_at="2026-09-25T09:00:03Z",
                )
            )
            self.assertEqual(len(reopened.obligations), 1)

    def test_reused_obligation_identity_with_changed_economics_conflicts(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            settlements = durable(store)
            item = obligation(store)
            settlements.register_obligations(
                (item,),
                command_id="register-1",
                idempotency_key="register-1",
                committed_at="2026-09-25T09:00:02Z",
            )
            changed = SettlementObligation(
                obligation_id=item.obligation_id,
                cause_event_id=item.cause_event_id,
                currency=item.currency,
                amount="99",
                trade_date=item.trade_date,
                settlement_date=item.settlement_date,
                component_id=item.component_id,
                source_transaction_id=item.source_transaction_id,
                rule_binding=item.rule_binding,
            )
            with self.assertRaisesRegex(
                SettlementConflict,
                "different economic content",
            ):
                settlements.register_obligations(
                    (changed,),
                    command_id="changed",
                    idempotency_key="changed",
                    committed_at="2026-09-25T09:00:03Z",
                )

    def test_provider_settlement_releases_cash_once_across_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economic = economics(store)
            economic.append(
                book_external_cash_flow(
                    transaction_id="deposit-1",
                    cause_event_id="deposit-event-1",
                    currency="USD",
                    amount="1000",
                )
            )
            sold = sell_transaction()
            economic.append(sold)

            settlements = durable(store)
            settlements.register_obligations(
                (obligation(store, sold),),
                command_id="register-sell",
                idempotency_key="register-sell",
                committed_at="2026-09-25T09:00:02Z",
            )
            before = settlements.project(economic)
            self.assertEqual(economic.cash("USD"), Decimal("1100"))
            self.assertEqual(before.snapshot("USD").settled_cash, Decimal("1000"))
            self.assertEqual(
                before.snapshot("USD").unsettled_receivable,
                Decimal("100"),
            )

            settlement_obligation = obligation(store, sold)
            settlement_evidence = bind_evidence(
                store,
                settlement_obligation,
            )
            self.assertTrue(
                settlements.apply_settlement(
                    settlement_evidence,
                    as_of=date(2026, 9, 26),
                    command_id="settle-sell",
                    idempotency_key="settle-sell",
                    committed_at="2026-09-26T15:00:01Z",
                )
            )
            after = settlements.project(economic)
            self.assertEqual(after.snapshot("USD").settled_cash, Decimal("1100"))
            self.assertEqual(after.available_to_spend("USD"), Decimal("1100"))

            reopened_store = JournalStore(path)
            reopened_settlements = durable(reopened_store)
            reopened_economic = economics(reopened_store)
            restarted = reopened_settlements.project(reopened_economic)
            self.assertEqual(restarted.available_to_spend("USD"), Decimal("1100"))
            self.assertFalse(
                reopened_settlements.apply_settlement(
                    settlement_evidence,
                    as_of=date(2026, 9, 27),
                    command_id="settle-retry",
                    idempotency_key="settle-retry",
                    committed_at="2026-09-27T09:00:00Z",
                )
            )
            self.assertEqual(
                reopened_settlements.project(reopened_economic).available_to_spend("USD"),
                Decimal("1100"),
            )

    def test_changed_evidence_after_settlement_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            settlements = durable(store)
            settlements.register_obligations(
                (obligation(store),),
                command_id="register",
                idempotency_key="register",
                committed_at="2026-09-25T09:00:02Z",
            )
            target = obligation(store)
            settlements.apply_settlement(
                bind_evidence(store, target),
                as_of=date(2026, 9, 26),
                command_id="settle",
                idempotency_key="settle",
                committed_at="2026-09-26T15:00:01Z",
            )
            with self.assertRaisesRegex(
                SettlementConflict,
                "different settlement evidence",
            ):
                settlements.apply_settlement(
                    bind_evidence(
                        store,
                        target,
                        raw_evidence(minute=1),
                    ),
                    as_of=date(2026, 9, 26),
                    command_id="settle-changed",
                    idempotency_key="settle-changed",
                    committed_at="2026-09-26T15:00:02Z",
                )

    def test_fabricated_provider_read_hash_cannot_release_receivable(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economic = economics(store)
            economic.append(
                book_external_cash_flow(
                    transaction_id="deposit-fabricated",
                    cause_event_id="deposit-event-fabricated",
                    currency="USD",
                    amount="1000",
                )
            )
            sold = sell_transaction()
            economic.append(sold)
            target = obligation(store, sold)
            settlements = durable(store)
            settlements.register_obligations(
                (target,),
                command_id="register-fabricated",
                idempotency_key="register-fabricated",
                committed_at="2026-09-25T09:00:02Z",
            )
            before = settlements.project(economic)
            self.assertEqual(before.available_to_spend("USD"), Decimal("1000"))

            with self.assertRaisesRegex(
                SettlementConflict,
                "bind artifact UUID",
            ):
                settlements.apply_settlement(
                    raw_evidence(),
                    as_of=date(2026, 9, 26),
                    command_id="fake-provider-read",
                    idempotency_key="fake-provider-read",
                    committed_at="2026-09-26T15:00:01Z",
                )

            after = settlements.project(economic)
            self.assertEqual(after.snapshot("USD").unsettled_receivable, Decimal("100"))
            self.assertEqual(after.available_to_spend("USD"), Decimal("1000"))

    def test_missing_well_formed_rule_artifact_cannot_register_obligation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            fake_ref = (
                "artifact:22222222-2222-2222-2222-222222222222@sha256:"
                + "a" * 64
            )
            unverified_rule = replace(
                raw_rule(),
                evidence_refs=("instrument:ABC", fake_ref),
            )
            target = equity_cash_obligation_from_transaction(
                sell_transaction(),
                obligation_id="unverified-rule-obligation",
                instrument="ABC",
                settlement_currency="USD",
                settlement_date=date(2026, 9, 26),
                rule_binding=unverified_rule,
            )
            with self.assertRaisesRegex(
                SettlementConflict,
                "artifact verification failed",
            ):
                durable(store).register_obligations(
                    (target,),
                    command_id="unverified-rule",
                    idempotency_key="unverified-rule",
                    committed_at="2026-09-25T09:00:02Z",
                )

    def test_failed_registration_commit_does_not_mutate_restart_state(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            settlements = durable(store)
            original_commit = store.commit_command

            def fail(**kwargs):
                raise RuntimeError("injected settlement commit failure")

            store.commit_command = fail
            try:
                with self.assertRaisesRegex(RuntimeError, "commit failure"):
                    settlements.register_obligations(
                        (obligation(store),),
                        command_id="register",
                        idempotency_key="register",
                        committed_at="2026-09-25T09:00:02Z",
                    )
            finally:
                store.commit_command = original_commit

            self.assertEqual(durable(JournalStore(path)).obligations, ())


if __name__ == "__main__":
    unittest.main()
