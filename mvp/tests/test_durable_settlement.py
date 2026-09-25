from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict, book_equity_fill
from mvp.autotrade_mvp.durable_reservations import DurableReservationBook
from mvp.autotrade_mvp.durable_settlement import (
    DurableSettlementBook,
    SettlementRuleEvidence,
    commit_fill_with_settlement_obligations,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.settlement import (
    SettlementConflict,
    SettlementEvidence,
    equity_cash_obligation,
)


PROVIDER = "PROVIDER-A"
ACCOUNT = "acct-1"
ENVIRONMENT = "PAPER"
INSTRUMENT_VERSION = "11111111-1111-1111-1111-111111111111@1"
RULE_REF = (
    "artifact:22222222-2222-2222-2222-222222222222@sha256:" + "a" * 64
)
SETTLEMENT_REF = "provider-read:sha256:" + "b" * 64


def rule(*, trade_day=24, settle_day=26):
    return SettlementRuleEvidence(
        provider_id=PROVIDER,
        account_id=ACCOUNT,
        environment=ENVIRONMENT,
        instrument_version=INSTRUMENT_VERSION,
        trade_date=date(2026, 9, trade_day),
        settlement_date=date(2026, 9, settle_day),
        evidence_ref=RULE_REF,
    )


def settlement_evidence(obligation_id):
    return SettlementEvidence(
        obligation_id=obligation_id,
        evidence_ref=SETTLEMENT_REF,
        observed_at=datetime(2026, 9, 26, 10, tzinfo=timezone.utc),
    )


class DurableSettlementCapitalTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "journal.sqlite3"
        self.store = JournalStore(self.path)

    def economics(self, store=None):
        return DurableProviderEconomicBook(
            store or self.store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
        )

    def reservations(self, store=None, *, resource="CASH:USD", amount="100"):
        book = DurableReservationBook(
            store or self.store,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )
        book.reserve(
            command_id="reserve-command",
            idempotency_key="reserve-idem",
            reservation_id="reservation-1",
            intent_id="intent-1",
            requirements={resource: amount},
            available={resource: "1000"},
        )
        return book

    def settlement(self, store=None, *, opening="1000"):
        return DurableSettlementBook(
            store or self.store,
            provider_id=PROVIDER,
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            opening_settled_cash={"USD": opening},
        )

    @staticmethod
    def transaction(*, side="BUY", price="100", cause="fill-1"):
        return book_equity_fill(
            transaction_id="economic-" + cause,
            cause_event_id=cause,
            instrument="ABC",
            settlement_currency="USD",
            side=side,
            quantity="1",
            price=price,
            economic_effective_at="2026-09-24T09:00:00Z",
            economic_order_key="provider:PROVIDER-A:execution:" + cause,
            observed_at="2026-09-24T09:00:01Z",
        )

    @staticmethod
    def obligation(*, side="BUY", price="100", cause="fill-1", obligation_id="cash-1"):
        return equity_cash_obligation(
            obligation_id=obligation_id,
            cause_event_id=cause,
            settlement_currency="USD",
            side=side,
            quantity="1",
            price=price,
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 26),
        )

    def commit(self, economics, reservations, settlement, *, side="BUY", price="100"):
        return commit_fill_with_settlement_obligations(
            economics,
            reservations,
            settlement,
            command_id="fill-command-1",
            idempotency_key="fill-idem-1",
            reservation_id="reservation-1",
            usage={"CASH:USD": "100"},
            transactions=(self.transaction(side=side, price=price),),
            settlement_obligations=(
                (self.obligation(side=side, price=price), rule()),
            ),
            committed_at="2026-09-24T09:00:02Z",
        )

    def test_buy_payable_is_atomic_with_fill_and_blocks_capital_reuse_after_restart(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()

        self.assertTrue(self.commit(economics, reservations, settlement))
        self.assertEqual(economics.position("ABC"), Decimal("1"))
        self.assertEqual(economics.cash("USD"), Decimal("-100"))
        self.assertEqual(
            reservations.get("reservation-1").consumed["CASH:USD"],
            Decimal("100"),
        )
        projection = settlement.snapshot("USD")
        self.assertEqual(projection.settled_cash, Decimal("1000"))
        self.assertEqual(projection.unsettled_payable, Decimal("100"))
        self.assertEqual(settlement.available_to_spend("USD"), Decimal("900"))

        reopened = JournalStore(self.path)
        restarted_settlement = self.settlement(reopened)
        restarted_economics = self.economics(reopened)
        restarted_reservations = DurableReservationBook(
            reopened,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )
        self.assertEqual(restarted_settlement.available_to_spend("USD"), Decimal("900"))
        self.assertEqual(restarted_economics.position("ABC"), Decimal("1"))
        self.assertEqual(
            restarted_reservations.get("reservation-1").remaining["CASH:USD"],
            Decimal("0"),
        )

    def test_sale_receivable_is_not_available_until_immutable_settlement_evidence(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()

        self.assertTrue(self.commit(economics, reservations, settlement, side="SELL"))
        before = settlement.capital_snapshot("USD")
        self.assertEqual(before.unsettled_receivable, Decimal("100"))
        self.assertEqual(before.available_cash, Decimal("1000"))
        self.assertTrue(before.settlement_state_digest.startswith("sha256:"))

        self.assertTrue(
            settlement.settle(
                command_id="settle-sale-1",
                idempotency_key="settle-sale-idem-1",
                obligation_id="cash-1",
                as_of=date(2026, 9, 26),
                evidence=settlement_evidence("cash-1"),
                committed_at="2026-09-26T10:00:01Z",
            )
        )
        after = settlement.capital_snapshot("USD")
        self.assertEqual(after.unsettled_receivable, Decimal("0"))
        self.assertEqual(after.settled_cash, Decimal("1100"))
        self.assertEqual(after.available_cash, Decimal("1100"))

        restarted = self.settlement(JournalStore(self.path))
        self.assertFalse(
            restarted.settle(
                command_id="settle-sale-1",
                idempotency_key="settle-sale-idem-1",
                obligation_id="cash-1",
                as_of=date(2026, 9, 26),
                evidence=settlement_evidence("cash-1"),
                committed_at="2026-09-26T10:00:01Z",
            )
        )
        self.assertEqual(restarted.available_to_spend("USD"), Decimal("1100"))

    def test_ack_loss_after_three_projection_commit_retries_without_duplicate(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        original = self.store.commit_command
        injected = False

        def lose_ack(**kwargs):
            nonlocal injected
            result = original(**kwargs)
            if (
                kwargs.get("actor") == "atomic-fill-settlement-integration"
                and result[1]
                and not injected
            ):
                injected = True
                raise RuntimeError("injected acknowledgement loss")
            return result

        self.store.commit_command = lose_ack
        try:
            with self.assertRaisesRegex(RuntimeError, "acknowledgement loss"):
                self.commit(economics, reservations, settlement)
        finally:
            self.store.commit_command = original

        reopened = JournalStore(self.path)
        restarted_economics = self.economics(reopened)
        restarted_reservations = DurableReservationBook(
            reopened,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )
        restarted_settlement = self.settlement(reopened)
        self.assertFalse(
            self.commit(
                restarted_economics,
                restarted_reservations,
                restarted_settlement,
            )
        )
        self.assertEqual(len(restarted_economics.transactions), 1)
        self.assertEqual(len(restarted_settlement.obligations), 1)
        self.assertEqual(
            restarted_reservations.get("reservation-1").consumed["CASH:USD"],
            Decimal("100"),
        )
        self.assertEqual(restarted_settlement.available_to_spend("USD"), Decimal("900"))

    def test_failure_before_shared_commit_leaves_all_three_projections_unmutated(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        original = self.store.commit_command

        def fail(**kwargs):
            if kwargs.get("actor") == "atomic-fill-settlement-integration":
                raise RuntimeError("injected pre-commit failure")
            return original(**kwargs)

        self.store.commit_command = fail
        try:
            with self.assertRaisesRegex(RuntimeError, "pre-commit"):
                self.commit(economics, reservations, settlement)
        finally:
            self.store.commit_command = original

        reopened = JournalStore(self.path)
        restarted_economics = self.economics(reopened)
        restarted_reservations = DurableReservationBook(
            reopened,
            environment=ENVIRONMENT,
            account_id=ACCOUNT,
        )
        restarted_settlement = self.settlement(reopened)
        self.assertEqual(restarted_economics.transactions, ())
        self.assertEqual(restarted_settlement.obligations, ())
        self.assertEqual(
            restarted_reservations.get("reservation-1").consumed["CASH:USD"],
            Decimal("0"),
        )

    def test_preexisting_economics_without_settlement_fails_closed_as_partial_state(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        self.assertTrue(economics.append(self.transaction()))

        with self.assertRaisesRegex(AccountingConflict, "partially committed"):
            self.commit(economics, reservations, settlement)

        self.assertEqual(len(economics.transactions), 1)
        self.assertEqual(settlement.obligations, ())
        self.assertEqual(
            reservations.get("reservation-1").consumed["CASH:USD"],
            Decimal("0"),
        )

    def test_obligations_must_exactly_cover_trade_date_cash_by_cause_and_currency(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        wrong = self.obligation(price="99")

        with self.assertRaisesRegex(AccountingConflict, "exactly cover fill cash"):
            commit_fill_with_settlement_obligations(
                economics,
                reservations,
                settlement,
                command_id="fill-command-1",
                idempotency_key="fill-idem-1",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                transactions=(self.transaction(price="100"),),
                settlement_obligations=((wrong, rule()),),
                committed_at="2026-09-24T09:00:02Z",
            )

        self.assertEqual(economics.transactions, ())
        self.assertEqual(settlement.obligations, ())

    def test_rule_scope_and_dates_fail_before_any_mutation(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        bad_rule = SettlementRuleEvidence(
            provider_id="OTHER",
            account_id=ACCOUNT,
            environment=ENVIRONMENT,
            instrument_version=INSTRUMENT_VERSION,
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 26),
            evidence_ref=RULE_REF,
        )

        with self.assertRaisesRegex(SettlementConflict, "rule scope"):
            commit_fill_with_settlement_obligations(
                economics,
                reservations,
                settlement,
                command_id="fill-command-1",
                idempotency_key="fill-idem-1",
                reservation_id="reservation-1",
                usage={"CASH:USD": "100"},
                transactions=(self.transaction(),),
                settlement_obligations=((self.obligation(), bad_rule),),
                committed_at="2026-09-24T09:00:02Z",
            )
        self.assertEqual(economics.transactions, ())
        self.assertEqual(settlement.obligations, ())

    def test_changed_settlement_evidence_under_same_idempotency_conflicts(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement()
        self.assertTrue(self.commit(economics, reservations, settlement, side="SELL"))
        self.assertTrue(
            settlement.settle(
                command_id="settle-sale-1",
                idempotency_key="settle-sale-idem-1",
                obligation_id="cash-1",
                as_of=date(2026, 9, 26),
                evidence=settlement_evidence("cash-1"),
                committed_at="2026-09-26T10:00:01Z",
            )
        )
        changed = SettlementEvidence(
            obligation_id="cash-1",
            evidence_ref="provider-read:sha256:" + "c" * 64,
            observed_at=datetime(2026, 9, 26, 10, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(SettlementConflict, "different settlement request"):
            settlement.settle(
                command_id="settle-sale-1",
                idempotency_key="settle-sale-idem-1",
                obligation_id="cash-1",
                as_of=date(2026, 9, 26),
                evidence=changed,
                committed_at="2026-09-26T10:00:01Z",
            )

    def test_opening_cash_is_bound_into_durable_history_and_cannot_change_on_restart(self):
        reservations = self.reservations()
        economics = self.economics()
        settlement = self.settlement(opening="1000")
        self.assertTrue(self.commit(economics, reservations, settlement))

        with self.assertRaisesRegex(SettlementConflict, "opening cash binding"):
            self.settlement(JournalStore(self.path), opening="999")


if __name__ == "__main__":
    unittest.main()
