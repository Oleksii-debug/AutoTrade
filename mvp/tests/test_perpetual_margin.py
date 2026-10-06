from datetime import datetime, timezone
from decimal import (
    Decimal,
    ROUND_CEILING,
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    localcontext,
)
from fractions import Fraction
from hashlib import sha256
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.perpetual_margin import (
    MarginTier,
    PerpetualMarginError,
    PerpetualMarginEvidence,
    PerpetualStress,
    evaluate_perpetual_margin,
)
from autotrade_runtime.artifacts.store import ArtifactIntegrityError, ArtifactStore


def tier(
    upper="10000",
    rate="0.005",
    adjustment="0",
    convention="ADD",
    *,
    cls=MarginTier,
):
    return cls(
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
        environment="SIMULATION",
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
    )
    values.update(overrides)
    observed_at = values["observed_at"]
    claim_fields = {
        key: value
        for key, value in values.items()
        if key != "snapshot_id"
    }
    claims = tuple(
        CapabilityClaim(
            source=source,
            **claim_fields,
            evidence_ref={
                "artifact_id": str(__import__("uuid").uuid4()),
                "sha256": "sha256:" + "f" * 64,
                "observed_at": observed_at.astimezone(timezone.utc).isoformat().replace(
                    "+00:00", "Z"
                ),
                "source_uri": "https://example.invalid/perpetual-capability-fixture",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=values["snapshot_id"],
        claims=claims,
        observed_at=observed_at,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def evidence(*, cls=PerpetualMarginEvidence, **overrides):
    values = dict(
        provider_id="TEST_PROVIDER",
        account_id="account-A",
        entity_id="perpetual-account",
        environment="SIMULATION",
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
    return cls(**values)


class EvidenceArtifactStore:
    """Minimal immutable-store contract used by margin unit tests."""

    def __init__(self, value: PerpetualMarginEvidence):
        common = {
            "schema_version": 1,
            "provider_id": value.provider_id,
            "account_id": value.account_id,
            "entity_id": value.entity_id,
            "environment": value.environment,
            "provider_environment": value.provider_environment,
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
        "provider_environment": value.provider_environment,
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


def stress(*, cls=PerpetualStress, **overrides):
    values = dict(
        price_loss_fraction=Decimal("0.05"),
        collateral_fx_loss_fraction=Decimal("0"),
        exit_cost_fraction=Decimal("0.001"),
        additional_funding_loss=Decimal("0"),
        unavailable_exit_extra_loss=Decimal("0"),
        notional_increase_fraction=Decimal("0"),
    )
    values.update(overrides)
    return cls(**values)


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


class HostileDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("Decimal subclass virtual method must not run")

    def as_tuple(self):
        raise AssertionError("Decimal subclass virtual method must not run")

    def normalize(self, *args, **kwargs):
        raise AssertionError("Decimal subclass virtual method must not run")

    def __lt__(self, other):
        raise AssertionError("Decimal subclass comparison must not run")

    def __le__(self, other):
        raise AssertionError("Decimal subclass comparison must not run")


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

    def test_paper_and_live_caller_authored_margin_evidence_fail_closed(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment):
                scoped_capability = capability(environment=environment)
                scoped_evidence = evidence(environment=environment)
                with TemporaryDirectory() as directory:
                    store = ArtifactStore(directory)
                    publish_margin_artifacts(store, scoped_evidence)
                    with patch.object(
                        ArtifactStore,
                        "read_authenticated_snapshot",
                        autospec=True,
                        side_effect=AssertionError(
                            "artifact evidence must not be read before provider-origin authority"
                        ),
                    ):
                        with self.assertRaisesRegex(
                            PerpetualMarginError,
                            "requires canonical provider-origin evidence",
                        ):
                            evaluate(
                                capability=scoped_capability,
                                evidence=scoped_evidence,
                                artifact_store=store,
                            )

    def test_provider_environment_is_exact_capability_scope(self):
        testnet_capability = capability(
            provider_id="BYBIT",
            provider_environment="TESTNET",
        )
        testnet_evidence = evidence(
            provider_id="BYBIT",
            provider_environment="TESTNET",
        )
        result = evaluate(
            capability=testnet_capability,
            evidence=testnet_evidence,
        )
        self.assertEqual(result.verdict, "ALLOW_NEW_RISK")
        self.assertEqual(testnet_evidence.provider_environment, "TESTNET")

        with self.assertRaisesRegex(
            PerpetualMarginError,
            "capability scope mismatch",
        ):
            evaluate(
                capability=testnet_capability,
                evidence=evidence(
                    provider_id="BYBIT",
                    provider_environment="DEMO",
                ),
            )

    def test_bybit_margin_evidence_requires_explicit_provider_environment(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "BYBIT requires explicit provider_environment",
        ):
            evidence(provider_id="BYBIT")

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

    def test_margin_evidence_payloads_ignore_ambient_decimal_context(self):
        trusted = evidence(
            mark_price=Decimal("1234567890123456789012345678.1"),
            index_price=Decimal("1234567890123456789012345678.2"),
            collateral_fx_to_settlement=Decimal("0.9999999999999999999999999999"),
            margin_tiers=(
                MarginTier(
                    notional_upper_bound=Decimal(
                        "1234567890123456789012345678.3"
                    ),
                    maintenance_rate=Decimal(
                        "0.1234567890123456789012345678"
                    ),
                    maintenance_adjustment=Decimal(
                        "0.000000000000000000123456789"
                    ),
                ),
            ),
        )

        with localcontext() as context:
            context.prec = 6
            low_precision = (
                trusted.tier_table_payload(),
                trusted.evidence_bundle_payload(),
            )
        with localcontext() as context:
            context.prec = 80
            high_precision = (
                trusted.tier_table_payload(),
                trusted.evidence_bundle_payload(),
            )

        self.assertEqual(low_precision, high_precision)
        self.assertEqual(
            low_precision[1]["mark_price"],
            "1234567890123456789012345678.1",
        )
        self.assertEqual(
            low_precision[0]["tiers"][0]["maintenance_rate"],
            "0.1234567890123456789012345678",
        )

    def test_decimal_subclass_is_rejected_before_virtual_dispatch(self):
        hostile = HostileDecimal("10000")
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "bounded exact decimal",
        ):
            MarginTier(
                notional_upper_bound=hostile,
                maintenance_rate=Decimal("0.005"),
            )

    def test_margin_numeric_ingress_uses_shared_resource_envelope(self):
        with self.assertRaisesRegex(
            PerpetualMarginError,
            "bounded exact decimal",
        ):
            MarginTier(
                notional_upper_bound="1e1000000",
                maintenance_rate="0.005",
            )

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

    def test_margin_arithmetic_and_divergence_are_exact_across_decimal_contexts(self):
        exact_evidence = evidence(
            mark_price=Decimal("4"),
            index_price=Decimal("3"),
            margin_tiers=(
                tier(
                    "2000000000000000000000000000",
                    "0.0000000000000000000000000001",
                    "0.0000000000000000000000000001",
                ),
            ),
        )
        exact_stress = stress(
            price_loss_fraction=Decimal("0"),
            collateral_fx_loss_fraction=Decimal("0"),
            exit_cost_fraction=Decimal("0"),
            additional_funding_loss=Decimal("0"),
            unavailable_exit_extra_loss=Decimal("0"),
            notional_increase_fraction=Decimal("0"),
        )
        common = dict(
            evidence=exact_evidence,
            signed_notional_settlement=Decimal(
                "1000000000000000000000000000.1"
            ),
            collateral_amount=Decimal(
                "1000000000000000000000000000.2"
            ),
            unrealized_pnl_settlement=Decimal(
                "0.0000000000000000000000000003"
            ),
            collateral_haircut_fraction=Decimal(
                "0.0000000000000000000000000001"
            ),
            stress=exact_stress,
        )

        allowed = []
        blocked = []
        for precision in (6, 10, 28, 80):
            for rounding in (ROUND_FLOOR, ROUND_CEILING, ROUND_HALF_EVEN):
                with localcontext() as context:
                    context.prec = precision
                    context.rounding = rounding
                    allowed.append(
                        evaluate(
                            **common,
                            maximum_mark_index_divergence_bps=Decimal(
                                "3333.3334"
                            ),
                        )
                    )
                    blocked.append(
                        evaluate(
                            **common,
                            maximum_mark_index_divergence_bps=Decimal(
                                "3333.3333"
                            ),
                        )
                    )

        self.assertTrue(all(result == allowed[0] for result in allowed))
        self.assertTrue(all(result == blocked[0] for result in blocked))
        self.assertEqual(allowed[0].verdict, "ALLOW_NEW_RISK")
        self.assertEqual(blocked[0].verdict, "BLOCK_NEW_RISK")
        self.assertIn("MARK_INDEX_DIVERGENCE", blocked[0].reasons)
        self.assertEqual(
            allowed[0].mark_index_divergence_bps,
            Fraction(10000, 3),
        )
        self.assertEqual(
            allowed[0].maintenance_requirement,
            Decimal("0.10000000000000000000000000011"),
        )

    def test_margin_authority_rejects_polymorphic_domain_objects_before_virtual_dispatch(self):
        class ForgedEvidence(PerpetualMarginEvidence):
            verification_called = False

            def verify_immutable_artifacts(self, store):
                type(self).verification_called = True
                return None

        class ForgedStress(PerpetualStress):
            pass

        class ForgedTier(MarginTier):
            maintenance_called = False

            def maintenance_requirement(self, notional):
                type(self).maintenance_called = True
                return Decimal("0")

        forged_evidence = evidence(cls=ForgedEvidence)
        with self.assertRaisesRegex(TypeError, "exact PerpetualMarginEvidence"):
            evaluate(evidence=forged_evidence)
        self.assertFalse(ForgedEvidence.verification_called)

        forged_stress = stress(cls=ForgedStress)
        with self.assertRaisesRegex(TypeError, "exact PerpetualStress"):
            evaluate(stress=forged_stress)

        with self.assertRaisesRegex(TypeError, "exact MarginTier"):
            evidence(margin_tiers=(tier(cls=ForgedTier),))
        self.assertFalse(ForgedTier.maintenance_called)

        with self.assertRaisesRegex(TypeError, "exact tuple"):
            evidence(margin_tiers=[tier()])

    def test_margin_text_identity_rejects_string_subclass_before_strip_dispatch(self):
        class HostileText(str):
            strip_called = False

            def strip(self, *args, **kwargs):
                type(self).strip_called = True
                raise AssertionError("string subclass strip must not run")

        with self.assertRaisesRegex(PerpetualMarginError, "provider_id is required"):
            evidence(provider_id=HostileText("TEST_PROVIDER"))
        self.assertFalse(HostileText.strip_called)

    def test_margin_artifact_refs_require_canonical_lowercase_uuid(self):
        for field, value in (
            ("evidence_bundle_ref", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"),
            ("tier_table_evidence_ref", "{33333333-3333-4333-8333-333333333333}"),
        ):
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    PerpetualMarginError,
                    "canonical lowercase artifact UUID",
                ):
                    evidence(**{field: value})

    def test_freshness_budget_requires_exact_int_before_artifact_io(self):
        class HostileInt(int):
            comparison_called = False

            def __lt__(self, other):
                type(self).comparison_called = True
                raise AssertionError("integer subclass comparison must not run")

        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            with patch(
                "mvp.autotrade_mvp.perpetual_margin."
                "_READ_AUTHENTICATED_ARTIFACT_SNAPSHOT",
                side_effect=AssertionError(
                    "artifact reader must not run before freshness-budget admission"
                ),
            ):
                with self.assertRaisesRegex(
                    PerpetualMarginError,
                    "maximum_evidence_age_seconds must be a non-negative integer",
                ):
                    evaluate(
                        evidence=trusted,
                        artifact_store=store,
                        maximum_evidence_age_seconds=HostileInt(30),
                    )
        self.assertFalse(HostileInt.comparison_called)

    def test_margin_artifacts_use_one_authenticated_snapshot_per_artifact(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            original = ArtifactStore.read_authenticated_snapshot
            calls = []

            def read_snapshot(instance, artifact_id):
                calls.append(artifact_id)
                return original(instance, artifact_id)

            with patch(
                "mvp.autotrade_mvp.perpetual_margin."
                "_READ_AUTHENTICATED_ARTIFACT_SNAPSHOT",
                side_effect=read_snapshot,
            ):
                result = evaluate(evidence=trusted, artifact_store=store)

            self.assertEqual(result.verdict, "ALLOW_NEW_RISK")
            self.assertEqual(calls, [TIER_TABLE_ID, EVIDENCE_BUNDLE_ID])

    def test_instance_poisoning_cannot_split_margin_artifact_snapshot(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)

            def poisoned(*_args, **_kwargs):
                raise AssertionError("instance artifact reader must not execute")

            store.load_manifest = poisoned
            store.read_bytes = poisoned
            store.read_authenticated_snapshot = poisoned

            result = evaluate(evidence=trusted, artifact_store=store)
            self.assertEqual(result.verdict, "ALLOW_NEW_RISK")

    def test_class_rebinding_cannot_replace_margin_artifact_reader(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                side_effect=AssertionError(
                    "late ArtifactStore class rebinding must not become financial authority"
                ),
            ):
                result = evaluate(evidence=trusted, artifact_store=store)
            self.assertEqual(result.verdict, "ALLOW_NEW_RISK")

    def test_margin_artifact_snapshot_requires_exact_builtin_bytes(self):
        class HostileBytes(bytes):
            pass

        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            original = ArtifactStore.read_authenticated_snapshot

            def hostile_snapshot(instance, artifact_id):
                manifest, payload = original(instance, artifact_id)
                return manifest, HostileBytes(payload)

            with patch(
                "mvp.autotrade_mvp.perpetual_margin."
                "_READ_AUTHENTICATED_ARTIFACT_SNAPSHOT",
                side_effect=hostile_snapshot,
            ):
                with self.assertRaisesRegex(
                    PerpetualMarginError,
                    "unsupported representation",
                ):
                    evaluate(evidence=trusted, artifact_store=store)

    def test_margin_artifact_snapshot_failures_are_bounded_and_fail_closed(self):
        trusted = evidence()
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            publish_margin_artifacts(store, trusted)
            for error in (
                ArtifactIntegrityError("integrity"),
                OSError("io"),
                TypeError("type"),
                ValueError("value"),
            ):
                with self.subTest(error=type(error).__name__):
                    with patch(
                        "mvp.autotrade_mvp.perpetual_margin."
                        "_READ_AUTHENTICATED_ARTIFACT_SNAPSHOT",
                        side_effect=error,
                    ):
                        with self.assertRaisesRegex(
                            PerpetualMarginError,
                            "artifact is missing or corrupt",
                        ):
                            evaluate(evidence=trusted, artifact_store=store)

    def test_artifact_store_subclass_is_rejected_before_virtual_dispatch(self):
        class ForgedArtifactStore(ArtifactStore):
            load_called = False
            snapshot_called = False

            def load_manifest(self, artifact_id):
                type(self).load_called = True
                raise AssertionError("subclass method must not run")

            def read_bytes(self, artifact_id):
                raise AssertionError("subclass method must not run")

            def read_authenticated_snapshot(self, artifact_id):
                type(self).snapshot_called = True
                raise AssertionError("subclass snapshot reader must not run")

        trusted = evidence()
        with TemporaryDirectory() as directory:
            canonical = ArtifactStore(directory)
            publish_margin_artifacts(canonical, trusted)
            forged = ForgedArtifactStore(directory)
            with self.assertRaisesRegex(
                PerpetualMarginError,
                "canonical ArtifactStore",
            ):
                evaluate(evidence=trusted, artifact_store=forged)
            self.assertFalse(ForgedArtifactStore.load_called)
            self.assertFalse(ForgedArtifactStore.snapshot_called)

if __name__ == "__main__":
    unittest.main()
