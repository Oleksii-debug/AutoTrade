from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.bybit_fee_currency_authority import (
    BybitExecutionFeeCurrencyAuthority,
    BybitFeeCurrencyAuthorityError,
    bybit_execution_fee_currency_semantic_claim,
    issue_bybit_execution_fee_currency_authority,
    project_bybit_execution_fee_currency_authority,
)
from mvp.autotrade_mvp.bybit_v5 import parse_executions
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.provider_core import ProviderCoreError
from mvp.autotrade_mvp.qualification_attestation import EvidenceArtifactRef
from mvp.tests.test_bybit_v5 import bound_execution_response, read_capability
from mvp.tests.test_provider_selection import accepted_spot_q


NOW = datetime(2026, 10, 4, 5, 5, tzinfo=timezone.utc)
TRADE_AT = datetime(2026, 10, 4, 5, 2, tzinfo=timezone.utc)
RULE_FROM = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
RULE_UNTIL = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)
INSTRUMENT_ID = "55555555-5555-4555-8555-555555555555"
UNDERLYING_ID = "66666666-6666-4666-8666-666666666666"
INSTRUMENT_REF = f"{INSTRUMENT_ID}@1"


def linear_instrument(*, with_evidence=True) -> InstrumentVersion:
    evidence = ()
    if with_evidence:
        evidence = (
            {
                "artifact_id": "77777777-7777-4777-8777-777777777777",
                "sha256": "sha256:" + "7" * 64,
                "observed_at": "2026-09-30T12:00:00Z",
                "source_uri": "https://bybit-exchange.github.io/docs/v5/market/instrument",
            },
        )
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id="BYBIT",
        venue_id="BYBIT",
        provider_symbol="ETHPERP",
        asset_class="PERPETUAL",
        base_currency="ETH",
        quote_currency="USDT",
        settlement_currency="USDT",
        quantity_unit="CONTRACT",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 9, 1, tzinfo=timezone.utc),
        status="ACTIVE",
        payoff="LINEAR",
        underlying_id=f"{UNDERLYING_ID}@1",
        settlement_method="CASH",
        funding_schedule={"interval_minutes": 480},
        margin_model_id="BYBIT_LINEAR_V1",
        metadata_evidence=evidence,
    )


def execution_response(*, fee_currency="", exec_time=TRADE_AT, account_capability=None):
    response = {
        "retCode": 0,
        "result": {
            "category": "linear",
            "list": [
                {
                    "execId": "exec-qualified-fee-rule",
                    "orderLinkId": "",
                    "symbol": "ETHPERP",
                    "side": "Buy",
                    "execQty": "0.1",
                    "execPrice": "1190.15",
                    "execFee": "0.071409",
                    "feeCurrency": fee_currency,
                    "extraFees": "",
                    "execTime": str(int(exec_time.timestamp() * 1000)),
                }
            ],
        },
    }
    return bound_execution_response(
        response,
        instrument_version=INSTRUMENT_REF,
        query_category="linear",
        capability=account_capability,
        at=NOW,
    )


def issued_fixture(
    *,
    q_fee_currency="USDT",
    issued_fee_currency="USDT",
    include_claim=True,
    account_id="paper-1",
):
    instrument = linear_instrument()
    registry = InstrumentRegistry(versions=(instrument,))
    capability = read_capability(
        account_id=account_id,
        instrument_version=INSTRUMENT_REF,
        provider_environment="TESTNET",
        at=NOW,
    )
    claim_key, claim_digest = bybit_execution_fee_currency_semantic_claim(
        provider_environment="TESTNET",
        product_family="LINEAR_DERIVATIVES",
        category="linear",
        instrument=instrument,
        fee_currency=q_fee_currency,
        rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
        rule_version="2026-10",
        rule_valid_from=RULE_FROM,
        rule_valid_until=RULE_UNTIL,
    )
    qualification, _receipt, _protocol = accepted_spot_q(
        ordinal=91,
        product_family="LINEAR_DERIVATIVES",
        extra_route_semantics=(
            {claim_key: claim_digest} if include_claim else None
        ),
    )
    authority = issue_bybit_execution_fee_currency_authority(
        qualification=qualification,
        capability=capability,
        instrument_registry=registry,
        venue_id="BYBIT",
        provider_symbol="ETHPERP",
        fee_currency=issued_fee_currency,
        rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
        rule_version="2026-10",
        rule_valid_from=RULE_FROM,
        rule_valid_until=RULE_UNTIL,
        at=NOW,
    )
    return instrument, registry, capability, qualification, authority


class BybitFeeCurrencyAuthorityTests(unittest.TestCase):
    def test_empty_provider_currency_uses_only_sealed_q_and_instrument_authority(self):
        _instrument, _registry, capability, qualification, authority = (
            issued_fixture()
        )
        observation = execution_response(account_capability=capability)
        fill = parse_executions(
            observation,
            instrument_versions={"ETHPERP": INSTRUMENT_REF},
            fee_currency_authorities=(authority,),
        )[0]
        projection = project_bybit_execution_fee_currency_authority(authority)

        self.assertEqual(fill.fee_amount, Decimal("0.071409"))
        self.assertEqual(fill.fee_currency, "USDT")
        self.assertEqual(fill.instrument, INSTRUMENT_REF)
        self.assertIn(observation.evidence_ref, fill.evidence_refs)
        self.assertIn(projection.evidence_ref, fill.evidence_refs)
        self.assertEqual(projection.qualification_id, qualification.qualification_id)
        self.assertEqual(projection.instrument_version, INSTRUMENT_REF)
        self.assertEqual(projection.provider_environment, "TESTNET")
        self.assertEqual(projection.category, "linear")

    def test_q_claim_prevents_caller_selected_alternative_currency(self):
        instrument = linear_instrument()
        registry = InstrumentRegistry(versions=(instrument,))
        capability = read_capability(
            instrument_version=INSTRUMENT_REF,
            provider_environment="TESTNET",
            at=NOW,
        )
        claim_key, claim_digest = bybit_execution_fee_currency_semantic_claim(
            provider_environment="TESTNET",
            product_family="LINEAR_DERIVATIVES",
            category="linear",
            instrument=instrument,
            fee_currency="USDT",
            rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
            rule_version="2026-10",
            rule_valid_from=RULE_FROM,
            rule_valid_until=RULE_UNTIL,
        )
        qualification, _receipt, _protocol = accepted_spot_q(
            ordinal=92,
            product_family="LINEAR_DERIVATIVES",
            extra_route_semantics={claim_key: claim_digest},
        )
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "does not cover exact fee-currency rule",
        ):
            issue_bybit_execution_fee_currency_authority(
                qualification=qualification,
                capability=capability,
                instrument_registry=registry,
                venue_id="BYBIT",
                provider_symbol="ETHPERP",
                fee_currency="USDC",
                rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
                rule_version="2026-10",
                rule_valid_from=RULE_FROM,
                rule_valid_until=RULE_UNTIL,
                at=NOW,
            )

    def test_q_without_exact_rule_claim_cannot_issue_authority(self):
        instrument = linear_instrument()
        registry = InstrumentRegistry(versions=(instrument,))
        capability = read_capability(
            instrument_version=INSTRUMENT_REF,
            provider_environment="TESTNET",
            at=NOW,
        )
        qualification, _receipt, _protocol = accepted_spot_q(
            ordinal=93,
            product_family="LINEAR_DERIVATIVES",
        )
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "does not cover exact fee-currency rule",
        ):
            issue_bybit_execution_fee_currency_authority(
                qualification=qualification,
                capability=capability,
                instrument_registry=registry,
                venue_id="BYBIT",
                provider_symbol="ETHPERP",
                fee_currency="USDT",
                rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
                rule_version="2026-10",
                rule_valid_from=RULE_FROM,
                rule_valid_until=RULE_UNTIL,
                at=NOW,
            )

    def test_present_provider_currency_is_primary_but_must_reconcile_with_rule(self):
        _instrument, _registry, capability, _qualification, authority = (
            issued_fixture()
        )
        observation = execution_response(
            fee_currency="USDT",
            account_capability=capability,
        )
        fill = parse_executions(
            observation,
            instrument_versions={"ETHPERP": INSTRUMENT_REF},
            fee_currency_authorities=(authority,),
        )[0]
        self.assertEqual(fill.fee_currency, "USDT")

        conflicting = execution_response(
            fee_currency="USDC",
            account_capability=capability,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "provider fee currency conflicts with qualified rule",
        ):
            parse_executions(
                conflicting,
                instrument_versions={"ETHPERP": INSTRUMENT_REF},
                fee_currency_authorities=(authority,),
            )

        provider_only = parse_executions(
            observation,
            instrument_versions={"ETHPERP": INSTRUMENT_REF},
        )[0]
        self.assertEqual(provider_only.fee_currency, "USDT")
        self.assertEqual(
            provider_only.evidence_refs,
            (observation.evidence_ref,),
        )

    def test_authority_is_bound_to_capability_identity_and_rule_interval(self):
        _instrument, _registry, capability, _qualification, authority = (
            issued_fixture()
        )
        other_capability = read_capability(
            instrument_version=INSTRUMENT_REF,
            provider_environment="TESTNET",
            at=NOW,
        )
        other_observation = execution_response(
            account_capability=other_capability,
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "capability mismatch",
        ):
            parse_executions(
                other_observation,
                instrument_versions={"ETHPERP": INSTRUMENT_REF},
                fee_currency_authorities=(authority,),
            )

        before_rule = execution_response(
            account_capability=capability,
            exec_time=datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(
            ProviderCoreError,
            "not valid at execution time",
        ):
            parse_executions(
                before_rule,
                instrument_versions={"ETHPERP": INSTRUMENT_REF},
                fee_currency_authorities=(authority,),
            )

    def test_direct_mutated_or_non_tuple_authority_input_fails_closed(self):
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "must come from canonical",
        ):
            BybitExecutionFeeCurrencyAuthority()

        _instrument, _registry, capability, _qualification, authority = (
            issued_fixture()
        )
        observation = execution_response(account_capability=capability)
        with self.assertRaisesRegex(
            ProviderCoreError,
            "exact tuple",
        ):
            parse_executions(
                observation,
                instrument_versions={"ETHPERP": INSTRUMENT_REF},
                fee_currency_authorities=[authority],
            )

        object.__setattr__(authority, "fee_currency", "USDC")
        with self.assertRaisesRegex(
            ProviderCoreError,
            "not canonically issued",
        ):
            parse_executions(
                observation,
                instrument_versions={"ETHPERP": INSTRUMENT_REF},
                fee_currency_authorities=(authority,),
            )

    def test_mutated_q_chronology_or_campaign_evidence_cannot_issue_authority(self):
        instrument, registry, capability, qualification, _authority = issued_fixture()
        object.__setattr__(
            qualification,
            "valid_until",
            "2026-10-09T00:00:00Z",
        )
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "chronology does not match Q identity",
        ):
            issue_bybit_execution_fee_currency_authority(
                qualification=qualification,
                capability=capability,
                instrument_registry=registry,
                venue_id="BYBIT",
                provider_symbol="ETHPERP",
                fee_currency="USDT",
                rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
                rule_version="2026-10",
                rule_valid_from=RULE_FROM,
                rule_valid_until=RULE_UNTIL,
                at=NOW,
            )

        instrument, registry, capability, qualification, _authority = issued_fixture()
        original_ref = qualification.campaign_artifact_ref
        object.__setattr__(
            qualification,
            "campaign_artifact_ref",
            EvidenceArtifactRef(
                artifact_id=original_ref.artifact_id,
                sha256="sha256:" + "8" * 64,
                media_type=original_ref.media_type,
                evidence_kind=original_ref.evidence_kind,
                source_sha=original_ref.source_sha,
            ),
        )
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "evidence set does not match Q identity",
        ):
            issue_bybit_execution_fee_currency_authority(
                qualification=qualification,
                capability=capability,
                instrument_registry=registry,
                venue_id="BYBIT",
                provider_symbol="ETHPERP",
                fee_currency="USDT",
                rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
                rule_version="2026-10",
                rule_valid_from=RULE_FROM,
                rule_valid_until=RULE_UNTIL,
                at=NOW,
            )

    def test_instrument_without_metadata_evidence_cannot_back_fee_rule(self):
        instrument = linear_instrument(with_evidence=False)
        with self.assertRaisesRegex(
            BybitFeeCurrencyAuthorityError,
            "lacks immutable metadata evidence",
        ):
            bybit_execution_fee_currency_semantic_claim(
                provider_environment="TESTNET",
                product_family="LINEAR_DERIVATIVES",
                category="linear",
                instrument=instrument,
                fee_currency="USDT",
                rule_id="BYBIT_EXECUTION_FEE_CURRENCY",
                rule_version="2026-10",
                rule_valid_from=RULE_FROM,
                rule_valid_until=RULE_UNTIL,
            )


if __name__ == "__main__":
    unittest.main()
