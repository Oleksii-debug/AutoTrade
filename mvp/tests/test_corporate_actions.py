from datetime import date
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.corporate_actions import (
    CorporateActionBook,
    CorporateEvent,
    EquityState,
    accrue_borrow_financing,
    cover_recalled_short,
    establish_short,
    record_recall,
    record_unsettled_purchase,
    settle_cash,
)


def state(**overrides):
    values = dict(
        symbol="AAA",
        quantity="10",
        total_basis="1000",
        settled_cash="1000",
        unsettled_cash="0",
        currency="USD",
    )
    values.update(overrides)
    return EquityState.create(**values)


class CorporateSettlementTests(unittest.TestCase):
    def test_split_changes_quantity_not_total_basis_or_pnl(self):
        book = CorporateActionBook(state())
        event = CorporateEvent.create(
            event_id="split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )
        result = book.apply(event)
        self.assertEqual(result.after.quantity, Decimal("20"))
        self.assertEqual(result.after.total_basis, Decimal("1000"))
        self.assertEqual(result.after.unit_basis, Decimal("50"))
        self.assertEqual(result.economic_pnl, Decimal("0"))
        self.assertEqual(book.apply(event), result)

    def test_split_scales_short_borrow_and_recall_obligations(self):
        book = CorporateActionBook(
            state(
                quantity="-10",
                total_basis="1000",
                borrowed_quantity="10",
                recalled_quantity="4",
            )
        )
        event = CorporateEvent.create(
            event_id="short-split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )
        result = book.apply(event)
        self.assertEqual(result.after.quantity, Decimal("-20"))
        self.assertEqual(result.after.borrowed_quantity, Decimal("20"))
        self.assertEqual(result.after.recalled_quantity, Decimal("8"))
        self.assertEqual(result.after.total_basis, Decimal("1000"))

    def test_cash_dividend_stays_unsettled_until_explicit_settlement(self):
        book = CorporateActionBook(state())
        event = CorporateEvent.create(
            event_id="div-1",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1.50"},
        )
        result = book.apply(event)
        self.assertEqual(result.after.unsettled_cash, Decimal("15.00"))
        self.assertEqual(result.after.settled_cash, Decimal("1000"))
        settled = settle_cash(result.after, "15")
        self.assertEqual(settled.unsettled_cash, Decimal("0.00"))
        self.assertEqual(settled.settled_cash, Decimal("1015"))

    def test_short_dividend_payable_can_settle_as_negative_cash(self):
        short_state = state(quantity="-10", total_basis="1000")
        book = CorporateActionBook(short_state)
        event = CorporateEvent.create(
            event_id="short-div-1",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1.50"},
        )
        result = book.apply(event)
        self.assertEqual(result.after.unsettled_cash, Decimal("-15.00"))
        self.assertEqual(result.economic_pnl, Decimal("-15.00"))

        settled = settle_cash(result.after, "-15")
        self.assertEqual(settled.unsettled_cash, Decimal("0.00"))
        self.assertEqual(settled.settled_cash, Decimal("985"))

    def test_settlement_cannot_flip_or_overrun_unsettled_balance(self):
        receivable = state(unsettled_cash="15")
        payable = state(unsettled_cash="-15")
        with self.assertRaisesRegex(ValueError, "same sign"):
            settle_cash(receivable, "-1")
        with self.assertRaisesRegex(ValueError, "same sign"):
            settle_cash(payable, "1")
        with self.assertRaisesRegex(ValueError, "more cash"):
            settle_cash(receivable, "16")
        with self.assertRaisesRegex(ValueError, "more cash"):
            settle_cash(payable, "-16")

    def test_cash_merger_extinguishes_position_once(self):
        book = CorporateActionBook(state())
        event = CorporateEvent.create(
            event_id="merger-1",
            kind="MERGER_CASH",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"cash_per_share": "110"},
        )
        result = book.apply(event)
        self.assertEqual(result.after.quantity, Decimal("0"))
        self.assertEqual(result.after.total_basis, Decimal("0"))
        self.assertEqual(result.after.unsettled_cash, Decimal("1100"))
        self.assertEqual(result.economic_pnl, Decimal("100"))

    def test_delist_without_evidenced_consideration_fails_closed(self):
        book = CorporateActionBook(state())
        event = CorporateEvent.create(
            event_id="delist-1",
            kind="DELIST",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={},
        )
        with self.assertRaises(ValueError):
            book.apply(event)
        self.assertEqual(book.state.quantity, Decimal("10"))

    def test_unsettled_purchase_cannot_spend_unfunded_cash(self):
        with self.assertRaises(ValueError):
            record_unsettled_purchase(state(settled_cash="50"), quantity="1", price="100")

    def test_short_proceeds_are_unsettled_and_borrow_is_explicit(self):
        short = establish_short(
            state(quantity="0", total_basis="0"),
            quantity="2",
            sale_price="100",
        )
        self.assertEqual(short.quantity, Decimal("-2"))
        self.assertEqual(short.borrowed_quantity, Decimal("2"))
        self.assertEqual(short.unsettled_cash, Decimal("200"))

    def test_borrow_financing_and_recall_are_not_silent(self):
        short = establish_short(
            state(quantity="0", total_basis="0"),
            quantity="2",
            sale_price="100",
        )
        financed = accrue_borrow_financing(
            short,
            daily_rate="0.001",
            marked_value="220",
            days=2,
        )
        self.assertEqual(financed.accrued_financing, Decimal("0.440"))
        self.assertEqual(financed.unsettled_cash, Decimal("199.560"))
        recalled = record_recall(financed, "1")
        self.assertEqual(recalled.recalled_quantity, Decimal("1"))

    def test_recall_cover_requires_settled_funding(self):
        short = establish_short(
            state(quantity="0", total_basis="0", settled_cash="50"),
            quantity="1",
            sale_price="100",
        )
        recalled = record_recall(short, "1")
        with self.assertRaises(ValueError):
            cover_recalled_short(recalled, quantity="1", buy_price="90")

    def test_corporate_event_rejects_duplicate_normalized_payload_keys(self):
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            CorporateEvent.create(
                event_id="ambiguous-split",
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"numerator": 2, " numerator ": 3, "denominator": 1},
            )

    def test_direct_equity_state_construction_cannot_bypass_exactness_or_borrow_invariants(self):
        with self.assertRaises(TypeError):
            EquityState(
                symbol="ABC",
                quantity=1.0,
                total_basis=Decimal("100"),
                settled_cash=Decimal("1000"),
                unsettled_cash=Decimal("0"),
                currency="USD",
            )
        with self.assertRaisesRegex(ValueError, "cannot exceed"):
            EquityState(
                symbol="ABC",
                quantity=Decimal("-1"),
                total_basis=Decimal("100"),
                settled_cash=Decimal("1000"),
                unsettled_cash=Decimal("0"),
                currency="USD",
                borrowed_quantity=Decimal("1"),
                recalled_quantity=Decimal("2"),
            )

    def test_direct_corporate_event_construction_cannot_bypass_exact_payload(self):
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            CorporateEvent(
                event_id="direct-float",
                kind="cash_dividend",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"per_share": 0.1},
            )

    def test_corporate_event_rejects_binary_float_economics(self):
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            CorporateEvent.create(
                event_id="split-float",
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"numerator": 2.0, "denominator": 1},
            )
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            CorporateEvent.create(
                event_id="div-float",
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"per_share": 0.1},
            )

    def test_duplicate_event_identity_with_changed_content_is_rejected(self):
        book = CorporateActionBook(state())
        first = CorporateEvent.create(
            event_id="split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )
        second = CorporateEvent.create(
            event_id="split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r2",
            payload={"numerator": 3, "denominator": 1},
        )
        book.apply(first)
        with self.assertRaises(ValueError):
            book.apply(second)


if __name__ == "__main__":
    unittest.main()
