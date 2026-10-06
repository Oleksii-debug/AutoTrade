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
import qualification.strategy_economics.qualify as economics_authority
from qualification.strategy_economics.qualify import (
    StrategyEconomicsAuthorityError,
    assess_strategy_economics_authority,
    require_qualified_strategy_economics,
)
import research.autotrade_research.strategies.deterministic as deterministic_strategy
from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    RegisteredStrategyRunReceipt,
    ReturnThresholdBaseline,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    run_registered_baseline,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
INSTRUMENT_ID = str(UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
INSTRUMENT_VERSION = f"{INSTRUMENT_ID}@1"


def _descriptor() -> StrategyDescriptor:
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


def _run():
    strategy = ReturnThresholdBaseline(
        lookback=2,
        threshold="0.01",
        proposal_quantity="2",
        descriptor=_descriptor(),
    )
    return run_registered_baseline(
        strategy,
        (
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
        ),
        decision_time=BASE + timedelta(minutes=1),
        symbol="AAA",
        instrument_version=INSTRUMENT_VERSION,
    )


def _binding(item, receipt, **overrides) -> StrategyEconomicsBinding:
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
        registered_run_receipt_sha256=receipt.fingerprint,
        required_evidence_dimensions=("FX",),
        dimension_evidence=(("FX", "sha256:" + "3" * 64),),
        status="QUALIFIED",
    )
    values.update(overrides)
    return StrategyEconomicsBinding(**values)


def _registry() -> InstrumentRegistry:
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
    return InstrumentRegistry(calendars=(calendar,), versions=(version,))


class RegisteredRunEconomicsAuthorityTests(unittest.TestCase):
    def test_replayed_receipt_closes_only_registered_run_owner(self):
        item, receipt = _run()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item, receipt),
            instrument_registry=_registry(),
            registered_run_receipt=receipt,
        )

        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertEqual(
            assessment.registered_run_receipt_digest,
            receipt.fingerprint,
        )
        self.assertIn(
            "registered_strategy_run_receipt",
            assessment.verified_owners,
        )
        self.assertNotIn(
            "registered_strategy_run_receipt",
            assessment.unresolved_owners,
        )
        for owner in (
            "execution_calibration_authority",
            "capacity_evidence_authority",
            "after_cost_projection_authority",
            "instrument_registry_authority",
            "provider_economic_cut",
            "provider_scope_binding",
            "dimension_fx",
        ):
            self.assertIn(owner, assessment.unresolved_owners)
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "terminal strategy economics is INCONCLUSIVE",
        ):
            require_qualified_strategy_economics(assessment)

    def test_binding_must_name_exact_replayed_receipt(self):
        item, receipt = _run()
        binding = _binding(
            item,
            receipt,
            registered_run_receipt_sha256="sha256:" + "9" * 64,
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "does not name the replayed registered strategy run",
        ):
            assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
                registered_run_receipt=receipt,
            )

    def test_public_registered_run_verifier_rebind_cannot_redirect_assessment(self):
        item, receipt = _run()
        binding = _binding(item, receipt)
        original = economics_authority.verify_registered_strategy_run
        calls = []

        def hostile_verify(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError(
                "rebound public registered-run verifier executed"
            )

        economics_authority.verify_registered_strategy_run = hostile_verify
        try:
            assessment = assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
                registered_run_receipt=receipt,
            )
        finally:
            economics_authority.verify_registered_strategy_run = original

        self.assertEqual(calls, [])
        self.assertEqual(
            assessment.registered_run_receipt_digest,
            receipt.fingerprint,
        )

    def test_registered_run_verifier_dependency_rebind_fails_closed(self):
        item, receipt = _run()
        binding = _binding(item, receipt)
        original = deterministic_strategy.run_baseline
        calls = []

        def hostile_run(*args, **kwargs):
            calls.append((args, kwargs))
            return item

        deterministic_strategy.run_baseline = hostile_run
        try:
            with self.assertRaisesRegex(
                StrategyEconomicsAuthorityError,
                "registered strategy-run verifier dependency changed",
            ):
                assess_strategy_economics_authority(
                    item,
                    binding,
                    instrument_registry=_registry(),
                    registered_run_receipt=receipt,
                )
        finally:
            deterministic_strategy.run_baseline = original

        self.assertEqual(calls, [])

    def test_favorable_fake_economics_remain_unresolved_after_real_run_replay(self):
        item, receipt = _run()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(
                item,
                receipt,
                after_cost_lower_bound="999",
                max_feasible_quantity="100",
                input_manifest_refs=("sha256:" + "9" * 64,),
                dimension_evidence=(("FX", "sha256:" + "8" * 64),),
            ),
            instrument_registry=_registry(),
            registered_run_receipt=receipt,
        )
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertIn(
            "after_cost_projection_authority",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "capacity_evidence_authority",
            assessment.unresolved_owners,
        )
        self.assertIn("dimension_fx", assessment.unresolved_owners)

    def test_receipt_must_be_exact_canonical_type(self):
        item, receipt = _run()

        class ReceiptSubclass(RegisteredStrategyRunReceipt):
            pass

        hostile = object.__new__(ReceiptSubclass)
        with self.assertRaisesRegex(TypeError, "must be exact"):
            assess_strategy_economics_authority(
                item,
                _binding(item, receipt),
                instrument_registry=_registry(),
                registered_run_receipt=hostile,
            )


if __name__ == "__main__":
    unittest.main()
