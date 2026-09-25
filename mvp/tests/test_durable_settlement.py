from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    book_equity_fill,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.durable_settlement import DurableSettlementBook
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.settlement import (
    SettlementAccountScope,
    SettlementConflict,
    SettlementEvidence,
    SettlementObligation,
    SettlementRuleBinding,
    equity_cash_obligation_from_transaction,
)


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"


def rule() -> SettlementRuleBinding:
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


def obligation(transaction=None) -> SettlementObligation:
    transaction = sell_transaction() if transaction is None else transaction
    return equity_cash_obligation_from_transaction(
        transaction,
        obligation_id="settlement-sell-1",
        instrument="ABC",
        settlement_currency="USD",
        settlement_date=date(2026, 9, 26),
        rule_binding=rule(),
    )


def durable(store: JournalStore) -> DurableSettlementBook:
    return DurableSettlementBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def economics(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
    )


def evidence(ref="provider:settlement:sell-1") -> SettlementEvidence:
    return SettlementEvidence(
        obligation_id="settlement-sell-1",
        evidence_ref=ref,
        observed_at=datetime(2026, 9, 26, 15, tzinfo=timezone.utc),
    )


class DurableSettlementBookTests(unittest.TestCase):
    def test_registration_restarts_and_exact_retry_is_noop(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            settlements = durable(store)
            item = obligation()

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
            item = obligation()
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
                (obligation(sold),),
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

            settlement_evidence = evidence()
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
                (obligation(),),
                command_id="register",
                idempotency_key="register",
                committed_at="2026-09-25T09:00:02Z",
            )
            settlements.apply_settlement(
                evidence(),
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
                    evidence("provider:settlement:changed"),
                    as_of=date(2026, 9, 26),
                    command_id="settle-changed",
                    idempotency_key="settle-changed",
                    committed_at="2026-09-26T15:00:02Z",
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
                        (obligation(),),
                        command_id="register",
                        idempotency_key="register",
                        committed_at="2026-09-25T09:00:02Z",
                    )
            finally:
                store.commit_command = original_commit

            self.assertEqual(durable(JournalStore(path)).obligations, ())


if __name__ == "__main__":
    unittest.main()
