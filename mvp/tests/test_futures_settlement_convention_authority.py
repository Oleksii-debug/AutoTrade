from dataclasses import fields, replace
from datetime import datetime, timezone
from inspect import signature
import unittest
from uuid import UUID

from mvp.autotrade_mvp.futures import settle_and_book_inverse_variation_margin
from mvp.autotrade_mvp.instruments import (
    InstrumentRegistry,
    InstrumentRegistryError,
    InstrumentVersion,
)
from mvp.autotrade_mvp.settlement_convention import (
    SettlementConvention,
    SettlementConventionError,
)


INSTRUMENT_ID = "00000000-0000-0000-0000-000000000101"
UNDERLYING_ID = "00000000-0000-0000-0000-000000000202@1"
EXPIRY = datetime(2027, 3, 26, 8, tzinfo=timezone.utc)
LAST_TRADE = datetime(2027, 3, 26, 7, 55, tzinfo=timezone.utc)
DELIVERY_CUTOFF = datetime(2027, 3, 26, 7, 50, tzinfo=timezone.utc)


def settlement_convention(**overrides):
    values = {
        "provider_id": "KRAKEN_FUTURES",
        "instrument_id": INSTRUMENT_ID,
        "instrument_version": 1,
        "settlement_currency": "BTC",
        "quantum": "0.00000001",
        "rounding": "HALF_EVEN",
        "evidence_artifact_id": "00000000-0000-0000-0000-000000000303",
        "evidence_sha256": "sha256:" + "3" * 64,
    }
    values.update(overrides)
    return SettlementConvention(**values)


def inverse_future(**overrides):
    values = {
        "instrument_id": INSTRUMENT_ID,
        "version": 1,
        "provider_id": "KRAKEN_FUTURES",
        "venue_id": "KRAKEN_FUTURES",
        "provider_symbol": "PI_XBTUSD",
        "asset_class": "FUTURE",
        "base_currency": "BTC",
        "quote_currency": "USD",
        "settlement_currency": "BTC",
        "quantity_unit": "CONTRACT",
        "contract_multiplier": "1",
        "price_tick": "0.5",
        "quantity_step": "1",
        "minimum_quantity": "1",
        "calendar_id": "CONTINUOUS_24_7",
        "timezone_id": "UTC",
        "effective_from": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "payoff": "INVERSE",
        "underlying_id": UNDERLYING_ID,
        "expiry": EXPIRY,
        "last_trade_at": LAST_TRADE,
        "delivery_cutoff": DELIVERY_CUTOFF,
        "settlement_method": "CASH",
        "margin_model_id": "KRAKEN-INVERSE-V1",
        "metadata_evidence": (
            {
                "artifact_id": str(UUID("00000000-0000-0000-0000-000000000303")),
                "sha256": "sha256:" + "3" * 64,
                "observed_at": "2026-01-01T00:00:00Z",
                "source_uri": "https://docs.kraken.com/futures/contracts",
            },
        ),
    }
    values.update(overrides)
    return InstrumentVersion(**values)


class FuturesSettlementConventionAuthorityTests(unittest.TestCase):
    def test_convention_identity_changes_with_terminal_economics_or_evidence(self):
        original = settlement_convention()
        for name, value in {
            "quantum": "0.0000001",
            "rounding": "DOWN",
            "instrument_version": 2,
            "settlement_currency": "USD",
            "evidence_artifact_id": "00000000-0000-0000-0000-000000000304",
            "evidence_sha256": "sha256:" + "4" * 64,
        }.items():
            with self.subTest(field=name):
                changed = replace(original, **{name: value})
                self.assertNotEqual(changed.convention_id, original.convention_id)

    def test_convention_rejects_noncanonical_quantum_and_rounding(self):
        with self.assertRaises(SettlementConventionError):
            settlement_convention(quantum="1e-8")
        with self.assertRaises(SettlementConventionError):
            settlement_convention(quantum="0")
        with self.assertRaises(SettlementConventionError):
            settlement_convention(rounding="CEILING")
        with self.assertRaises(SettlementConventionError):
            settlement_convention(instrument_version=True)

    def test_instrument_registry_rejects_version_subclass_before_virtual_reads(self):
        calls = []

        class HostileInstrumentVersion(InstrumentVersion):
            def __getattribute__(self, name):
                if name in {
                    "calendar_id",
                    "timezone_id",
                    "instrument_id",
                    "version",
                    "provider_id",
                    "venue_id",
                    "provider_symbol",
                    "effective_from",
                    "effective_to",
                }:
                    calls.append(name)
                return super().__getattribute__(name)

        base = inverse_future()
        hostile = HostileInstrumentVersion(**base.__dict__)
        calls.clear()

        with self.assertRaisesRegex(TypeError, "exact InstrumentVersion"):
            InstrumentRegistry().add(hostile)
        self.assertEqual(calls, [])

        calls.clear()
        with self.assertRaisesRegex(TypeError, "exact InstrumentVersion"):
            InstrumentRegistry(versions=(hostile,))
        self.assertEqual(calls, [])

    def test_inverse_future_contract_has_versioned_settlement_convention_field(self):
        names = {field.name for field in fields(InstrumentVersion)}
        self.assertIn(
            "settlement_convention",
            names,
            "INVERSE settlement quantum/rounding must be immutable InstrumentVersion authority",
        )

    def test_inverse_future_cannot_exist_without_settlement_convention(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "settlement convention|settlement_convention",
        ):
            inverse_future()

    def test_inverse_booking_does_not_accept_free_caller_quantization_policy(self):
        parameters = signature(settle_and_book_inverse_variation_margin).parameters
        self.assertNotIn(
            "settlement_quantum",
            parameters,
            "terminal INVERSE quantization must come from canonical InstrumentVersion",
        )
        self.assertNotIn(
            "rounding",
            parameters,
            "terminal INVERSE rounding must come from canonical InstrumentVersion",
        )


if __name__ == "__main__":
    unittest.main()
