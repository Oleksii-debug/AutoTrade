from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentRegistry,
    InstrumentVersion,
)
from mvp.autotrade_mvp.option_lifecycle import (
    OptionLifecycleError,
    OptionLifecycleObservation,
    _bind_version,
)


OPTION_ID = "11111111-1111-1111-1111-111111111111"
UNDERLYING_ID = "22222222-2222-2222-2222-222222222222"
NOW = datetime(2026, 12, 18, 19, tzinfo=timezone.utc)


def option_version(*, quantity_step: str) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=OPTION_ID,
        version=1,
        provider_id="BYBIT",
        venue_id="OPTIONS",
        provider_symbol="ABC-202612-C50",
        asset_class="OPTION",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="contract",
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal(quantity_step),
        minimum_quantity=Decimal(quantity_step),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="ACTIVE",
        payoff="OPTION",
        underlying_id=f"{UNDERLYING_ID}@1",
        expiry=datetime(2026, 12, 18, 21, tzinfo=timezone.utc),
        delivery_cutoff=datetime(2026, 12, 18, 20, tzinfo=timezone.utc),
        settlement_method="PHYSICAL",
        margin_model_id="option-margin-v1",
        strike=Decimal("50"),
        option_right="CALL",
        exercise_style="AMERICAN",
        deliverable=(DeliverableLeg("ABC", Decimal("100")),),
    )


def observation() -> OptionLifecycleObservation:
    return OptionLifecycleObservation(
        provider_id="BYBIT",
        account_id="paper-1",
        environment="PAPER",
        venue_id="OPTIONS",
        instrument_version=f"{OPTION_ID}@1",
        external_event_id="hostile-registry-grid",
        event_kind="EXERCISE",
        signed_contracts=Decimal("0.5"),
        effective_at=NOW,
        observed_at=NOW,
        raw_evidence_digest="sha256:" + "a" * 64,
        provider_revision="provider-r1",
        underlying_price=Decimal("60"),
    )


class OptionLifecycleRegistryAuthorityTests(unittest.TestCase):
    def test_registry_subclass_cannot_manufacture_lifecycle_quantity_grid(self):
        canonical = option_version(quantity_step="1")
        forged = option_version(quantity_step="0.5")
        calls = []

        class HostileRegistry(InstrumentRegistry):
            def exact(self, instrument_version):
                calls.append(("exact", instrument_version))
                return forged

            def at(self, instrument_id, instant):
                calls.append(("at", instrument_id, instant))
                return forged

        with self.assertRaisesRegex(TypeError, "exact InstrumentRegistry"):
            HostileRegistry(versions=(canonical,))
        # Exercise the lifecycle admission fence independently of the earlier
        # canonical registry constructor fence, without invoking virtual methods.
        registry = object.__new__(HostileRegistry)

        with self.assertRaises(TypeError):
            _bind_version(registry, observation())

        self.assertEqual(calls, [])

    def test_exact_registry_instance_shadow_cannot_manufacture_lifecycle_grid(self):
        canonical = option_version(quantity_step="1")
        forged = option_version(quantity_step="0.5")
        registry = InstrumentRegistry(versions=(canonical,))
        calls = []

        registry.exact = (
            lambda instrument_version: calls.append(("exact", instrument_version))
            or forged
        )
        registry.at = (
            lambda instrument_id, instant: calls.append(("at", instrument_id, instant))
            or forged
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "canonical instrument quantity_step",
        ):
            _bind_version(registry, observation())

        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
