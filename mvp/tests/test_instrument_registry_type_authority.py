from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentRegistry,
    InstrumentRegistryError,
    InstrumentVersion,
    OffsetTransition,
    TradingCalendar,
)


_INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


def _instrument(cls=InstrumentVersion):
    return cls(
        instrument_id=_INSTRUMENT_ID,
        version=1,
        provider_id="WHITEBIT",
        venue_id="WHITEBIT",
        provider_symbol="BTC_USDT",
        asset_class="CRYPTO_SPOT",
        base_currency="BTC",
        quote_currency="USDT",
        settlement_currency="USDT",
        quantity_unit="BTC",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.000001"),
        minimum_quantity=Decimal("0.000001"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _option_instrument() -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=_INSTRUMENT_ID,
        version=1,
        provider_id="BYBIT",
        venue_id="OPTIONS",
        provider_symbol="BTC-20261231-C-50000",
        asset_class="OPTION",
        base_currency="BTC",
        quote_currency="USDT",
        settlement_currency="USDT",
        quantity_unit="contract",
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        payoff="OPTION",
        underlying_id="22222222-2222-4222-8222-222222222222@1",
        expiry=datetime(2026, 12, 31, tzinfo=timezone.utc),
        delivery_cutoff=datetime(2026, 12, 31, 20, tzinfo=timezone.utc),
        settlement_method="PHYSICAL",
        strike=Decimal("50000"),
        option_right="CALL",
        exercise_style="EUROPEAN",
        deliverable=(DeliverableLeg("BTC", Decimal("100")),),
        margin_model_id="option-margin-v1",
    )


class InstrumentRegistryTypeAuthorityTests(unittest.TestCase):
    def test_exact_instrument_version_still_registers(self):
        instrument = _instrument()
        registry = InstrumentRegistry()
        registry.add(instrument)

        published = registry.exact(f"{_INSTRUMENT_ID}@1")
        self.assertEqual(published, instrument)
        self.assertIsNot(published, instrument)


    def test_registry_detaches_exact_version_from_post_registration_mutation(self):
        instrument = _instrument()
        registry = InstrumentRegistry()
        registry.add(instrument)

        published = registry.exact(f"{_INSTRUMENT_ID}@1")
        object.__setattr__(instrument, "quantity_step", Decimal("1"))

        self.assertEqual(published.quantity_step, Decimal("0.000001"))
        self.assertEqual(
            registry.exact(f"{_INSTRUMENT_ID}@1").quantity_step,
            Decimal("0.000001"),
        )

    def test_exact_read_is_detached_from_internal_registry_authority(self):
        instrument = _instrument()
        registry = InstrumentRegistry(versions=(instrument,))

        exposed = registry.exact(f"{_INSTRUMENT_ID}@1")
        object.__setattr__(exposed, "quantity_step", Decimal("1"))

        reread = registry.exact(f"{_INSTRUMENT_ID}@1")
        self.assertEqual(reread.quantity_step, Decimal("0.000001"))
        self.assertIsNot(reread, exposed)

    def test_versions_and_at_reads_cannot_mutate_internal_registry_authority(self):
        instrument = _instrument()
        registry = InstrumentRegistry(versions=(instrument,))

        from_versions = registry.versions(_INSTRUMENT_ID)[0]
        from_at = registry.at(
            _INSTRUMENT_ID,
            datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        object.__setattr__(from_versions, "provider_symbol", "FORGED")
        object.__setattr__(from_at, "quantity_step", Decimal("1"))

        reread = registry.exact(f"{_INSTRUMENT_ID}@1")
        self.assertEqual(reread.provider_symbol, "BTC_USDT")
        self.assertEqual(reread.quantity_step, Decimal("0.000001"))

    def test_option_read_mutation_cannot_change_lifecycle_authority_fields(self):
        registry = InstrumentRegistry(versions=(_option_instrument(),))

        exposed = InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1")
        object.__setattr__(exposed, "quantity_step", Decimal("0.5"))
        object.__setattr__(exposed, "strike", Decimal("1"))
        object.__setattr__(exposed.deliverable[0], "quantity", Decimal("1"))

        reread = InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1")
        self.assertEqual(reread.quantity_step, Decimal("1"))
        self.assertEqual(reread.strike, Decimal("50000"))
        self.assertEqual(reread.deliverable[0].quantity, Decimal("100"))

    def test_caller_written_versions_attribute_cannot_replace_closure_authority(self):
        canonical = _instrument()
        registry = InstrumentRegistry(versions=(canonical,))
        forged = _instrument()
        object.__setattr__(forged, "quantity_step", Decimal("1"))

        registry._versions = {_INSTRUMENT_ID: [forged]}

        reread = InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1")
        self.assertEqual(reread.quantity_step, Decimal("0.000001"))

    def test_instance_shadowed_internal_helpers_do_not_enter_add_authority(self):
        canonical = _instrument()
        registry = InstrumentRegistry()
        calls = []

        registry._candidate_state = lambda candidate: calls.append("candidate") or {}
        registry._intervals = lambda state: calls.append("intervals") or ()
        registry._overlap = lambda *args: calls.append("overlap") or False

        InstrumentRegistry.add(registry, canonical)

        self.assertEqual(calls, [])
        reread = InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1")
        self.assertEqual(reread, canonical)

    def test_calendar_is_detached_from_post_registration_mutation(self):
        calendar = TradingCalendar(
            calendar_id="CUSTOM",
            timezone_id="UTC",
            continuous=True,
            transitions=(
                OffsetTransition(
                    datetime(1970, 1, 1, tzinfo=timezone.utc),
                    0,
                ),
            ),
        )
        registry = InstrumentRegistry(calendars=(calendar,))
        object.__setattr__(calendar, "timezone_id", "FORGED")

        instrument = _instrument()
        object.__setattr__(instrument, "calendar_id", "CUSTOM")
        InstrumentRegistry.add(registry, instrument)

        self.assertEqual(
            InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1").timezone_id,
            "UTC",
        )

    def test_caller_written_calendars_attribute_cannot_replace_closure_authority(self):
        registry = InstrumentRegistry()
        forged = TradingCalendar.continuous_24_7()
        object.__setattr__(forged, "timezone_id", "FORGED")
        registry._calendars = {"CONTINUOUS_24_7": forged}

        InstrumentRegistry.add(registry, _instrument())

        self.assertEqual(
            InstrumentRegistry.exact(registry, f"{_INSTRUMENT_ID}@1").timezone_id,
            "UTC",
        )

    def test_calendar_subclass_is_rejected_before_authority_field_reads(self):
        calls = []

        class HostileCalendar(TradingCalendar):
            def __getattribute__(self, name):
                if name == "_armed":
                    return object.__getattribute__(self, name)
                armed = object.__getattribute__(self, "__dict__").get("_armed", False)
                if armed and name in {"calendar_id", "timezone_id", "continuous"}:
                    calls.append(name)
                    raise AssertionError(f"virtual calendar read: {name}")
                return super().__getattribute__(name)

        hostile = HostileCalendar(
            calendar_id="HOSTILE",
            timezone_id="UTC",
            continuous=True,
            transitions=(
                OffsetTransition(
                    datetime(1970, 1, 1, tzinfo=timezone.utc),
                    0,
                ),
            ),
        )
        object.__setattr__(hostile, "_armed", True)

        registry = InstrumentRegistry()
        with self.assertRaisesRegex(TypeError, "exact TradingCalendar"):
            InstrumentRegistry.add_calendar(registry, hostile)

        self.assertEqual(calls, [])

    def test_reinitialization_cannot_replace_existing_registry_authority(self):
        registry = InstrumentRegistry(versions=(_instrument(),))

        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "version authority is already established",
        ):
            InstrumentRegistry.__init__(registry)

        self.assertEqual(
            InstrumentRegistry.exact(
                registry,
                f"{_INSTRUMENT_ID}@1",
            ).provider_symbol,
            "BTC_USDT",
        )

    def test_registry_rejects_mutated_decimal_subclass_before_virtual_dispatch(self):
        calls = []

        class HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("virtual decimal read")

        instrument = _instrument()
        object.__setattr__(
            instrument,
            "quantity_step",
            HostileDecimal("0.000001"),
        )

        registry = InstrumentRegistry()
        with self.assertRaisesRegex(TypeError, "quantity_step must be exact Decimal"):
            registry.add(instrument)

        self.assertEqual(calls, [])
        self.assertEqual(registry.versions(_INSTRUMENT_ID), ())

    def test_registry_rejects_mutated_string_subclass_before_virtual_dispatch(self):
        calls = []

        class HostileString(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("virtual string read")

        instrument = _instrument()
        object.__setattr__(instrument, "provider_symbol", HostileString("BTC_USDT"))

        registry = InstrumentRegistry()
        with self.assertRaisesRegex(TypeError, "provider_symbol must be exact str"):
            registry.add(instrument)

        self.assertEqual(calls, [])
        self.assertEqual(registry.versions(_INSTRUMENT_ID), ())

    def test_instrument_rejects_polymorphic_deliverable_before_field_reads(self):
        calls = []

        class HostileDeliverableLeg(DeliverableLeg):
            def __getattribute__(self, name):
                if name == "_armed":
                    return object.__getattribute__(self, name)
                armed = object.__getattribute__(self, "__dict__").get("_armed", False)
                if armed and name in {"asset_id", "quantity"}:
                    calls.append(name)
                    raise AssertionError(f"virtual deliverable read: {name}")
                return super().__getattribute__(name)

        hostile_leg = HostileDeliverableLeg("BTC", Decimal("1"))
        object.__setattr__(hostile_leg, "_armed", True)

        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "exact DeliverableLeg",
        ):
            InstrumentVersion(
                instrument_id=_INSTRUMENT_ID,
                version=1,
                provider_id="WHITEBIT",
                venue_id="WHITEBIT",
                provider_symbol="BTC-20261231-C-50000",
                asset_class="OPTION",
                base_currency="BTC",
                quote_currency="USDT",
                settlement_currency="USDT",
                quantity_unit="contract",
                contract_multiplier=Decimal("1"),
                price_tick=Decimal("0.01"),
                quantity_step=Decimal("1"),
                minimum_quantity=Decimal("1"),
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
                payoff="OPTION",
                underlying_id="22222222-2222-4222-8222-222222222222@1",
                expiry=datetime(2026, 12, 31, tzinfo=timezone.utc),
                settlement_method="PHYSICAL",
                strike=Decimal("50000"),
                option_right="CALL",
                exercise_style="EUROPEAN",
                deliverable=(hostile_leg,),
                margin_model_id="option-margin-v1",
            )
        self.assertEqual(calls, [])

    def test_subclass_is_rejected_before_authority_field_reads(self):
        calls = []

        class HostileInstrumentVersion(InstrumentVersion):
            def __getattribute__(self, name):
                if name == "_armed":
                    return object.__getattribute__(self, name)
                armed = object.__getattribute__(self, "__dict__").get("_armed", False)
                if armed and name in {
                    "instrument_id",
                    "version",
                    "provider_id",
                    "venue_id",
                    "provider_symbol",
                    "calendar_id",
                    "timezone_id",
                    "effective_from",
                    "effective_to",
                }:
                    calls.append(name)
                    raise AssertionError(f"virtual authority read: {name}")
                return super().__getattribute__(name)

        hostile = _instrument(HostileInstrumentVersion)
        object.__setattr__(hostile, "_armed", True)

        registry = InstrumentRegistry()
        with self.assertRaisesRegex(TypeError, "exact InstrumentVersion"):
            registry.add(hostile)
        self.assertEqual(calls, [])
        self.assertEqual(registry.versions(_INSTRUMENT_ID), ())

    def test_bulk_constructor_uses_same_exact_type_gate(self):
        calls = []

        class HostileInstrumentVersion(InstrumentVersion):
            def __getattribute__(self, name):
                if name == "_armed":
                    return object.__getattribute__(self, name)
                armed = object.__getattribute__(self, "__dict__").get("_armed", False)
                if armed and name in {
                    "instrument_id",
                    "version",
                    "calendar_id",
                    "timezone_id",
                }:
                    calls.append(name)
                    raise AssertionError(f"virtual authority read: {name}")
                return super().__getattribute__(name)

        hostile = _instrument(HostileInstrumentVersion)
        object.__setattr__(hostile, "_armed", True)

        with self.assertRaisesRegex(TypeError, "exact InstrumentVersion"):
            InstrumentRegistry(versions=(hostile,))
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
