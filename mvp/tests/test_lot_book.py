from decimal import Decimal, ROUND_CEILING, localcontext
import unittest

from mvp.autotrade_mvp.lot_book import FifoLotBook


class HostileDecimal(Decimal):
    def is_finite(self):
        return True

    def __str__(self):
        return "1"


class FifoLotBookTests(unittest.TestCase):
    def test_buy_builds_exact_basis(self):
        book = FifoLotBook()
        state = book.buy("2", "100", fee="2")
        self.assertEqual(state.position, Decimal("2"))
        self.assertEqual(state.open_basis, Decimal("202"))
        self.assertEqual(state.realized_pnl, Decimal("0"))

    def test_partial_fifo_sale_realizes_only_closed_basis(self):
        book = FifoLotBook()
        book.buy("2", "100")
        book.buy("1", "120")
        state = book.sell("1.5", "130")
        self.assertEqual(state.position, Decimal("1.5"))
        self.assertEqual(state.open_basis, Decimal("170"))
        self.assertEqual(state.realized_pnl, Decimal("45"))
        self.assertEqual([(lot.quantity, lot.unit_cost) for lot in book.lots], [
            (Decimal("0.5"), Decimal("100")),
            (Decimal("1"), Decimal("120")),
        ])

    def test_fees_affect_basis_and_realized_pnl(self):
        book = FifoLotBook()
        book.buy("1", "100", fee="1")
        state = book.sell("1", "110", fee="2")
        self.assertEqual(state.position, Decimal("0"))
        self.assertEqual(state.open_basis, Decimal("0"))
        self.assertEqual(state.realized_pnl, Decimal("7"))

    def test_mark_to_market_does_not_mutate_realized_pnl(self):
        book = FifoLotBook()
        book.buy("2", "100")
        self.assertEqual(book.mark_to_market("110"), Decimal("20"))
        state = book.snapshot()
        self.assertEqual(state.realized_pnl, Decimal("0"))
        self.assertEqual(state.open_basis, Decimal("200"))

    def test_cannot_create_unsupported_short_position(self):
        book = FifoLotBook()
        book.buy("1", "100")
        with self.assertRaisesRegex(ValueError, "available long position"):
            book.sell("2", "100")

    def test_invalid_values_fail_closed(self):
        book = FifoLotBook()
        for args in [
            ("0", "100", "0"),
            ("1", "0", "0"),
            ("1", "100", "-1"),
        ]:
            with self.assertRaises(ValueError):
                book.buy(args[0], args[1], fee=args[2])
        with self.assertRaises(ValueError):
            book.mark_to_market("NaN")

    def test_binary_float_boolean_and_decimal_subclass_inputs_are_rejected(self):
        book = FifoLotBook()
        with self.assertRaises(TypeError):
            book.buy(1.0, "100")
        with self.assertRaises(TypeError):
            book.buy(True, "100")
        with self.assertRaises(TypeError):
            book.buy(HostileDecimal("2"), "100")

    def test_nonterminating_unit_basis_fails_before_book_mutation(self):
        book = FifoLotBook()
        with self.assertRaisesRegex(ValueError, "unit cost is non-terminating"):
            book.buy("3", "100", fee="1")
        self.assertEqual(book.lots, ())
        self.assertEqual(book.snapshot().position, Decimal("0"))
        self.assertEqual(book.snapshot().open_basis, Decimal("0"))
        self.assertEqual(book.snapshot().realized_pnl, Decimal("0"))

    def test_authoritative_arithmetic_is_independent_of_ambient_decimal_context(self):
        book = FifoLotBook()
        with localcontext() as context:
            context.prec = 2
            context.rounding = ROUND_CEILING
            state = book.buy("1.25", "80.04", fee="0.05")
            self.assertEqual(state.open_basis, Decimal("100.10"))
            state = book.sell("0.75", "83.99", fee="0.02")
            self.assertEqual(state.position, Decimal("0.50"))
            self.assertEqual(state.open_basis, Decimal("40.04"))
            self.assertEqual(state.realized_pnl, Decimal("2.9125"))
            self.assertEqual(book.mark_to_market("84.00"), Decimal("1.96"))

    def test_oversized_decimal_ingress_fails_closed(self):
        book = FifoLotBook()
        with self.assertRaisesRegex(ValueError, "bounded exact decimal"):
            book.buy("1" * 10000, "1")

    def test_full_round_trip_clears_basis(self):
        book = FifoLotBook()
        book.buy("1.25", "80")
        state = book.sell("1.25", "84")
        self.assertEqual(state.position, Decimal("0"))
        self.assertEqual(state.open_basis, Decimal("0"))
        self.assertEqual(state.realized_pnl, Decimal("5"))


if __name__ == "__main__":
    unittest.main()
