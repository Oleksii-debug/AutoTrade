from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentRegistryError,
    InstrumentVersion,
)
from mvp.autotrade_mvp.option_lifecycle import _contract_from_version


UTC = timezone.utc
OPTION_ID = "11111111-1111-4111-8111-111111111111"
UNDERLYING_ID = "22222222-2222-4222-8222-222222222222"


def _option(
    *,
    settlement_method: str = "PHYSICAL",
    deliverable_quantity: str = "150",
    exercise_cash_per_contract: Decimal | None = Decimal("4750.25"),
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=OPTION_ID,
        version=3,
        provider_id="TEST",
        venue_id="OPTIONS",
        provider_symbol="ADJ-CALL",
        asset_class="OPTION",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="contract",
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="TEST_CAL",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        payoff="OPTION",
        underlying_id=f"{UNDERLYING_ID}@1",
        expiry=datetime(2026, 12, 18, 21, tzinfo=UTC),
        delivery_cutoff=datetime(2026, 12, 18, 20, tzinfo=UTC),
        settlement_method=settlement_method,
        strike=Decimal("50"),
        option_right="CALL",
        exercise_style="AMERICAN",
        deliverable=(DeliverableLeg("ABC", Decimal(deliverable_quantity)),),
        exercise_cash_per_contract=exercise_cash_per_contract,
        margin_model_id="option-margin-v1",
    )


class Section5AdjustedOptionContractTests(unittest.TestCase):
    def test_adjusted_physical_option_requires_explicit_exercise_cash(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "adjusted physical option requires explicit exercise_cash_per_contract",
        ):
            _option(exercise_cash_per_contract=None)

    def test_explicit_adjusted_exercise_cash_is_versioned_and_consumed(self):
        version = _option()
        self.assertEqual(
            version.to_contract_dict()["exercise_cash_per_contract"],
            "4750.25",
        )
        contract = _contract_from_version(version)
        self.assertEqual(contract.exercise_cash_per_contract, Decimal("4750.25"))
        self.assertEqual(contract.deliverable[0].quantity_per_contract, Decimal("150"))

    def test_standard_physical_contract_keeps_deterministic_legacy_derivation(self):
        version = _option(
            deliverable_quantity="100",
            exercise_cash_per_contract=None,
        )
        contract = _contract_from_version(version)
        self.assertEqual(contract.exercise_cash_per_contract, Decimal("5000"))

    def test_cash_settled_option_rejects_physical_exercise_cash(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "cash-settled option cannot carry physical exercise cash",
        ):
            _option(
                settlement_method="CASH",
                exercise_cash_per_contract=Decimal("1"),
            )

    def test_exercise_cash_is_option_only(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "option-only fields are not valid",
        ):
            InstrumentVersion(
                instrument_id="33333333-3333-4333-8333-333333333333",
                version=1,
                provider_id="TEST",
                venue_id="SPOT",
                provider_symbol="ABCUSD",
                asset_class="CRYPTO_SPOT",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="ABC",
                contract_multiplier=Decimal("1"),
                price_tick=Decimal("0.01"),
                quantity_step=Decimal("0.001"),
                minimum_quantity=Decimal("0.001"),
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                exercise_cash_per_contract=Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()
