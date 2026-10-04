from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.corporate_action_accounting import _exact_utc_instant
from mvp.autotrade_mvp.corporate_actions import (
    CorporateActionBook,
    CorporateActionCheckpoint,
    CorporateEvent,
    EquityState,
    Transition,
    accrue_borrow_financing,
    settle_cash,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


class CorporateActionIngressTrustTests(unittest.TestCase):
    def _event(self, **overrides):
        values = {
            "event_id": "event-1",
            "instrument_id": "instrument-1",
            "instrument_version": 1,
            "kind": "CASH_DIVIDEND",
            "effective_date": date(2026, 1, 2),
            "source_revision": "revision-1",
            "payload": {"per_share": "1", "currency": "USD"},
            "source_sequence": 0,
        }
        values.update(overrides)
        return CorporateEvent.create(**values)

    def _state(self):
        return EquityState.create(
            symbol="AAA",
            quantity="0",
            total_basis="0",
            settled_cash="0",
            currency="USD",
        )

    def _instrument(self):
        return InstrumentVersion(
            instrument_id=INSTRUMENT_ID,
            version=1,
            provider_id="simulated",
            venue_id="simulated-venue",
            provider_symbol="AAA",
            asset_class="CASH_EQUITY",
            base_currency="AAA",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="AAA",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            status="ACTIVE",
        )

    def test_payload_mapping_subclass_is_rejected_before_items_dispatch(self):
        class HostileDict(dict):
            def items(self):
                raise AssertionError("hostile mapping items dispatch")

        payload = HostileDict(per_share="1", currency="USD")
        with self.assertRaisesRegex(TypeError, "exact dict"):
            self._event(payload=payload)

        with self.assertRaisesRegex(TypeError, "exact dict"):
            CorporateEvent(
                event_id="event-direct",
                instrument_id="instrument-1",
                instrument_version=1,
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="revision-1",
                payload=payload,
                source_sequence=0,
            )

    def test_event_payload_is_detached_and_immutable_after_acceptance(self):
        payload = {"per_share": "1", "currency": "USD"}
        event = self._event(payload=payload)
        payload["per_share"] = "999"
        self.assertEqual(event.payload["per_share"], "1")
        with self.assertRaises(TypeError):
            event.payload["per_share"] = "2"

    def test_integer_subclasses_are_rejected_before_comparison_dispatch(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile integer comparison")

            def __str__(self):
                raise AssertionError("hostile integer stringification")

        hostile_one = HostileInt(1)
        with self.assertRaisesRegex(ValueError, "instrument_version"):
            self._event(instrument_version=hostile_one)

        with self.assertRaisesRegex(ValueError, "source_sequence"):
            self._event(source_sequence=hostile_one)

    def test_numeric_payload_is_bounded_before_stringification(self):
        oversized_decimal = Decimal("1e100000")
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            self._event(
                payload={"per_share": oversized_decimal, "currency": "USD"},
            )

        with self.assertRaisesRegex(ValueError, "resource envelope"):
            CorporateEvent(
                event_id="event-direct",
                instrument_id="instrument-1",
                instrument_version=1,
                kind="CASH_DIVIDEND",
                effective_date=date(2026, 1, 2),
                source_revision="revision-1",
                payload={"per_share": oversized_decimal, "currency": "USD"},
                source_sequence=0,
            )

        oversized_int = 10 ** 5000
        with self.assertRaisesRegex(ValueError, "resource envelope"):
            self._event(
                payload={"per_share": oversized_int, "currency": "USD"},
            )

    def test_financing_days_reject_integer_subclasses(self):
        class HostileInt(int):
            def __lt__(self, other):
                raise AssertionError("hostile integer comparison")

        state = self._state()
        with self.assertRaisesRegex(ValueError, "days"):
            accrue_borrow_financing(
                state,
                daily_rate="0",
                marked_value="0",
                days=HostileInt(1),
            )

    def test_transition_revalidates_nested_financial_values(self):
        base_state = self._state()

        class StateSubclass(EquityState):
            pass

        derived_state = StateSubclass(**vars(base_state))
        with self.assertRaisesRegex(TypeError, "transition before"):
            Transition(
                event_id="event-1",
                before=derived_state,
                after=base_state,
                economic_pnl=Decimal("0"),
                reason="test",
            )

        class DecimalSubclass(Decimal):
            pass

        with self.assertRaisesRegex(TypeError, "economic_pnl"):
            Transition(
                event_id="event-1",
                before=base_state,
                after=base_state,
                economic_pnl=DecimalSubclass("0"),
                reason="test",
            )

    def test_checkpoint_rejects_polymorphic_or_mutable_nested_authority(self):
        base_state = self._state()
        current = self._instrument()

        class StateSubclass(EquityState):
            pass

        derived_state = StateSubclass(**vars(base_state))
        with self.assertRaisesRegex(TypeError, "checkpoint state"):
            CorporateActionCheckpoint(
                checkpoint_id="checkpoint-state-subclass",
                state=derived_state,
                instrument_version=current,
                records=(),
            )

        class InstrumentVersionSubclass(InstrumentVersion):
            pass

        derived_version = InstrumentVersionSubclass(**vars(current))
        with self.assertRaisesRegex(TypeError, "checkpoint instrument_version"):
            CorporateActionCheckpoint(
                checkpoint_id="checkpoint-version-subclass",
                state=base_state,
                instrument_version=derived_version,
                records=(),
            )

        with self.assertRaisesRegex(TypeError, "checkpoint records"):
            CorporateActionCheckpoint(
                checkpoint_id="checkpoint-mutable-records",
                state=base_state,
                instrument_version=current,
                records=[],
            )

    def test_checkpoint_rejects_transition_subclass(self):
        base_state = self._state()
        current = self._instrument()
        event = self._event()
        transition = Transition(
            event_id=event.event_id,
            before=base_state,
            after=base_state,
            economic_pnl=Decimal("0"),
            reason="no-op test transition",
        )

        class TransitionSubclass(Transition):
            pass

        derived_transition = TransitionSubclass(**vars(transition))
        with self.assertRaisesRegex(TypeError, "CorporateEvent/Transition"):
            CorporateActionCheckpoint(
                checkpoint_id="checkpoint-transition-subclass",
                state=base_state,
                instrument_version=current,
                records=((event, derived_transition),),
            )

    def test_book_rejects_polymorphic_authority_before_semantic_reads(self):
        base_state = self._state()
        current = self._instrument()
        registry = InstrumentRegistry(versions=(current,))

        class HostileState(EquityState):
            def __getattribute__(self, name):
                if name == "symbol":
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError("hostile state semantic read")
                return super().__getattribute__(name)

        hostile_state = HostileState(**vars(base_state))
        object.__setattr__(hostile_state, "_armed", True)
        with self.assertRaisesRegex(TypeError, "state"):
            CorporateActionBook(
                hostile_state,
                instrument_version=current,
                registry=registry,
            )

        class InstrumentVersionSubclass(InstrumentVersion):
            pass

        derived_version = InstrumentVersionSubclass(**vars(current))
        with self.assertRaisesRegex(TypeError, "instrument_version"):
            CorporateActionBook(
                base_state,
                instrument_version=derived_version,
                registry=registry,
            )

        class HostileRegistry(InstrumentRegistry):
            def versions(self, instrument_id):
                raise AssertionError("hostile registry dispatch")

        hostile_registry = object.__new__(HostileRegistry)
        with self.assertRaisesRegex(TypeError, "registry"):
            CorporateActionBook(
                base_state,
                instrument_version=current,
                registry=hostile_registry,
            )

    def test_cash_helper_rejects_state_subclass_before_semantic_read(self):
        base_state = self._state()

        class HostileState(EquityState):
            def __getattribute__(self, name):
                if name == "unsettled_cash":
                    try:
                        armed = object.__getattribute__(self, "_armed")
                    except AttributeError:
                        armed = False
                    if armed:
                        raise AssertionError("hostile cash state read")
                return super().__getattribute__(name)

        hostile_state = HostileState(**vars(base_state))
        object.__setattr__(hostile_state, "_armed", True)
        with self.assertRaisesRegex(TypeError, "state"):
            settle_cash(hostile_state, "0")

    def test_symbol_successor_rejects_huge_text_without_integer_conversion(self):
        current = self._instrument()
        registry = InstrumentRegistry(versions=(current,))
        book = CorporateActionBook(
            self._state(),
            instrument_version=current,
            registry=registry,
        )
        event = CorporateEvent.create(
            event_id="symbol-huge-version",
            instrument_id=current.instrument_id,
            instrument_version=current.version,
            kind="SYMBOL_CHANGE",
            effective_date=date(2026, 1, 2),
            effective_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            source_revision="revision-symbol-huge",
            payload={"successor_instrument_version": "9" * 5000},
        )
        with self.assertRaisesRegex(ValueError, "next instrument version"):
            book.apply(event)

    def test_activation_instants_reject_datetime_subclasses_before_dispatch(self):
        class HostileDatetime(datetime):
            @property
            def tzinfo(self):
                raise AssertionError("hostile datetime tzinfo dispatch")

            def astimezone(self, tz=None):
                raise AssertionError("hostile datetime astimezone dispatch")

        hostile = HostileDatetime(2026, 1, 2, tzinfo=timezone.utc)
        with self.assertRaisesRegex(TypeError, "exact datetime"):
            _exact_utc_instant(hostile, name="activation_at")

    def test_activation_instants_reject_custom_tzinfo_before_callbacks(self):
        class HostileTimezone(tzinfo):
            def utcoffset(self, dt):
                raise AssertionError("hostile tzinfo utcoffset dispatch")

            def dst(self, dt):
                raise AssertionError("hostile tzinfo dst dispatch")

        hostile_tz = HostileTimezone()
        value = datetime(2026, 1, 2, tzinfo=hostile_tz)
        with self.assertRaisesRegex(TypeError, "fixed built-in timezone"):
            _exact_utc_instant(value, name="activation_cut")

    def test_activation_instants_preserve_fixed_builtin_timezone_support(self):
        fixed = timezone(timedelta(hours=2))
        value = datetime(2026, 1, 2, 2, 30, tzinfo=fixed)
        self.assertEqual(
            _exact_utc_instant(value, name="activation_at"),
            datetime(2026, 1, 2, 0, 30, tzinfo=timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
