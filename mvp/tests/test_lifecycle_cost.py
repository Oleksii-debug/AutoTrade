from datetime import datetime, timedelta, timezone
from decimal import Decimal, Inexact, Rounded, ROUND_UP, localcontext
import unittest

from mvp.autotrade_mvp.lifecycle_cost import (
    AllocationCostSplit,
    LifecycleCostComponent,
    LifecycleCostError,
    LifecycleCostProfile,
    LifecycleCostRequirements,
    allocation_cost_split,
)


AS_OF = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
HORIZON = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
VALID_UNTIL = datetime(2026, 12, 4, 12, 0, tzinfo=timezone.utc)

def split_for_profile(profile):
    return allocation_cost_split(
        profile,
        decision_scope_ref=profile.decision_scope_ref,
        instrument_version=profile.instrument_version,
        decision_time=profile.as_of,
        horizon_end=profile.horizon_end,
    )



class LifecycleCostTests(unittest.TestCase):
    def component(self, component_id, kind, rate, **overrides):
        phase = (
            "TURNOVER"
            if kind in {
                "COMMISSION",
                "EXCHANGE_FEE",
                "SPREAD",
                "SLIPPAGE",
                "FX_CONVERSION",
                "TRANSACTION_TAX",
            }
            else "HOLDING"
        )
        values = dict(
            component_id=component_id,
            phase=phase,
            kind=kind,
            normalized_rate=rate,
            evidence_ref=f"evidence:{component_id}",
            observed_at=AS_OF - timedelta(days=1),
            valid_until=VALID_UNTIL,
        )
        values.update(overrides)
        return LifecycleCostComponent(**values)

    def profile(self, *, components=None, required_kinds=None, **overrides):
        if components is None:
            components = (
                self.component("commission", "COMMISSION", "0.001"),
                self.component("spread", "SPREAD", "0.002"),
                self.component("funding", "FUNDING", "0.003"),
                self.component("borrow", "BORROW", "0.004"),
            )
        if required_kinds is None:
            required_kinds = tuple(component.kind for component in components)
        values = dict(
            profile_id="cost-profile-1",
            decision_scope_ref="decision-scope:test:v1",
            instrument_version="11111111-1111-4111-8111-111111111111@1",
            as_of=AS_OF,
            horizon_end=HORIZON,
            requirements=LifecycleCostRequirements(requirements_ref="requirements:test:v1", required_kinds=required_kinds),
            components=components,
        )
        values.update(overrides)
        return LifecycleCostProfile(**values)

    def test_full_lifecycle_profile_projects_exact_allocator_split(self):
        profile = self.profile()
        split = split_for_profile(profile)

        self.assertEqual(profile.net_expected_rate, Decimal("0.010"))
        self.assertEqual(split.turnover_cost_rate, Decimal("0.003"))
        self.assertEqual(split.holding_cost_rate, Decimal("0.007"))
        self.assertEqual(split.cost_rate, Decimal("0.010"))
        self.assertEqual(
            split.turnover_cost_rate + split.holding_cost_rate,
            split.cost_rate,
        )
        self.assertEqual(split.lifecycle_cost_digest, profile.digest)
        self.assertEqual(split.profile_id, profile.profile_id)
        self.assertEqual(split.decision_scope_ref, profile.decision_scope_ref)
        self.assertEqual(split.requirements_ref, profile.requirements.requirements_ref)
        self.assertEqual(split.instrument_version, profile.instrument_version)
        self.assertEqual(split.decision_time, profile.as_of)
        self.assertEqual(split.horizon_end, profile.horizon_end)
        self.assertEqual(
            split.component_evidence_refs,
            (
                ("BORROW", "evidence:borrow"),
                ("COMMISSION", "evidence:commission"),
                ("FUNDING", "evidence:funding"),
                ("SPREAD", "evidence:spread"),
            ),
        )

    def test_favorable_signed_components_remain_evidence_but_never_reduce_cost_budget(self):
        profile = self.profile(
            components=(
                self.component("rebate", "COMMISSION", "-0.005"),
                self.component("spread", "SPREAD", "0.002"),
                self.component("funding", "FUNDING", "-0.003"),
                self.component("borrow", "BORROW", "0.004"),
            ),
            required_kinds=("COMMISSION", "SPREAD", "FUNDING", "BORROW"),
        )
        split = split_for_profile(profile)

        self.assertEqual(profile.net_expected_rate, Decimal("-0.002"))
        self.assertEqual(split.turnover_cost_rate, Decimal("0.002"))
        self.assertEqual(split.holding_cost_rate, Decimal("0.004"))
        self.assertEqual(split.cost_rate, Decimal("0.006"))

    def test_zero_rate_still_represents_required_cost_family(self):
        profile = self.profile(
            components=(self.component("commission", "COMMISSION", "0"),),
            required_kinds=("COMMISSION",),
        )
        split = split_for_profile(profile)
        self.assertEqual(profile.net_expected_rate, Decimal("0"))
        self.assertEqual(split.cost_rate, Decimal("0"))

    def test_missing_required_cost_family_fails_closed(self):
        with self.assertRaisesRegex(
            LifecycleCostError,
            "missing required kinds: BORROW",
        ):
            self.profile(
                components=(self.component("commission", "COMMISSION", "0.001"),),
                required_kinds=("COMMISSION", "BORROW"),
            )

    def test_duplicate_cost_kind_fails_instead_of_double_counting(self):
        with self.assertRaisesRegex(
            LifecycleCostError,
            "each lifecycle cost kind must be represented exactly once",
        ):
            self.profile(
                components=(
                    self.component("commission-entry", "COMMISSION", "0.001"),
                    self.component("commission-exit", "COMMISSION", "0.001"),
                ),
                required_kinds=("COMMISSION",),
            )

    def test_duplicate_component_identity_fails_closed(self):
        with self.assertRaisesRegex(LifecycleCostError, "component_id must be unique"):
            self.profile(
                components=(
                    self.component("same", "COMMISSION", "0.001"),
                    self.component("same", "SPREAD", "0.001"),
                ),
                required_kinds=("COMMISSION", "SPREAD"),
            )

    def test_component_observed_after_profile_cut_is_rejected(self):
        late = self.component(
            "commission",
            "COMMISSION",
            "0.001",
            observed_at=AS_OF + timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            LifecycleCostError,
            "observed after profile as_of",
        ):
            self.profile(components=(late,), required_kinds=("COMMISSION",))

    def test_component_must_cover_entire_holding_horizon(self):
        short = self.component(
            "funding",
            "FUNDING",
            "0.003",
            valid_until=HORIZON - timedelta(seconds=1),
        )
        with self.assertRaisesRegex(
            LifecycleCostError,
            "does not cover the full horizon",
        ):
            self.profile(components=(short,), required_kinds=("FUNDING",))

    def test_phase_and_kind_must_match(self):
        with self.assertRaisesRegex(
            LifecycleCostError,
            "cost kind BORROW is not valid for phase TURNOVER",
        ):
            self.component(
                "borrow",
                "BORROW",
                "0.004",
                phase="TURNOVER",
            )

    def test_digest_is_stable_under_component_input_order(self):
        commission = self.component("commission", "COMMISSION", "0.001")
        spread = self.component("spread", "SPREAD", "0.002")
        funding = self.component("funding", "FUNDING", "0.003")
        first = self.profile(
            components=(commission, spread, funding),
            required_kinds=("COMMISSION", "SPREAD", "FUNDING"),
        )
        second = self.profile(
            components=(funding, commission, spread),
            required_kinds=("FUNDING", "COMMISSION", "SPREAD"),
        )
        self.assertEqual(first.digest, second.digest)

    def test_digest_changes_when_evidence_rate_or_horizon_changes(self):
        base = self.profile()
        changed_evidence = self.profile(
            components=(
                self.component(
                    "commission",
                    "COMMISSION",
                    "0.001",
                    evidence_ref="evidence:other",
                ),
                self.component("spread", "SPREAD", "0.002"),
                self.component("funding", "FUNDING", "0.003"),
                self.component("borrow", "BORROW", "0.004"),
            )
        )
        changed_rate = self.profile(
            components=(
                self.component("commission", "COMMISSION", "0.0011"),
                self.component("spread", "SPREAD", "0.002"),
                self.component("funding", "FUNDING", "0.003"),
                self.component("borrow", "BORROW", "0.004"),
            )
        )
        changed_horizon = self.profile(
            horizon_end=HORIZON + timedelta(days=1),
        )
        self.assertNotEqual(base.digest, changed_evidence.digest)
        self.assertNotEqual(base.digest, changed_rate.digest)
        self.assertNotEqual(base.digest, changed_horizon.digest)

    def test_digest_changes_when_decision_scope_changes(self):
        base = self.profile()
        changed = self.profile(decision_scope_ref="decision-scope:other:v1")
        self.assertNotEqual(base.digest, changed.digest)

    def test_blank_decision_scope_fails_closed(self):
        with self.assertRaisesRegex(LifecycleCostError, "decision_scope_ref is required"):
            self.profile(decision_scope_ref="   ")

    def test_digest_changes_when_requirements_evidence_changes(self):
        base = self.profile()
        changed = LifecycleCostProfile(
            profile_id=base.profile_id,
            decision_scope_ref="decision-scope:test:v1",
            instrument_version=base.instrument_version,
            as_of=base.as_of,
            horizon_end=base.horizon_end,
            requirements=LifecycleCostRequirements(
                requirements_ref="requirements:other:v2",
                required_kinds=base.requirements.required_kinds,
            ),
            components=base.components,
        )
        self.assertNotEqual(base.digest, changed.digest)

    def test_blank_requirements_reference_fails_closed(self):
        with self.assertRaisesRegex(LifecycleCostError, "requirements_ref is required"):
            LifecycleCostRequirements(
                requirements_ref="   ",
                required_kinds=("COMMISSION",),
            )

    def test_identity_text_rejects_surrounding_whitespace_instead_of_aliasing(self):
        with self.assertRaisesRegex(LifecycleCostError, "surrounding whitespace"):
            self.profile(decision_scope_ref=" decision-scope:test:v1")
        with self.assertRaisesRegex(LifecycleCostError, "surrounding whitespace"):
            LifecycleCostRequirements(
                requirements_ref="requirements:test:v1 ",
                required_kinds=("COMMISSION",),
            )
        with self.assertRaisesRegex(LifecycleCostError, "surrounding whitespace"):
            self.component(
                "commission",
                "COMMISSION",
                "0.001",
                evidence_ref=" evidence:commission",
            )

    def test_rate_sum_overflow_is_reported_as_lifecycle_error(self):
        maximum = "9" * 256
        profile = self.profile(
            components=(
                self.component("commission", "COMMISSION", maximum),
                self.component("spread", "SPREAD", maximum),
            ),
            required_kinds=("COMMISSION", "SPREAD"),
        )
        with self.assertRaisesRegex(
            LifecycleCostError,
            "arithmetic exceeds exact-decimal resource envelope",
        ):
            _ = profile.net_expected_rate
        with self.assertRaisesRegex(
            LifecycleCostError,
            "arithmetic exceeds exact-decimal resource envelope",
        ):
            split_for_profile(profile)


    def test_exact_cost_math_ignores_ambient_decimal_context(self):
        profile = self.profile(
            components=(
                self.component("commission", "COMMISSION", "0.00000000000000000001"),
                self.component("spread", "SPREAD", "0.00000000000000000002"),
                self.component("funding", "FUNDING", "0.00000000000000000003"),
            ),
            required_kinds=("COMMISSION", "SPREAD", "FUNDING"),
        )
        values = []
        for precision in (1, 80):
            with localcontext() as context:
                context.prec = precision
                context.rounding = ROUND_UP
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                split = split_for_profile(profile)
                values.append(
                    (
                        profile.net_expected_rate,
                        split.turnover_cost_rate,
                        split.holding_cost_rate,
                        split.cost_rate,
                    )
                )
        self.assertEqual(
            values,
            [
                (
                    Decimal("0.00000000000000000006"),
                    Decimal("0.00000000000000000003"),
                    Decimal("0.00000000000000000003"),
                    Decimal("0.00000000000000000006"),
                )
            ] * 2,
        )

    def test_hostile_text_subclass_is_rejected_before_string_dispatch(self):
        calls = []

        class HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("hostile strip executed")

            def upper(self):
                calls.append("upper")
                raise AssertionError("hostile upper executed")

        with self.assertRaises(TypeError):
            self.component(
                "commission",
                "COMMISSION",
                "0.001",
                evidence_ref=HostileText("evidence:commission"),
            )
        self.assertEqual(calls, [])

    def test_hostile_decimal_subclass_and_float_fail_before_numeric_dispatch(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("hostile decimal executed")

        with self.assertRaises(TypeError):
            self.component("commission", "COMMISSION", HostileDecimal("0.001"))
        with self.assertRaises(TypeError):
            self.component("commission", "COMMISSION", 0.001)

    def test_datetime_subclass_and_custom_tzinfo_are_not_temporal_authority(self):
        class HostileDatetime(datetime):
            def astimezone(self, *args, **kwargs):
                raise AssertionError("hostile datetime executed")

        with self.assertRaises(TypeError):
            self.component(
                "commission",
                "COMMISSION",
                "0.001",
                observed_at=HostileDatetime(2026, 10, 3, 12, tzinfo=timezone.utc),
            )

    def test_profile_rejects_component_and_requirements_subclasses(self):
        class DerivedComponent(LifecycleCostComponent):
            pass

        base = self.component("commission", "COMMISSION", "0.001")
        derived_component = DerivedComponent(
            component_id=base.component_id,
            phase=base.phase,
            kind=base.kind,
            normalized_rate=base.normalized_rate,
            evidence_ref=base.evidence_ref,
            observed_at=base.observed_at,
            valid_until=base.valid_until,
        )
        with self.assertRaises(TypeError):
            self.profile(
                components=(derived_component,),
                required_kinds=("COMMISSION",),
            )

        class DerivedRequirements(LifecycleCostRequirements):
            pass

        with self.assertRaises(TypeError):
            LifecycleCostProfile(
                profile_id="cost-profile-1",
                decision_scope_ref="decision-scope:test:v1",
                instrument_version="11111111-1111-4111-8111-111111111111@1",
                as_of=AS_OF,
                horizon_end=HORIZON,
                requirements=DerivedRequirements(
                    requirements_ref="requirements:test:v1",
                    required_kinds=("COMMISSION",),
                ),
                components=(base,),
            )

    def test_component_container_must_be_exact_tuple(self):
        with self.assertRaises(TypeError):
            LifecycleCostProfile(
                profile_id="cost-profile-1",
                decision_scope_ref="decision-scope:test:v1",
                instrument_version="11111111-1111-4111-8111-111111111111@1",
                as_of=AS_OF,
                horizon_end=HORIZON,
                requirements=LifecycleCostRequirements(requirements_ref="requirements:test:v1", required_kinds=("COMMISSION",)),
                components=[self.component("commission", "COMMISSION", "0.001")],
            )

    def test_allocation_projection_revalidates_profile_instead_of_trusting_object_identity(self):
        profile = self.profile()
        object.__setattr__(profile, "profile_id", "")
        with self.assertRaises(LifecycleCostError):
            split_for_profile(profile)

    def test_profile_subclass_is_not_accepted_as_cost_authority(self):
        class DerivedProfile(LifecycleCostProfile):
            pass

        base = self.profile()
        derived = DerivedProfile(
            profile_id=base.profile_id,
            decision_scope_ref="decision-scope:test:v1",
            instrument_version=base.instrument_version,
            as_of=base.as_of,
            horizon_end=base.horizon_end,
            requirements=base.requirements,
            components=base.components,
        )
        with self.assertRaises(TypeError):
            split_for_profile(derived)

    def test_allocation_projection_rejects_cross_decision_scope_reuse(self):
        profile = self.profile()
        with self.assertRaisesRegex(
            LifecycleCostError,
            "decision scope does not match allocation decision scope",
        ):
            allocation_cost_split(
                profile,
                decision_scope_ref="decision-scope:other:v1",
                instrument_version=profile.instrument_version,
                decision_time=profile.as_of,
                horizon_end=profile.horizon_end,
            )

    def test_allocation_projection_rejects_cross_instrument_reuse(self):
        profile = self.profile()
        with self.assertRaisesRegex(
            LifecycleCostError,
            "instrument does not match allocation instrument",
        ):
            allocation_cost_split(
                profile,
                decision_scope_ref=profile.decision_scope_ref,
                instrument_version="22222222-2222-4222-8222-222222222222@1",
                decision_time=profile.as_of,
                horizon_end=profile.horizon_end,
            )

    def test_allocation_projection_rejects_wrong_decision_cut(self):
        profile = self.profile()
        with self.assertRaisesRegex(
            LifecycleCostError,
            "decision cut does not match allocation decision time",
        ):
            allocation_cost_split(
                profile,
                decision_scope_ref=profile.decision_scope_ref,
                instrument_version=profile.instrument_version,
                decision_time=profile.as_of + timedelta(seconds=1),
                horizon_end=profile.horizon_end,
            )

    def test_allocation_projection_rejects_wrong_forecast_horizon(self):
        profile = self.profile()
        with self.assertRaisesRegex(
            LifecycleCostError,
            "horizon does not match allocation horizon",
        ):
            allocation_cost_split(
                profile,
                decision_scope_ref=profile.decision_scope_ref,
                instrument_version=profile.instrument_version,
                decision_time=profile.as_of,
                horizon_end=profile.horizon_end + timedelta(days=1),
            )


    def test_projection_dto_rejects_inconsistent_total(self):
        profile = self.profile()
        split = split_for_profile(profile)
        with self.assertRaisesRegex(
            LifecycleCostError,
            "must sum exactly to cost_rate",
        ):
            AllocationCostSplit(
                cost_rate="99",
                turnover_cost_rate=split.turnover_cost_rate,
                holding_cost_rate=split.holding_cost_rate,
                lifecycle_cost_digest=split.lifecycle_cost_digest,
                profile_id=split.profile_id,
                decision_scope_ref=split.decision_scope_ref,
                requirements_ref=split.requirements_ref,
                component_evidence_refs=split.component_evidence_refs,
                instrument_version=split.instrument_version,
                decision_time=split.decision_time,
                horizon_end=split.horizon_end,
            )

    def test_projection_dto_rejects_forged_digest_shape_and_duplicate_refs(self):
        profile = self.profile()
        split = split_for_profile(profile)
        with self.assertRaisesRegex(
            LifecycleCostError,
            "canonical sha256",
        ):
            AllocationCostSplit(
                cost_rate=split.cost_rate,
                turnover_cost_rate=split.turnover_cost_rate,
                holding_cost_rate=split.holding_cost_rate,
                lifecycle_cost_digest="not-a-digest",
                profile_id=split.profile_id,
                decision_scope_ref=split.decision_scope_ref,
                requirements_ref=split.requirements_ref,
                component_evidence_refs=split.component_evidence_refs,
                instrument_version=split.instrument_version,
                decision_time=split.decision_time,
                horizon_end=split.horizon_end,
            )
        with self.assertRaisesRegex(
            LifecycleCostError,
            "kinds must be unique",
        ):
            AllocationCostSplit(
                cost_rate=split.cost_rate,
                turnover_cost_rate=split.turnover_cost_rate,
                holding_cost_rate=split.holding_cost_rate,
                lifecycle_cost_digest=split.lifecycle_cost_digest,
                profile_id=split.profile_id,
                decision_scope_ref=split.decision_scope_ref,
                requirements_ref=split.requirements_ref,
                component_evidence_refs=(
                    ("COMMISSION", "evidence:a"),
                    ("COMMISSION", "evidence:b"),
                ),
                instrument_version=split.instrument_version,
                decision_time=split.decision_time,
                horizon_end=split.horizon_end,
            )



if __name__ == "__main__":
    unittest.main()
