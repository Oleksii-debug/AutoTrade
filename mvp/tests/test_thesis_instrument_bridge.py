from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.instruments import InstrumentVersion
from mvp.autotrade_mvp.thesis_implementation import (
    ImplementationPolicy,
    MarketThesis,
    ThesisImplementationError,
    select_implementation,
)
from mvp.autotrade_mvp.thesis_instrument_bridge import (
    candidate_from_instrument_version,
)


AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HORIZON = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class ThesisInstrumentBridgeTests(unittest.TestCase):
    def thesis(self):
        return MarketThesis(
            thesis_id="gold-down-1",
            subject="GOLD",
            direction="SHORT",
            as_of=AS_OF,
            horizon_end=HORIZON,
            required_notional="10000",
            notional_currency="USD",
        )

    def instrument(self, **overrides):
        values = dict(
            instrument_id="11111111-1111-4111-8111-111111111111",
            version=1,
            provider_id="SIMULATED",
            venue_id="SIM",
            provider_symbol="GLD",
            asset_class="FUND",
            base_currency="GLD",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="share",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="SIM-UTC",
            timezone_id="UTC",
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            effective_to=datetime(2026, 12, 31, tzinfo=timezone.utc),
            status="ACTIVE",
        )
        values.update(overrides)
        return InstrumentVersion(**values)

    def candidate(self, instrument=None, **overrides):
        values = dict(
            thesis=self.thesis(),
            instrument=instrument or self.instrument(),
            candidate_id="fund-short",
            exposure_subject="GOLD",
            exposure_direction="SHORT",
            route_id="simulated-fund-route",
            legal=True,
            technically_available=True,
            route_qualified=True,
            fee_rate="0.001",
            spread_rate="0.001",
            holding_cost_rate="0.001",
            leverage_ratio="1",
            liquidation_risk="0",
            liquidity_capacity="50000",
            liquidity_currency="USD",
        )
        values.update(overrides)
        return candidate_from_instrument_version(**values)

    def test_canonical_instrument_identity_is_snapshotted_into_candidate(self):
        candidate = self.candidate()
        self.assertEqual(
            candidate.instrument_version,
            "11111111-1111-4111-8111-111111111111@1",
        )
        self.assertEqual(candidate.provider_id, "SIMULATED")
        self.assertEqual(candidate.asset_class, "FUND")
        self.assertEqual(
            candidate.tradable_until,
            datetime(2026, 12, 31, tzinfo=timezone.utc),
        )

        decision = select_implementation(
            thesis=self.thesis(),
            policy=ImplementationPolicy(
                max_total_cost_rate="0.01",
                max_leverage_ratio="2",
                max_liquidation_risk="0.10",
                min_liquidity_capacity="10000",
                permitted_asset_classes=("FUND",),
            ),
            candidates=(candidate,),
        )
        self.assertEqual(decision.status, "SELECTED")
        self.assertEqual(decision.selected_candidate_id, "fund-short")

    def test_subject_mapping_must_be_explicitly_consistent_with_thesis(self):
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "exposure_subject does not match thesis subject",
        ):
            self.candidate(exposure_subject="OIL")

    def test_inactive_instrument_cannot_enter_candidate_set(self):
        with self.assertRaisesRegex(ThesisImplementationError, "not ACTIVE"):
            self.candidate(instrument=self.instrument(status="INACTIVE"))

    def test_instrument_must_be_effective_at_thesis_cut(self):
        late = self.instrument(
            effective_from=datetime(2026, 10, 5, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "not effective at thesis as_of",
        ):
            self.candidate(instrument=late)

    def test_instrument_terminal_cut_is_forwarded_to_horizon_screening(self):
        short_lived = self.instrument(
            effective_to=datetime(2026, 10, 20, tzinfo=timezone.utc),
        )
        candidate = self.candidate(instrument=short_lived)
        decision = select_implementation(
            thesis=self.thesis(),
            policy=ImplementationPolicy(
                max_total_cost_rate="0.01",
                max_leverage_ratio="2",
                max_liquidation_risk="0.10",
                permitted_asset_classes=("FUND",),
            ),
            candidates=(candidate,),
        )
        self.assertEqual(decision.status, "NO_TRADE")
        self.assertEqual(
            decision.rejected_reasons["fund-short"],
            ("HORIZON_NOT_COVERED",),
        )

    def test_source_instrument_mutation_after_bridge_cannot_retarget_candidate(self):
        instrument = self.instrument()
        candidate = self.candidate(instrument=instrument)
        object.__setattr__(instrument, "provider_id", "FORGED")
        object.__setattr__(instrument, "asset_class", "CRYPTO_SPOT")
        object.__setattr__(instrument, "version", 99)
        self.assertEqual(candidate.provider_id, "SIMULATED")
        self.assertEqual(candidate.asset_class, "FUND")
        self.assertEqual(
            candidate.instrument_version,
            "11111111-1111-4111-8111-111111111111@1",
        )

    def test_mutated_noncanonical_instrument_scalar_fails_before_normalization(self):
        instrument = self.instrument()

        class HostileText(str):
            def strip(self, *args, **kwargs):
                raise AssertionError("hostile strip executed")

            def upper(self):
                raise AssertionError("hostile upper executed")

        object.__setattr__(instrument, "provider_id", HostileText("SIMULATED"))
        with self.assertRaises(TypeError):
            self.candidate(instrument=instrument)

    def test_instrument_subclass_is_not_accepted_as_registry_authority(self):
        class DerivedInstrument(InstrumentVersion):
            pass

        base = self.instrument()
        derived = DerivedInstrument(**{
            "instrument_id": base.instrument_id,
            "version": base.version,
            "provider_id": base.provider_id,
            "venue_id": base.venue_id,
            "provider_symbol": base.provider_symbol,
            "asset_class": base.asset_class,
            "base_currency": base.base_currency,
            "quote_currency": base.quote_currency,
            "settlement_currency": base.settlement_currency,
            "quantity_unit": base.quantity_unit,
            "contract_multiplier": base.contract_multiplier,
            "price_tick": base.price_tick,
            "quantity_step": base.quantity_step,
            "minimum_quantity": base.minimum_quantity,
            "calendar_id": base.calendar_id,
            "timezone_id": base.timezone_id,
            "effective_from": base.effective_from,
            "effective_to": base.effective_to,
            "status": base.status,
        })
        with self.assertRaises(TypeError):
            self.candidate(instrument=derived)


    def test_provider_identity_is_preserved_across_bridge(self):
        candidate = self.candidate(
            instrument=self.instrument(provider_id="Provider-X"),
        )
        self.assertEqual(candidate.provider_id, "Provider-X")

    def test_mutated_whitespace_identity_is_not_laundered_by_bridge(self):
        instrument = self.instrument()
        object.__setattr__(instrument, "provider_id", " SIMULATED")
        with self.assertRaisesRegex(
            ThesisImplementationError,
            "canonical non-empty text",
        ):
            self.candidate(instrument=instrument)

if __name__ == "__main__":
    unittest.main()
