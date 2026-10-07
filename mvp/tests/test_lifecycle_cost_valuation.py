from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.allocation_valuation import normalize_allocation_valuation
from mvp.autotrade_mvp.lifecycle_cost import (
    LifecycleCostComponent,
    LifecycleCostError,
    LifecycleCostProfile,
    LifecycleCostRequirements,
)
from mvp.autotrade_mvp.lifecycle_cost_valuation import (
    project_lifecycle_cost_to_valuation,
)


BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)
HORIZON = BASE + timedelta(days=1)
INSTRUMENT_VERSION = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa@1"
SCOPE = "allocation-scope:test:v1"

RATES = {
    "COMMISSION": ("TURNOVER", "0.001"),
    "EXCHANGE_FEE": ("TURNOVER", "0.0002"),
    "SPREAD": ("TURNOVER", "0.0003"),
    "SLIPPAGE": ("TURNOVER", "0.0004"),
    "FX_CONVERSION": ("TURNOVER", "0.0005"),
    "TRANSACTION_TAX": ("TURNOVER", "0.0001"),
    "FUNDING": ("HOLDING", "-0.0009"),
    "BORROW": ("HOLDING", "0.0010"),
    "FINANCING": ("HOLDING", "0.0006"),
    "CARRY": ("HOLDING", "0.0007"),
    "STORAGE": ("HOLDING", "0.0008"),
}


def _profile(*, evidence_suffix: str = "v1") -> LifecycleCostProfile:
    components = tuple(
        LifecycleCostComponent(
            component_id=f"component:{kind.lower()}",
            phase=phase,
            kind=kind,
            normalized_rate=rate,
            evidence_ref=f"evidence:{kind.lower()}:{evidence_suffix}",
            observed_at=BASE - timedelta(minutes=1),
            valid_until=HORIZON,
        )
        for kind, (phase, rate) in RATES.items()
    )
    return LifecycleCostProfile(
        profile_id="profile:test:v1",
        decision_scope_ref=SCOPE,
        instrument_version=INSTRUMENT_VERSION,
        as_of=BASE,
        horizon_end=HORIZON,
        requirements=LifecycleCostRequirements(
            requirements_ref="requirements:test:v1",
            required_kinds=tuple(RATES),
        ),
        components=components,
    )


def _project(profile: LifecycleCostProfile):
    return project_lifecycle_cost_to_valuation(
        profile,
        decision_scope_ref=SCOPE,
        instrument_version=INSTRUMENT_VERSION,
        decision_time=BASE,
        horizon_end=HORIZON,
    )


class LifecycleCostValuationBridgeTests(unittest.TestCase):
    def test_projection_is_conservative_and_exactly_matches_lifecycle_total(self):
        projection = _project(_profile())

        self.assertEqual(
            dict(projection.cost_rate_components),
            {
                "execution": Decimal("0.0020"),
                "financing": Decimal("0.0021"),
                "funding": Decimal("0"),
                "borrow": Decimal("0.0010"),
                "fx": Decimal("0.0005"),
            },
        )
        self.assertEqual(projection.cost_rate, Decimal("0.0056"))
        self.assertEqual(
            tuple(projection.cost_rate_components),
            ("execution", "financing", "funding", "borrow", "fx"),
        )
        self.assertEqual(set(projection.cost_evidence_refs), set(projection.cost_rate_components))
        for ref in projection.cost_evidence_refs.values():
            self.assertRegex(ref, r"^sha256:[0-9a-f]{64}$")

    def test_favorable_funding_is_evidence_but_cannot_reduce_valuation_cost(self):
        projection = _project(_profile())

        self.assertIn(
            ("FUNDING", "evidence:funding:v1"),
            projection.component_evidence_refs,
        )
        self.assertEqual(projection.cost_rate_components["funding"], Decimal("0"))
        self.assertGreater(projection.cost_rate, Decimal("0"))

    def test_projection_is_consumed_by_existing_canonical_allocation_valuation(self):
        projection = _project(_profile())
        market = {
            "instrument_version": INSTRUMENT_VERSION,
            "capability_snapshot_id": "capability:test:v1",
            "asset_class": "CRYPTO_SPOT",
            "payoff": "LINEAR",
            "quantity_unit": "AAA",
            "contract_multiplier": "1",
            "quote_currency": "USD",
            "settlement_currency": "USD",
        }
        valuation = {
            **market,
            "symbol": "AAA",
            "source_price": "10",
            "portfolio_base_currency": "USD",
            "fx_rate": "1",
            "fx_source_id": "IDENTITY",
            "unit_base_notional": "10",
            "capital_requirement_rate": "1",
            "min_notional_base": "0",
            "fee_floor_base": "0",
            "max_executable_notional_base": "1000",
            "payoff_identity": "crypto-spot:linear:test:v1",
            "cost_rate_components": dict(projection.cost_rate_components),
            "cost_evidence_refs": dict(projection.cost_evidence_refs),
        }

        normalized = normalize_allocation_valuation(
            symbol="AAA",
            market_payload=market,
            valuation_payload=valuation,
            source_price="10",
            expected_cost_rate=projection.cost_rate,
            expected_capital_requirement_rate="1",
            expected_min_notional_base="0",
            expected_fee_floor_base="0",
            expected_max_executable_notional_base="1000",
            decision_time="2026-01-01T00:00:00Z",
            portfolio_base_currency="USD",
        )

        self.assertEqual(
            dict(normalized.cost_rate_components),
            dict(projection.cost_rate_components),
        )
        self.assertEqual(
            dict(normalized.cost_evidence_refs),
            dict(projection.cost_evidence_refs),
        )

    def test_evidence_identity_changes_when_underlying_component_evidence_changes(self):
        first = _project(_profile(evidence_suffix="v1"))
        second = _project(_profile(evidence_suffix="v2"))

        self.assertNotEqual(first.lifecycle_cost_digest, second.lifecycle_cost_digest)
        self.assertNotEqual(
            dict(first.cost_evidence_refs),
            dict(second.cost_evidence_refs),
        )
        self.assertEqual(
            dict(first.cost_rate_components),
            dict(second.cost_rate_components),
        )

    def test_context_reuse_fails_closed_through_canonical_lifecycle_gate(self):
        profile = _profile()
        with self.assertRaisesRegex(LifecycleCostError, "decision scope"):
            project_lifecycle_cost_to_valuation(
                profile,
                decision_scope_ref="allocation-scope:other:v1",
                instrument_version=INSTRUMENT_VERSION,
                decision_time=BASE,
                horizon_end=HORIZON,
            )
        with self.assertRaisesRegex(LifecycleCostError, "horizon"):
            project_lifecycle_cost_to_valuation(
                profile,
                decision_scope_ref=SCOPE,
                instrument_version=INSTRUMENT_VERSION,
                decision_time=BASE,
                horizon_end=HORIZON + timedelta(seconds=1),
            )

    def test_non_applicable_buckets_are_explicit_zero_with_applicability_bound_refs(self):
        profile = LifecycleCostProfile(
            profile_id="profile:commission-only:v1",
            decision_scope_ref=SCOPE,
            instrument_version=INSTRUMENT_VERSION,
            as_of=BASE,
            horizon_end=HORIZON,
            requirements=LifecycleCostRequirements(
                requirements_ref="requirements:commission-only:v1",
                required_kinds=("COMMISSION",),
            ),
            components=(
                LifecycleCostComponent(
                    component_id="component:commission",
                    phase="TURNOVER",
                    kind="COMMISSION",
                    normalized_rate="0.001",
                    evidence_ref="evidence:commission:v1",
                    observed_at=BASE,
                    valid_until=HORIZON,
                ),
            ),
        )
        projection = _project(profile)

        self.assertEqual(projection.cost_rate, Decimal("0.001"))
        for bucket in ("financing", "funding", "borrow", "fx"):
            self.assertEqual(projection.cost_rate_components[bucket], Decimal("0"))
            self.assertRegex(
                projection.cost_evidence_refs[bucket],
                r"^sha256:[0-9a-f]{64}$",
            )


if __name__ == "__main__":
    unittest.main()
