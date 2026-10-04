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


def caller_future() -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id="12121212-1212-4212-8212-121212121212",
        version=1,
        provider_id="CALLER_CLEARER",
        venue_id="CALLER_VENUE",
        provider_symbol="CALLER-FUT-202609",
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
        underlying_id="34343434-3434-4434-8434-343434343434@1",
        expiry=utc(30, 21),
        last_trade_at=utc(30, 20),
        delivery_cutoff=utc(29, 12),
        settlement_method="CASH",
        margin_model_id="CALLER_MARGIN_V1",
    )


class FuturesSelectedRegistryAuthorityTests(unittest.TestCase):
    def test_caller_cannot_mint_both_version_and_registry_to_authorize_exposure(self):
        """Moving selection from a value to a caller-created registry is not provenance."""

        version = caller_future()
        caller_registry = InstrumentRegistry(versions=(version,))
        selected = InstrumentRegistry.exact(
            caller_registry,
            f"{version.instrument_id}@{version.version}",
        )
        contract = FuturesContract.from_instrument_version(selected)

        # An exact registry proves internal consistency of that registry, but a
        # caller can construct the registry itself. New-exposure authority must
        # therefore come from the product-selected registry/composition rather
        # than whichever exact InstrumentRegistry the caller supplies here.
        with self.assertRaisesRegex(
            FuturesError,
            "selected|authority|canonical|composition",
        ):
            require_open_for_new_exposure(
                contract,
                utc(29, 11),
                instrument_registry=caller_registry,
            )


if __name__ == "__main__":
    unittest.main()
