from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentRegistry,
    InstrumentVersion,
)
from mvp.autotrade_mvp.option_lifecycle import (
    DurableOptionLifecycleAuthority,
    OptionLifecycleError,
    OptionLifecycleObservation,
    _bind_version,
)
from mvp.autotrade_mvp.persistence import payload_digest


OPTION_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNDERLYING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NOW = datetime(2026, 12, 18, 19, tzinfo=timezone.utc)


def option_version(
    *,
    version: int = 1,
    quantity_step: str = "1",
    quantity_unit: str = "contract",
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=OPTION_ID,
        version=version,
        provider_id="BYBIT",
        venue_id="OPTIONS",
        provider_symbol=f"ABC-202612-C50-v{version}",
        asset_class="OPTION",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit=quantity_unit,
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


def observation(
    signed_contracts: str,
    *,
    instrument_version: str = f"{OPTION_ID}@1",
    event_kind: str = "EXERCISE",
) -> OptionLifecycleObservation:
    return OptionLifecycleObservation(
        provider_id="BYBIT",
        account_id="paper-1",
        environment="PAPER",
        provider_environment="TESTNET",
        venue_id="OPTIONS",
        instrument_version=instrument_version,
        external_event_id=f"life-{event_kind.lower()}-{signed_contracts}",
        event_kind=event_kind,
        signed_contracts=Decimal(signed_contracts),
        effective_at=NOW,
        observed_at=NOW,
        raw_evidence_digest="sha256:" + "a" * 64,
        provider_revision="r1",
    )


class OptionLifecycleQuantityAuthorityTests(unittest.TestCase):
    def test_fractional_contract_off_canonical_grid_is_rejected(self):
        registry = InstrumentRegistry(versions=(option_version(quantity_step="1"),))
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "canonical instrument quantity grid",
        ):
            _bind_version(registry, observation("0.5"))

    def test_off_grid_apply_fails_before_durable_or_economic_mutation_paths(self):
        registry = InstrumentRegistry(versions=(option_version(quantity_step="1"),))
        authority = object.__new__(DurableOptionLifecycleAuthority)
        authority.registry = registry
        authority.economic_book = SimpleNamespace(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            provider_environment="TESTNET",
        )
        authority._observation_from_evidence = Mock(
            return_value=(observation("0.5"), object())
        )
        authority._events = Mock(
            side_effect=AssertionError("durable events must not be read for off-grid quantity")
        )

        with self.assertRaisesRegex(OptionLifecycleError, "canonical instrument quantity grid"):
            authority.apply("provider-read:sha256:" + "0" * 64)
        authority._events.assert_not_called()

    def test_positive_and_negative_aligned_contract_counts_are_accepted(self):
        version = option_version(quantity_step="1")
        registry = InstrumentRegistry(versions=(version,))
        self.assertEqual(_bind_version(registry, observation("2")), version)
        self.assertEqual(
            _bind_version(
                registry,
                observation("-2", event_kind="ASSIGNMENT"),
            ),
            version,
        )

    def test_explicit_smaller_canonical_step_is_honored_without_integer_assumption(self):
        version = option_version(quantity_step="0.25")
        registry = InstrumentRegistry(versions=(version,))
        self.assertEqual(_bind_version(registry, observation("0.5")), version)
        with self.assertRaisesRegex(OptionLifecycleError, "canonical instrument quantity grid"):
            _bind_version(registry, observation("0.3"))

    def test_grid_verdict_is_invariant_to_hostile_decimal_context(self):
        version = option_version(quantity_step="0.000000000000000001")
        registry = InstrumentRegistry(versions=(version,))
        aligned = observation("1.234567890123456789")
        off_grid = observation("1.2345678901234567895")

        for precision, rounding in (
            (6, ROUND_FLOOR),
            (10, ROUND_CEILING),
            (28, ROUND_FLOOR),
            (80, ROUND_CEILING),
        ):
            with self.subTest(precision=precision, rounding=rounding):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    self.assertEqual(_bind_version(registry, aligned), version)
                    with self.assertRaisesRegex(OptionLifecycleError, "canonical instrument quantity grid"):
                        _bind_version(registry, off_grid)

    def test_instrument_digest_distinguishes_quantity_unit_and_version(self):
        contracts = option_version(version=1, quantity_unit="contract")
        lots = option_version(version=2, quantity_unit="lot")
        self.assertNotEqual(
            payload_digest(contracts.to_contract_dict()),
            payload_digest(lots.to_contract_dict()),
        )


if __name__ == "__main__":
    unittest.main()
