from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, localcontext
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.funding import (
    FundingConflict,
    FundingEvent,
    FundingRevisionBook,
    book_funding_delta,
    canonical_funding_cash_flow,
)


class HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("hostile is_finite dispatch")

    def as_tuple(self):
        raise AssertionError("hostile as_tuple dispatch")

    def __format__(self, format_spec):
        raise AssertionError("hostile format dispatch")

    def __eq__(self, other):
        raise AssertionError("hostile equality dispatch")


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

    def test_polymorphic_decimal_is_rejected_before_virtual_dispatch(self):
        hostile = HostileDecimal("0.0001")
        with self.assertRaisesRegex(
            ValueError,
            "bounded exact decimal input",
        ):
            canonical_funding_cash_flow(
                signed_notional=Decimal("1000"),
                rate=hostile,
                sign_convention="POSITIVE_LONG_PAYS",
            )
        with self.assertRaisesRegex(
            ValueError,
            "bounded exact decimal input",
        ):
            FundingEvent(
                funding_id="hostile",
                revision=1,
                kind="FINAL",
                effective_at=moment(8),
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=hostile,
                rate=Decimal("0.0001"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:hostile",
            )
        with self.assertRaisesRegex(
            ValueError,
            "bounded exact decimal input",
        ):
            book_funding_delta(
                transaction_id="hostile-tx",
                cause_event_id="hostile-event",
                settlement_currency="USD",
                economic_delta=hostile,
            )

    def test_funding_economics_ignore_ambient_decimal_context(self):
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_CEILING
            first = FundingEvent(
                funding_id="context",
                revision=1,
                kind="FINAL",
                effective_at=moment(8),
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=Decimal("12345678901234567890.123456789"),
                rate=Decimal("0.000000001"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:context-r1",
            )
            corrected = FundingEvent(
                funding_id="context",
                revision=2,
                kind="FINAL",
                effective_at=moment(8),
                available_at=moment(9),
                settlement_currency="USD",
                signed_notional=Decimal("12345678901234567890.123456789"),
                rate=Decimal("0.000000002"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:context-r2",
            )
            book = FundingRevisionBook()
            first_update = book.record(first)
            correction = book.record(corrected)
            transaction = book_funding_delta(
                transaction_id="context-tx",
                cause_event_id="context-event",
                settlement_currency="USD",
                economic_delta=correction.economic_delta,
            )

        self.assertEqual(
            first_update.economic_delta,
            Decimal("-12345678901.234567890123456789"),
        )
        self.assertEqual(
            correction.economic_delta,
            Decimal("-12345678901.234567890123456789"),
        )
        self.assertEqual(
            transaction.postings[0].signed_amount,
            Decimal("-12345678901.234567890123456789"),
        )
        self.assertEqual(
            transaction.postings[1].signed_amount,
            Decimal("12345678901.234567890123456789"),
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



    def test_restart_rehydrates_final_before_booking_only_correction_delta(self):
        first_event = event(revision=1, kind="FINAL", rate="0.0001")
        first_book = FundingRevisionBook()
        first = first_book.record(first_event)
        ledger = EconomicBook(
            [
                book_funding_delta(
                    transaction_id="funding-r1",
                    cause_event_id="funding-observation-r1",
                    settlement_currency="USD",
                    economic_delta=first.economic_delta,
                )
            ]
        )

        restarted = FundingRevisionBook(first_book.events)
        correction_event = event(revision=2, kind="FINAL", rate="0.00012", available_hour=9)
        correction = restarted.record(correction_event)
        self.assertEqual(correction.economic_delta, Decimal("-0.02000"))
        self.assertEqual(
            restarted.events,
            (first_event, correction_event),
        )

        ledger.append(
            book_funding_delta(
                transaction_id="funding-r2",
                cause_event_id="funding-observation-r2",
                settlement_currency="USD",
                economic_delta=correction.economic_delta,
            )
        )
        self.assertEqual(ledger.cash("USD"), Decimal("-0.12000"))

    def test_rehydration_fails_closed_on_non_monotonic_history(self):
        with self.assertRaisesRegex(FundingConflict, "cannot move backwards"):
            FundingRevisionBook(
                (
                    event(revision=2, kind="FINAL", rate="0.00012", available_hour=9),
                    event(revision=1, kind="FINAL", rate="0.0001", available_hour=9),
                )
            )

    def test_currency_aliases_are_canonical_across_revision_and_posting(self):
        first = FundingEvent(
            funding_id="funding:BTC-PERP:currency",
            revision=1,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(8),
            settlement_currency=" usd ",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.0001"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:currency-r1",
        )
        correction = FundingEvent(
            funding_id=first.funding_id,
            revision=2,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(9),
            settlement_currency="USD",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.00012"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:currency-r2",
        )
        book = FundingRevisionBook((first,))
        update = book.record(correction)
        self.assertEqual(first.settlement_currency, "USD")
        self.assertEqual(update.economic_delta, Decimal("-0.02000"))

        transaction = book_funding_delta(
            transaction_id="funding-currency",
            cause_event_id="funding-currency-r2",
            settlement_currency=" usd ",
            economic_delta=update.economic_delta,
        )
        self.assertEqual(transaction.postings[0].ledger_account, "CASH:USD")
        self.assertEqual(transaction.postings[0].asset_or_currency, "USD")

    def test_record_rejects_non_event_boundary(self):
        with self.assertRaisesRegex(TypeError, "FundingEvent"):
            FundingRevisionBook().record({"funding_id": "forged"})


if __name__ == "__main__":
    unittest.main()
