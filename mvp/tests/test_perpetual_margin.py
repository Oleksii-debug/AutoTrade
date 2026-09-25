from decimal import Decimal
import unittest

from mvp.autotrade_mvp.perpetual_margin import (
    MarginTier,
    PerpetualMarginError,
    PerpetualMarginEvidence,
    PerpetualStress,
    evaluate_perpetual_margin,
)


def tier(upper="10000", rate="0.005", fixed="0"):
    return MarginTier(
        notional_upper_bound=Decimal(upper),
        maintenance_rate=Decimal(rate),
        maintenance_fixed=Decimal(fixed),
    )


def evidence(**overrides):
    values = dict(
        instrument_version="BTC-PERP@v4",
        mark_price=Decimal("100"),
        index_price=Decimal("100"),
        collateral_fx_to_settlement=Decimal("1"),
        mark_observed_at="2026-09-25T00:00:00Z",
        index_observed_at="2026-09-25T00:00:00Z",
        collateral_fx_observed_at="2026-09-25T00:00:00Z",
        margin_tiers_observed_at="2026-09-25T00:00:00Z",
        margin_tiers=(tier(), tier("50000", "0.01", "10")),
        evidence_ref="artifact:margin:sha256:abc",
    )
    values.update(overrides)
    return PerpetualMarginEvidence(**values)


def stress(**overrides):
    values = dict(
        price_loss_fraction=Decimal("0.05"),
        collateral_fx_loss_fraction=Decimal("0"),
        exit_cost_fraction=Decimal("0.001"),
        additional_funding_loss=Decimal("0"),
        unavailable_exit_extra_loss=Decimal("0"),
    )
    values.update(overrides)
    return PerpetualStress(**values)


def evaluate(**overrides):
    values = dict(
        instrument_version="BTC-PERP@v4",
        signed_notional_settlement=Decimal("5000"),
        collateral_amount=Decimal("1000"),
        unrealized_pnl_settlement=Decimal("0"),
        evidence=evidence(),
        stress=stress(),
        evaluated_at="2026-09-25T00:00:10Z",
        maximum_evidence_age_seconds=30,
        maximum_mark_index_divergence_bps=Decimal("50"),
        collateral_haircut_fraction=Decimal("0"),
    )
    values.update(overrides)
    return evaluate_perpetual_margin(**values)


class PerpetualMarginTests(unittest.TestCase):
    def test_fresh_well_collateralized_position_allows_new_risk(self):
        result = evaluate()
        self.assertEqual(result.verdict, "ALLOW_NEW_RISK")
        self.assertEqual(result.maintenance_requirement, Decimal("25.000"))
        self.assertEqual(result.stressed_equity_settlement, Decimal("745.000"))
        self.assertEqual(result.liquidation_headroom, Decimal("720.000"))

    def test_stale_margin_metadata_blocks_new_risk(self):
        result = evaluate(
            evidence=evidence(
                margin_tiers_observed_at="2026-09-24T23:58:00Z",
            )
        )
        self.assertEqual(result.verdict, "BLOCK_NEW_RISK")
        self.assertIn("margin_tiers_observed_at:STALE", result.reasons)

    def test_stale_collateral_fx_blocks_new_risk(self):
        result = evaluate(
            evidence=evidence(
                collateral_fx_observed_at="2026-09-24T23:58:00Z",
            )
        )
        self.assertEqual(result.verdict, "BLOCK_NEW_RISK")
        self.assertIn("collateral_fx_observed_at:STALE", result.reasons)

    def test_mark_index_divergence_blocks_new_risk(self):
        result = evaluate(
            evidence=evidence(mark_price=Decimal("102"), index_price=Decimal("100")),
            maximum_mark_index_divergence_bps=Decimal("100"),
        )
        self.assertEqual(result.verdict, "BLOCK_NEW_RISK")
        self.assertEqual(result.mark_index_divergence_bps, Decimal("200"))
        self.assertIn("MARK_INDEX_DIVERGENCE", result.reasons)

    def test_collateral_depeg_stress_reduces_headroom(self):
        base = evaluate(stress=stress(collateral_fx_loss_fraction=Decimal("0")))
        depeg = evaluate(
            stress=stress(collateral_fx_loss_fraction=Decimal("0.20"))
        )
        self.assertLess(depeg.liquidation_headroom, base.liquidation_headroom)
        self.assertEqual(
            base.liquidation_headroom - depeg.liquidation_headroom,
            Decimal("200.000"),
        )

    def test_unavailable_exit_stress_can_cross_liquidation_headroom(self):
        result = evaluate(
            stress=stress(
                price_loss_fraction=Decimal("0.15"),
                unavailable_exit_extra_loss=Decimal("300"),
            )
        )
        self.assertEqual(result.verdict, "LIQUIDATION_STRESS")
        self.assertLess(result.liquidation_headroom, 0)
        self.assertIn(
            "STRESSED_LIQUIDATION_HEADROOM_NEGATIVE",
            result.reasons,
        )

    def test_margin_tier_changes_requirement(self):
        result = evaluate(
            signed_notional_settlement=Decimal("15000"),
            collateral_amount=Decimal("5000"),
            stress=stress(price_loss_fraction=Decimal("0.01")),
        )
        self.assertEqual(result.selected_tier_upper_bound, Decimal("50000"))
        self.assertEqual(
            result.maintenance_requirement,
            Decimal("160.00"),
        )

    def test_notional_outside_evidenced_tiers_fails_closed(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "exceeds evidenced margin tier coverage",
        ):
            evaluate(signed_notional_settlement=Decimal("60000"))

    def test_future_dated_evidence_never_counts_as_fresh(self):
        result = evaluate(
            evidence=evidence(mark_observed_at="2026-09-25T00:00:11Z")
        )
        self.assertEqual(result.verdict, "BLOCK_NEW_RISK")
        self.assertIn("mark_observed_at:FUTURE_EVIDENCE", result.reasons)

    def test_instrument_version_must_match(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "instrument_version must match",
        ):
            evaluate(instrument_version="BTC-PERP@v5")

    def test_float_money_and_rates_are_rejected(self):
        with self.assertRaises(TypeError):
            evaluate(collateral_amount=1000.0)
        with self.assertRaises(TypeError):
            PerpetualStress(
                price_loss_fraction=0.05,
                collateral_fx_loss_fraction=Decimal("0"),
                exit_cost_fraction=Decimal("0"),
            )

    def test_margin_tiers_must_be_strictly_increasing(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "strictly increasing",
        ):
            evidence(
                margin_tiers=(
                    tier("10000"),
                    tier("9000"),
                )
            )


if __name__ == "__main__":
    unittest.main()
