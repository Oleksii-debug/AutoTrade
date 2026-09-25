from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.capabilities import CapabilitySnapshot
from mvp.autotrade_mvp.perpetual_margin import (
    MarginTier,
    PerpetualMarginError,
    PerpetualMarginEvidence,
    PerpetualStress,
    evaluate_perpetual_margin,
)
from research.autotrade_research.artifacts.store import ArtifactStore


def tier(upper="10000", rate="0.005", adjustment="0", convention="ADD"):
    return MarginTier(
        notional_upper_bound=Decimal(upper),
        maintenance_rate=Decimal(rate),
        maintenance_adjustment=Decimal(adjustment),
        adjustment_convention=convention,
    )


SNAPSHOT_ID = "11111111-1111-4111-8111-111111111111"
EVIDENCE_BUNDLE_ID = "22222222-2222-4222-8222-222222222222"
TIER_TABLE_ID = "33333333-3333-4333-8333-333333333333"


def capability(**overrides):
    values = dict(
        snapshot_id=SNAPSHOT_ID,
        provider_id="TEST_PROVIDER",
        account_id="account-A",
        entity_id="perpetual-account",
        environment="PAPER",
        instrument_version="BTC-PERP@v4",
        observed_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 26, tzinfo=timezone.utc),
        supported_order_types=frozenset({"MARKET", "LIMIT"}),
        time_in_force=frozenset({"GTC", "IOC"}),
        permission_scopes=frozenset({"ORDER_WRITE"}),
        position_mode="ONE_WAY",
        native_protection=frozenset(),
        rate_limit_policy_id="test-rate",
        data_entitlements=frozenset({"MARK", "INDEX", "MARGIN"}),
        evidence=(),
        status="VERIFIED",
        sources=frozenset({"DOCUMENTED"}),
    )
    values.update(overrides)
    return CapabilitySnapshot(**values)


def evidence(**overrides):
    values = dict(
        provider_id="TEST_PROVIDER",
        account_id="account-A",
        entity_id="perpetual-account",
        environment="PAPER",
        instrument_version="BTC-PERP@v4",
        capability_snapshot_id=SNAPSHOT_ID,
        position_mode="ONE_WAY",
        margin_mode="CROSS",
        collateral_currency="USD",
        settlement_currency="USD",
        risk_tier_revision="tier-v7",
        evidence_bundle_ref=EVIDENCE_BUNDLE_ID,
        tier_table_evidence_ref=TIER_TABLE_ID,
        mark_price=Decimal("100"),
        index_price=Decimal("100"),
        collateral_fx_to_settlement=Decimal("1"),
        mark_observed_at="2026-09-25T00:00:00Z",
        index_observed_at="2026-09-25T00:00:00Z",
        collateral_fx_observed_at="2026-09-25T00:00:00Z",
        margin_tiers_observed_at="2026-09-25T00:00:00Z",
        margin_tiers=(tier(), tier("50000", "0.01", "10", "ADD")),
    )
    values.update(overrides)
    return PerpetualMarginEvidence(**values)


class EvidenceArtifactStore:
    """Minimal immutable-store contract used by margin unit tests."""

    def __init__(self, value: PerpetualMarginEvidence):
        common = {
            "schema_version": 1,
            "provider_id": value.provider_id,
            "account_id": value.account_id,
            "entity_id": value.entity_id,
            "environment": value.environment,
            "instrument_version": value.instrument_version,
            "capability_snapshot_id": value.capability_snapshot_id,
            "position_mode": value.position_mode,
            "margin_mode": value.margin_mode,
            "collateral_currency": value.collateral_currency,
            "settlement_currency": value.settlement_currency,
            "risk_tier_revision": value.risk_tier_revision,
        }
        self._records = {}
        self._add(
            value.tier_table_evidence_ref,
            value.tier_table_payload(),
            {
                **common,
                "artifact_kind": "PERPETUAL_MARGIN_TIER_TABLE",
                "observed_at": value.margin_tiers_observed_at,
            },
        )
        self._add(
            value.evidence_bundle_ref,
            value.evidence_bundle_payload(),
            {
                **common,
                "artifact_kind": "PERPETUAL_MARGIN_EVIDENCE_BUNDLE",
                "observed_at": value.mark_observed_at,
            },
        )

    def _add(self, artifact_id, payload, metadata):
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        self._records[artifact_id] = (
            {
                "artifact_id": artifact_id,
                "sha256": "sha256:" + sha256(data).hexdigest(),
                "media_type": "application/json",
                "metadata": metadata,
            },
            data,
        )

    def load_manifest(self, artifact_id):
        try:
            return dict(self._records[artifact_id][0])
        except KeyError as error:
            raise FileNotFoundError(artifact_id) from error

    def read_bytes(self, artifact_id):
        try:
            return self._records[artifact_id][1]
        except KeyError as error:
            raise FileNotFoundError(artifact_id) from error


def publish_margin_artifacts(store: ArtifactStore, value: PerpetualMarginEvidence):
    common = {
        "schema_version": 1,
        "provider_id": value.provider_id,
        "account_id": value.account_id,
        "entity_id": value.entity_id,
        "environment": value.environment,
        "instrument_version": value.instrument_version,
        "capability_snapshot_id": value.capability_snapshot_id,
        "position_mode": value.position_mode,
        "margin_mode": value.margin_mode,
        "collateral_currency": value.collateral_currency,
        "settlement_currency": value.settlement_currency,
        "risk_tier_revision": value.risk_tier_revision,
    }
    records = (
        (
            value.tier_table_evidence_ref,
            value.tier_table_payload(),
            {
                **common,
                "artifact_kind": "PERPETUAL_MARGIN_TIER_TABLE",
                "observed_at": value.margin_tiers_observed_at,
            },
        ),
        (
            value.evidence_bundle_ref,
            value.evidence_bundle_payload(),
            {
                **common,
                "artifact_kind": "PERPETUAL_MARGIN_EVIDENCE_BUNDLE",
                "observed_at": value.mark_observed_at,
            },
        ),
    )
    for artifact_id, payload, metadata in records:
        data = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        store.publish_bytes(
            artifact_id=artifact_id,
            data=data,
            media_type="application/json",
            rights={"storage": True, "export": False},
            source_refs=["provider:margin-evidence"],
            metadata=metadata,
        )


def stress(**overrides):
    values = dict(
        price_loss_fraction=Decimal("0.05"),
        collateral_fx_loss_fraction=Decimal("0"),
        exit_cost_fraction=Decimal("0.001"),
        additional_funding_loss=Decimal("0"),
        unavailable_exit_extra_loss=Decimal("0"),
        notional_increase_fraction=Decimal("0"),
    )
    values.update(overrides)
    return PerpetualStress(**values)


def evaluate(**overrides):
    values = dict(
        capability=capability(),
        instrument_version="BTC-PERP@v4",
        margin_mode="CROSS",
        collateral_currency="USD",
        settlement_currency="USD",
        risk_tier_revision="tier-v7",
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
    if "artifact_store" in overrides:
        return evaluate_perpetual_margin(**values)
    with TemporaryDirectory() as directory:
        artifact_store = ArtifactStore(directory)
        publish_margin_artifacts(artifact_store, values["evidence"])
        return evaluate_perpetual_margin(
            **values,
            artifact_store=artifact_store,
        )


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

    def test_account_scope_mismatch_fails_before_arithmetic(self):
        with self.assertRaisesRegex(PerpetualMarginError, "capability scope mismatch"):
            evaluate(capability=capability(account_id="account-B"))

    def test_capability_identity_case_is_preserved_not_reinterpreted(self):
        lower = capability(
            provider_id="bybit",
            position_mode="one_way",
        )
        matching = evidence(
            provider_id="bybit",
            position_mode="one_way",
        )
        decision = evaluate(
            capability=lower,
            evidence=matching,
        )
        self.assertIsNotNone(decision)

        with self.assertRaisesRegex(
            PerpetualMarginError,
            "capability scope mismatch|position mode",
        ):
            evaluate(
                capability=lower,
                evidence=evidence(
                    provider_id="BYBIT",
                    position_mode="ONE_WAY",
                ),
            )

    def test_paper_evidence_cannot_be_reused_for_live_scope(self):
        with self.assertRaisesRegex(PerpetualMarginError, "capability scope mismatch"):
            evaluate(capability=capability(environment="LIVE"))

    def test_position_and_margin_modes_are_not_portable(self):
        with self.assertRaisesRegex(PerpetualMarginError, "position mode"):
            evaluate(capability=capability(position_mode="HEDGE"))
        with self.assertRaisesRegex(PerpetualMarginError, "margin mode"):
            evaluate(margin_mode="ISOLATED")

    def test_capability_snapshot_and_tier_revision_are_immutable_scope(self):
        with self.assertRaisesRegex(PerpetualMarginError, "capability snapshot"):
            evaluate(
                capability=capability(
                    snapshot_id="22222222-2222-4222-8222-222222222222"
                )
            )
        with self.assertRaisesRegex(PerpetualMarginError, "risk tier revision"):
            evaluate(risk_tier_revision="tier-v8")

    def test_currency_semantics_are_explicit_scope(self):
        with self.assertRaisesRegex(PerpetualMarginError, "collateral currency"):
            evaluate(collateral_currency="USDT")
        with self.assertRaisesRegex(PerpetualMarginError, "settlement currency"):
            evaluate(settlement_currency="USDT")

    def test_stale_capability_blocks_margin_evaluation(self):
        with self.assertRaisesRegex(PerpetualMarginError, "capability snapshot is stale"):
            evaluate(
                capability=capability(
                    expires_at=datetime(2026, 9, 25, 0, 0, 5, tzinfo=timezone.utc)
                )
            )

    def test_tier_identity_survives_reconstruction_and_changes_with_revision(self):
        original = evidence()
        reopened = evidence(
            provider_id=original.provider_id,
            account_id=original.account_id,
            entity_id=original.entity_id,
            environment=original.environment,
            instrument_version=original.instrument_version,
            capability_snapshot_id=original.capability_snapshot_id,
            position_mode=original.position_mode,
            margin_mode=original.margin_mode,
            collateral_currency=original.collateral_currency,
            settlement_currency=original.settlement_currency,
            risk_tier_revision=original.risk_tier_revision,
            evidence_bundle_ref=original.evidence_bundle_ref,
            tier_table_evidence_ref=original.tier_table_evidence_ref,
        )
        self.assertEqual(reopened.capability_identity, original.capability_identity)
        self.assertEqual(reopened.tier_identity, original.tier_identity)
        revised = evidence(risk_tier_revision="tier-v8")
        self.assertNotEqual(revised.tier_identity, original.tier_identity)

    def test_real_artifact_store_binds_exact_margin_economics(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            result = evaluate(evidence=trusted, artifact_store=store)
            self.assertEqual(result.verdict, "ALLOW_NEW_RISK")

            altered = evidence(
                margin_tiers=(
                    tier("10000", "0.001"),
                    tier("50000", "0.002"),
                )
            )
            with self.assertRaisesRegex(
                PerpetualMarginError,
                "content does not match supplied economics",
            ):
                evaluate(evidence=altered, artifact_store=store)

    def test_same_tier_ref_cannot_authorize_altered_tier_economics(self):
        trusted = evidence()
        altered = evidence(
            margin_tiers=(
                tier("10000", "0.001"),
                tier("50000", "0.002", "0", "ADD"),
            )
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            with self.assertRaisesRegex(
                PerpetualMarginError,
                "artifact content does not match supplied economics",
            ):
                evaluate(evidence=altered, artifact_store=store)

    def test_same_bundle_ref_cannot_authorize_mixed_mark_index_fx_content(self):
        trusted = evidence()
        mixed = evidence(
            mark_price=Decimal("101"),
            collateral_fx_to_settlement=Decimal("0.99"),
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            with self.assertRaisesRegex(
                PerpetualMarginError,
                "artifact content does not match supplied economics",
            ):
                evaluate(evidence=mixed, artifact_store=store)

    def test_margin_evaluation_requires_resolvable_immutable_artifacts(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            missing = ArtifactStore(directory)
            with self.assertRaisesRegex(
                PerpetualMarginError,
                "artifact is missing or corrupt",
            ):
                evaluate(evidence=trusted, artifact_store=missing)

    def test_duck_typed_evidence_store_cannot_be_financial_authority(self):
        trusted = evidence()
        fake_store = EvidenceArtifactStore(trusted)
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "canonical ArtifactStore",
        ):
            evaluate(evidence=trusted, artifact_store=fake_store)

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


    def test_provider_tier_can_explicitly_use_rate_minus_deduction(self):
        result = evaluate(
            signed_notional_settlement=Decimal("15000"),
            collateral_amount=Decimal("5000"),
            evidence=evidence(
                margin_tiers=(
                    tier("10000", "0.005"),
                    tier("50000", "0.01", "10", "DEDUCT"),
                )
            ),
            stress=stress(price_loss_fraction=Decimal("0.01")),
        )
        self.assertEqual(result.maintenance_requirement, Decimal("140.00"))

    def test_tier_convention_cannot_be_implicit_or_unknown(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "adjustment_convention",
        ):
            tier("10000", "0.005", "0", "PROVIDER_MAGIC")

    def test_deduction_cannot_create_negative_maintenance(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "negative maintenance",
        ):
            evaluate(
                signed_notional_settlement=Decimal("100"),
                evidence=evidence(
                    margin_tiers=(
                        tier("10000", "0.001", "1", "DEDUCT"),
                    )
                ),
            )


    def test_adverse_notional_growth_reselects_margin_tier(self):
        result = evaluate(
            signed_notional_settlement=Decimal("9000"),
            collateral_amount=Decimal("5000"),
            evidence=evidence(
                margin_tiers=(
                    tier("10000", "0.005"),
                    tier("50000", "0.02", "0", "ADD"),
                )
            ),
            stress=stress(
                price_loss_fraction=Decimal("0"),
                exit_cost_fraction=Decimal("0"),
                notional_increase_fraction=Decimal("0.20"),
            ),
        )
        self.assertEqual(result.maintenance_requirement, Decimal("45.000"))
        self.assertEqual(result.stressed_notional, Decimal("10800.00"))
        self.assertEqual(
            result.stressed_maintenance_requirement,
            Decimal("216.0000"),
        )
        self.assertEqual(
            result.liquidation_headroom,
            result.stressed_equity_settlement - Decimal("216.0000"),
        )

    def test_stressed_notional_outside_evidenced_tiers_fails_closed(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "exceeds evidenced margin tier coverage",
        ):
            evaluate(
                signed_notional_settlement=Decimal("49000"),
                collateral_amount=Decimal("10000"),
                stress=stress(notional_increase_fraction=Decimal("0.10")),
            )

if __name__ == "__main__":
    unittest.main()
