from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.asset_provider_crosswalk import (
    CrosswalkError,
    Lifecycle,
    LifecycleEvidence,
    advertised_lifecycle_keys,
    lifecycle_evidence_bytes,
    lifecycle_evidence_payload,
    qualify_asset_provider_crosswalk,
    required_cases,
)
from mvp.autotrade_mvp.corporate_actions import (
    CorporateActionBook,
    CorporateEvent,
    EquityState,
)
from mvp.autotrade_mvp.futures import linear_futures_pnl
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.options import (
    OptionContract,
    expiration_cash_settlement,
)
from mvp.autotrade_mvp.perpetuals import (
    FundingConvention,
    MarketSnapshot,
    PerpetualContract,
    funding_cashflow,
)
from mvp.autotrade_mvp.qualification_attestation import (
    EvidenceArtifactRef,
    QualificationScope,
    SignedQualificationAttestation,
)
from mvp.tests.test_qualification_attestation import (
    attestation,
    policy as attestation_policy,
    root as attestation_root,
    sign,
)


SOURCE = "1" * 40
ADAPTER = "2" * 40
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def complete_evidence(key):
    artifact_id = str(
        uuid5(
            NAMESPACE_URL,
            "autotrade:wp61:"
            + key.provider_id
            + ":"
            + key.product_family
            + ":"
            + key.lifecycle.value,
        )
    )
    provisional = LifecycleEvidence(
        key=key,
        source_sha=SOURCE,
        adapter_sha=ADAPTER,
        cases=required_cases(key.lifecycle),
        reconciliation_complete=True,
        economic_units_exact=True,
        artifact_id=artifact_id,
        artifact_sha256="sha256:" + "0" * 64,
    )
    return replace(
        provisional,
        artifact_sha256="sha256:"
        + sha256(lifecycle_evidence_bytes(provisional)).hexdigest(),
    )


def adapter_map():
    return {
        (key.provider_id, key.product_family): ADAPTER
        for key in advertised_lifecycle_keys()
    }


def trusted_qualify(evidence, *, signing_root=None, canonical_policy=None):
    evidence = tuple(evidence)
    with TemporaryDirectory() as directory:
        store = ArtifactStore(directory)
        for item in evidence:
            store.publish_bytes(
                artifact_id=item.artifact_id,
                data=lifecycle_evidence_bytes(item),
                media_type="application/vnd.autotrade.asset-provider-lifecycle",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{item.source_sha}"],
                metadata=lifecycle_evidence_payload(item),
            )
        trust_root = signing_root or attestation_root(
            scopes=(
                QualificationScope(
                    "ASSET_PROVIDER_CROSSWALK",
                    "INTEGRATION",
                ),
            )
        )
        trust_policy = attestation_policy(trust_root)
        canonical_policy = canonical_policy or trust_policy
        signed = attestation(
            trust_root,
            source_sha=SOURCE,
            domain="ASSET_PROVIDER_CROSSWALK",
            gate="INTEGRATION",
            package_id="WP-61",
            protocol_id="asset-provider-crosswalk-v1",
            protocol_version="1.0.0",
            requirement_ids=("complete-advertised-lifecycle-matrix",),
            evidence_refs=tuple(
                EvidenceArtifactRef(
                    artifact_id=item.artifact_id,
                    sha256=item.artifact_sha256,
                    media_type="application/vnd.autotrade.asset-provider-lifecycle",
                    evidence_kind="ASSET_PROVIDER_LIFECYCLE",
                    source_sha=item.source_sha,
                )
                for item in evidence
            ),
            result="PASS",
        )
        with patch(
            "mvp.autotrade_mvp.qualification_attestation."
            "load_canonical_qualification_trust_policy",
            return_value=canonical_policy,
        ):
            return qualify_asset_provider_crosswalk(
                evidence,
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
                evidence_store=store,
                qualification_receipt=SignedQualificationAttestation(
                    signed,
                    sign(signed),
                ),
            )


class AssetProviderCrosswalkTests(unittest.TestCase):
    def test_crosswalk_covers_every_declared_lifecycle_combination(self):
        keys = advertised_lifecycle_keys()
        self.assertEqual(len(keys), 19)
        self.assertIn(
            ("BYBIT", "OPTIONS", Lifecycle.OPTIONS),
            {(k.provider_id, k.product_family, k.lifecycle) for k in keys},
        )
        self.assertIn(
            ("IBKR", "EQUITIES", Lifecycle.CORPORATE),
            {(k.provider_id, k.product_family, k.lifecycle) for k in keys},
        )
        self.assertIn(
            ("BINANCE", "USD_M", Lifecycle.PERPETUAL),
            {(k.provider_id, k.product_family, k.lifecycle) for k in keys},
        )

    def test_complete_exact_build_matrix_passes_without_granting_trade_authority(self):
        evidence = [complete_evidence(key) for key in advertised_lifecycle_keys()]
        verdict = trusted_qualify(evidence)
        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.missing_keys, ())
        self.assertEqual(verdict.invalid_keys, ())
        self.assertFalse(verdict.trading_authority_granted)

    def test_self_selected_root_cannot_authorize_crosswalk_pass(self):
        evidence = [complete_evidence(key) for key in advertised_lifecycle_keys()]
        candidate_root = attestation_root(
            scopes=(
                QualificationScope(
                    "ASSET_PROVIDER_CROSSWALK",
                    "INTEGRATION",
                ),
            )
        )
        canonical_root = replace(
            candidate_root,
            producer_id="qualifier.canonical.crosswalk",
        )
        verdict = trusted_qualify(
            evidence,
            signing_root=candidate_root,
            canonical_policy=attestation_policy(canonical_root),
        )
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertIn(
            "independent_evidence_trust_invalid",
            verdict.reason_codes,
        )
        self.assertFalse(verdict.trading_authority_granted)

    def test_self_asserted_complete_matrix_is_not_terminal_pass(self):
        evidence = [complete_evidence(key) for key in advertised_lifecycle_keys()]
        verdict = qualify_asset_provider_crosswalk(
            evidence,
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertIn(
            "immutable_evidence_store_unavailable",
            verdict.reason_codes,
        )
        self.assertIn(
            "independent_evidence_trust_unavailable",
            verdict.reason_codes,
        )
        self.assertFalse(verdict.trading_authority_granted)

    def test_mutated_lifecycle_facts_do_not_match_immutable_receipt(self):
        key = advertised_lifecycle_keys()[0]
        item = complete_evidence(key)
        mutated = replace(
            item,
            cases=item.cases | {"caller-invented-case"},
        )
        with TemporaryDirectory() as directory:
            store = ArtifactStore(directory)
            store.publish_bytes(
                artifact_id=item.artifact_id,
                data=lifecycle_evidence_bytes(item),
                media_type="application/vnd.autotrade.asset-provider-lifecycle",
                rights={"storage": True, "export": False},
                source_refs=[f"git:{item.source_sha}"],
                metadata=lifecycle_evidence_payload(item),
            )
            verdict = qualify_asset_provider_crosswalk(
                [mutated],
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
                evidence_store=store,
            )
        self.assertIn(key, verdict.invalid_keys)
        self.assertEqual(verdict.status, "INCOMPLETE")

    def test_missing_combination_fails_closed(self):
        keys = advertised_lifecycle_keys()
        evidence = [complete_evidence(key) for key in keys[1:]]
        verdict = qualify_asset_provider_crosswalk(
            evidence,
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertEqual(verdict.status, "INCOMPLETE")
        self.assertEqual(verdict.missing_keys, (keys[0],))
        self.assertFalse(verdict.trading_authority_granted)

    def test_wrong_source_or_adapter_revision_invalidates_exact_combination(self):
        key = advertised_lifecycle_keys()[0]
        wrong_source = replace(complete_evidence(key), source_sha="3" * 40)
        verdict = qualify_asset_provider_crosswalk(
            [wrong_source],
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertIn(key, verdict.invalid_keys)

        wrong_adapter = replace(complete_evidence(key), adapter_sha="4" * 40)
        verdict = qualify_asset_provider_crosswalk(
            [wrong_adapter],
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertIn(key, verdict.invalid_keys)

    def test_missing_lifecycle_case_or_reconciliation_is_invalid(self):
        key = next(
            key for key in advertised_lifecycle_keys()
            if key.lifecycle == Lifecycle.OPTIONS
        )
        evidence = complete_evidence(key)
        without_assignment = replace(
            evidence, cases=evidence.cases - {"assignment"}
        )
        verdict = qualify_asset_provider_crosswalk(
            [without_assignment],
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertIn(key, verdict.invalid_keys)

        without_reconciliation = replace(
            evidence, reconciliation_complete=False
        )
        verdict = qualify_asset_provider_crosswalk(
            [without_reconciliation],
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertIn(key, verdict.invalid_keys)

    def test_duplicate_evidence_is_rejected(self):
        key = advertised_lifecycle_keys()[0]
        evidence = complete_evidence(key)
        with self.assertRaisesRegex(CrosswalkError, "duplicate"):
            qualify_asset_provider_crosswalk(
                [evidence, evidence],
                exact_source_sha=SOURCE,
                exact_adapter_shas=adapter_map(),
            )

    def test_exact_decimal_futures_economics_anchor(self):
        pnl = linear_futures_pnl(
            signed_contracts="2",
            multiplier="5",
            entry_price="100",
            exit_price="103",
        )
        self.assertEqual(pnl, Decimal("30"))

    def test_perpetual_funding_anchor_uses_explicit_venue_convention(self):
        contract = PerpetualContract(
            instrument_id="BTC-PERP",
            settlement_currency="USDT",
            collateral_currency="USDT",
            multiplier=Decimal("1"),
            payoff="LINEAR",
        )
        snapshot = MarketSnapshot(
            mark_price=Decimal("100"),
            index_price=Decimal("100"),
            observed_at=NOW,
            max_age=timedelta(minutes=1),
            max_mark_index_deviation=Decimal("0.01"),
        )
        currency, cashflow = funding_cashflow(
            contract=contract,
            signed_contracts="2",
            funding_rate="0.001",
            snapshot=snapshot,
            convention=FundingConvention("LONG_PAYS", "MARK"),
            at=NOW,
        )
        self.assertEqual(currency, "USDT")
        self.assertEqual(cashflow, Decimal("-0.200"))

    def test_option_expiry_anchor_keeps_exact_multiplier_units(self):
        contract = OptionContract(
            instrument="ABC-C-100",
            right="CALL",
            strike=Decimal("100"),
            multiplier=Decimal("100"),
            settlement_currency="USD",
            settlement_method="CASH",
            exercise_style="EUROPEAN",
            expiry=NOW + timedelta(days=1),
            exercise_cutoff=NOW + timedelta(hours=23),
        )
        amount = expiration_cash_settlement(
            contract,
            signed_contracts="1",
            underlying_price="105",
        )
        self.assertEqual(amount, Decimal("500"))

    def test_corporate_action_anchor_is_idempotent_and_basis_preserving(self):
        state = EquityState.create(
            symbol="ABC",
            quantity="10",
            total_basis="1000",
            settled_cash="500",
            currency="USD",
        )
        instrument_id = "11111111-1111-4111-8111-111111111111"
        version = InstrumentVersion(
            instrument_id=instrument_id,
            version=1,
            provider_id="simulated",
            venue_id="simulated-venue",
            provider_symbol="ABC",
            asset_class="CASH_EQUITY",
            base_currency="ABC",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="ABC",
            contract_multiplier=Decimal("1"),
            price_tick=Decimal("0.01"),
            quantity_step=Decimal("1"),
            minimum_quantity=Decimal("1"),
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            status="ACTIVE",
        )
        registry = InstrumentRegistry(versions=(version,))
        event = CorporateEvent.create(
            event_id="split-1",
            instrument_id=instrument_id,
            instrument_version=1,
            kind="SPLIT",
            effective_date=date(2026, 9, 25),
            source_revision="official-v1",
            payload={"numerator": "2", "denominator": "1"},
        )
        book = CorporateActionBook(
            state,
            instrument_version=version,
            registry=registry,
        )
        first = book.apply(event)
        second = book.apply(event)
        self.assertEqual(first, second)
        self.assertEqual(book.state.quantity, Decimal("20"))
        self.assertEqual(book.state.total_basis, Decimal("1000"))


if __name__ == "__main__":
    unittest.main()
