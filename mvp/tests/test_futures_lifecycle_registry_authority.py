from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesError,
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

    def test_selected_registry_version_authorizes_normal_open_path(self):
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
