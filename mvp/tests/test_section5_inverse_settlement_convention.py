from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.instruments import InstrumentRegistryError, InstrumentVersion
from mvp.autotrade_mvp.settlement_convention import (
    SettlementConvention,
    SettlementConventionError,
)


UTC = timezone.utc
FUTURE_ID = "44444444-4444-4444-8444-444444444444"
UNDERLYING_ID = "55555555-5555-4555-8555-555555555555"
EVIDENCE_ID = "66666666-6666-4666-8666-666666666666"
EVIDENCE_SHA = "sha256:" + "a" * 64


def _convention(**overrides) -> SettlementConvention:
    values = {
        "provider_id": "TEST_PROVIDER",
        "instrument_id": FUTURE_ID,
        "instrument_version": 2,
        "settlement_currency": "BTC",
        "quantum": "0.00000001",
        "rounding": "HALF_EVEN",
        "evidence_artifact_id": EVIDENCE_ID,
        "evidence_sha256": EVIDENCE_SHA,
    }
    values.update(overrides)
    return SettlementConvention(**values)


def _future(*, convention=_convention(), evidence_sha=EVIDENCE_SHA) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=FUTURE_ID,
        version=2,
        provider_id="TEST_PROVIDER",
        venue_id="DERIVATIVES",
        provider_symbol="BTCUSD-202612",
        asset_class="FUTURE",
        base_currency="BTC",
        quote_currency="USD",
        settlement_currency="BTC",
        quantity_unit="contract",
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.5"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="DERIVATIVES_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=UTC),
        payoff="INVERSE",
        underlying_id=f"{UNDERLYING_ID}@1",
        expiry=datetime(2026, 12, 18, 21, tzinfo=UTC),
        last_trade_at=datetime(2026, 12, 18, 20, 59, tzinfo=UTC),
        delivery_cutoff=datetime(2026, 12, 18, 21, tzinfo=UTC),
        settlement_method="CASH",
        margin_model_id="inverse-margin-v1",
        metadata_evidence=(
            {
                "artifact_id": EVIDENCE_ID,
                "sha256": evidence_sha,
                "observed_at": "2026-01-01T00:00:00Z",
            },
        ),
        settlement_convention=convention,
    )


class Section5InverseSettlementConventionTests(unittest.TestCase):
    def test_inverse_future_requires_versioned_settlement_convention(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "INVERSE future requires a settlement convention",
        ):
            _future(convention=None)

    def test_convention_is_part_of_immutable_contract_identity(self):
        version = _future()
        payload = version.to_contract_dict()
        self.assertEqual(
            payload["settlement_convention"],
            version.settlement_convention.payload(),
        )
        self.assertEqual(payload["settlement_convention"]["quantum"], "0.00000001")
        self.assertEqual(payload["settlement_convention"]["rounding"], "HALF_EVEN")
        self.assertIn("settlement-convention:sha256:", version.settlement_convention.convention_id)

    def test_convention_scope_must_match_exact_instrument_version(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "settlement convention scope must match InstrumentVersion",
        ):
            _future(convention=_convention(instrument_version=3))

    def test_convention_evidence_must_be_covered_by_instrument_metadata(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "settlement convention evidence must be covered by metadata_evidence",
        ):
            _future(evidence_sha="sha256:" + "b" * 64)

    def test_linear_future_cannot_smuggle_inverse_settlement_convention(self):
        version = _future()
        values = version.to_contract_dict()
        self.assertEqual(values["payoff"], "INVERSE")
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "settlement convention requires an INVERSE future",
        ):
            InstrumentVersion(
                instrument_id=FUTURE_ID,
                version=2,
                provider_id="TEST_PROVIDER",
                venue_id="DERIVATIVES",
                provider_symbol="BTCUSD-LINEAR",
                asset_class="FUTURE",
                base_currency="BTC",
                quote_currency="USD",
                settlement_currency="BTC",
                quantity_unit="contract",
                contract_multiplier=Decimal("100"),
                price_tick=Decimal("0.5"),
                quantity_step=Decimal("1"),
                minimum_quantity=Decimal("1"),
                calendar_id="DERIVATIVES_24_7",
                timezone_id="UTC",
                effective_from=datetime(2026, 1, 1, tzinfo=UTC),
                payoff="LINEAR",
                underlying_id=f"{UNDERLYING_ID}@1",
                expiry=datetime(2026, 12, 18, 21, tzinfo=UTC),
                last_trade_at=datetime(2026, 12, 18, 20, 59, tzinfo=UTC),
                delivery_cutoff=datetime(2026, 12, 18, 21, tzinfo=UTC),
                settlement_method="CASH",
                margin_model_id="linear-margin-v1",
                metadata_evidence=(
                    {
                        "artifact_id": EVIDENCE_ID,
                        "sha256": EVIDENCE_SHA,
                        "observed_at": "2026-01-01T00:00:00Z",
                    },
                ),
                settlement_convention=_convention(),
            )

    def test_quantum_text_is_canonical_and_bounded(self):
        with self.assertRaises(SettlementConventionError):
            _convention(quantum="1E-8")
        with self.assertRaises(SettlementConventionError):
            _convention(quantum="0")


if __name__ == "__main__":
    unittest.main()
