from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest
from uuid import UUID

from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentVersion,
    TradingCalendar,
)
from autotrade_research.strategies.deterministic import (
    CausalObservation,
    ReturnThresholdBaseline,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    run_baseline,
)
from autotrade_research.strategies.economics_authority import (
    StrategyEconomicsAuthority,
    StrategyEconomicsAuthorityError,
    assess_strategy_economics_authority,
    require_strategy_economics_authority,
    terminal_decision_proposal,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
INSTRUMENT_ID = str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
INSTRUMENT_VERSION = f"{INSTRUMENT_ID}@1"


def descriptor() -> StrategyDescriptor:
    return StrategyDescriptor(
        strategy_id="return-threshold-baseline",
        version=1,
        family="DETERMINISTIC_RETURN_THRESHOLD",
        feature_schema="price-only-v1",
        market_requirements=("CAUSAL_PRICE",),
        minimum_history=2,
        horizon_seconds=3600,
        decision_schedule="ON_REGISTERED_CUTOFF",
        proposal_semantics="BUY_SELL_HOLD_RESEARCH_PROPOSAL",
        parameter_bounds=(
            ("threshold", "0", "0.10"),
            ("proposal_quantity", "0.0001", "100"),
        ),
        resource_profile="CPU_LIGHT_ZERO_MODEL",
        supported_regimes=("UNSPECIFIED",),
        source_license="FIRST_PARTY",
        evaluation_protocol_sha256="sha256:" + "a" * 64,
        artifact_sha256="sha256:" + "b" * 64,
    )


def proposal():
    strategy = ReturnThresholdBaseline(
        lookback=2,
        threshold="0.01",
        proposal_quantity="2",
        descriptor=descriptor(),
    )
    observations = (
        CausalObservation.create(
            event_id="event-0",
            symbol="AAA",
            available_at=BASE,
            price="100",
        ),
        CausalObservation.create(
            event_id="event-1",
            symbol="AAA",
            available_at=BASE + timedelta(minutes=1),
            price="102",
        ),
    )
    return run_baseline(
        strategy,
        observations,
        decision_time=BASE + timedelta(minutes=1),
        symbol="AAA",
    )


def economics_binding(item, **overrides) -> StrategyEconomicsBinding:
    values = dict(
        strategy_fingerprint=item.strategy_fingerprint,
        strategy_configuration_fingerprint=(
            item.strategy_configuration_fingerprint
        ),
        instrument_version=INSTRUMENT_VERSION,
        information_cutoff=item.information_cutoff,
        decision_time=item.decision_time,
        horizon_seconds=item.horizon_seconds,
        expiry=item.expiry,
        available_at=item.information_cutoff,
        input_manifest_refs=("sha256:" + "c" * 64,),
        gross_return_distribution_sha256="sha256:" + "d" * 64,
        after_cost_return_distribution_sha256="sha256:" + "e" * 64,
        after_cost_lower_bound="0.01",
        execution_model_fingerprint="sha256:" + "f" * 64,
        execution_calibration_sha256="sha256:" + "1" * 64,
        execution_fidelity="FROZEN_EX_ANTE",
        capacity_assessment_sha256="sha256:" + "2" * 64,
        max_feasible_quantity="2",
        lot_size="1",
        required_evidence_dimensions=("FX",),
        dimension_evidence=(("FX", "sha256:" + "3" * 64),),
        status="QUALIFIED",
    )
    values.update(overrides)
    return StrategyEconomicsBinding(**values)


def registry() -> InstrumentRegistry:
    calendar = TradingCalendar.continuous_24_7("CONTINUOUS_24_7")
    version = InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id="SIMULATED",
        venue_id="SIM",
        provider_symbol="AAA",
        asset_class="CRYPTO_SPOT",
        base_currency="AAA",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="AAA",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id=calendar.calendar_id,
        timezone_id=calendar.timezone_id,
        effective_from=BASE,
    )
    return InstrumentRegistry(
        calendars=(calendar,),
        versions=(version,),
    )


class StrategyEconomicsAuthorityTests(unittest.TestCase):
    def test_structurally_qualified_binding_is_not_terminal_authority(self):
        item = proposal()
        binding = economics_binding(item)
        with self.assertRaises(TypeError):
            terminal_decision_proposal(
                binding,
                item,
                proposal_id="11111111-1111-4111-8111-111111111111",
                instrument_version=INSTRUMENT_VERSION,
                economics_binding=binding,
                exit_policy_ref="exit:v1",
                compute_cost_currency="USD",
            )

    def test_assessment_is_issued_inconclusive_with_missing_owner_graph(self):
        item = proposal()
        binding = economics_binding(item)
        authority = assess_strategy_economics_authority(
            item,
            binding,
            instrument_registry=registry(),
        )
        self.assertEqual(authority.status, "INCONCLUSIVE")
        self.assertIn("instrument_registry", authority.verified_owners)
        self.assertIn(
            "registered_strategy_run_receipt",
            authority.unresolved_owners,
        )
        self.assertIn("dimension_fx", authority.unresolved_owners)
        self.assertIs(
            require_strategy_economics_authority(authority),
            authority,
        )

    def test_fake_hashes_and_favorable_values_cannot_reach_terminal_projection(self):
        item = proposal()
        binding = economics_binding(
            item,
            after_cost_lower_bound="999",
            max_feasible_quantity="100",
            input_manifest_refs=("sha256:" + "9" * 64,),
            dimension_evidence=(("FX", "sha256:" + "8" * 64),),
        )
        authority = assess_strategy_economics_authority(
            item,
            binding,
            instrument_registry=registry(),
        )
        self.assertEqual(authority.status, "INCONCLUSIVE")
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "terminal strategy economics is INCONCLUSIVE",
        ):
            terminal_decision_proposal(
                authority,
                item,
                proposal_id="11111111-1111-4111-8111-111111111111",
                instrument_version=INSTRUMENT_VERSION,
                economics_binding=binding,
                exit_policy_ref="exit:v1",
                compute_cost_currency="USD",
            )

    def test_caller_cannot_construct_or_relabel_authority(self):
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "must be issued",
        ):
            StrategyEconomicsAuthority(
                status="QUALIFIED",
                binding_fingerprint="sha256:" + "a" * 64,
                bound_proposal_fingerprint="sha256:" + "b" * 64,
                instrument_version=INSTRUMENT_VERSION,
                instrument_provider_id="SIMULATED",
                verified_owners=(),
                unresolved_owners=(),
                provider_economic_cut_digest=None,
            )

        item = proposal()
        authority = assess_strategy_economics_authority(
            item,
            economics_binding(item),
            instrument_registry=registry(),
        )
        object.__setattr__(authority, "status", "QUALIFIED")
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "unissued or changed",
        ):
            require_strategy_economics_authority(authority)

    def test_unknown_instrument_fails_before_authority_issuance(self):
        item = proposal()
        with self.assertRaises(Exception):
            assess_strategy_economics_authority(
                item,
                economics_binding(item),
                instrument_registry=InstrumentRegistry(
                    calendars=(TradingCalendar.continuous_24_7(),)
                ),
            )


if __name__ == "__main__":
    unittest.main()
