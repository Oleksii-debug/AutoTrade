from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.futures import (
    FuturesContract,
    FuturesError,
    require_open_for_new_exposure,
)
from mvp.autotrade_mvp.instruments import InstrumentVersion


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


if __name__ == "__main__":
    unittest.main()
