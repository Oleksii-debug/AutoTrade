from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesError,
    lifecycle_gate,
    require_open_for_new_exposure,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion


def utc(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


class FuturesLifecycleRegistryAuthorityTests(unittest.TestCase):
    def test_caller_authored_exact_instrument_version_cannot_authorize_new_exposure_without_registry_selection(self):
        """Exact type/content is not proof that the selected registry authorized it."""

        caller_authored = InstrumentVersion(
            instrument_id="33333333-3333-4333-8333-333333333333",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-CALLER-AUTHORED-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        contract = FuturesContract.from_instrument_version(caller_authored)

        # A correct repair may fail closed explicitly (FuturesError) or make a
        # selected registry/authority a required argument (TypeError here).
        # What must never happen is the current silent authorization of OPEN.
        with self.assertRaises((FuturesError, TypeError)):
            require_open_for_new_exposure(contract, utc(29, 11))

    def test_caller_constructed_registry_is_diagnostic_not_new_exposure_authority(self):
        version = InstrumentVersion(
            instrument_id="44444444-4444-4444-8444-444444444444",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-REGISTRY-SELECTED-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        registry = InstrumentRegistry(versions=(version,))
        selected = InstrumentRegistry.exact(
            registry,
            f"{version.instrument_id}@{version.version}",
        )
        contract = FuturesContract.from_instrument_version(selected)

        self.assertEqual(
            lifecycle_gate(
                contract,
                utc(29, 11),
                instrument_registry=registry,
            ),
            "OPEN",
        )
        with self.assertRaisesRegex(
            FuturesError,
            "product-selected.*composition authority",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=registry,
            )

    def test_superseded_registry_version_cannot_authorize_new_exposure(self):
        instrument_id = "77777777-7777-4777-8777-777777777777"
        version_1 = InstrumentVersion(
            instrument_id=instrument_id,
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-EFFECTIVE-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="abababab-abab-4bab-8bab-abababababab@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        version_2 = InstrumentVersion(
            instrument_id=instrument_id,
            version=2,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-EFFECTIVE-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(20),
            payoff="LINEAR",
            underlying_id="abababab-abab-4bab-8bab-abababababab@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        registry = InstrumentRegistry(versions=(version_1, version_2))
        selected_old = InstrumentRegistry.exact(registry, f"{instrument_id}@1")
        contract = FuturesContract.from_instrument_version(selected_old)

        with self.assertRaisesRegex(
            FuturesError,
            "not effective at requested instant",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=registry,
            )

    def test_same_registry_identity_cannot_hide_forged_futures_economics(self):
        instrument_id = "88888888-8888-4888-8888-888888888888"
        canonical = InstrumentVersion(
            instrument_id=instrument_id,
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-ECONOMICS-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        forged = InstrumentVersion(
            instrument_id=instrument_id,
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-ECONOMICS-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("999"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        registry = InstrumentRegistry(versions=(canonical,))
        contract = FuturesContract.from_instrument_version(forged)

        with self.assertRaisesRegex(
            FuturesError,
            "differs from canonical registry selection",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=registry,
            )

    def test_inactive_registry_version_cannot_authorize_new_exposure(self):
        version = InstrumentVersion(
            instrument_id="99999999-9999-4999-8999-999999999999",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-INACTIVE-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            status="INACTIVE",
            payoff="LINEAR",
            underlying_id="dededede-dede-4ede-8ede-dededededede@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        registry = InstrumentRegistry(versions=(version,))
        selected = InstrumentRegistry.exact(registry, f"{version.instrument_id}@1")
        contract = FuturesContract.from_instrument_version(selected)

        with self.assertRaisesRegex(FuturesError, "INSTRUMENT_INACTIVE"):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=registry,
            )

    def test_wrong_registry_cannot_authorize_bound_contract(self):
        version = InstrumentVersion(
            instrument_id="55555555-5555-4555-8555-555555555555",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-WRONG-REGISTRY-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        selected_registry = InstrumentRegistry(versions=(version,))
        selected = InstrumentRegistry.exact(
            selected_registry,
            f"{version.instrument_id}@{version.version}",
        )
        contract = FuturesContract.from_instrument_version(selected)
        wrong_registry = InstrumentRegistry()

        with self.assertRaisesRegex(
            FuturesError,
            "not selected by canonical InstrumentRegistry",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=wrong_registry,
            )

    def test_registry_subclass_is_not_lifecycle_authority(self):
        class DerivedRegistry(InstrumentRegistry):
            pass

        version = InstrumentVersion(
            instrument_id="66666666-6666-4666-8666-666666666666",
            version=1,
            provider_id="TEST_CLEARER",
            venue_id="TEST_VENUE",
            provider_symbol="FUT-DERIVED-REGISTRY-202609",
            asset_class="FUTURE",
            base_currency="TEST",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="CONTRACT",
            contract_multiplier=Decimal("10"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=utc(1),
            payoff="LINEAR",
            underlying_id="ffffffff-ffff-4fff-8fff-ffffffffffff@1",
            expiry=utc(30, 21),
            last_trade_at=utc(30, 20),
            delivery_cutoff=utc(29, 12),
            settlement_method="CASH",
            margin_model_id="TEST_FUTURES_MARGIN_V1",
        )
        registry = InstrumentRegistry(versions=(version,))
        selected = InstrumentRegistry.exact(
            registry,
            f"{version.instrument_id}@{version.version}",
        )
        contract = FuturesContract.from_instrument_version(selected)
        hostile_registry = object.__new__(DerivedRegistry)

        with self.assertRaisesRegex(
            FuturesError,
            "requires exact canonical InstrumentRegistry",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=hostile_registry,
            )


if __name__ == "__main__":
    unittest.main()
