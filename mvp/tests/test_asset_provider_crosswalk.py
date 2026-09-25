from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.asset_provider_crosswalk import (
    CrosswalkError,
    Lifecycle,
    LifecycleEvidence,
    advertised_lifecycle_keys,
    qualify_asset_provider_crosswalk,
    required_cases,
)
from mvp.autotrade_mvp.corporate_actions import (
    CorporateActionBook,
    CorporateEvent,
    EquityState,
)
from mvp.autotrade_mvp.futures import linear_futures_pnl
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


SOURCE = "1" * 40
ADAPTER = "2" * 40
NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)


def complete_evidence(key):
    return LifecycleEvidence(
        key=key,
        source_sha=SOURCE,
        adapter_sha=ADAPTER,
        cases=required_cases(key.lifecycle),
        reconciliation_complete=True,
        economic_units_exact=True,
    )


def adapter_map():
    return {
        (key.provider_id, key.product_family): ADAPTER
        for key in advertised_lifecycle_keys()
    }


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
        verdict = qualify_asset_provider_crosswalk(
            evidence,
            exact_source_sha=SOURCE,
            exact_adapter_shas=adapter_map(),
        )
        self.assertEqual(verdict.status, "PASS")
        self.assertEqual(verdict.missing_keys, ())
        self.assertEqual(verdict.invalid_keys, ())
        self.assertFalse(verdict.trading_authority_granted)

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
        event = CorporateEvent.create(
            event_id="split-1",
            instrument_id="ABC",
            instrument_version=1,
            kind="SPLIT",
            effective_date=date(2026, 9, 25),
            source_revision="official-v1",
            payload={"numerator": "2", "denominator": "1"},
        )
        book = CorporateActionBook(state)
        first = book.apply(event)
        second = book.apply(event)
        self.assertEqual(first, second)
        self.assertEqual(book.state.quantity, Decimal("20"))
        self.assertEqual(book.state.total_basis, Decimal("1000"))


if __name__ == "__main__":
    unittest.main()
