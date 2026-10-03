from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    EconomicBook,
    ScopedEconomicBook,
    book_external_cash_flow,
)
from mvp.autotrade_mvp.settlement import SettlementBook


def opening_cash(amount="1000"):
    return book_external_cash_flow(
        transaction_id="opening-1",
        cause_event_id="deposit-1",
        currency="USD",
        amount=Decimal(amount),
    )


class SettlementEconomicBookAuthorityTests(unittest.TestCase):
    def test_economic_book_subclass_cannot_manufacture_settled_opening_cash(self):
        opening = opening_cash()
        calls = []

        class HostileEconomicBook(EconomicBook):
            @property
            def transactions(self):
                calls.append("transactions")
                return (opening,)

            def cash(self, currency):
                calls.append(("cash", currency))
                return Decimal("1100")

        hostile = HostileEconomicBook()

        with self.assertRaises(TypeError):
            SettlementBook.from_economic_book(
                economic_book=hostile,
                obligations=(),
            )

        self.assertEqual(calls, [])

    def test_exact_economic_book_instance_method_shadows_cannot_inflate_capital(self):
        book = EconomicBook((opening_cash(),))
        calls = []
        book.cash = (
            lambda currency: calls.append(("cash", currency)) or Decimal("1100")
        )
        book.balance = (
            lambda account, currency: calls.append(("balance", account, currency))
            or Decimal("1100")
        )

        rebuilt = SettlementBook.from_economic_book(
            economic_book=book,
            obligations=(),
        )

        self.assertEqual(rebuilt.available_to_spend("USD"), Decimal("1000"))
        self.assertEqual(calls, [])

    def test_scoped_economic_book_subclass_is_rejected_before_dispatch(self):
        calls = []

        class HostileScopedEconomicBook(ScopedEconomicBook):
            @property
            def transactions(self):
                calls.append("transactions")
                return (opening_cash(),)

            def cash(self, currency):
                calls.append(("cash", currency))
                return Decimal("1100")

        hostile = HostileScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
            transactions=(opening_cash(),),
        )

        with self.assertRaises(TypeError):
            SettlementBook.from_economic_book(
                economic_book=hostile,
                obligations=(),
            )

        self.assertEqual(calls, [])

    def test_exact_scoped_book_and_inner_book_method_shadows_are_inert(self):
        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
            transactions=(opening_cash(),),
        )
        calls = []
        scoped.cash = (
            lambda currency: calls.append(("scoped_cash", currency))
            or Decimal("1100")
        )
        scoped._book.cash = (
            lambda currency: calls.append(("inner_cash", currency))
            or Decimal("1100")
        )
        scoped._book.balance = (
            lambda account, currency: calls.append(
                ("inner_balance", account, currency)
            )
            or Decimal("1100")
        )

        rebuilt = SettlementBook.from_economic_book(
            economic_book=scoped,
            obligations=(),
        )

        self.assertEqual(rebuilt.available_to_spend("USD"), Decimal("1000"))
        self.assertEqual(calls, [])

    def test_scoped_book_cannot_swap_in_a_hostile_inner_economic_book(self):
        calls = []

        class HostileEconomicBook(EconomicBook):
            @property
            def transactions(self):
                calls.append("transactions")
                return (opening_cash(),)

        scoped = ScopedEconomicBook(
            environment="PAPER",
            account_id="acct-1",
            transactions=(opening_cash(),),
        )
        scoped._book = HostileEconomicBook((opening_cash(),))

        with self.assertRaises(TypeError):
            SettlementBook.from_economic_book(
                economic_book=scoped,
                obligations=(),
            )

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
