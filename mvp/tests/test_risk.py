from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import unittest
from uuid import NAMESPACE_URL, uuid5

from mvp.autotrade_mvp.risk import (
    _canonical_decimal_text,
    LiquidationHeadroomEvidence,
    LiquidationScope,
    RiskContext,
    RiskIntent,
    RiskPolicy,
    evaluate_risk,
    risk_decision_fingerprint,
    stress_scenario_digest,
    tail_scenario_set_digest,
)


LIQUIDATION_BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _LiquidationEvidenceStore:
    def __init__(self):
        self._manifests = {}
        self._objects = {}

    def add(self, evidence, payload):
        raw = json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        actual = "sha256:" + sha256(raw).hexdigest()
        if actual != evidence.sha256:
            raise AssertionError("test evidence digest mismatch")
        self._objects[evidence.artifact_id] = raw
        self._manifests[evidence.artifact_id] = {
            "artifact_id": evidence.artifact_id,
            "sha256": evidence.sha256,
            "manifest_hash": "sha256:" + "f" * 64,
            "metadata": dict(payload),
        }

    def load_manifest(self, artifact_id):
        return dict(self._manifests[artifact_id])

    def read_bytes(self, artifact_id):
        return self._objects[artifact_id]


def liquidation_evidence(
    store,
    *,
    headroom="0.25",
    provider_id="BYBIT",
    account_id="acct-1",
    environment="PAPER",
    margin_mode="CROSS",
    risk_tier_version="tier-v1",
    state_version=7,
    observed_at=LIQUIDATION_BASE,
    expires_at=None,
):
    expires = expires_at or (observed_at + timedelta(hours=1))
    payload = {
        "artifact_kind": "LIQUIDATION_HEADROOM_EVIDENCE",
        "schema_version": 1,
        "provider_id": provider_id,
        "account_id": account_id,
        "environment": environment,
        "margin_mode": margin_mode,
        "risk_tier_version": risk_tier_version,
        "state_version": state_version,
        "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": expires.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "headroom": (
            "0"
            if Decimal(headroom) == 0
            else format(Decimal(headroom).normalize(), "f")
        ),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = "sha256:" + sha256(raw).hexdigest()
    artifact_id = str(uuid5(NAMESPACE_URL, digest))
    evidence = LiquidationHeadroomEvidence.create(
        headroom=headroom,
        state_version=state_version,
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        margin_mode=margin_mode,
        risk_tier_version=risk_tier_version,
        observed_at=observed_at,
        expires_at=expires,
        artifact_id=artifact_id,
        sha256=digest,
    )
    store.add(evidence, payload)
    scope = LiquidationScope(
        provider_id=provider_id,
        account_id=account_id,
        environment=environment,
        margin_mode=margin_mode,
        risk_tier_version=risk_tier_version,
    )
    return {
        "liquidation_headroom": headroom,
        "liquidation_scope": scope,
        "liquidation_headroom_evidence": evidence,
        "decision_time": observed_at + timedelta(minutes=1),
    }


def policy(**overrides):
    values = dict(
        max_abs_position="10",
        max_single_notional="2000",
        max_gross_leverage="2",
        max_net_leverage="2",
        max_daily_loss="500",
        max_drawdown_fraction="0.20",
        max_data_age_seconds="5",
        max_fx_age_seconds="60",
        min_margin_headroom="0.20",
        max_stress_loss="500",
        max_asset_concentration_fraction=None,
        max_venue_concentration_fraction=None,
        max_order_participation_fraction=None,
        max_abs_factor_exposure=None,
        max_spread_fraction=None,
        max_slippage_fraction=None,
        max_clock_age_seconds=None,
        allowed_actions=None,
        require_settlement_evidence=False,
        require_option_exercise_evidence=False,
        min_futures_delivery_headroom_seconds=None,
    )
    values.update(overrides)
    return RiskPolicy.create(**values)


def context(**overrides):
    values = dict(
        state_version=7,
        equity="1000",
        positions={"ABC": "2"},
        marks={"ABC": "100", "XYZ": "50"},
        reserved_position_delta={},
        daily_pnl="-10",
        drawdown_fraction="0.05",
        market_data_age_seconds="1",
        fx_age_seconds={"USD": "10"},
        margin_headroom="0.50",
        capability_allowed=True,
        borrow_available=True,
        stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.20"},),
    )
    values.update(overrides)
    return RiskContext.create(**values)


class IndependentRiskTests(unittest.TestCase):
    def test_boundary_equal_to_limits_is_admitted(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="8", price="100",
                expected_state_version=7,
            ),
            context(),
            policy(max_abs_position="10", max_single_notional="1000"),
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.resulting_position, Decimal("10"))

    def test_stale_state_and_data_fail_closed(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=6,
        )
        decision = evaluate_risk(
            intent,
            context(market_data_age_seconds="6", fx_age_seconds={"USD": "61"}),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"state_version", "market_freshness", "fx_freshness"} <= failed)
        self.assertFalse(decision.admitted)

    def test_required_fx_evidence_fails_closed_when_missing(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        missing = evaluate_risk(
            intent,
            context(fx_age_seconds={}, fx_required=True),
            policy(),
        )
        rule = next(item for item in missing.rules if item.rule == "fx_freshness")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "UNKNOWN")
        self.assertFalse(missing.admitted)

        not_required = evaluate_risk(
            intent,
            context(fx_age_seconds={}, fx_required=False),
            policy(),
        )
        self.assertTrue(
            next(item for item in not_required.rules if item.rule == "fx_freshness").passed
        )

    def test_fx_required_must_be_real_boolean(self):
        with self.assertRaises(TypeError):
            context(fx_required="yes")

    def test_reserved_exposure_counts_against_position_and_leverage(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="3", price="100",
                expected_state_version=7,
            ),
            context(reserved_position_delta={"ABC": "6"}),
            policy(max_abs_position="10"),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("position_limit", failed)
        self.assertEqual(decision.resulting_position, Decimal("11"))

    def test_short_requires_affirmative_borrow(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="3", price="100",
                expected_state_version=7,
            ),
            context(positions={"ABC": "0"}, borrow_available=None),
            policy(),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("short_borrow", {r.rule for r in decision.rules if not r.passed})

    def test_reduce_only_sell_cannot_cross_through_flat(self):
        intent = RiskIntent.create(
            symbol="ABC", side="SELL", quantity="3", price="100",
            expected_state_version=7, reduce_only=True,
        )
        decision = evaluate_risk(intent, context(positions={"ABC": "2"}), policy())
        self.assertFalse(decision.admitted)
        self.assertIn("reduce_only", {r.rule for r in decision.rules if not r.passed})

    def test_reduce_only_cannot_use_pending_reservation_to_increase_current_position(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="BUY",
                quantity="4",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                reserved_position_delta={"ABC": "-15"},
                stress_scenarios=({"ABC": "-0.10"},),
            ),
            policy(),
        )
        self.assertEqual(decision.resulting_position, Decimal("-1"))
        reduce_rule = next(item for item in decision.rules if item.rule == "reduce_only")
        self.assertFalse(reduce_rule.passed)
        self.assertIn("current=14", reduce_rule.observed)
        self.assertFalse(decision.admitted)

    def test_genuine_reduce_only_can_decrease_risk_while_account_is_over_limits(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="2",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                marks={"ABC": "100"},
                daily_pnl="-600",
                drawdown_fraction="0.30",
                margin_headroom="0.10",
                stress_scenarios=({"ABC": "-0.50"},),
            ),
            policy(
                max_abs_position="5",
                max_single_notional="100",
                max_gross_leverage="0.5",
                max_net_leverage="0.5",
                max_daily_loss="100",
                max_drawdown_fraction="0.10",
                min_margin_headroom="0.30",
                max_stress_loss="50",
            ),
        )
        self.assertTrue(decision.admitted)
        self.assertEqual(decision.resulting_position, Decimal("8"))
        self.assertFalse({r.rule for r in decision.rules if not r.passed})

    def test_reduce_only_does_not_get_exception_if_portfolio_net_risk_worsens(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10", "XYZ": "-20"},
                marks={"ABC": "100", "XYZ": "50"},
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(
                max_abs_position="5",
                max_gross_leverage="1",
                max_net_leverage="0.05",
            ),
        )
        self.assertFalse(decision.admitted)
        failed = {r.rule for r in decision.rules if not r.passed}
        self.assertTrue({"position_limit", "gross_leverage", "net_leverage"} & failed)

    def test_protective_reduction_still_requires_fresh_data_and_state(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=6,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                marks={"ABC": "100"},
                market_data_age_seconds="10",
            ),
            policy(max_abs_position="5", max_data_age_seconds="5"),
        )
        self.assertFalse(decision.admitted)
        failed = {r.rule for r in decision.rules if not r.passed}
        self.assertTrue({"state_version", "market_freshness"} <= failed)

    def test_unmarked_reduction_cannot_bypass_breached_caps(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=False,
            ),
            context(positions={"ABC": "10"}, marks={"ABC": "100"}),
            policy(max_abs_position="5", max_gross_leverage="0.5"),
        )
        self.assertFalse(decision.admitted)

    def test_high_order_price_cannot_bypass_single_notional_limit(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="6", price="200",
                expected_state_version=7,
            ),
            context(positions={"ABC": "0"}, marks={"ABC": "100"}),
            policy(max_single_notional="1000"),
        )
        self.assertFalse(decision.admitted)
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("single_notional", failed)

    def test_risk_boolean_inputs_fail_closed(self):
        with self.assertRaises(TypeError):
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, reduce_only="false",
            )
        with self.assertRaises(TypeError):
            context(capability_allowed="false")
        with self.assertRaises(TypeError):
            context(borrow_available="true")

    def test_drawdown_fraction_above_one_is_rejected(self):
        with self.assertRaises(ValueError):
            context(drawdown_fraction="1.01")

    def test_stress_and_daily_loss_and_drawdown_are_independent_hard_rules(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "8"},
                daily_pnl="-600",
                drawdown_fraction="0.25",
                stress_scenarios=({"ABC": "-0.80"},),
            ),
            policy(max_stress_loss="500"),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"daily_loss", "drawdown", "stress_loss"} <= failed)

    def test_capability_and_margin_are_not_strategy_overridable(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(capability_allowed=False, margin_headroom="0.10"),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertTrue({"capability", "margin_headroom"} <= failed)
        self.assertFalse(decision.admitted)

    def test_missing_mark_fails_before_admission(self):
        with self.assertRaisesRegex(ValueError, "Missing mark"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC", side="BUY", quantity="1", price="100",
                    expected_state_version=7,
                ),
                context(marks={"XYZ": "50"}),
                policy(),
            )

    def test_missing_stress_scenarios_block_nonzero_exposure(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(stress_scenarios=()),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("stress_coverage", failed)
        self.assertIn("stress_loss", failed)
        self.assertFalse(decision.admitted)

    def test_incomplete_stress_scenario_cannot_hide_existing_position_risk(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "3"},
                stress_scenarios=({"ABC": "-0.10"},),
            ),
            policy(),
        )
        failed = {rule.rule for rule in decision.rules if not rule.passed}
        self.assertIn("stress_coverage", failed)
        stress_rule = next(rule for rule in decision.rules if rule.rule == "stress_coverage")
        self.assertEqual(stress_rule.observed, "XYZ")
        self.assertFalse(decision.admitted)

    def test_full_liquidation_does_not_require_artificial_stress_scenario(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, reduce_only=True,
            ),
            context(
                positions={"ABC": "-1"},
                stress_scenarios=(),
            ),
            policy(),
        )
        coverage = next(rule for rule in decision.rules if rule.rule == "stress_coverage")
        self.assertTrue(coverage.passed)
        self.assertTrue(decision.admitted)

    def test_semantically_duplicate_identity_keys_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                positions={"ABC": "1", " ABC ": "2"},
                marks={"ABC": "100"},
            )
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                marks={"ABC": "100", " ABC ": "101"},
            )
        with self.assertRaisesRegex(ValueError, "unique after normalization"):
            context(
                stress_scenarios=(
                    {"ABC": "-0.1", " ABC ": "-0.2", "XYZ": "-0.2"},
                ),
            )

    def test_risk_mapping_and_scenario_container_types_fail_closed(self):
        with self.assertRaisesRegex(TypeError, "positions must be a mapping"):
            context(positions=[("ABC", "1")])
        with self.assertRaisesRegex(TypeError, "stress_scenarios must be a sequence"):
            context(stress_scenarios="ABC:-0.1")

    def test_float_inputs_are_rejected(self):
        with self.assertRaises(TypeError):
            RiskPolicy.create(
                max_abs_position=10.0,
                max_single_notional="1000",
                max_gross_leverage="2",
                max_net_leverage="2",
                max_daily_loss="100",
                max_drawdown_fraction="0.2",
                max_data_age_seconds="5",
                max_fx_age_seconds="60",
                min_margin_headroom="0.2",
                max_stress_loss="100",
            )


    def test_authority_margin_and_borrow_evidence_cannot_be_omitted(self):
        common = dict(
            state_version=7,
            equity="1000",
            positions={"ABC": "2"},
            marks={"ABC": "100"},
        )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                capability_allowed=True,
                borrow_available=True,
            )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                margin_headroom="0.50",
                borrow_available=True,
            )
        with self.assertRaises(TypeError):
            RiskContext.create(
                **common,
                margin_headroom="0.50",
                capability_allowed=True,
            )

    def test_explicit_unknown_borrow_blocks_new_short_but_not_long(self):
        unknown = context(borrow_available=None)
        short = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="3", price="100",
                expected_state_version=7,
            ),
            unknown,
            policy(),
        )
        self.assertFalse(short.admitted)
        failed = {rule.rule for rule in short.rules if not rule.passed}
        self.assertIn("short_borrow", failed)

        long = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            unknown,
            policy(),
        )
        self.assertTrue(long.admitted)

    def test_configured_liquidity_participation_fails_closed_without_capacity(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(max_order_participation_fraction="0.10")

        missing = evaluate_risk(intent, context(liquidity_capacity={}), configured)
        missing_rule = next(
            rule for rule in missing.rules if rule.rule == "liquidity_participation"
        )
        self.assertFalse(missing_rule.passed)
        self.assertEqual(missing_rule.observed, "UNKNOWN")

        oversized = evaluate_risk(
            intent,
            context(liquidity_capacity={"ABC": "5"}),
            configured,
        )
        oversized_rule = next(
            rule for rule in oversized.rules if rule.rule == "liquidity_participation"
        )
        self.assertFalse(oversized_rule.passed)
        self.assertEqual(oversized_rule.observed, "0.2")

    def test_asset_concentration_uses_whole_projected_portfolio(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "10"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(max_asset_concentration_fraction="0.65"),
        )
        rule = next(rule for rule in decision.rules if rule.rule == "asset_concentration")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "0.625")

        blocked = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "10"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(max_asset_concentration_fraction="0.60"),
        )
        # XYZ is 500 of 800 gross = 0.625, so the configured 0.60 cap blocks.
        self.assertFalse(blocked.admitted)
        self.assertIn(
            "asset_concentration",
            {item.rule for item in blocked.rules if not item.passed},
        )

    def test_concentration_requires_complete_bucket_and_venue_identity(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "3"},
                asset_buckets={"ABC": "TECH"},
                venues={"ABC": "VENUE-A"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(
                max_asset_concentration_fraction="1",
                max_venue_concentration_fraction="1",
            ),
        )
        failed = {item.rule: item.observed for item in decision.rules if not item.passed}
        self.assertEqual(failed["asset_concentration"], "MISSING:XYZ")
        self.assertEqual(failed["venue_concentration"], "MISSING:XYZ")

    def test_balanced_concentration_and_liquidity_can_pass(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "4"},
                asset_buckets={"ABC": "TECH", "XYZ": "INDUSTRIAL"},
                venues={"ABC": "VENUE-A", "XYZ": "VENUE-B"},
                liquidity_capacity={"ABC": "10"},
                stress_scenarios=(
                    {"ABC": "-0.10", "XYZ": "-0.10"},
                ),
            ),
            policy(
                max_asset_concentration_fraction="0.60",
                max_venue_concentration_fraction="0.60",
                max_order_participation_fraction="0.20",
            ),
        )
        self.assertTrue(decision.admitted)

    def test_optional_risk_fractions_reject_float_and_values_above_one(self):
        with self.assertRaises(TypeError):
            policy(max_order_participation_fraction=0.1)
        with self.assertRaises(ValueError):
            policy(max_asset_concentration_fraction="1.01")
        with self.assertRaises(ValueError):
            policy(max_venue_concentration_fraction="1.01")

    def test_factor_exposure_aggregates_correlated_positions(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "2", "XYZ": "4"},
                factor_loadings={
                    "ABC": {"EQUITY": "1"},
                    "XYZ": {"EQUITY": "0.8"},
                },
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="450"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "460.0")
        self.assertFalse(decision.admitted)

    def test_factor_exposure_recognizes_signed_hedge(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "1", "XYZ": "-4"},
                factor_loadings={
                    "ABC": {"EQUITY": "1"},
                    "XYZ": {"EQUITY": "1"},
                },
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="50"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "0")

    def test_factor_exposure_fails_closed_on_missing_loading(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                positions={"ABC": "1", "XYZ": "1"},
                factor_loadings={"ABC": {"EQUITY": "1"}},
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            policy(max_abs_factor_exposure="1000"),
        )
        rule = next(item for item in decision.rules if item.rule == "factor_exposure")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "MISSING:XYZ")

    def test_factor_loading_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(factor_loadings={"ABC": {"EQUITY": 1.0}})

    def test_execution_quality_limits_fail_closed_on_missing_evidence(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(spread_fraction={}, slippage_fraction={}),
            policy(max_spread_fraction="0.01", max_slippage_fraction="0.02"),
        )
        failed = {item.rule: item.observed for item in decision.rules if not item.passed}
        self.assertEqual(failed["spread"], "UNKNOWN")
        self.assertEqual(failed["slippage"], "UNKNOWN")

    def test_execution_quality_limits_block_excess_cost(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(
                spread_fraction={"ABC": "0.005"},
                slippage_fraction={"ABC": "0.03"},
            ),
            policy(max_spread_fraction="0.01", max_slippage_fraction="0.02"),
        )
        spread = next(item for item in decision.rules if item.rule == "spread")
        slippage = next(item for item in decision.rules if item.rule == "slippage")
        self.assertTrue(spread.passed)
        self.assertFalse(slippage.passed)
        self.assertFalse(decision.admitted)

    def test_execution_quality_evidence_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(spread_fraction={"ABC": 0.01})
        with self.assertRaises(TypeError):
            context(slippage_fraction={"ABC": 0.01})

    def test_clock_freshness_fails_closed_when_required_evidence_missing(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(clock_age_seconds=None),
            policy(max_clock_age_seconds="2"),
        )
        rule = next(item for item in decision.rules if item.rule == "clock_freshness")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "UNKNOWN")

    def test_clock_freshness_boundary_is_exact(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        exact = evaluate_risk(
            intent,
            context(clock_age_seconds="2"),
            policy(max_clock_age_seconds="2"),
        )
        stale = evaluate_risk(
            intent,
            context(clock_age_seconds="2.0001"),
            policy(max_clock_age_seconds="2"),
        )
        self.assertTrue(next(x for x in exact.rules if x.rule == "clock_freshness").passed)
        self.assertFalse(next(x for x in stale.rules if x.rule == "clock_freshness").passed)

    def test_clock_age_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(clock_age_seconds=0.1)

    def test_action_policy_blocks_disallowed_action_class(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7, action="HEDGE",
            ),
            context(),
            policy(allowed_actions=("TRADE", "REDUCE")),
        )
        rule = next(item for item in decision.rules if item.rule == "allowed_action")
        self.assertFalse(rule.passed)
        self.assertEqual(rule.observed, "HEDGE")
        self.assertFalse(decision.admitted)

    def test_reduce_and_flatten_labels_require_reduce_only_semantics(self):
        with self.assertRaisesRegex(ValueError, "requires reduce_only"):
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="1", price="100",
                expected_state_version=7, action="REDUCE",
            )
        with self.assertRaisesRegex(ValueError, "requires reduce_only"):
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="1", price="100",
                expected_state_version=7, action="FLATTEN",
            )

    def test_action_label_does_not_override_numeric_risk(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="20", price="100",
                expected_state_version=7, action="HEDGE",
            ),
            context(),
            policy(allowed_actions=("HEDGE",)),
        )
        self.assertFalse(decision.admitted)
        self.assertIn("position_limit", {x.rule for x in decision.rules if not x.passed})

    def test_allowed_action_configuration_rejects_duplicates_and_unknowns(self):
        with self.assertRaises(ValueError):
            policy(allowed_actions=("TRADE", "trade"))
        with self.assertRaises(ValueError):
            policy(allowed_actions=("MAGIC",))

    def test_settlement_policy_fails_closed_without_affirmative_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        missing = evaluate_risk(
            intent,
            context(settlement_allowed=None),
            policy(require_settlement_evidence=True),
        )
        blocked = evaluate_risk(
            intent,
            context(settlement_allowed=False),
            policy(require_settlement_evidence=True),
        )
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "settlement").observed,
            "UNKNOWN",
        )
        self.assertFalse(next(x for x in missing.rules if x.rule == "settlement").passed)
        self.assertFalse(next(x for x in blocked.rules if x.rule == "settlement").passed)

    def test_settlement_policy_accepts_only_explicit_true(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
            ),
            context(settlement_allowed=True),
            policy(require_settlement_evidence=True),
        )
        self.assertTrue(next(x for x in decision.rules if x.rule == "settlement").passed)

    def test_settlement_inputs_require_real_booleans(self):
        with self.assertRaises(TypeError):
            context(settlement_allowed="true")
        with self.assertRaises(TypeError):
            policy(require_settlement_evidence="true")

    def test_option_exercise_requires_verified_deliverable_and_buying_power(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="5",
                expected_state_version=7,
                action="EXERCISE", instrument_type="OPTION",
            ),
            context(
                option_deliverable_verified=None,
                option_exercise_cash_required="5000",
                option_exercise_cash_available="4999.99",
            ),
            policy(
                max_single_notional="10000",
                require_option_exercise_evidence=True,
            ),
        )
        failed = {item.rule for item in decision.rules if not item.passed}
        self.assertIn("option_deliverable", failed)
        self.assertIn("option_exercise_funding", failed)

    def test_option_exercise_accepts_exact_buying_power_boundary(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="5",
                expected_state_version=7,
                action="EXERCISE", instrument_type="OPTION",
            ),
            context(
                option_deliverable_verified=True,
                option_exercise_cash_required="5000",
                option_exercise_cash_available="5000",
            ),
            policy(
                max_single_notional="10000",
                require_option_exercise_evidence=True,
            ),
        )
        option_rules = {
            item.rule: item.passed
            for item in decision.rules
            if item.rule.startswith("option_")
        }
        self.assertEqual(
            option_rules,
            {"option_deliverable": True, "option_exercise_funding": True},
        )

    def test_exercise_action_cannot_be_labeled_on_non_option(self):
        with self.assertRaisesRegex(ValueError, "requires OPTION"):
            RiskIntent.create(
                symbol="ABC", side="BUY", quantity="1", price="100",
                expected_state_version=7,
                action="EXERCISE", instrument_type="EQUITY",
            )

    def test_option_obligation_inputs_reject_binary_float_and_fake_booleans(self):
        with self.assertRaises(TypeError):
            context(option_exercise_cash_required=5000.0)
        with self.assertRaises(TypeError):
            context(option_deliverable_verified="true")
        with self.assertRaises(TypeError):
            policy(require_option_exercise_evidence="true")

    def test_future_new_risk_requires_delivery_headroom_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7, instrument_type="FUTURE",
        )
        missing = evaluate_risk(
            intent,
            context(futures_delivery_headroom_seconds={}),
            policy(min_futures_delivery_headroom_seconds="3600"),
        )
        too_close = evaluate_risk(
            intent,
            context(futures_delivery_headroom_seconds={"ABC": "3599.9"}),
            policy(min_futures_delivery_headroom_seconds="3600"),
        )
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "futures_delivery_cutoff").observed,
            "UNKNOWN",
        )
        self.assertFalse(
            next(x for x in too_close.rules if x.rule == "futures_delivery_cutoff").passed
        )

    def test_future_reduce_only_can_flatten_inside_delivery_cutoff(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC", side="SELL", quantity="2", price="100",
                expected_state_version=7, reduce_only=True,
                action="FLATTEN", instrument_type="FUTURE",
            ),
            context(
                positions={"ABC": "2"},
                futures_delivery_headroom_seconds={"ABC": "-10"},
                stress_scenarios=(),
            ),
            policy(
                min_futures_delivery_headroom_seconds="3600",
                allowed_actions=("FLATTEN",),
            ),
        )
        rule = next(x for x in decision.rules if x.rule == "futures_delivery_cutoff")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "RISK_REDUCTION")

    def test_future_delivery_headroom_rejects_binary_float(self):
        with self.assertRaises(TypeError):
            context(futures_delivery_headroom_seconds={"ABC": 3600.0})
        with self.assertRaises(TypeError):
            policy(min_futures_delivery_headroom_seconds=3600.0)

    def test_configured_stress_regimes_require_explicit_labeled_coverage(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(
            required_stress_scenario_labels=(
                "price_gap",
                "correlation_one",
                "venue_loss",
            ),
            required_stress_scenario_digests={
                "price_gap": stress_scenario_digest({"ABC": "-0.10"}),
                "correlation_one": stress_scenario_digest({"ABC": "-0.20"}),
                "venue_loss": stress_scenario_digest({"ABC": "-0.30"}),
            },
        )
        decision = evaluate_risk(
            intent,
            context(
                stress_scenarios=(
                    {"ABC": "-0.10"},
                    {"ABC": "-0.20"},
                    {"ABC": "-0.30"},
                ),
                stress_scenario_labels=(
                    "price_gap",
                    "correlation_one",
                    "venue_loss",
                ),
            ),
            configured,
        )
        rule = next(x for x in decision.rules if x.rule == "stress_regime_coverage")
        self.assertTrue(rule.passed)

        missing = evaluate_risk(
            intent,
            context(
                stress_scenarios=(
                    {"ABC": "-0.10"},
                    {"ABC": "-0.20"},
                ),
                stress_scenario_labels=("price_gap", "correlation_one"),
            ),
            configured,
        )
        missing_rule = next(
            x for x in missing.rules if x.rule == "stress_regime_coverage"
        )
        self.assertFalse(missing_rule.passed)
        self.assertEqual(missing_rule.observed, "MISSING:venue_loss")
        self.assertFalse(missing.admitted)

        substituted = evaluate_risk(
            intent,
            context(
                stress_scenarios=(
                    {"ABC": "-0.10"},
                    {"ABC": "-0.01"},
                    {"ABC": "-0.30"},
                ),
                stress_scenario_labels=(
                    "price_gap",
                    "correlation_one",
                    "venue_loss",
                ),
            ),
            configured,
        )
        substituted_rule = next(
            x for x in substituted.rules if x.rule == "stress_regime_coverage"
        )
        self.assertFalse(substituted_rule.passed)
        self.assertEqual(substituted_rule.observed, "MISMATCH:correlation_one")
        self.assertFalse(substituted.admitted)

    def test_full_liquidation_does_not_require_artificial_stress_regime_labels(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="2",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "2"},
                stress_scenarios=(),
                stress_scenario_labels=(),
            ),
            policy(
                required_stress_scenario_labels=("price_gap", "correlation_one"),
                required_stress_scenario_digests={
                    "price_gap": stress_scenario_digest({"ABC": "-0.10"}),
                    "correlation_one": stress_scenario_digest({"ABC": "-0.20"}),
                },
                max_expected_shortfall="0",
                expected_shortfall_tail_fraction="1",
                required_tail_scenario_set_digest=tail_scenario_set_digest(
                    ({"ABC": "-0.10"},)
                ),
            ),
        )
        regime = next(
            x for x in decision.rules if x.rule == "stress_regime_coverage"
        )
        expected_shortfall = next(
            x for x in decision.rules if x.rule == "expected_shortfall"
        )
        self.assertTrue(regime.passed)
        self.assertEqual(regime.observed, "NO_PROJECTED_RISK")
        self.assertTrue(expected_shortfall.passed)
        self.assertEqual(expected_shortfall.observed, "0")
        self.assertTrue(decision.admitted)

    def test_stress_scenario_labels_are_unique_and_align_with_scenarios(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            context(
                stress_scenarios=({"ABC": "-0.10"}, {"ABC": "-0.20"}),
                stress_scenario_labels=("gap", "gap"),
            )
        with self.assertRaisesRegex(ValueError, "one-to-one"):
            context(
                stress_scenarios=({"ABC": "-0.10"}, {"ABC": "-0.20"}),
                stress_scenario_labels=("gap",),
            )
        with self.assertRaisesRegex(ValueError, "at least one"):
            policy(required_stress_scenario_labels=())
        with self.assertRaisesRegex(ValueError, "configured together"):
            policy(required_stress_scenario_labels=("gap",))
        with self.assertRaisesRegex(ValueError, "exactly match"):
            policy(
                required_stress_scenario_labels=("gap",),
                required_stress_scenario_digests={
                    "other": stress_scenario_digest({"ABC": "-0.10"}),
                },
            )

    def test_expected_shortfall_uses_complete_projected_tail_distribution(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        tail = (
            {"ABC": "-0.10"},
            {"ABC": "-0.20"},
            {"ABC": "0.05"},
            {"ABC": "-0.40"},
        )
        boundary = evaluate_risk(
            intent,
            context(tail_scenarios=tail),
            policy(
                max_expected_shortfall="90",
                expected_shortfall_tail_fraction="0.50",
                required_tail_scenario_set_digest=tail_scenario_set_digest(tail),
            ),
        )
        rule = next(x for x in boundary.rules if x.rule == "expected_shortfall")
        self.assertTrue(rule.passed)
        self.assertEqual(rule.observed, "90")

        blocked = evaluate_risk(
            intent,
            context(tail_scenarios=tail),
            policy(
                max_expected_shortfall="89.99",
                expected_shortfall_tail_fraction="0.50",
                required_tail_scenario_set_digest=tail_scenario_set_digest(tail),
            ),
        )
        self.assertFalse(blocked.admitted)
        blocked_rule = next(x for x in blocked.rules if x.rule == "expected_shortfall")
        self.assertFalse(blocked_rule.passed)
        self.assertEqual(blocked_rule.observed, "90")

    def test_expected_shortfall_fails_closed_without_complete_tail_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        frozen_tail = ({"ABC": "-0.10", "XYZ": "-0.10"},)
        configured = policy(
            max_expected_shortfall="500",
            expected_shortfall_tail_fraction="0.25",
            required_tail_scenario_set_digest=tail_scenario_set_digest(frozen_tail),
        )
        missing = evaluate_risk(
            intent,
            context(tail_scenarios=()),
            configured,
        )
        self.assertFalse(missing.admitted)
        self.assertFalse(next(x for x in missing.rules if x.rule == "tail_coverage").passed)
        self.assertEqual(
            next(x for x in missing.rules if x.rule == "expected_shortfall").observed,
            "UNKNOWN",
        )

        incomplete = evaluate_risk(
            intent,
            context(
                positions={"ABC": "2", "XYZ": "1"},
                tail_scenarios=({"ABC": "-0.10"},),
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            configured,
        )
        coverage = next(x for x in incomplete.rules if x.rule == "tail_coverage")
        self.assertFalse(coverage.passed)
        self.assertEqual(coverage.observed, "XYZ")

        cherry_picked = evaluate_risk(
            intent,
            context(
                positions={"ABC": "2", "XYZ": "1"},
                tail_scenarios=({"ABC": "-0.01", "XYZ": "-0.01"},),
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
            ),
            configured,
        )
        cherry_coverage = next(
            x for x in cherry_picked.rules if x.rule == "tail_coverage"
        )
        self.assertFalse(cherry_coverage.passed)
        self.assertEqual(cherry_coverage.observed, "DISTRIBUTION_MISMATCH")
        self.assertFalse(cherry_picked.admitted)

    def test_expected_shortfall_policy_requires_explicit_tail_fraction(self):
        with self.assertRaisesRegex(ValueError, "configured together"):
            policy(max_expected_shortfall="100")
        with self.assertRaisesRegex(ValueError, "configured together"):
            policy(expected_shortfall_tail_fraction="0.05")
        with self.assertRaises(ValueError):
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="1.01",
            )
        with self.assertRaisesRegex(ValueError, "frozen tail distribution digest"):
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="0.05",
            )

    def test_liquidation_headroom_is_fail_closed_and_exact_at_boundary(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(min_liquidation_headroom="0.25")
        missing = evaluate_risk(
            intent,
            context(liquidation_headroom=None),
            configured,
        )
        missing_rule = next(
            x for x in missing.rules if x.rule == "liquidation_headroom"
        )
        self.assertFalse(missing_rule.passed)
        self.assertEqual(missing_rule.observed, "UNKNOWN")

        # A numerically valid caller value is no longer financial authority.
        bare = evaluate_risk(
            intent,
            context(liquidation_headroom="0.25"),
            configured,
        )
        bare_rule = next(
            x for x in bare.rules if x.rule == "liquidation_headroom"
        )
        self.assertFalse(bare_rule.passed)
        self.assertEqual(bare_rule.observed, "UNVERIFIED")

        store = _LiquidationEvidenceStore()
        bound = liquidation_evidence(store, headroom="0.25")
        exact = evaluate_risk(
            intent,
            context(**bound),
            configured,
            evidence_store=store,
        )
        exact_rule = next(
            x for x in exact.rules if x.rule == "liquidation_headroom"
        )
        self.assertTrue(exact_rule.passed)
        self.assertEqual(exact_rule.observed, "0.25")
        self.assertTrue(exact.admitted)

    def test_negative_liquidation_headroom_is_evidence_not_a_parse_failure(self):
        configured = policy(min_liquidation_headroom="0.25")
        store = _LiquidationEvidenceStore()
        bound = liquidation_evidence(store, headroom="-0.10")
        increasing = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="BUY",
                quantity="1",
                price="100",
                expected_state_version=7,
            ),
            context(**bound),
            configured,
            evidence_store=store,
        )
        increasing_rule = next(
            x for x in increasing.rules if x.rule == "liquidation_headroom"
        )
        self.assertFalse(increasing_rule.passed)
        self.assertEqual(increasing_rule.observed, "-0.10")
        self.assertFalse(increasing.admitted)

        protective = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "2"},
                stress_scenarios=({"ABC": "-0.10"},),
                **bound,
            ),
            configured,
            evidence_store=store,
        )
        protective_rule = next(
            x for x in protective.rules if x.rule == "liquidation_headroom"
        )
        self.assertTrue(protective_rule.passed)
        self.assertEqual(protective_rule.observed, "-0.10")
        self.assertTrue(protective.admitted)

    def test_reduce_only_cannot_use_exception_when_tail_risk_worsens(self):
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10", "XYZ": "10"},
                marks={"ABC": "100", "XYZ": "50"},
                stress_scenarios=({"ABC": "-0.10", "XYZ": "-0.10"},),
                tail_scenarios=({"ABC": "0.10", "XYZ": "-1.00"},),
                liquidation_headroom="0.10",
            ),
            policy(
                max_abs_position="5",
                max_expected_shortfall="405",
                expected_shortfall_tail_fraction="1",
                required_tail_scenario_set_digest=tail_scenario_set_digest(
                    ({"ABC": "0.10", "XYZ": "-1.00"},)
                ),
                min_liquidation_headroom="0.25",
            ),
        )
        failed = {item.rule for item in decision.rules if not item.passed}
        self.assertIn("expected_shortfall", failed)
        self.assertIn("liquidation_headroom", failed)
        self.assertIn("position_limit", failed)
        self.assertEqual(
            next(x for x in decision.rules if x.rule == "expected_shortfall").observed,
            "410.00",
        )
        self.assertFalse(decision.admitted)

    def test_reduce_only_exception_requires_base_scenario_coverage_for_removed_hedge(self):
        intent = RiskIntent.create(
            symbol="HEDGE",
            side="SELL",
            quantity="1",
            price="100",
            expected_state_version=7,
            reduce_only=True,
        )
        configured = policy(
            max_abs_position="0.5",
            max_expected_shortfall="10",
            expected_shortfall_tail_fraction="1",
            required_tail_scenario_set_digest=tail_scenario_set_digest(
                ({"HEDGE": "0.50", "CORE": "-0.50"},)
            ),
            min_liquidation_headroom="0.25",
            max_stress_loss="10",
        )
        # HEDGE is fully removed by the intent while CORE remains projected.
        # A scenario that omits HEDGE cannot prove that removing it is
        # non-worsening: the omitted base position may have been the hedge.
        incomplete = evaluate_risk(
            intent,
            context(
                positions={"HEDGE": "1", "CORE": "1"},
                marks={"HEDGE": "100", "CORE": "100"},
                stress_scenarios=({"CORE": "-0.50"},),
                tail_scenarios=({"CORE": "-0.50"},),
                liquidation_headroom="0.10",
            ),
            configured,
        )
        self.assertFalse(incomplete.admitted)
        failed = {item.rule for item in incomplete.rules if not item.passed}
        self.assertIn("position_limit", failed)
        self.assertIn("expected_shortfall", failed)
        self.assertIn("liquidation_headroom", failed)

        # With complete base coverage we can actually evaluate the hedge
        # removal. Here HEDGE offsets CORE in the base portfolio, so removing
        # it worsens tail loss and the protective exception still must not fire.
        complete = evaluate_risk(
            intent,
            context(
                positions={"HEDGE": "1", "CORE": "1"},
                marks={"HEDGE": "100", "CORE": "100"},
                stress_scenarios=({"HEDGE": "0.50", "CORE": "-0.50"},),
                tail_scenarios=({"HEDGE": "0.50", "CORE": "-0.50"},),
                liquidation_headroom="0.10",
            ),
            configured,
        )
        self.assertFalse(complete.admitted)
        self.assertEqual(
            next(x for x in complete.rules if x.rule == "expected_shortfall").observed,
            "50",
        )

    def test_strict_reduce_only_can_pass_known_liquidation_breach_when_tail_improves(self):
        store = _LiquidationEvidenceStore()
        bound = liquidation_evidence(store, headroom="0.10")
        decision = evaluate_risk(
            RiskIntent.create(
                symbol="ABC",
                side="SELL",
                quantity="1",
                price="100",
                expected_state_version=7,
                reduce_only=True,
            ),
            context(
                positions={"ABC": "10"},
                marks={"ABC": "100"},
                stress_scenarios=({"ABC": "-0.50"},),
                tail_scenarios=({"ABC": "-0.50"},),
                **bound,
            ),
            policy(
                max_abs_position="5",
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="1",
                required_tail_scenario_set_digest=tail_scenario_set_digest(
                    ({"ABC": "-0.50"},)
                ),
                min_liquidation_headroom="0.25",
                max_stress_loss="100",
            ),
            evidence_store=store,
        )
        self.assertTrue(decision.admitted)
        self.assertTrue(
            next(x for x in decision.rules if x.rule == "liquidation_headroom").passed
        )


    def test_liquidation_evidence_scope_cannot_cross_account_environment_or_margin(self):
        store = _LiquidationEvidenceStore()
        bound = liquidation_evidence(
            store,
            provider_id="BYBIT",
            account_id="acct-1",
            environment="PAPER",
            margin_mode="CROSS",
            risk_tier_version="tier-v1",
        )
        evidence = bound["liquidation_headroom_evidence"]
        mismatches = (
            LiquidationScope("KRAKEN", "acct-1", "PAPER", "CROSS", "tier-v1"),
            LiquidationScope("BYBIT", "acct-2", "PAPER", "CROSS", "tier-v1"),
            LiquidationScope("BYBIT", "acct-1", "LIVE", "CROSS", "tier-v1"),
            LiquidationScope("BYBIT", "acct-1", "PAPER", "ISOLATED", "tier-v1"),
            LiquidationScope("BYBIT", "acct-1", "PAPER", "CROSS", "tier-v2"),
        )
        for bad_scope in mismatches:
            with self.subTest(scope=bad_scope):
                with self.assertRaisesRegex(ValueError, "scope differs"):
                    context(
                        liquidation_headroom=evidence.headroom,
                        liquidation_scope=bad_scope,
                        liquidation_headroom_evidence=evidence,
                        decision_time=bound["decision_time"],
                    )

    def test_liquidation_evidence_future_stale_or_tampered_fails_closed(self):
        configured = policy(min_liquidation_headroom="0.25")
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )

        future_store = _LiquidationEvidenceStore()
        future = liquidation_evidence(
            future_store,
            headroom="0.50",
            observed_at=LIQUIDATION_BASE + timedelta(minutes=10),
        )
        future["decision_time"] = LIQUIDATION_BASE + timedelta(minutes=9)
        future_decision = evaluate_risk(
            intent,
            context(**future),
            configured,
            evidence_store=future_store,
        )
        self.assertFalse(
            next(
                x for x in future_decision.rules
                if x.rule == "liquidation_headroom"
            ).passed
        )

        stale_store = _LiquidationEvidenceStore()
        stale = liquidation_evidence(
            stale_store,
            headroom="0.50",
            observed_at=LIQUIDATION_BASE,
            expires_at=LIQUIDATION_BASE + timedelta(minutes=2),
        )
        stale["decision_time"] = LIQUIDATION_BASE + timedelta(minutes=3)
        stale_decision = evaluate_risk(
            intent,
            context(**stale),
            configured,
            evidence_store=stale_store,
        )
        self.assertFalse(
            next(
                x for x in stale_decision.rules
                if x.rule == "liquidation_headroom"
            ).passed
        )

        tampered_store = _LiquidationEvidenceStore()
        tampered = liquidation_evidence(tampered_store, headroom="0.50")
        artifact_id = tampered["liquidation_headroom_evidence"].artifact_id
        tampered_store._objects[artifact_id] = b"{}"
        tampered_decision = evaluate_risk(
            intent,
            context(**tampered),
            configured,
            evidence_store=tampered_store,
        )
        tampered_rule = next(
            x for x in tampered_decision.rules if x.rule == "liquidation_headroom"
        )
        self.assertFalse(tampered_rule.passed)
        self.assertEqual(tampered_rule.observed, "UNVERIFIED")

    def test_liquidation_evidence_identity_is_in_risk_fingerprint(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        configured = policy(min_liquidation_headroom="0.25")

        store_a = _LiquidationEvidenceStore()
        tier_a = liquidation_evidence(
            store_a,
            headroom="0.50",
            risk_tier_version="tier-v1",
        )
        decision_a = evaluate_risk(
            intent,
            context(**tier_a),
            configured,
            evidence_store=store_a,
        )

        store_b = _LiquidationEvidenceStore()
        tier_b = liquidation_evidence(
            store_b,
            headroom="0.50",
            risk_tier_version="tier-v2",
        )
        decision_b = evaluate_risk(
            intent,
            context(**tier_b),
            configured,
            evidence_store=store_b,
        )

        self.assertTrue(decision_a.admitted)
        self.assertTrue(decision_b.admitted)
        self.assertNotEqual(
            decision_a.input_fingerprint,
            decision_b.input_fingerprint,
        )
        self.assertNotEqual(
            risk_decision_fingerprint(decision_a),
            risk_decision_fingerprint(decision_b),
        )

    def test_tail_and_liquidation_inputs_reject_binary_float(self):
        with self.assertRaises(TypeError):
            context(tail_scenarios=({"ABC": -0.10},))
        with self.assertRaises(TypeError):
            context(liquidation_headroom=0.25)
        with self.assertRaises(TypeError):
            policy(
                max_expected_shortfall=100.0,
                expected_shortfall_tail_fraction="0.05",
            )
        with self.assertRaises(TypeError):
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction=0.05,
            )

    def test_evaluate_risk_revalidates_direct_dataclass_construction(self):
        good_context = context()
        good_policy = policy()

        forged_intent = RiskIntent(
            symbol="ABC",
            side="BUY",
            quantity=1.0,
            price=Decimal("100"),
            expected_state_version=7,
        )
        with self.assertRaisesRegex(TypeError, "quantity must use Decimal"):
            evaluate_risk(forged_intent, good_context, good_policy)

        forged_context = RiskContext(
            **{
                **good_context.__dict__,
                "capability_allowed": "true",
            }
        )
        with self.assertRaisesRegex(TypeError, "capability_allowed must be a boolean"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC",
                    side="BUY",
                    quantity="1",
                    price="100",
                    expected_state_version=7,
                ),
                forged_context,
                good_policy,
            )

        forged_policy = RiskPolicy(
            **{
                **good_policy.__dict__,
                "max_drawdown_fraction": Decimal("1.01"),
            }
        )
        with self.assertRaisesRegex(ValueError, "max_drawdown_fraction cannot exceed 1"):
            evaluate_risk(
                RiskIntent.create(
                    symbol="ABC",
                    side="BUY",
                    quantity="1",
                    price="100",
                    expected_state_version=7,
                ),
                good_context,
                forged_policy,
            )

    def test_evaluate_risk_rejects_wrong_boundary_types(self):
        valid_intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        with self.assertRaisesRegex(TypeError, "intent must be RiskIntent"):
            evaluate_risk({}, context(), policy())
        with self.assertRaisesRegex(TypeError, "context must be RiskContext"):
            evaluate_risk(valid_intent, {}, policy())
        with self.assertRaisesRegex(TypeError, "policy must be RiskPolicy"):
            evaluate_risk(valid_intent, context(), {})

    def test_risk_input_fingerprint_distinguishes_equal_decisions_from_different_evidence(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        first_tail = (
            {"ABC": "-0.10"},
            {"ABC": "-0.20"},
        )
        changed_tail = (
            {"ABC": "-0.05"},
            {"ABC": "-0.20"},
        )
        first = evaluate_risk(
            intent,
            context(tail_scenarios=first_tail),
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="0.50",
                required_tail_scenario_set_digest=tail_scenario_set_digest(first_tail),
            ),
        )
        changed_evidence = evaluate_risk(
            intent,
            context(tail_scenarios=changed_tail),
            policy(
                max_expected_shortfall="100",
                expected_shortfall_tail_fraction="0.50",
                required_tail_scenario_set_digest=tail_scenario_set_digest(changed_tail),
            ),
        )
        first_es = next(x for x in first.rules if x.rule == "expected_shortfall")
        changed_es = next(
            x for x in changed_evidence.rules if x.rule == "expected_shortfall"
        )
        self.assertEqual(first_es.observed, changed_es.observed)
        self.assertNotEqual(
            first.input_fingerprint,
            changed_evidence.input_fingerprint,
        )
        self.assertNotEqual(
            risk_decision_fingerprint(first),
            risk_decision_fingerprint(changed_evidence),
        )
        self.assertEqual(len(first.input_fingerprint), 64)

    def test_risk_decision_fingerprint_is_deterministic_and_evidence_sensitive(self):
        intent = RiskIntent.create(
            symbol="ABC", side="BUY", quantity="1", price="100",
            expected_state_version=7,
        )
        configured = policy(max_clock_age_seconds="5")
        first = evaluate_risk(
            intent,
            context(clock_age_seconds="1"),
            configured,
        )
        repeated = evaluate_risk(
            intent,
            context(clock_age_seconds="1"),
            configured,
        )
        changed = evaluate_risk(
            intent,
            context(clock_age_seconds="2"),
            configured,
        )
        first_hash = risk_decision_fingerprint(first)
        self.assertEqual(first_hash, risk_decision_fingerprint(repeated))
        self.assertNotEqual(first_hash, risk_decision_fingerprint(changed))
        self.assertEqual(len(first_hash), 64)

    def test_decimal_scale_and_negative_zero_do_not_change_risk_identity(self):
        intent = RiskIntent.create(
            symbol="ABC",
            side="BUY",
            quantity="1",
            price="100",
            expected_state_version=7,
        )
        first = evaluate_risk(
            intent,
            context(clock_age_seconds="1.0"),
            policy(max_clock_age_seconds="5.00"),
        )
        equivalent = evaluate_risk(
            intent,
            context(clock_age_seconds="1.00"),
            policy(max_clock_age_seconds="5.0"),
        )
        self.assertEqual(first.input_fingerprint, equivalent.input_fingerprint)
        self.assertEqual(
            risk_decision_fingerprint(first),
            risk_decision_fingerprint(equivalent),
        )
        clock_rule = next(
            item for item in equivalent.rules if item.rule == "clock_freshness"
        )
        self.assertEqual(clock_rule.observed, "1")
        self.assertEqual(clock_rule.limit, "5")
        self.assertEqual(_canonical_decimal_text(Decimal("-0.000")), "0")

    def test_risk_decision_fingerprint_rejects_wrong_type(self):
        with self.assertRaises(TypeError):
            risk_decision_fingerprint({"admitted": True})


if __name__ == "__main__":
    unittest.main()
