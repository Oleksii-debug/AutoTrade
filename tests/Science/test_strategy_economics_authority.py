from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest
from uuid import UUID

from mvp.autotrade_mvp.instruments import (
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentVersion,
    TradingCalendar,
)
import qualification.strategy_economics.qualify as economics_authority
from qualification.strategy_economics.qualify import (
    StrategyEconomicsAuthorityAssessment,
    StrategyEconomicsAuthorityError,
    assess_strategy_economics_authority,
    require_qualified_strategy_economics,
    require_strategy_economics_assessment,
)
from research.autotrade_research.strategies.deterministic import (
    CausalObservation,
    RegisteredStrategyRunReceipt,
    ReturnThresholdBaseline,
    StrategyDescriptor,
    StrategyEconomicsBinding,
    run_registered_baseline,
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


def _proposal_and_receipt():
    strategy = ReturnThresholdBaseline(
        lookback=2,
        threshold="0.01",
        proposal_quantity="2",
        descriptor=_descriptor(),
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
    return run_registered_baseline(
        strategy,
        observations,
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


class StrategyEconomicsAuthorityTests(unittest.TestCase):
    def test_replayed_registered_run_closes_only_that_owner(self):
        item, receipt = _proposal_and_receipt()
        binding = _binding(item, receipt)
        diagnostic = to_decision_proposal(
            item,
            proposal_id=(
                "11111111-1111-4111-8111-111111111111"
            ),
            instrument_version=INSTRUMENT_VERSION,
            economics_binding=binding,
            registered_run_receipt=receipt,
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
            "provider_scope_binding",
            "instrument_registry_authority",
            "provider_economic_cut",
            "dimension_fx",
        ):
            self.assertIn(owner, assessment.unresolved_owners)
        self.assertIn(
            "instrument_registry_shape",
            assessment.verified_owners,
        )
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "terminal strategy economics is INCONCLUSIVE",
        ):
            require_qualified_strategy_economics(assessment)

    def test_exposure_without_registered_run_receipt_fails_before_assessment(self):
        item, receipt = _proposal_and_receipt()
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "requires registered-run replay authority",
        ):
            assess_strategy_economics_authority(
                item,
                _binding(item, receipt),
                instrument_registry=_registry(),
            )

    def test_binding_must_name_exact_replayed_registered_run(self):
        item, receipt = _proposal_and_receipt()
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

    def test_registered_run_instrument_must_match_economics(self):
        item, receipt = _proposal_and_receipt()
        object.__setattr__(receipt, "instrument_version", "other@1")
        with self.assertRaisesRegex(
            StrategyEconomicsAuthorityError,
            "instrument does not match economics",
        ):
            assess_strategy_economics_authority(
                item,
                _binding(
                    item,
                    receipt,
                    registered_run_receipt_sha256=receipt.fingerprint,
                ),
                instrument_registry=_registry(),
                registered_run_receipt=receipt,
            )

    def test_fake_hashes_and_favorable_numbers_do_not_mint_authority(self):
        item, receipt = _proposal_and_receipt()
        binding = _binding(
            item,
            receipt,
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

    def test_provider_symbol_alias_does_not_close_provider_scope_owner(self):
        item, receipt = _proposal_and_receipt()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item, receipt),
            instrument_registry=_registry(
                _instrument_version(provider_symbol="BBB")
            ),
            registered_run_receipt=receipt,
        )
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertIn(
            "provider_scope_binding",
            assessment.unresolved_owners,
        )

    def test_future_instrument_version_is_not_valid_at_information_cutoff(self):
        item, receipt = _proposal_and_receipt()
        with self.assertRaises(InstrumentNotFound):
            assess_strategy_economics_authority(
                item,
                _binding(item, receipt),
                instrument_registry=_registry(
                    _instrument_version(
                        effective_from=BASE + timedelta(minutes=2)
                    )
                ),
                registered_run_receipt=receipt,
            )

    def test_superseded_exact_version_cannot_bind_later_proposal_cut(self):
        item, receipt = _proposal_and_receipt()
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
                _binding(item, receipt),
                instrument_registry=registry,
                registered_run_receipt=receipt,
            )

    def test_public_structural_binder_rebind_cannot_redirect_assessment(self):
        item, receipt = _proposal_and_receipt()
        binding = _binding(item, receipt)
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
                registered_run_receipt=receipt,
            )
        finally:
            economics_authority.bind_strategy_economics = original

        self.assertEqual(calls, [])
        self.assertEqual(assessment.status, "INCONCLUSIVE")
        self.assertEqual(
            assessment.binding_fingerprint,
            binding.fingerprint,
        )

    def test_public_registered_run_verifier_rebind_cannot_redirect_assessment(self):
        item, receipt = _proposal_and_receipt()
        binding = _binding(item, receipt)
        original = economics_authority.verify_registered_strategy_run
        calls = []

        def hostile_verify(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("rebound public registered-run verifier executed")

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

    def test_public_registry_exact_rebind_cannot_redirect_assessment(self):
        item, receipt = _proposal_and_receipt()
        binding = _binding(item, receipt)
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
                registered_run_receipt=receipt,
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
        item, receipt = _proposal_and_receipt()
        binding = _binding(item, receipt)
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
                registered_run_receipt=receipt,
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
        item, receipt = _proposal_and_receipt()
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
                _binding(item, receipt),
                instrument_registry=_registry(),
                registered_run_receipt=receipt,
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
        item, receipt = _proposal_and_receipt()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item, receipt),
            instrument_registry=_registry(),
            registered_run_receipt=receipt,
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
                registered_run_receipt_digest=None,
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
                registered_run_receipt_digest=None,
                provider_economic_cut_digest=None,
                _token=economics_authority._ISSUE_TOKEN,
            )

    def test_private_registry_cannot_reseal_mutated_positive_authority(self):
        item, receipt = _proposal_and_receipt()
        assessment = assess_strategy_economics_authority(
            item,
            _binding(item, receipt),
            instrument_registry=_registry(),
            registered_run_receipt=receipt,
        )

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
        item, receipt = _proposal_and_receipt()
        empty_registry = InstrumentRegistry(
            calendars=(
                TradingCalendar.continuous_24_7(),
            )
        )
        with self.assertRaises(InstrumentNotFound):
            assess_strategy_economics_authority(
                item,
                _binding(item, receipt),
                instrument_registry=empty_registry,
                registered_run_receipt=receipt,
            )

    def test_receipt_must_be_exact_canonical_type(self):
        item, receipt = _proposal_and_receipt()

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