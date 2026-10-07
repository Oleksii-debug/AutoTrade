from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from uuid import UUID

from mvp.autotrade_mvp.allocation import (
    AllocationCandidate,
    AllocationPolicy,
    StressScenarioEvidence,
)
from mvp.autotrade_mvp.instruments import (
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentVersion,
    TradingCalendar,
)
import qualification.strategy_economics.capacity as capacity_authority
import qualification.strategy_economics.qualify as economics_authority
from qualification.strategy_economics.capacity import (
    StrategyCapacityAuthorityError,
    issue_allocation_capacity_evidence,
    require_allocation_capacity_evidence,
)
from qualification.strategy_economics.qualify import (
    StrategyEconomicsAuthorityAssessment,
    StrategyEconomicsAuthorityError,
    assess_strategy_economics_authority,
    require_qualified_strategy_economics,
    require_strategy_economics_assessment,
)
from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    ReturnThresholdBaseline,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    run_baseline,
    to_decision_proposal,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
INSTRUMENT_ID = str(
    UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
)
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


def _proposal():
    strategy = ReturnThresholdBaseline(
        lookback=2,
        threshold="0.01",
        proposal_quantity="2",
        descriptor=_descriptor(),
    )
    return run_baseline(
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
    )


def _binding(item, **overrides) -> StrategyEconomicsBinding:
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
        dimension_evidence=(
            ("FX", "sha256:" + "3" * 64),
        ),
        status="QUALIFIED",
    )
    values.update(overrides)
    return StrategyEconomicsBinding(**values)


def _instrument_version(
    *,
    version: int = 1,
    provider_symbol: str = "AAA",
    effective_from: datetime = BASE,
) -> InstrumentVersion:
    calendar = TradingCalendar.continuous_24_7(
        "CONTINUOUS_24_7"
    )
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=version,
        provider_id="SIMULATED",
        venue_id="SIM",
        provider_symbol=provider_symbol,
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
        effective_from=effective_from,
    )


def _registry(*versions: InstrumentVersion) -> InstrumentRegistry:
    calendar = TradingCalendar.continuous_24_7(
        "CONTINUOUS_24_7"
    )
    if not versions:
        versions = (_instrument_version(),)
    return InstrumentRegistry(
        calendars=(calendar,),
        versions=versions,
    )


def _capacity_evidence(item, *, max_executable_notional="102"):
    candidate = AllocationCandidate.create(
        symbol=item.symbol,
        desired_notional="204",
        price="102",
        lot_size="1",
        max_executable_notional=max_executable_notional,
    )
    policy = AllocationPolicy.create(
        cash_available="1000",
        max_gross_notional="1000",
        max_net_notional="1000",
        max_symbol_notional="1000",
        max_total_cost="100",
        max_stress_loss="1000",
        max_turnover_notional="1000",
    )
    stress = (
        StressScenarioEvidence.create(
            name="adverse-quarter",
            shocks={item.symbol: "-0.25"},
            observed_at=BASE.isoformat().replace("+00:00", "Z"),
            valid_until=item.expiry.isoformat().replace("+00:00", "Z"),
            source_ref="science:capacity:stress:v1",
        ),
    )
    return issue_allocation_capacity_evidence(
        item,
        instrument_version=INSTRUMENT_VERSION,
        candidate=candidate,
        policy=policy,
        stress_evidence=stress,
    )


class StrategyEconomicsAuthorityTests(unittest.TestCase):
    def test_canonical_allocator_capacity_replay_does_not_mint_capacity_owner(self):
        item = _proposal()
        capacity = _capacity_evidence(item)
        self.assertEqual(capacity.max_feasible_quantity, "1")
        binding = _binding(
            item,
            capacity_assessment_sha256=capacity.digest,
            max_feasible_quantity="1",
            required_evidence_dimensions=("CAPACITY",),
            dimension_evidence=(("CAPACITY", capacity.digest),),
        )

        assessment = assess_strategy_economics_authority(
            item,
            binding,
            instrument_registry=_registry(),
            capacity_evidence=capacity,
        )

        self.assertEqual(
            assessment.capacity_assessment_digest,
            capacity.digest,
        )
        self.assertIn(
            "capacity_replay_consistency",
            assessment.verified_owners,
        )
        self.assertIn(
            "capacity_evidence_authority",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "after_cost_projection_authority",
            assessment.unresolved_owners,
        )
        self.assertEqual(assessment.status, "INCONCLUSIVE")

    def test_capacity_evidence_digest_cannot_be_detached_from_binding(self):
        item = _proposal()
        capacity = _capacity_evidence(item)
        binding = _binding(
            item,
            capacity_assessment_sha256="sha256:" + "7" * 64,
            max_feasible_quantity="1",
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "capacity evidence digest does not match economics binding",
        ):
            assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
                capacity_evidence=capacity,
            )

    def test_capacity_evidence_is_non_expansive_and_tamper_evident(self):
        item = _proposal()
        capacity = _capacity_evidence(
            item,
            max_executable_notional="1000",
        )
        self.assertEqual(capacity.proposal_quantity, "2")
        self.assertEqual(capacity.max_feasible_quantity, "2")
        object.__setattr__(capacity, "max_feasible_quantity", "3")
        with self.assertRaisesRegex(
            StrategyCapacityAuthorityError,
            "unissued or changed",
        ):
            require_allocation_capacity_evidence(capacity)

    def test_public_allocator_rebind_cannot_redirect_capacity_issuer(self):
        item = _proposal()
        original = capacity_authority.allocate_targets
        calls = []

        def hostile_allocator(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public allocator executed")

        capacity_authority.allocate_targets = hostile_allocator
        try:
            capacity = _capacity_evidence(item)
        finally:
            capacity_authority.allocate_targets = original

        self.assertEqual(calls, [])
        self.assertEqual(capacity.max_feasible_quantity, "1")

    def test_structural_qualified_binding_remains_terminally_inconclusive(self):
        item = _proposal()
        binding = _binding(item)
        diagnostic = to_decision_proposal(
            item,
            proposal_id=(
                "11111111-1111-4111-8111-111111111111"
            ),
            instrument_version=INSTRUMENT_VERSION,
            economics_binding=binding,
            exit_policy_ref="exit:v1",
            compute_cost_currency="USD",
        )
        self.assertEqual(
            diagnostic["candidate_instruments"],
            [INSTRUMENT_VERSION],
        )

        assessment = assess_strategy_economics_authority(
            item,
            binding,
            instrument_registry=_registry(),
        )
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertIn(
            "registered_strategy_run_receipt",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "dimension_fx",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "provider_scope_binding",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "instrument_registry_authority",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "provider_economic_cut",
            assessment.unresolved_owners,
        )
        self.assertIn(
            "instrument_registry_shape",
            assessment.verified_owners,
        )
        self.assertNotIn(
            "instrument_registry",
            assessment.verified_owners,
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "terminal strategy economics is INCONCLUSIVE",
        ):
            require_qualified_strategy_economics(assessment)

    def test_fake_hashes_and_favorable_numbers_do_not_mint_authority(self):
        item = _proposal()
        binding = _binding(
            item,
            after_cost_lower_bound="999",
            max_feasible_quantity="100",
            input_manifest_refs=(
                "sha256:" + "9" * 64,
            ),
            dimension_evidence=(
                ("FX", "sha256:" + "8" * 64),
            ),
        )
        assessment = assess_strategy_economics_authority(
            item,
            binding,
            instrument_registry=_registry(),
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

    def test_provider_symbol_alias_does_not_close_provider_scope_owner(self):
        item = _proposal()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item),
            instrument_registry=_registry(
                _instrument_version(provider_symbol="BBB")
            ),
        )
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertIn(
            "provider_scope_binding",
            assessment.unresolved_owners,
        )

    def test_future_instrument_version_is_not_valid_at_information_cutoff(self):
        item = _proposal()
        with self.assertRaises(InstrumentNotFound):
            assess_strategy_economics_authority(
                item,
                _binding(item),
                instrument_registry=_registry(
                    _instrument_version(
                        effective_from=BASE + timedelta(minutes=2)
                    )
                ),
            )

    def test_superseded_exact_version_cannot_bind_later_proposal_cut(self):
        item = _proposal()
        registry = _registry(
            _instrument_version(),
            _instrument_version(
                version=2,
                effective_from=BASE + timedelta(seconds=30),
            ),
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "instrument_version is not the registry version effective at information_cutoff",
        ):
            assess_strategy_economics_authority(
                item,
                _binding(item),
                instrument_registry=registry,
            )

    def test_public_structural_binder_rebind_cannot_redirect_assessment(self):
        item = _proposal()
        binding = _binding(item)
        original = economics_authority.bind_strategy_economics
        calls = []

        def hostile_bind(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public structural binder executed")

        economics_authority.bind_strategy_economics = hostile_bind
        try:
            assessment = assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
            )
        finally:
            economics_authority.bind_strategy_economics = original

        self.assertEqual(calls, [])
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertEqual(
            assessment.binding_fingerprint,
            binding.fingerprint,
        )

    def test_public_registry_exact_rebind_cannot_redirect_assessment(self):
        item = _proposal()
        binding = _binding(item)
        original = InstrumentRegistry.exact
        calls = []

        def hostile_exact(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public registry lookup executed")

        InstrumentRegistry.exact = hostile_exact
        try:
            assessment = assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
            )
        finally:
            InstrumentRegistry.exact = original

        self.assertEqual(calls, [])
        self.assertEqual(assessment.instrument_version, INSTRUMENT_VERSION)
        self.assertIn(
            "instrument_registry_authority",
            assessment.unresolved_owners,
        )

    def test_public_registry_at_rebind_cannot_redirect_assessment(self):
        item = _proposal()
        binding = _binding(item)
        original = InstrumentRegistry.at
        calls = []

        def hostile_at(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public effective-version lookup executed")

        InstrumentRegistry.at = hostile_at
        try:
            assessment = assess_strategy_economics_authority(
                item,
                binding,
                instrument_registry=_registry(),
            )
        finally:
            InstrumentRegistry.at = original

        self.assertEqual(calls, [])
        self.assertEqual(assessment.instrument_version, INSTRUMENT_VERSION)
        self.assertIn(
            "instrument_registry_authority",
            assessment.unresolved_owners,
        )

    def test_caller_provider_replay_does_not_satisfy_owner_authority(self):
        item = _proposal()
        original = economics_authority._REVERIFY_PROVIDER_ECONOMIC_CUT
        calls = []

        def replay(book, cut, *, expected_visibility_journal_sequence):
            calls.append((book, cut, expected_visibility_journal_sequence))
            return SimpleNamespace(
                provider_id="SIMULATED",
                cut_digest="sha256:" + "4" * 64,
            )

        book = object.__new__(economics_authority.DurableProviderEconomicBook)
        cut = object.__new__(economics_authority.ProviderEconomicCut)
        economics_authority._REVERIFY_PROVIDER_ECONOMIC_CUT = replay
        try:
            assessment = assess_strategy_economics_authority(
                item,
                _binding(item),
                instrument_registry=_registry(),
                provider_economic_book=book,
                provider_economic_cut=cut,
                expected_visibility_journal_sequence=7,
            )
        finally:
            economics_authority._REVERIFY_PROVIDER_ECONOMIC_CUT = original

        self.assertEqual(len(calls), 1)
        self.assertIn(
            "provider_economic_cut_replay",
            assessment.verified_owners,
        )
        self.assertIn(
            "provider_economic_cut",
            assessment.unresolved_owners,
        )
        self.assertEqual(
            assessment.provider_economic_cut_digest,
            "sha256:" + "4" * 64,
        )

    def test_assessment_is_issued_and_mutation_invalidates_it(self):
        item = _proposal()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item),
            instrument_registry=_registry(),
        )
        self.assertIs(
            require_strategy_economics_assessment(assessment),
            assessment,
        )
        original_digest = assessment.digest
        object.__setattr__(
            assessment,
            "status",
            "QUALIFIED",
        )
        self.assertNotEqual(assessment.digest, original_digest)
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "unissued or changed",
        ):
            require_strategy_economics_assessment(assessment)

    def test_caller_cannot_construct_qualified_assessment(self):
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "must be issued canonically",
        ):
            StrategyEconomicsAuthorityAssessment(
                status="QUALIFIED",
                binding_fingerprint="sha256:" + "a" * 64,
                bound_proposal_fingerprint="sha256:" + "b" * 64,
                instrument_version=INSTRUMENT_VERSION,
                instrument_provider_id="SIMULATED",
                verified_owners=(),
                unresolved_owners=(),
                provider_economic_cut_digest=None,
            )

    def test_private_issue_token_still_cannot_mint_positive_status(self):
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "positive strategy economics issuance is unavailable",
        ):
            StrategyEconomicsAuthorityAssessment(
                status="QUALIFIED",
                binding_fingerprint="sha256:" + "a" * 64,
                bound_proposal_fingerprint="sha256:" + "b" * 64,
                instrument_version=INSTRUMENT_VERSION,
                instrument_provider_id="SIMULATED",
                verified_owners=(
                    "instrument_registry_shape",
                ),
                unresolved_owners=(),
                provider_economic_cut_digest=None,
                _token=economics_authority._ISSUE_TOKEN,
            )

    def test_private_registry_cannot_reseal_mutated_positive_authority(self):
        item = _proposal()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item),
            instrument_registry=_registry(),
        )

        # Python-private symbols are importable by same-process callers.  Prove
        # that even a caller who mutates an issued diagnostic and re-registers
        # a matching private seal still cannot obtain terminal authority.
        object.__setattr__(assessment, "status", "QUALIFIED")
        object.__setattr__(assessment, "unresolved_owners", ())
        economics_authority._register_issued(
            assessment,
            _token=economics_authority._ISSUE_TOKEN,
        )

        self.assertIs(
            require_strategy_economics_assessment(assessment),
            assessment,
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "positive strategy economics issuance is unavailable on current main",
        ):
            require_qualified_strategy_economics(assessment)

    def test_unknown_instrument_fails_before_assessment_issuance(self):
        item = _proposal()
        empty_registry = InstrumentRegistry(
            calendars=(
                TradingCalendar.continuous_24_7(),
            )
        )
        with self.assertRaises(InstrumentNotFound):
            assess_strategy_economics_authority(
                item,
                _binding(item),
                instrument_registry=empty_registry,
            )


if __name__ == "__main__":
    unittest.main()