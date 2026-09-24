from datetime import date
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.reservations import ReservationBook
from mvp.autotrade_mvp.settlement import (
    SettlementBook,
    SettlementConflict,
    SettlementObligation,
    equity_cash_obligation,
)


class SettlementBookTests(unittest.TestCase):
    def test_obligation_constructor_canonicalizes_financial_fields(self):
        obligation = SettlementObligation(
            " obligation-1 ",
            " fill-1 ",
            " USD ",
            "10.50",
            date(2026, 9, 24),
            date(2026, 9, 25),
        )
        self.assertEqual(obligation.obligation_id, "obligation-1")
        self.assertEqual(obligation.cause_event_id, "fill-1")
        self.assertEqual(obligation.currency, "USD")
        self.assertIsInstance(obligation.amount, Decimal)
        self.assertEqual(obligation.amount, Decimal("10.50"))

        book = SettlementBook(obligations=(obligation,))
        self.assertEqual(
            book.snapshot("USD").unsettled_receivable,
            Decimal("10.50"),
        )

    def test_obligation_rejects_non_date_temporal_fields(self):
        with self.assertRaises(TypeError):
            SettlementObligation(
                "x",
                "event",
                "USD",
                Decimal("1"),
                "2026-09-24",
                date(2026, 9, 25),
            )

    def test_unsettled_sale_proceeds_are_not_spendable(self):
        book = SettlementBook(settled_cash={"USD": "100"})
        sale = equity_cash_obligation(
            obligation_id="sale-1",
            cause_event_id="fill-1",
            settlement_currency="USD",
            side="SELL",
            quantity="2",
            price="50",
            fee="1",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 26),
        )
        book.add(sale)
        snapshot = book.snapshot("USD")
        self.assertEqual(snapshot.settled_cash, Decimal("100"))
        self.assertEqual(snapshot.unsettled_receivable, Decimal("99"))
        self.assertEqual(snapshot.unsettled_payable, Decimal("0"))
        self.assertEqual(snapshot.economic_cash, Decimal("199"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("100"))

    def test_buy_creates_unsettled_payable_and_settles_exactly_once(self):
        book = SettlementBook(settled_cash={"USD": "1000"})
        buy = equity_cash_obligation(
            obligation_id="buy-1",
            cause_event_id="fill-1",
            settlement_currency="USD",
            side="BUY",
            quantity="2",
            price="100",
            fee="1",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        self.assertTrue(book.add(buy))
        before = book.snapshot("USD")
        self.assertEqual(before.unsettled_payable, Decimal("201"))
        self.assertEqual(before.economic_cash, Decimal("799"))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        with self.assertRaisesRegex(SettlementConflict, "before contractual"):
            book.settle("buy-1", as_of=date(2026, 9, 24))
        self.assertTrue(book.settle("buy-1", as_of=date(2026, 9, 25)))
        self.assertFalse(book.settle("buy-1", as_of=date(2026, 9, 26)))
        after = book.snapshot("USD")
        self.assertEqual(after.settled_cash, Decimal("799"))
        self.assertEqual(after.unsettled_payable, Decimal("0"))

    def test_settle_due_orders_deterministically(self):
        book = SettlementBook(settled_cash={"USD": "0"})
        for obligation in (
            SettlementObligation("b", "e2", "USD", Decimal("3"), date(2026, 9, 20), date(2026, 9, 22)),
            SettlementObligation("a", "e1", "USD", Decimal("2"), date(2026, 9, 20), date(2026, 9, 22)),
            SettlementObligation("c", "e3", "USD", Decimal("5"), date(2026, 9, 20), date(2026, 9, 23)),
        ):
            book.add(obligation)
        self.assertEqual(
            book.settle_due(as_of=date(2026, 9, 22)),
            ("a", "b"),
        )
        self.assertEqual(book.snapshot("USD").settled_cash, Decimal("5"))
        self.assertEqual(book.snapshot("USD").unsettled_receivable, Decimal("5"))

    def test_duplicate_identity_is_idempotent_but_changed_content_conflicts(self):
        original = SettlementObligation(
            "x", "event", "EUR", Decimal("10"), date(2026, 9, 20), date(2026, 9, 22)
        )
        book = SettlementBook()
        self.assertTrue(book.add(original))
        self.assertFalse(book.add(original))
        changed = SettlementObligation(
            "x", "event", "EUR", Decimal("11"), date(2026, 9, 20), date(2026, 9, 22)
        )
        with self.assertRaises(SettlementConflict):
            book.add(changed)

    def test_reserve_reduces_only_settled_spendable_cash(self):
        book = SettlementBook(settled_cash={"USD": "100"})
        book.add(SettlementObligation(
            "sale", "event", "USD", Decimal("500"), date(2026, 9, 20), date(2026, 9, 23)
        ))
        self.assertEqual(book.available_to_spend("USD", reserve="30"), Decimal("70"))
        self.assertEqual(book.available_to_spend("USD", reserve="130"), Decimal("0"))

    def test_multi_currency_obligations_do_not_cross_net(self):
        book = SettlementBook(settled_cash={"USD": "100", "EUR": "50"})
        book.add(SettlementObligation(
            "usd-r", "u1", "USD", Decimal("20"), date(2026, 9, 20), date(2026, 9, 22)
        ))
        book.add(SettlementObligation(
            "eur-p", "e1", "EUR", Decimal("-10"), date(2026, 9, 20), date(2026, 9, 22)
        ))
        self.assertEqual(book.snapshot("USD").economic_cash, Decimal("120"))
        self.assertEqual(book.snapshot("EUR").economic_cash, Decimal("40"))

    def test_invalid_dates_zero_amount_float_and_negative_reserve_fail_closed(self):
        with self.assertRaises(ValueError):
            SettlementObligation(
                "x", "e", "USD", Decimal("1"), date(2026, 9, 23), date(2026, 9, 22)
            )
        with self.assertRaises(ValueError):
            SettlementObligation(
                "x", "e", "USD", Decimal("0"), date(2026, 9, 22), date(2026, 9, 22)
            )
        with self.assertRaises(TypeError):
            equity_cash_obligation(
                obligation_id="x",
                cause_event_id="e",
                settlement_currency="USD",
                side="BUY",
                quantity=1,
                price=100.1,
                trade_date=date(2026, 9, 22),
                settlement_date=date(2026, 9, 22),
            )
        with self.assertRaises(ValueError):
            SettlementBook(settled_cash={"USD": "10"}).available_to_spend("USD", reserve="-1")

    def test_fee_is_applied_in_settlement_currency_without_hidden_conversion(self):
        buy = equity_cash_obligation(
            obligation_id="buy",
            cause_event_id="fill",
            settlement_currency="EUR",
            side="BUY",
            quantity="1.5",
            price="20",
            fee="0.25",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        sell = equity_cash_obligation(
            obligation_id="sell",
            cause_event_id="fill2",
            settlement_currency="EUR",
            side="SELL",
            quantity="1.5",
            price="20",
            fee="0.25",
            trade_date=date(2026, 9, 24),
            settlement_date=date(2026, 9, 25),
        )
        self.assertEqual(buy.amount, Decimal("-30.25"))
        self.assertEqual(sell.amount, Decimal("29.75"))


    def test_payable_and_explicit_reserve_are_both_conservative(self):
        book = SettlementBook(settled_cash={"USD": "1000"})
        book.add(SettlementObligation(
            "buy", "fill-buy", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        book.add(SettlementObligation(
            "sale", "fill-sale", "USD", Decimal("500"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        self.assertEqual(book.available_to_spend("USD", reserve="99"), Decimal("700"))

    def test_payables_do_not_cross_net_between_currencies(self):
        book = SettlementBook(settled_cash={"USD": "1000", "EUR": "100"})
        book.add(SettlementObligation(
            "usd-buy", "fill-usd", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        book.add(SettlementObligation(
            "eur-sale", "fill-eur", "EUR", Decimal("500"),
            date(2026, 9, 24), date(2026, 9, 26),
        ))
        self.assertEqual(book.available_to_spend("USD"), Decimal("799"))
        self.assertEqual(book.available_to_spend("EUR"), Decimal("100"))

    def test_restart_reconstruction_preserves_settlement_projection(self):
        obligation = SettlementObligation(
            "buy", "fill", "USD", Decimal("-201"),
            date(2026, 9, 24), date(2026, 9, 25),
        )
        book = SettlementBook(settled_cash={"USD": "1000"}, obligations=(obligation,))
        self.assertTrue(book.settle("buy", as_of=date(2026, 9, 25)))
        snapshot = book.snapshot("USD")

        restored = SettlementBook(
            settled_cash={"USD": snapshot.settled_cash},
            obligations=(obligation,),
            settled_obligation_ids=("buy",),
        )
        self.assertTrue(restored.is_settled("buy"))
        self.assertEqual(restored.snapshot("USD"), snapshot)
        self.assertEqual(restored.available_to_spend("USD"), Decimal("799"))
        self.assertFalse(restored.settle("buy", as_of=date(2026, 9, 26)))

    def test_restart_rejects_unknown_settled_identity(self):
        with self.assertRaisesRegex(SettlementConflict, "unknown obligation"):
            SettlementBook(
                settled_cash={"USD": "100"},
                settled_obligation_ids=("missing",),
            )


    def test_reservation_to_settlement_handoff_never_double_locks_same_fill(self):
        reservations = ReservationBook()
        settlement = SettlementBook(settled_cash={"USD": "1000"})
        reservations.reserve(
            reservation_id="reservation-buy-1",
            intent_id="intent-buy-1",
            requirements={"CASH:USD": "201"},
            available={"CASH:USD": "1000"},
        )

        before_fill_reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(before_fill_reserve, Decimal("201"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=before_fill_reserve),
            Decimal("799"),
        )
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("0"))

        reservations.consume("reservation-buy-1", {"CASH:USD": "201"})
        filled = reservations.mark_terminal(
            "reservation-buy-1",
            outcome="FILLED",
            resolution_evidence="provider-execution-fill-1",
        )
        self.assertEqual(filled.state, "FILLED")
        self.assertEqual(reservations.total_reserved("CASH:USD"), Decimal("0"))

        settlement.add(
            equity_cash_obligation(
                obligation_id="settlement-buy-1",
                cause_event_id="provider-execution-fill-1",
                settlement_currency="USD",
                side="BUY",
                quantity="2",
                price="100",
                fee="1",
                trade_date=date(2026, 9, 24),
                settlement_date=date(2026, 9, 26),
            )
        )
        after_fill_reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(after_fill_reserve, Decimal("0"))
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("201"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=after_fill_reserve),
            Decimal("799"),
        )
        self.assertNotEqual(
            settlement.available_to_spend("USD", reserve=after_fill_reserve),
            Decimal("598"),
        )

    def test_working_or_unknown_reservation_does_not_create_settlement_payable(self):
        reservations = ReservationBook()
        settlement = SettlementBook(settled_cash={"USD": "1000"})
        reservations.reserve(
            reservation_id="reservation-unknown",
            intent_id="intent-unknown",
            requirements={"CASH:USD": "201"},
            available={"CASH:USD": "1000"},
        )
        reservations.mark_unknown("reservation-unknown")

        reserve = reservations.total_reserved("CASH:USD")
        self.assertEqual(reserve, Decimal("201"))
        self.assertEqual(settlement.snapshot("USD").unsettled_payable, Decimal("0"))
        self.assertEqual(
            settlement.available_to_spend("USD", reserve=reserve),
            Decimal("799"),
        )


if __name__ == "__main__":
    unittest.main()
