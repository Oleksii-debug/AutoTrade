from datetime import date, datetime, timezone
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
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
OTHER_INSTRUMENT_ID = "22222222-2222-4222-8222-222222222222"


def instrument(
    *,
    instrument_id=INSTRUMENT_ID,
    version=1,
    symbol="AAA",
    effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    contract_multiplier=Decimal("1"),
    settlement_currency="USD",
    quantity_unit="AAA",
):
    return InstrumentVersion(
        instrument_id=instrument_id,
        version=version,
        provider_id="simulated",
        venue_id="simulated-venue",
        provider_symbol=symbol,
        asset_class="CASH_EQUITY",
        base_currency="AAA",
        quote_currency="USD",
        settlement_currency=settlement_currency,
        quantity_unit=quantity_unit,
        contract_multiplier=contract_multiplier,
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=effective_from,
        status="ACTIVE",
    )


def bound_book(state_value, *, current=None, registry=None):
    current = current or instrument()
    registry = registry or InstrumentRegistry(versions=(current,))
    return CorporateActionBook(
        state_value,
        instrument_version=current,
        registry=registry,
    )


def corporate_event(**kwargs):
    kwargs.setdefault("instrument_id", INSTRUMENT_ID)
    kwargs.setdefault("instrument_version", 1)
    return CorporateEvent.create(**kwargs)


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
        book = bound_book(state())
        event = corporate_event(
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
        book = bound_book(
            state(
                quantity="-10",
                total_basis="1000",
                borrowed_quantity="10",
                recalled_quantity="4",
            )
        )
        event = corporate_event(
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
        book = bound_book(state())
        event = corporate_event(
            event_id="div-1",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1.50", "currency": "USD"},
        )
        result = book.apply(event)
        self.assertEqual(result.after.unsettled_cash, Decimal("15.00"))
        self.assertEqual(result.after.settled_cash, Decimal("1000"))
        settled = settle_cash(result.after, "15")
        self.assertEqual(settled.unsettled_cash, Decimal("0.00"))
        self.assertEqual(settled.settled_cash, Decimal("1015"))

    def test_short_dividend_payable_can_settle_as_negative_cash(self):
        short_state = state(
            quantity="-10",
            total_basis="1000",
            borrowed_quantity="10",
        )
        book = bound_book(short_state)
        event = corporate_event(
            event_id="short-div-1",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1.50", "currency": "USD"},
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
        book = bound_book(state())
        event = corporate_event(
            event_id="merger-1",
            kind="MERGER_CASH",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"cash_per_share": "110", "currency": "USD"},
        )
        result = book.apply(event)
        self.assertEqual(result.after.quantity, Decimal("0"))
        self.assertEqual(result.after.total_basis, Decimal("0"))
        self.assertEqual(result.after.unsettled_cash, Decimal("1100"))
        self.assertEqual(result.economic_pnl, Decimal("100"))

    def test_delist_without_evidenced_consideration_fails_closed(self):
        book = bound_book(state())
        event = corporate_event(
            event_id="delist-1",
            kind="DELIST",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={},
        )
        with self.assertRaises(ValueError):
            book.apply(event)
        self.assertEqual(book.state.quantity, Decimal("10"))

    def test_cash_equity_short_must_match_borrow_and_cannot_use_long_purchase_to_cover(self):
        for values in (
            {"quantity": "-2", "borrowed_quantity": "0"},
            {"quantity": "-2", "borrowed_quantity": "1"},
            {"quantity": "2", "borrowed_quantity": "2"},
        ):
            with self.subTest(values=values), self.assertRaisesRegex(
                ValueError,
                "borrowed_quantity",
            ):
                state(**values)

        short = establish_short(
            state(quantity="0", total_basis="0"),
            quantity="2",
            sale_price="100",
        )
        with self.assertRaisesRegex(ValueError, "cannot implicitly cover"):
            record_unsettled_purchase(short, quantity="1", price="90")
        self.assertEqual(short.quantity, Decimal("-2"))
        self.assertEqual(short.borrowed_quantity, Decimal("2"))

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

    def test_equity_currency_is_canonical_and_bound_to_instrument_settlement(self):
        canonical = state(currency=" usd ")
        self.assertEqual(canonical.currency, "USD")

        with self.assertRaisesRegex(ValueError, "state currency"):
            bound_book(state(currency="EUR"))

    def test_cash_corporate_actions_require_explicit_matching_currency(self):
        book = bound_book(state())
        original = book.state

        missing = corporate_event(
            event_id="div-missing-currency",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1"},
        )
        with self.assertRaisesRegex(ValueError, "exactly per_share and currency"):
            book.apply(missing)
        self.assertEqual(book.state, original)
        self.assertEqual(book.applied_event_ids, ())

        mismatched = corporate_event(
            event_id="div-wrong-currency",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1", "currency": "EUR"},
        )
        with self.assertRaisesRegex(ValueError, "cash currency"):
            book.apply(mismatched)
        self.assertEqual(book.state, original)
        self.assertEqual(book.applied_event_ids, ())

    def test_same_date_actions_replay_by_authoritative_source_sequence(self):
        split = corporate_event(
            event_id="same-day-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="official-feed-r7",
            source_sequence=10,
            payload={"numerator": 2, "denominator": 1},
        )
        dividend = corporate_event(
            event_id="same-day-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="official-feed-r7",
            source_sequence=11,
            payload={"per_share": "1", "currency": "USD"},
        )

        forward = CorporateActionBook.replay(
            state(),
            instrument_version=instrument(),
            registry=InstrumentRegistry(versions=(instrument(),)),
            events=(split, dividend),
        )
        shuffled = CorporateActionBook.replay(
            state(),
            instrument_version=instrument(),
            registry=InstrumentRegistry(versions=(instrument(),)),
            events=(dividend, split),
        )
        self.assertEqual(forward.state, shuffled.state)
        self.assertEqual(forward.state.quantity, Decimal("20"))
        self.assertEqual(forward.state.unsettled_cash, Decimal("20"))
        self.assertEqual(
            forward.applied_event_ids,
            ("same-day-split", "same-day-dividend"),
        )
        self.assertEqual(forward.applied_event_ids, shuffled.applied_event_ids)

    def test_same_date_actions_without_authoritative_order_fail_closed(self):
        split = corporate_event(
            event_id="ambiguous-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="date-only-source",
            payload={"numerator": 2, "denominator": 1},
        )
        dividend = corporate_event(
            event_id="ambiguous-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="date-only-source",
            payload={"per_share": "1", "currency": "USD"},
        )
        with self.assertRaisesRegex(ValueError, "authoritative source_sequence"):
            CorporateActionBook.replay(
                state(),
                instrument_version=instrument(),
                registry=InstrumentRegistry(versions=(instrument(),)),
                events=(split, dividend),
            )

        incremental = bound_book(state())
        incremental.apply(split)
        with self.assertRaisesRegex(ValueError, "authoritative source_sequence"):
            incremental.apply(dividend)
        self.assertEqual(incremental.state.quantity, Decimal("20"))
        self.assertEqual(incremental.state.unsettled_cash, Decimal("0"))

        with self.assertRaisesRegex(ValueError, "non-negative integer"):
            corporate_event(
                event_id="bad-sequence",
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                source_sequence=-1,
                payload={"numerator": 2, "denominator": 1},
            )

    def test_accepted_event_history_is_immutable_and_complete(self):
        book = bound_book(state())
        split = corporate_event(
            event_id="history-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )
        dividend = corporate_event(
            event_id="history-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 3),
            source_revision="r2",
            payload={"per_share": "1", "currency": "USD"},
        )
        book.apply(split)
        book.apply(dividend)
        history = book.events
        self.assertEqual(history, (split, dividend))
        self.assertIsInstance(history, tuple)

    def test_corporate_event_rejects_duplicate_normalized_payload_keys(self):
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            corporate_event(
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
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                kind="cash_dividend",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"per_share": 0.1},
            )

    def test_corporate_event_rejects_binary_float_economics(self):
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            corporate_event(
                event_id="split-float",
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"numerator": 2.0, "denominator": 1},
            )
        with self.assertRaisesRegex(TypeError, "exact decimal"):
            corporate_event(
                event_id="div-float",
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"per_share": 0.1},
            )

    def test_duplicate_event_identity_with_changed_content_is_rejected(self):
        book = bound_book(state())
        first = corporate_event(
            event_id="split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )
        second = corporate_event(
            event_id="split-1",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r2",
            payload={"numerator": 3, "denominator": 1},
        )
        book.apply(first)
        with self.assertRaises(ValueError):
            book.apply(second)


    def test_corporate_event_for_other_instrument_or_version_fails_before_mutation(self):
        book = bound_book(state())
        original_state = book.state
        original_instrument = book.instrument_version

        for event in (
            corporate_event(
                event_id="wrong-instrument",
                instrument_id=OTHER_INSTRUMENT_ID,
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"numerator": 2, "denominator": 1},
            ),
            corporate_event(
                event_id="wrong-version",
                instrument_version=2,
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"per_share": "1", "currency": "USD"},
            ),
        ):
            with self.subTest(event=event.event_id):
                with self.assertRaisesRegex(
                    ValueError,
                    "instrument identity mismatch",
                ):
                    book.apply(event)
                self.assertEqual(book.state, original_state)
                self.assertEqual(book.instrument_version, original_instrument)
                self.assertEqual(book.applied_event_ids, ())

    def test_symbol_change_requires_registered_next_instrument_version_and_is_zero_pnl(self):
        first = instrument()
        second = instrument(
            version=2,
            symbol="BBB",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        registry = InstrumentRegistry(versions=(first, second))
        book = bound_book(state(), current=first, registry=registry)
        before = book.state
        event = corporate_event(
            event_id="symbol-change-1",
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 6, 1),
            source_revision="provider-revision-7",
            payload={"successor_instrument_version": 2},
        )

        result = book.apply(event)
        self.assertEqual(result.economic_pnl, Decimal("0"))
        self.assertEqual(result.before, before)
        self.assertEqual(result.after.symbol, "BBB")
        self.assertEqual(result.after.quantity, before.quantity)
        self.assertEqual(result.after.total_basis, before.total_basis)
        self.assertEqual(result.after.settled_cash, before.settled_cash)
        self.assertEqual(result.after.unsettled_cash, before.unsettled_cash)
        self.assertEqual(book.instrument_version, second)
        self.assertEqual(book.apply(event), result)

    def test_symbol_change_rejects_economic_identity_drift_before_mutation(self):
        first = instrument()
        cases = (
            ("contract_multiplier", {"contract_multiplier": Decimal("2")}),
            ("settlement_currency", {"settlement_currency": "EUR"}),
            ("quantity_unit", {"quantity_unit": "BBB"}),
        )
        for field, overrides in cases:
            with self.subTest(field=field):
                second = instrument(
                    version=2,
                    symbol="BBB",
                    effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
                    **overrides,
                )
                registry = InstrumentRegistry(versions=(first, second))
                book = bound_book(state(), current=first, registry=registry)
                original_state = book.state
                event = corporate_event(
                    event_id="symbol-change-economic-drift-" + field,
                    kind="SYMBOL_CHANGE",
                    effective_date=date(2026, 6, 1),
                    source_revision="r-economic-drift",
                    payload={"successor_instrument_version": 2},
                )
                with self.assertRaisesRegex(ValueError, "economic identity"):
                    book.apply(event)
                self.assertEqual(book.state, original_state)
                self.assertEqual(book.instrument_version, first)
                self.assertEqual(book.applied_event_ids, ())

    def test_symbol_change_rejects_unregistered_or_non_next_successor(self):
        first = instrument()
        registry = InstrumentRegistry(versions=(first,))
        book = bound_book(state(), current=first, registry=registry)
        event = corporate_event(
            event_id="symbol-change-missing",
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 6, 1),
            source_revision="r1",
            payload={"successor_instrument_version": 2},
        )
        with self.assertRaisesRegex(ValueError, "not registered"):
            book.apply(event)
        self.assertEqual(book.instrument_version, first)
        self.assertEqual(book.state, state())

    def test_replay_rejects_reverse_effective_date_before_mutation(self):
        book = bound_book(state())
        later = corporate_event(
            event_id="later-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 3, 1),
            source_revision="r2",
            payload={"per_share": "1", "currency": "USD"},
        )
        earlier = corporate_event(
            event_id="earlier-split",
            kind="SPLIT",
            effective_date=date(2026, 2, 1),
            source_revision="r1",
            payload={"numerator": 2, "denominator": 1},
        )

        first = book.apply(later)
        state_after_later = book.state
        with self.assertRaisesRegex(ValueError, "non-decreasing effective-date"):
            book.apply(earlier)

        self.assertEqual(book.state, state_after_later)
        self.assertEqual(book.applied_event_ids, ("later-dividend",))
        self.assertEqual(first.after, state_after_later)

        with self.assertRaisesRegex(ValueError, "non-decreasing effective-date"):
            CorporateActionBook.replay(
                state(),
                instrument_version=instrument(),
                registry=InstrumentRegistry(versions=(instrument(),)),
                events=(later, earlier),
            )

    def test_dividend_replay_is_restart_idempotent_without_double_receivable(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        dividend = corporate_event(
            event_id="dividend-restart",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 3, 1),
            source_revision="provider:r1",
            payload={"per_share": "1.25", "currency": "USD"},
        )
        initial = state(unsettled_cash="0")
        running = CorporateActionBook(
            initial,
            instrument_version=current,
            registry=registry,
        )
        first = running.apply(dividend)
        self.assertEqual(first.after.unsettled_cash, Decimal("12.50"))
        self.assertEqual(running.events, (dividend,))

        restarted = CorporateActionBook.replay(
            initial,
            instrument_version=current,
            registry=registry,
            events=running.events,
        )
        duplicate = restarted.apply(dividend)
        self.assertEqual(restarted.state.unsettled_cash, Decimal("12.50"))
        self.assertEqual(duplicate.after.unsettled_cash, Decimal("12.50"))
        self.assertEqual(restarted.applied_event_ids, ("dividend-restart",))
        self.assertEqual(restarted.events, (dividend,))

    def test_split_then_symbol_change_replay_is_deterministic(self):
        first = instrument()
        second = instrument(
            version=2,
            symbol="BBB",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        registry = InstrumentRegistry(versions=(first, second))
        events = (
            corporate_event(
                event_id="split-before-rename",
                kind="SPLIT",
                effective_date=date(2026, 1, 2),
                source_revision="r1",
                payload={"numerator": 2, "denominator": 1},
            ),
            corporate_event(
                event_id="symbol-change-after-split",
                kind="SYMBOL_CHANGE",
                effective_date=date(2026, 6, 1),
                source_revision="r2",
                payload={"successor_instrument_version": 2},
            ),
        )
        first_run = CorporateActionBook.replay(
            state(),
            instrument_version=first,
            registry=registry,
            events=events,
        )
        restarted = CorporateActionBook.replay(
            state(),
            instrument_version=first,
            registry=registry,
            events=events,
        )
        self.assertEqual(first_run.state, restarted.state)
        self.assertEqual(first_run.instrument_version, restarted.instrument_version)
        self.assertEqual(first_run.state.quantity, Decimal("20"))
        self.assertEqual(first_run.state.total_basis, Decimal("1000"))
        self.assertEqual(first_run.state.symbol, "BBB")
        self.assertEqual(
            first_run.applied_event_ids,
            ("split-before-rename", "symbol-change-after-split"),
        )



if __name__ == "__main__":
    unittest.main()
