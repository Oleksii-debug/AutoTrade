from datetime import datetime, timedelta, timezone, tzinfo
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
import unittest

from mvp.autotrade_mvp.accounting import EconomicBook
from mvp.autotrade_mvp.exact_decimal import MAX_INTEGER_DIGITS
from mvp.autotrade_mvp.funding import (
    FundingConflict,
    FundingError,
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


class HostileDecimal(Decimal):
    virtual_calls = 0

    def is_finite(self):
        type(self).virtual_calls += 1
        raise AssertionError("hostile Decimal.is_finite was dispatched")

    def as_tuple(self):
        type(self).virtual_calls += 1
        raise AssertionError("hostile Decimal.as_tuple was dispatched")


class HostileFundingEvent(FundingEvent):
    economic_reads = 0

    @property
    def economic_cash_flow(self):
        type(self).economic_reads += 1
        raise AssertionError("hostile FundingEvent economic property was dispatched")


class HostileText(str):
    virtual_calls = 0

    def strip(self, *args, **kwargs):
        type(self).virtual_calls += 1
        raise AssertionError("hostile str.strip was dispatched")

    def upper(self):
        type(self).virtual_calls += 1
        raise AssertionError("hostile str.upper was dispatched")

    def __eq__(self, other):
        type(self).virtual_calls += 1
        raise AssertionError("hostile str equality was dispatched")


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

    def test_final_cash_flow_is_context_invariant_under_hostile_decimal_contexts(self):
        notional = Decimal("1234567890123456789012345678.1")
        rate = Decimal("0.0001")
        expected_credit = Decimal("123456789012345678901234.56781")
        expected_debit = Decimal("-123456789012345678901234.56781")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        debit = canonical_funding_cash_flow(
                            signed_notional=notional,
                            rate=rate,
                            sign_convention="POSITIVE_LONG_PAYS",
                        )
                        credit = canonical_funding_cash_flow(
                            signed_notional=notional,
                            rate=rate,
                            sign_convention="POSITIVE_LONG_RECEIVES",
                        )
                    self.assertEqual(debit, expected_debit)
                    self.assertEqual(credit, expected_credit)

    def test_decimal_subclasses_are_rejected_before_virtual_scalar_dispatch(self):
        HostileDecimal.virtual_calls = 0
        hostile = HostileDecimal("1000")
        with self.assertRaisesRegex(FundingError, "exact built-in Decimal"):
            canonical_funding_cash_flow(
                signed_notional=hostile,
                rate=Decimal("0.0001"),
                sign_convention="POSITIVE_LONG_PAYS",
            )
        with self.assertRaisesRegex(FundingError, "exact built-in Decimal"):
            FundingEvent(
                funding_id="funding:hostile-decimal",
                revision=1,
                kind="FINAL",
                effective_at=moment(8),
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=Decimal("1000"),
                rate=hostile,
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:hostile-decimal",
            )
        self.assertEqual(HostileDecimal.virtual_calls, 0)

    def test_terminal_revision_rejects_event_subclass_before_economic_property(self):
        HostileFundingEvent.economic_reads = 0
        hostile = HostileFundingEvent(
            funding_id="funding:hostile-event",
            revision=1,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(8),
            settlement_currency="USD",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.0001"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:hostile-event",
        )
        book = FundingRevisionBook()
        with self.assertRaisesRegex(TypeError, "exact FundingEvent"):
            book.record(hostile)
        self.assertEqual(HostileFundingEvent.economic_reads, 0)
        self.assertEqual(book.events, ())
        self.assertIsNone(book.latest("funding:hostile-event"))

    def test_event_authority_rejects_polymorphic_text_before_dispatch(self):
        HostileText.virtual_calls = 0
        hostile = HostileText("POSITIVE_LONG_PAYS")
        with self.assertRaisesRegex(FundingError, "unsupported funding sign convention"):
            canonical_funding_cash_flow(
                signed_notional=Decimal("1000"),
                rate=Decimal("0.0001"),
                sign_convention=hostile,
            )
        with self.assertRaisesRegex(FundingError, "funding_id is required"):
            FundingEvent(
                funding_id=HostileText("funding:hostile-text"),
                revision=1,
                kind="FINAL",
                effective_at=moment(8),
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=Decimal("1000"),
                rate=Decimal("0.0001"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:hostile-text",
            )
        self.assertEqual(HostileText.virtual_calls, 0)

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

    def test_high_significance_correction_delta_is_context_invariant(self):
        expected_first = Decimal("-100000000000000000000000000.0")
        expected_final = Decimal("-100000000000000000000000001.0")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        first_event = FundingEvent(
                            funding_id="funding:precision",
                            revision=1,
                            kind="FINAL",
                            effective_at=moment(8),
                            available_at=moment(8),
                            settlement_currency="USD",
                            signed_notional=Decimal("1000000000000000000000000000000"),
                            rate=Decimal("0.0001"),
                            sign_convention="POSITIVE_LONG_PAYS",
                            evidence_ref="artifact:precision-r1",
                        )
                        correction_event = FundingEvent(
                            funding_id="funding:precision",
                            revision=2,
                            kind="FINAL",
                            effective_at=moment(8),
                            available_at=moment(9),
                            settlement_currency="USD",
                            signed_notional=Decimal("1000000000000000000000000000000"),
                            rate=Decimal("0.000100000000000000000000000001"),
                            sign_convention="POSITIVE_LONG_PAYS",
                            evidence_ref="artifact:precision-r2",
                        )
                        book = FundingRevisionBook()
                        first = book.record(first_event)
                        correction = book.record(correction_event)
                    self.assertEqual(first.economic_delta, expected_first)
                    self.assertEqual(correction.current_final_cash_flow, expected_final)
                    self.assertEqual(correction.economic_delta, Decimal("-1.0"))
                    self.assertEqual(book.events, (first_event, correction_event))

    def test_revision_delta_resource_failure_does_not_mutate_book(self):
        first_event = FundingEvent(
            funding_id="funding:resource-boundary",
            revision=1,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(8),
            settlement_currency="USD",
            signed_notional=Decimal("1e255"),
            rate=Decimal("1"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:resource-r1",
        )
        incompatible_correction = FundingEvent(
            funding_id="funding:resource-boundary",
            revision=2,
            kind="FINAL",
            effective_at=moment(8),
            available_at=moment(9),
            settlement_currency="USD",
            signed_notional=Decimal("1e-256"),
            rate=Decimal("1"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:resource-r2",
        )
        book = FundingRevisionBook()
        first = book.record(first_event)
        self.assertEqual(first.current_revision, 1)
        before = book.events
        with self.assertRaisesRegex(FundingError, "resource envelope"):
            book.record(incompatible_correction)
        self.assertEqual(book.events, before)
        self.assertEqual(book.latest(first_event.funding_id), first_event)

    def test_funding_posting_negation_is_context_invariant(self):
        amount = Decimal("-100000000000000000000000001.0")
        opposite = Decimal("100000000000000000000000001.0")
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN):
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as decimal_context:
                        decimal_context.prec = precision
                        decimal_context.rounding = rounding
                        transaction = book_funding_delta(
                            transaction_id=f"funding-{precision}-{rounding}",
                            cause_event_id=f"funding-event-{precision}-{rounding}",
                            settlement_currency="USD",
                            economic_delta=amount,
                        )
                    self.assertEqual(transaction.postings[0].signed_amount, amount)
                    self.assertEqual(transaction.postings[1].signed_amount, opposite)

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


class FundingBoundedIngressAndChronologyTests(unittest.TestCase):
    def test_at_limit_string_is_admitted_before_exact_economics(self):
        at_limit = "9" * MAX_INTEGER_DIGITS
        self.assertEqual(
            canonical_funding_cash_flow(
                signed_notional=at_limit,
                rate="0",
                sign_convention="POSITIVE_LONG_PAYS",
            ),
            Decimal("0"),
        )

    def test_one_over_string_and_integer_fail_closed(self):
        values = (
            "9" * (MAX_INTEGER_DIGITS + 1),
            10 ** MAX_INTEGER_DIGITS,
        )
        for value in values:
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(FundingError, "resource envelope"):
                    canonical_funding_cash_flow(
                        signed_notional=value,
                        rate="0",
                        sign_convention="POSITIVE_LONG_PAYS",
                    )

    def test_numeric_subclasses_are_rejected_before_virtual_dispatch(self):
        HostileText.virtual_calls = 0

        class HostileInt(int):
            virtual_calls = 0

            def bit_length(self):
                type(self).virtual_calls += 1
                raise AssertionError("hostile int.bit_length was dispatched")

        for value in (HostileText("1"), HostileInt(1)):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(FundingError, "exact built-in Decimal"):
                    canonical_funding_cash_flow(
                        signed_notional=value,
                        rate="0",
                        sign_convention="POSITIVE_LONG_PAYS",
                    )

        self.assertEqual(HostileText.virtual_calls, 0)
        self.assertEqual(HostileInt.virtual_calls, 0)

    def test_hostile_nested_timezone_is_rejected_without_callback(self):
        touched = []

        class HostileTz(tzinfo):
            def utcoffset(self, dt):
                touched.append("utcoffset")
                raise AssertionError("hostile timezone callback")

            def dst(self, dt):
                touched.append("dst")
                raise AssertionError("hostile timezone callback")

            def fromutc(self, dt):
                touched.append("fromutc")
                raise AssertionError("hostile timezone callback")

        hostile = datetime(2026, 9, 24, 8, tzinfo=HostileTz())
        with self.assertRaisesRegex(FundingError, "built-in timezone"):
            FundingEvent(
                funding_id="funding:hostile-timezone",
                revision=1,
                kind="FINAL",
                effective_at=hostile,
                available_at=moment(8),
                settlement_currency="USD",
                signed_notional=Decimal("1000"),
                rate=Decimal("0.0001"),
                sign_convention="POSITIVE_LONG_PAYS",
                evidence_ref="artifact:hostile-timezone",
            )

        self.assertEqual(touched, [])

    def test_builtin_fixed_offset_timezone_normalizes_deterministically(self):
        fixed = timezone(timedelta(hours=2))
        item = FundingEvent(
            funding_id="funding:fixed-offset",
            revision=1,
            kind="FINAL",
            effective_at=datetime(2026, 9, 24, 10, tzinfo=fixed),
            available_at=datetime(2026, 9, 24, 11, tzinfo=fixed),
            settlement_currency="USD",
            signed_notional=Decimal("1000"),
            rate=Decimal("0.0001"),
            sign_convention="POSITIVE_LONG_PAYS",
            evidence_ref="artifact:fixed-offset",
        )
        self.assertEqual(item.effective_at, datetime(2026, 9, 24, 8, tzinfo=timezone.utc))
        self.assertEqual(item.available_at, datetime(2026, 9, 24, 9, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
