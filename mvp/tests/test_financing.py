from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.financing import (
    FinancingConflict,
    FinancingError,
    FinancingEvent,
    FinancingRevisionBook,
    book_financing_delta,
)


BASE = datetime(2026, 9, 24, 18, tzinfo=timezone.utc)


def event(*, revision=1, kind="INDICATED", amount="1.25", available_at=None):
    return FinancingEvent.create(
        charge_id="borrow-BTC-20260924",
        revision=revision,
        kind=kind,
        effective_at=BASE,
        available_at=available_at or BASE,
        unit="BTC",
        amount=amount,
        source_account="BORROW_LIABILITY:BTC",
        evidence_ref=f"artifact:borrow-r{revision}",
    )


class FinancingTests(unittest.TestCase):
    def test_indicated_charge_is_not_economic(self):
        book = FinancingRevisionBook()
        update = book.record(event())
        self.assertEqual(update.economic_delta, Decimal("0"))
        self.assertEqual(update.current_final_charge, Decimal("0"))

    def test_final_charge_and_revision_book_only_delta(self):
        revisions = FinancingRevisionBook()
        revisions.record(event(revision=1, kind="INDICATED", amount="1.0"))
        first = revisions.record(
            event(
                revision=2,
                kind="FINAL",
                amount="1.2",
                available_at=BASE + timedelta(minutes=1),
            )
        )
        corrected = revisions.record(
            event(
                revision=3,
                kind="FINAL",
                amount="1.1",
                available_at=BASE + timedelta(minutes=2),
            )
        )
        self.assertEqual(first.economic_delta, Decimal("1.2"))
        self.assertEqual(corrected.economic_delta, Decimal("-0.1"))
        self.assertEqual(corrected.current_final_charge, Decimal("1.1"))

        economic = EconomicBook()
        economic.append(
            book_financing_delta(
                transaction_id="financing-1",
                cause_event_id="provider-charge-r2",
                unit="BTC",
                source_account="BORROW_LIABILITY:BTC",
                economic_delta=first.economic_delta,
            )
        )
        economic.append(
            book_financing_delta(
                transaction_id="financing-2",
                cause_event_id="provider-charge-r3",
                unit="BTC",
                source_account="BORROW_LIABILITY:BTC",
                economic_delta=corrected.economic_delta,
            )
        )
        self.assertEqual(
            economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            Decimal("1.1"),
        )
        self.assertEqual(
            economic.balance("BORROW_LIABILITY:BTC", "BTC"),
            Decimal("-1.1"),
        )

    def test_identical_retry_is_idempotent_and_conflicting_revision_fails(self):
        revisions = FinancingRevisionBook()
        final = event(revision=1, kind="FINAL")
        self.assertTrue(revisions.record(final).accepted)
        self.assertFalse(revisions.record(final).accepted)
        conflicting = FinancingEvent.create(
            charge_id=final.charge_id,
            revision=1,
            kind="FINAL",
            effective_at=BASE,
            available_at=BASE,
            unit="BTC",
            amount="9",
            source_account="BORROW_LIABILITY:BTC",
            evidence_ref="artifact:conflict",
        )
        with self.assertRaises(FinancingConflict):
            revisions.record(conflicting)

    def test_final_charge_cannot_be_backdated_or_downgraded_to_estimate(self):
        with self.assertRaises(FinancingError):
            event(
                revision=1,
                kind="FINAL",
                available_at=BASE - timedelta(seconds=1),
            )
        revisions = FinancingRevisionBook()
        revisions.record(event(revision=1, kind="FINAL"))
        with self.assertRaises(FinancingConflict):
            revisions.record(
                event(
                    revision=2,
                    kind="INDICATED",
                    available_at=BASE + timedelta(minutes=1),
                )
            )

    def test_float_money_is_rejected(self):
        with self.assertRaises(FinancingError):
            FinancingEvent.create(
                charge_id="x",
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount=0.1,
                source_account="CASH:USD",
                evidence_ref="artifact:x",
            )

    def test_zero_delta_is_not_booked(self):
        with self.assertRaises(FinancingError):
            book_financing_delta(
                transaction_id="zero",
                cause_event_id="zero",
                unit="USD",
                source_account="CASH:USD",
                economic_delta="0",
            )


if __name__ == "__main__":
    unittest.main()
