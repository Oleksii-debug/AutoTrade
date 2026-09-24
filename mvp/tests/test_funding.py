from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.funding import (
    FundingConflict,
    FundingEvent,
    FundingRevisionBook,
    book_funding_delta,
    canonical_funding_cash_flow,
)


def moment(hour: int):
    return datetime(2026, 9, 24, hour, tzinfo=timezone.utc)


def event(*, revision=1, kind="FINAL", rate="0.0001", available_hour=8):
    return FundingEvent(
        funding_id="funding:BTC-PERP:20260924T08",
        revision=revision,
        kind=kind,
        effective_at=moment(8),
        available_at=moment(available_hour),
        settlement_currency="USD",
        signed_notional=Decimal("1000"),
        rate=Decimal(rate),
        sign_convention="POSITIVE_LONG_PAYS",
        evidence_ref=f"artifact:funding-r{revision}",
    )


class FundingTests(unittest.TestCase):
    def test_canonical_long_funding_fixture_is_debit(self):
        self.assertEqual(
            canonical_funding_cash_flow(
                signed_notional="1000",
                rate="0.0001",
                sign_convention="POSITIVE_LONG_PAYS",
            ),
            Decimal("-0.1000"),
        )
        self.assertEqual(
            canonical_funding_cash_flow(
                signed_notional="-1000",
                rate="0.0001",
                sign_convention="POSITIVE_LONG_PAYS",
            ),
            Decimal("0.1000"),
        )

    def test_indicated_rate_never_creates_economic_posting(self):
        book = FundingRevisionBook()
        update = book.record(event(kind="INDICATED"))
        self.assertTrue(update.accepted)
        self.assertEqual(update.economic_delta, Decimal("0"))
        self.assertEqual(update.current_final_cash_flow, Decimal("0"))

    def test_final_rate_after_indication_posts_exact_charge_once(self):
        book = FundingRevisionBook()
        book.record(event(kind="INDICATED", revision=1))
        final = book.record(event(kind="FINAL", revision=2))
        self.assertEqual(final.economic_delta, Decimal("-0.1000"))
        retry = book.record(event(kind="FINAL", revision=2))
        self.assertFalse(retry.accepted)
        self.assertEqual(retry.economic_delta, Decimal("0"))

    def test_late_final_correction_posts_only_delta(self):
        book = FundingRevisionBook()
        first = book.record(event(kind="FINAL", revision=1, rate="0.0001"))
        correction = book.record(event(kind="FINAL", revision=2, rate="0.00012"))
        self.assertEqual(first.economic_delta, Decimal("-0.1000"))
        self.assertEqual(correction.current_final_cash_flow, Decimal("-0.12000"))
        self.assertEqual(correction.economic_delta, Decimal("-0.02000"))

        tx1 = book_funding_delta(
            transaction_id="funding-1",
            cause_event_id="funding-event-r1",
            settlement_currency="USD",
            economic_delta=first.economic_delta,
        )
        tx2 = book_funding_delta(
            transaction_id="funding-2",
            cause_event_id="funding-event-r2",
            settlement_currency="USD",
            economic_delta=correction.economic_delta,
        )
        ledger = EconomicBook([tx1, tx2])
        self.assertEqual(ledger.cash("USD"), Decimal("-0.12000"))

    def test_indicated_revision_cannot_erase_final_charge(self):
        book = FundingRevisionBook()
        book.record(event(kind="FINAL", revision=1))
        with self.assertRaises(FundingConflict):
            book.record(event(kind="INDICATED", revision=2, rate="0.0002"))

    def test_same_revision_conflict_is_rejected(self):
        book = FundingRevisionBook()
        book.record(event(kind="FINAL", revision=1, rate="0.0001"))
        with self.assertRaises(FundingConflict):
            book.record(event(kind="FINAL", revision=1, rate="0.0002"))

    def test_settlement_currency_and_effective_time_are_revision_identity(self):
        book = FundingRevisionBook()
        book.record(event(kind="FINAL", revision=1))
        changed_currency = FundingEvent(
            funding_id="funding:BTC-PERP:20260924T08",
            revision=2,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(9),
            settlement_currency="BTC",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.0001"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:changed",
        )
        with self.assertRaises(FundingConflict):
            book.record(changed_currency)

    def test_float_input_is_rejected(self):
        with self.assertRaises(ValueError):
            canonical_funding_cash_flow(
                signed_notional=1000.0,
                rate="0.0001",
                sign_convention="POSITIVE_LONG_PAYS",
            )

    def test_final_charge_cannot_be_known_before_effective_time(self):
        with self.assertRaises(ValueError):
            FundingEvent(
                funding_id="funding:future-final",
                revision=1,
                kind="FINAL",
                effective_at=moment(9),
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=Decimal("1000"),
                rate=Decimal("0.0001"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:invalid-final",
            )

    def test_revision_cannot_backdate_availability_or_change_sign_convention(self):
        book = FundingRevisionBook()
        book.record(event(kind="INDICATED", revision=1, available_hour=9))
        with self.assertRaises(FundingConflict):
            book.record(event(kind="FINAL", revision=2, available_hour=8))

        changed_sign = FundingEvent(
            funding_id="funding:BTC-PERP:20260924T08",
            revision=2,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(9),
            settlement_currency="USD",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.0001"),
            sign_convention="POSITIVE_LONG_RECEIVES",
            evidence_ref="artifact:changed-sign",
        )
        with self.assertRaises(FundingConflict):
            book.record(changed_sign)



if __name__ == "__main__":
    unittest.main()
