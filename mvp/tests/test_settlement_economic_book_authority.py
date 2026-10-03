from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook, book_external_cash_flow
from mvp.autotrade_mvp.settlement import SettlementBook


class SettlementEconomicBookAuthorityTests(unittest.TestCase):
    def test_economic_book_subclass_cannot_manufacture_settled_opening_cash(self):
        opening = book_external_cash_flow(
            transaction_id="opening-1",
            cause_event_id="deposit-1",
            currency="USD",
            amount=Decimal("1000"),
        )
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


if __name__ == "__main__":
    unittest.main()
