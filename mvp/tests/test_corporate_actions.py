from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, localcontext
import unittest

from mvp.autotrade_mvp.corporate_actions import (
    CorporateActionBook,
    CorporateActionCheckpoint,
    CorporateEvent,
    EquityState,
    accrue_borrow_financing,
    cover_recalled_short,
    establish_short,
    record_recall,
    record_unsettled_purchase,
    settle_cash,
)
from mvp.autotrade_mvp.exact_decimal import MAX_INTEGER_DIGITS
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
    if kwargs.get("kind", "").upper() == "SYMBOL_CHANGE" and "effective_at" not in kwargs:
        effective_date = kwargs.get("effective_date")
        if isinstance(effective_date, date):
            kwargs["effective_at"] = datetime(
                effective_date.year,
                effective_date.month,
                effective_date.day,
                tzinfo=timezone.utc,
            )
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

    def test_split_and_delist_reject_ignored_payload_fields_before_mutation(self):
        split_book = bound_book(state())
        original_split_state = split_book.state
        with self.assertRaisesRegex(ValueError, "exactly numerator and denominator"):
            split_book.apply(
                corporate_event(
                    event_id="split-extra",
                    kind="SPLIT",
                    effective_date=date(2026, 1, 2),
                    source_revision="r1",
                    payload={
                        "numerator": 2,
                        "denominator": 1,
                        "ignored": "provider-extension",
                    },
                )
            )
        self.assertEqual(split_book.state, original_split_state)
        self.assertEqual(split_book.applied_event_ids, ())

        delist_book = bound_book(state())
        original_delist_state = delist_book.state
        with self.assertRaisesRegex(ValueError, "exactly cash_per_share and currency"):
            delist_book.apply(
                corporate_event(
                    event_id="delist-extra",
                    kind="DELIST",
                    effective_date=date(2026, 1, 2),
                    source_revision="r1",
                    payload={
                        "cash_per_share": "110",
                        "currency": "USD",
                        "ignored": "provider-extension",
                    },
                )
            )
        self.assertEqual(delist_book.state, original_delist_state)
        self.assertEqual(delist_book.applied_event_ids, ())

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

    def test_checkpoint_plus_full_history_does_not_double_apply_dividend(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        dividend = corporate_event(
            event_id="checkpoint-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 2, 1),
            source_revision="provider:r1",
            payload={"per_share": "1.25", "currency": "USD"},
        )
        running = CorporateActionBook(
            state(),
            instrument_version=current,
            registry=registry,
        )
        first = running.apply(dividend)
        self.assertEqual(first.after.unsettled_cash, Decimal("12.50"))
        checkpoint = running.checkpoint("after-dividend")

        restored = CorporateActionBook.from_checkpoint(
            checkpoint,
            registry=registry,
            events=running.events,
        )
        self.assertEqual(restored.state, running.state)
        self.assertEqual(restored.state.unsettled_cash, Decimal("12.50"))
        self.assertEqual(restored.applied_event_ids, ("checkpoint-dividend",))
        retry = restored.apply(dividend)
        self.assertEqual(retry, first)
        self.assertEqual(restored.state.unsettled_cash, Decimal("12.50"))

    def test_checkpoint_replays_only_strict_suffix(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        split = corporate_event(
            event_id="checkpoint-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="provider:r1",
            payload={"numerator": 2, "denominator": 1},
        )
        dividend = corporate_event(
            event_id="checkpoint-later-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 3),
            source_revision="provider:r2",
            payload={"per_share": "1", "currency": "USD"},
        )

        running = CorporateActionBook(
            state(),
            instrument_version=current,
            registry=registry,
        )
        running.apply(split)
        checkpoint = running.checkpoint("after-split")
        running.apply(dividend)

        restored = CorporateActionBook.from_checkpoint(
            checkpoint,
            registry=registry,
            events=running.events,
        )
        self.assertEqual(restored.state, running.state)
        self.assertEqual(restored.state.quantity, Decimal("20"))
        self.assertEqual(restored.state.unsettled_cash, Decimal("20"))
        self.assertEqual(
            restored.applied_event_ids,
            ("checkpoint-split", "checkpoint-later-dividend"),
        )

    def test_checkpoint_rejects_changed_or_non_prefix_history_before_replay(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        original = corporate_event(
            event_id="checkpoint-prefix",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="provider:r1",
            payload={"numerator": 2, "denominator": 1},
        )
        running = CorporateActionBook(
            state(),
            instrument_version=current,
            registry=registry,
        )
        running.apply(original)
        checkpoint = running.checkpoint("prefix")

        changed = corporate_event(
            event_id="checkpoint-prefix",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="provider:r2",
            payload={"numerator": 3, "denominator": 1},
        )
        with self.assertRaisesRegex(ValueError, "exact retained-history prefix"):
            CorporateActionBook.from_checkpoint(
                checkpoint,
                registry=registry,
                events=(changed,),
            )
        self.assertEqual(checkpoint.state.quantity, Decimal("20"))

        with self.assertRaisesRegex(ValueError, "unique event identities"):
            CorporateActionBook.from_checkpoint(
                checkpoint,
                registry=registry,
                events=(original, original),
            )

    def test_checkpoint_validation_rejects_inconsistent_transition_state(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        running = CorporateActionBook(
            state(),
            instrument_version=current,
            registry=registry,
        )
        event = corporate_event(
            event_id="checkpoint-chain",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="provider:r1",
            payload={"numerator": 2, "denominator": 1},
        )
        transition = running.apply(event)
        with self.assertRaisesRegex(ValueError, "does not match final"):
            CorporateActionCheckpoint(
                checkpoint_id="tampered",
                state=state(),
                instrument_version=current,
                records=((event, transition),),
            )

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

    def test_symbol_change_successor_version_payload_must_be_canonical_ascii_integer(self):
        first = instrument()
        second = instrument(
            version=2,
            symbol="BBB",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        registry = InstrumentRegistry(versions=(first, second))
        for raw_version in ("02", "٢"):
            with self.subTest(raw_version=raw_version):
                book = bound_book(state(), current=first, registry=registry)
                with self.assertRaisesRegex(ValueError, "canonical positive integer"):
                    book.apply(
                        corporate_event(
                            event_id="symbol-change-noncanonical-" + raw_version,
                            kind="SYMBOL_CHANGE",
                            effective_date=date(2026, 6, 1),
                            source_revision="r1",
                            payload={"successor_instrument_version": raw_version},
                        )
                    )
                self.assertEqual(book.state.symbol, "AAA")
                self.assertEqual(book.instrument_version, first)
                self.assertEqual(book.applied_event_ids, ())

    def test_symbol_change_requires_exact_effective_instant_for_intraday_successor(self):
        first = instrument()
        successor_effective_at = datetime(
            2026, 6, 1, 14, 0, tzinfo=timezone.utc
        )
        second = instrument(
            version=2,
            symbol="BBB",
            effective_from=successor_effective_at,
        )
        registry = InstrumentRegistry(versions=(first, second))
        book = bound_book(state(), current=first, registry=registry)
        original_state = book.state

        early = corporate_event(
            event_id="symbol-change-too-early",
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 6, 1),
            effective_at=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
            source_revision="provider:r1",
            payload={"successor_instrument_version": 2},
        )
        with self.assertRaisesRegex(
            ValueError,
            "effective_at does not match successor effective_from",
        ):
            book.apply(early)
        self.assertEqual(book.state, original_state)
        self.assertEqual(book.instrument_version, first)
        self.assertEqual(book.applied_event_ids, ())

        exact = corporate_event(
            event_id="symbol-change-exact-instant",
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 6, 1),
            effective_at=successor_effective_at,
            source_revision="provider:r1",
            payload={"successor_instrument_version": 2},
        )
        result = book.apply(exact)
        self.assertEqual(result.economic_pnl, Decimal("0"))
        self.assertEqual(result.after.symbol, "BBB")
        self.assertEqual(book.instrument_version, second)

    def test_symbol_change_without_effective_at_fails_closed(self):
        first = instrument()
        second = instrument(
            version=2,
            symbol="BBB",
            effective_from=datetime(2026, 6, 1, tzinfo=timezone.utc),
        )
        registry = InstrumentRegistry(versions=(first, second))
        book = bound_book(state(), current=first, registry=registry)
        event = CorporateEvent.create(
            event_id="symbol-change-missing-effective-at",
            instrument_id=INSTRUMENT_ID,
            instrument_version=1,
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 6, 1),
            source_revision="provider:r1",
            payload={"successor_instrument_version": 2},
        )
        with self.assertRaisesRegex(
            ValueError,
            "requires exact timezone-aware effective_at",
        ):
            book.apply(event)
        self.assertEqual(book.state.symbol, "AAA")
        self.assertEqual(book.instrument_version, first)
        self.assertEqual(book.applied_event_ids, ())

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

    def test_incremental_reverse_date_rejects_but_replay_is_order_independent(self):
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

        registry = InstrumentRegistry(versions=(instrument(),))
        chronological = CorporateActionBook.replay(
            state(),
            instrument_version=instrument(),
            registry=registry,
            events=(earlier, later),
        )
        reversed_input = CorporateActionBook.replay(
            state(),
            instrument_version=instrument(),
            registry=registry,
            events=(later, earlier),
        )
        self.assertEqual(reversed_input.state, chronological.state)
        self.assertEqual(
            reversed_input.applied_event_ids,
            chronological.applied_event_ids,
        )
        self.assertEqual(
            reversed_input.applied_event_ids,
            ("earlier-split", "later-dividend"),
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




class CorporateActionExactArithmeticTests(unittest.TestCase):
    _PRECISIONS = (6, 10, 28, 80)
    _ROUNDINGS = (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN)

    def test_dividend_economics_are_context_invariant(self):
        expected_entitlement = Decimal("12345678902469135780.2469135780123456789")
        expected_unsettled = Decimal("12345678902469135780.24691357801234567891")
        event = corporate_event(
            event_id="exact-dividend",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"per_share": "1.0000000001", "currency": "USD"},
        )

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        book = bound_book(
                            state(
                                quantity="12345678901234567890.123456789",
                                unsettled_cash="0.00000000000000000001",
                            )
                        )
                        result = book.apply(event)
                        self.assertEqual(result.economic_pnl, expected_entitlement)
                        self.assertEqual(result.after.unsettled_cash, expected_unsettled)

    def test_split_quantity_is_context_invariant(self):
        expected = Decimal("18518518351851851835.1851851835")
        event = corporate_event(
            event_id="exact-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": "3", "denominator": "2"},
        )

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        book = bound_book(
                            state(quantity="12345678901234567890.123456789")
                        )
                        result = book.apply(event)
                        self.assertEqual(result.after.quantity, expected)
                        self.assertEqual(result.after.total_basis, Decimal("1000"))

    def test_nonterminating_split_fails_before_book_mutation(self):
        book = bound_book(state(quantity="1"))
        before = book.state
        event = corporate_event(
            event_id="nonterminating-split",
            kind="SPLIT",
            effective_date=date(2026, 1, 2),
            source_revision="r1",
            payload={"numerator": "1", "denominator": "3"},
        )

        with self.assertRaisesRegex(ValueError, "not an exact terminating decimal"):
            book.apply(event)

        self.assertEqual(book.state, before)
        self.assertEqual(book.applied_event_ids, ())

    def test_settlement_helpers_preserve_high_significance_economics(self):
        initial = state(
            quantity="0",
            total_basis="0",
            settled_cash="12345678901234567890.123456789",
            unsettled_cash="0.00000000000000000009",
        )
        expected_settled = Decimal("12345678901234567890.12345678900000000001")
        expected_unsettled = Decimal("0.00000000000000000008")

        for precision in self._PRECISIONS:
            for rounding in self._ROUNDINGS:
                with self.subTest(precision=precision, rounding=rounding):
                    with localcontext() as context:
                        context.prec = precision
                        context.rounding = rounding
                        result = settle_cash(
                            initial,
                            Decimal("0.00000000000000000001"),
                        )
                        self.assertEqual(result.settled_cash, expected_settled)
                        self.assertEqual(result.unsettled_cash, expected_unsettled)

    def test_decimal_subclasses_are_rejected_at_state_boundary(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("virtual Decimal method must not run")

            def as_tuple(self):
                raise AssertionError("virtual Decimal method must not run")

        with self.assertRaisesRegex(TypeError, "exact built-in Decimal"):
            EquityState.create(
                symbol="AAA",
                quantity=HostileDecimal("1"),
                total_basis="1",
                settled_cash="1",
                currency="USD",
            )




class CorporateActionSemanticGraphAuthorityTests(unittest.TestCase):
    def test_event_subclass_is_rejected_before_semantic_attribute_dispatch(self):
        touched = []

        class HostileEvent(CorporateEvent):
            def __getattribute__(self, name):
                if name not in {"__class__"}:
                    touched.append(name)
                    raise AssertionError("hostile event attribute dispatch")
                return super().__getattribute__(name)

        hostile = object.__new__(HostileEvent)
        book = bound_book(state())
        before_state = book.state
        before_events = book.events

        with self.assertRaisesRegex(TypeError, "exact CorporateEvent"):
            book.apply(hostile)

        self.assertEqual(touched, [])
        self.assertEqual(book.state, before_state)
        self.assertEqual(book.events, before_events)

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

        hostile_time = datetime(2026, 1, 2, 12, tzinfo=HostileTz())
        book = bound_book(state())
        before_state = book.state
        before_events = book.events

        with self.assertRaisesRegex(
            ValueError,
            "exact datetime with built-in timezone",
        ):
            corporate_event(
                event_id="hostile-timezone",
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                effective_at=hostile_time,
                source_revision="r1",
                payload={"per_share": "1", "currency": "USD"},
            )

        self.assertEqual(touched, [])
        self.assertEqual(book.state, before_state)
        self.assertEqual(book.events, before_events)

    def test_builtin_fixed_offset_timezone_normalizes_deterministically(self):
        fixed = timezone(timedelta(hours=2))
        event = corporate_event(
            event_id="builtin-timezone",
            kind="CASH_DIVIDEND",
            effective_date=date(2026, 1, 2),
            effective_at=datetime(2026, 1, 2, 12, tzinfo=fixed),
            source_revision="r1",
            payload={"per_share": "1", "currency": "USD"},
        )
        self.assertEqual(event.effective_at.tzinfo, timezone.utc)
        self.assertEqual(event.effective_at.hour, 10)



class CorporateActionBoundedIngressTests(unittest.TestCase):
    def test_at_limit_domain_decimal_is_admitted_exactly(self):
        text = "9" * MAX_INTEGER_DIGITS
        accepted = EquityState.create(
            symbol="AAA",
            quantity=text,
            total_basis="0",
            settled_cash="0",
            currency="USD",
        )
        self.assertEqual(accepted.quantity, Decimal(text))

    def test_one_over_domain_decimal_fails_without_state_change(self):
        initial = state(quantity="0", unsettled_cash="1")
        oversized = "9" * (MAX_INTEGER_DIGITS + 1)

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            settle_cash(initial, oversized)

        self.assertEqual(initial, state(quantity="0", unsettled_cash="1"))

    def test_one_over_domain_integer_fails_without_state_change(self):
        initial = state(quantity="0", unsettled_cash="1")
        oversized = 10 ** MAX_INTEGER_DIGITS

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            settle_cash(initial, oversized)

        self.assertEqual(initial, state(quantity="0", unsettled_cash="1"))

    def test_numeric_subclasses_fail_before_virtual_dispatch(self):
        touched = []

        class HostileNumericText(str):
            def __len__(self):
                touched.append("len")
                raise AssertionError("numeric string subclass dispatched")

        class HostileInt(int):
            def bit_length(self):
                touched.append("bit_length")
                raise AssertionError("integer subclass dispatched")

        for value in (HostileNumericText("1"), HostileInt(1)):
            with self.subTest(kind=type(value).__name__):
                with self.assertRaisesRegex(TypeError, "exact built-in Decimal"):
                    EquityState.create(
                        symbol="AAA",
                        quantity=value,
                        total_basis="0",
                        settled_cash="0",
                        currency="USD",
                    )

        self.assertEqual(touched, [])


if __name__ == "__main__":
    unittest.main()
