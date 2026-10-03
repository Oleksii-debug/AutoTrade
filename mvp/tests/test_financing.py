from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_DOWN, localcontext
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

    def test_direct_event_construction_cannot_bypass_financial_invariants(self):
        with self.assertRaises(FinancingError):
            FinancingEvent(
                charge_id="direct",
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount=0.1,
                source_account="CASH:USD",
                evidence_ref="artifact:direct",
            )
        with self.assertRaises(FinancingError):
            FinancingEvent(
                charge_id="direct",
                revision=0,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount=Decimal("1"),
                source_account="CASH:USD",
                evidence_ref="artifact:direct",
            )
        with self.assertRaises(FinancingError):
            FinancingEvent(
                charge_id="direct",
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE - timedelta(seconds=1),
                unit="USD",
                amount=Decimal("1"),
                source_account="CASH:USD",
                evidence_ref="artifact:direct",
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


    def test_restart_rehydrates_final_before_later_correction(self):
        first_event = event(
            revision=1,
            kind="FINAL",
            amount="1.2",
            available_at=BASE + timedelta(minutes=1),
        )
        first_book = FinancingRevisionBook()
        first = first_book.record(first_event)
        economic = EconomicBook(
            [
                book_financing_delta(
                    transaction_id="financing-r1",
                    cause_event_id="provider-financing-r1",
                    unit="BTC",
                    source_account="BORROW_LIABILITY:BTC",
                    economic_delta=first.economic_delta,
                )
            ]
        )

        restarted = FinancingRevisionBook(first_book.events)
        correction_event = event(
            revision=2,
            kind="FINAL",
            amount="1.1",
            available_at=BASE + timedelta(minutes=2),
        )
        correction = restarted.record(correction_event)
        self.assertEqual(correction.economic_delta, Decimal("-0.1"))
        self.assertEqual(
            restarted.events,
            (first_event, correction_event),
        )

        economic.append(
            book_financing_delta(
                transaction_id="financing-r2",
                cause_event_id="provider-financing-r2",
                unit="BTC",
                source_account="BORROW_LIABILITY:BTC",
                economic_delta=correction.economic_delta,
            )
        )
        self.assertEqual(
            economic.balance("FINANCING_EXPENSE:BTC", "BTC"),
            Decimal("1.1"),
        )

    def test_restart_history_fails_closed_when_revisions_move_backwards(self):
        with self.assertRaisesRegex(FinancingConflict, "cannot move backwards"):
            FinancingRevisionBook(
                (
                    event(
                        revision=2,
                        kind="FINAL",
                        amount="1.2",
                        available_at=BASE + timedelta(minutes=2),
                    ),
                    event(
                        revision=1,
                        kind="FINAL",
                        amount="1.1",
                        available_at=BASE + timedelta(minutes=2),
                    ),
                )
            )

    def test_financing_unit_is_canonical_across_event_and_posting(self):
        observed = FinancingEvent.create(
            charge_id="fee-usd",
            revision=1,
            kind="FINAL",
            effective_at=BASE,
            available_at=BASE,
            unit=" usd ",
            amount="1.25",
            source_account="CASH:USD",
            evidence_ref="artifact:fee-usd",
        )
        self.assertEqual(observed.unit, "USD")
        transaction = book_financing_delta(
            transaction_id="fee-usd-tx",
            cause_event_id="fee-usd-r1",
            unit=" usd ",
            source_account="CASH:USD",
            economic_delta="1.25",
        )
        self.assertEqual(transaction.postings[0].asset_or_currency, "USD")
        self.assertEqual(
            transaction.postings[1].ledger_account,
            "FINANCING_EXPENSE:USD",
        )


    def test_financing_event_rejects_decimal_subclass_before_dispatch(self):
        class HostileDecimal(Decimal):
            calls = 0

            def is_finite(self):
                type(self).calls += 1
                raise AssertionError("hostile Decimal dispatch")

        hostile = HostileDecimal("1.25")
        with self.assertRaisesRegex(FinancingError, "exact decimal input"):
            FinancingEvent.create(
                charge_id="hostile-decimal",
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount=hostile,
                source_account="CASH:USD",
                evidence_ref="artifact:hostile",
            )
        self.assertEqual(HostileDecimal.calls, 0)

    def test_financing_event_rejects_text_subclass_before_normalization(self):
        class HostileText(str):
            calls = 0

            def strip(self, *args, **kwargs):
                type(self).calls += 1
                raise AssertionError("hostile text strip")

            def upper(self):
                type(self).calls += 1
                raise AssertionError("hostile text upper")

        hostile = HostileText("charge")
        with self.assertRaisesRegex(FinancingError, "charge_id is required"):
            FinancingEvent.create(
                charge_id=hostile,
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount="1",
                source_account="CASH:USD",
                evidence_ref="artifact:hostile",
            )
        self.assertEqual(HostileText.calls, 0)

    def test_financing_event_rejects_revision_subclass_before_comparison(self):
        class HostileInt(int):
            calls = 0

            def __lt__(self, other):
                type(self).calls += 1
                raise AssertionError("hostile revision comparison")

        with self.assertRaisesRegex(FinancingError, "positive integer"):
            FinancingEvent.create(
                charge_id="hostile-revision",
                revision=HostileInt(1),
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount="1",
                source_account="CASH:USD",
                evidence_ref="artifact:hostile",
            )
        self.assertEqual(HostileInt.calls, 0)

    def test_financing_event_rejects_datetime_and_timezone_subclasses_before_callbacks(self):
        calls = []

        class HostileDateTime(datetime):
            def utcoffset(self):
                calls.append("datetime-utcoffset")
                raise AssertionError("hostile datetime utcoffset")

            def astimezone(self, *args, **kwargs):
                calls.append("datetime-astimezone")
                raise AssertionError("hostile datetime astimezone")

        hostile_datetime = HostileDateTime(
            2026,
            9,
            24,
            18,
            tzinfo=timezone.utc,
        )
        calls.clear()
        with self.assertRaisesRegex(FinancingError, "timezone-aware"):
            FinancingEvent.create(
                charge_id="hostile-time",
                revision=1,
                kind="FINAL",
                effective_at=hostile_datetime,
                available_at=BASE,
                unit="USD",
                amount="1",
                source_account="CASH:USD",
                evidence_ref="artifact:hostile",
            )
        self.assertEqual(calls, [])

        class HostileTimezone(tzinfo):
            def utcoffset(self, dt):
                calls.append("tz-utcoffset")
                raise AssertionError("hostile timezone utcoffset")

            def dst(self, dt):
                calls.append("tz-dst")
                raise AssertionError("hostile timezone dst")

        custom_zone = HostileTimezone()
        hostile_zone_datetime = datetime(
            2026,
            9,
            24,
            18,
            tzinfo=custom_zone,
        )
        calls.clear()
        with self.assertRaisesRegex(FinancingError, "timezone-aware"):
            FinancingEvent.create(
                charge_id="hostile-zone",
                revision=1,
                kind="FINAL",
                effective_at=hostile_zone_datetime,
                available_at=BASE,
                unit="USD",
                amount="1",
                source_account="CASH:USD",
                evidence_ref="artifact:hostile",
            )
        self.assertEqual(calls, [])

    def test_financing_event_subclass_and_forged_exact_event_fail_closed(self):
        class EventSubclass(FinancingEvent):
            pass

        with self.assertRaisesRegex(TypeError, "exact FinancingEvent class"):
            EventSubclass.create(
                charge_id="subclass",
                revision=1,
                kind="FINAL",
                effective_at=BASE,
                available_at=BASE,
                unit="USD",
                amount="1",
                source_account="CASH:USD",
                evidence_ref="artifact:subclass",
            )

        class HostileDecimal(Decimal):
            calls = 0

            def is_finite(self):
                type(self).calls += 1
                raise AssertionError("forged Decimal dispatch")

        forged = event(revision=1, kind="FINAL", amount="1")
        object.__setattr__(forged, "amount", HostileDecimal("1"))
        book = FinancingRevisionBook()
        with self.assertRaisesRegex(FinancingError, "exact decimal input"):
            book.record(forged)
        self.assertEqual(HostileDecimal.calls, 0)
        self.assertEqual(book.events, ())

    def test_history_subclass_is_rejected_before_iteration_and_exact_list_is_supported(self):
        class HostileList(list):
            calls = 0

            def __iter__(self):
                type(self).calls += 1
                raise AssertionError("hostile history iteration")

        hostile = HostileList([event()])
        HostileList.calls = 0
        with self.assertRaisesRegex(TypeError, "exact list or tuple"):
            FinancingRevisionBook(hostile)
        self.assertEqual(HostileList.calls, 0)

        restored = FinancingRevisionBook([event()])
        self.assertEqual(len(restored.events), 1)
        self.assertEqual(restored.events[0].revision, 1)

    def test_financing_delta_and_postings_ignore_ambient_decimal_context(self):
        book = FinancingRevisionBook()
        first = FinancingEvent.create(
            charge_id="high-significance",
            revision=1,
            kind="FINAL",
            effective_at=BASE,
            available_at=BASE,
            unit="BTC",
            amount="1000000000000000000000000000000",
            source_account="BORROW_LIABILITY:BTC",
            evidence_ref="artifact:high-r1",
        )
        second = FinancingEvent.create(
            charge_id="high-significance",
            revision=2,
            kind="FINAL",
            effective_at=BASE,
            available_at=BASE + timedelta(seconds=1),
            unit="BTC",
            amount="1000000000000000000000000000000.00000000000000000001",
            source_account="BORROW_LIABILITY:BTC",
            evidence_ref="artifact:high-r2",
        )
        with localcontext() as context:
            context.prec = 6
            context.rounding = ROUND_DOWN
            book.record(first)
            update = book.record(second)
            transaction = book_financing_delta(
                transaction_id="high-significance-r2",
                cause_event_id="high-significance-event-r2",
                unit="BTC",
                source_account="BORROW_LIABILITY:BTC",
                economic_delta=update.economic_delta,
            )
        self.assertEqual(update.economic_delta, Decimal("0.00000000000000000001"))
        self.assertEqual(
            transaction.postings[0].amount,
            Decimal("-0.00000000000000000001"),
        )
        self.assertEqual(
            transaction.postings[1].amount,
            Decimal("0.00000000000000000001"),
        )


if __name__ == "__main__":
    unittest.main()
