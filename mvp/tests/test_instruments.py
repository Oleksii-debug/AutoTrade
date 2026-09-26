from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from research.autotrade_research.artifacts.store import ArtifactStore

from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentConflict,
    InstrumentNotFound,
    InstrumentRegistry,
    InstrumentRegistryError,
    InstrumentVersion,
    OffsetTransition,
    TradingCalendar,
    WeeklySession,
)


A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"


def when(month: int, day: int = 1, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def spot(
    *,
    instrument_id: str = A,
    version: int = 1,
    symbol: str = "ABC",
    effective_from: datetime = when(1),
    status: str = "ACTIVE",
    calendar_id: str = "CONTINUOUS_24_7",
    timezone_id: str = "UTC",
    metadata_evidence=(),
    venue_id: str = "simulated-venue",
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=instrument_id,
        version=version,
        provider_id="simulated",
        venue_id=venue_id,
        provider_symbol=symbol,
        asset_class="CASH_EQUITY",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="ABC",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("0.001"),
        minimum_quantity=Decimal("0.001"),
        maximum_quantity=Decimal("10"),
        calendar_id=calendar_id,
        timezone_id=timezone_id,
        effective_from=effective_from,
        status=status,
        metadata_evidence=metadata_evidence,
    )


def option(
    *,
    version: int,
    effective_from: datetime,
    deliverable_quantity: str,
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=A,
        version=version,
        provider_id="simulated",
        venue_id="options",
        provider_symbol="ABC-202612-C100",
        asset_class="OPTION",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="contract",
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=effective_from,
        status="ACTIVE",
        payoff="OPTION",
        underlying_id=f"{B}@1",
        expiry=when(12, 18, 21),
        settlement_method="PHYSICAL",
        margin_model_id="option-margin-v1",
        strike=Decimal("100"),
        option_right="CALL",
        exercise_style="AMERICAN",
        deliverable=(DeliverableLeg("ABC", Decimal(deliverable_quantity)),),
    )


def publish_metadata_evidence(
    artifact_store: ArtifactStore,
    observed_at: datetime,
    *,
    artifact_id: str = B,
    committed_at: datetime | None = None,
) -> dict[str, str]:
    committed = committed_at or observed_at
    with patch(
        "research.autotrade_research.artifacts.store.datetime"
    ) as artifact_datetime:
        artifact_datetime.now.return_value = committed
        manifest = artifact_store.publish_bytes(
            artifact_id=artifact_id,
            data=f"instrument-metadata:{artifact_id}".encode("utf-8"),
            media_type="application/vnd.autotrade.instrument-metadata+json",
            rights={"storage": True, "export": False},
            source_refs=["provider:instrument-metadata"],
            metadata={"kind": "instrument-metadata"},
        )
    return {
        "artifact_id": artifact_id,
        "sha256": manifest["sha256"],
        "observed_at": observed_at.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "rights_id": "provider-metadata-rights",
    }


class InstrumentRegistryTests(unittest.TestCase):
    def test_symbol_rename_preserves_identity_and_history(self):
        registry = InstrumentRegistry()
        registry.add(spot(symbol="OLD"))
        registry.add(spot(version=2, symbol="NEW", effective_from=when(6)))

        before = registry.resolve("simulated", "simulated-venue", "OLD", when(5))
        after = registry.resolve("simulated", "simulated-venue", "NEW", when(7))

        self.assertEqual(before.instrument_id, A)
        self.assertEqual(after.instrument_id, A)
        self.assertEqual(before.version, 1)
        self.assertEqual(after.version, 2)
        with self.assertRaises(InstrumentNotFound):
            registry.resolve("simulated", "simulated-venue", "OLD", when(7))

    def test_causal_lookup_does_not_let_late_metadata_retroactively_truncate_history(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            old_evidence = publish_metadata_evidence(
                artifact_store,
                when(1),
                artifact_id=B,
            )
            new_evidence = publish_metadata_evidence(
                artifact_store,
                when(8),
                artifact_id=C,
            )
            registry = InstrumentRegistry()
            registry.add(
                spot(
                    symbol="OLD",
                    metadata_evidence=(old_evidence,),
                )
            )
            registry.add(
                spot(
                    version=2,
                    symbol="NEW",
                    effective_from=when(6),
                    metadata_evidence=(new_evidence,),
                )
            )

            # Current operational truth knows v2 and therefore sees the rename.
            self.assertEqual(registry.at(A, when(7)).version, 2)

            # A replay at July 1 could not have known metadata committed in August.
            causal = registry.at_known(
                A,
                when(7),
                knowledge_cutoff=when(7),
                artifact_store=artifact_store,
            )
            self.assertEqual(causal.version, 1)
            self.assertEqual(causal.provider_symbol, "OLD")
            self.assertEqual(
                registry.resolve_known(
                    "simulated",
                    "simulated-venue",
                    "OLD",
                    when(7),
                    knowledge_cutoff=when(7),
                    artifact_store=artifact_store,
                ).version,
                1,
            )
            with self.assertRaises(InstrumentNotFound):
                registry.resolve_known(
                    "simulated",
                    "simulated-venue",
                    "NEW",
                    when(7),
                    knowledge_cutoff=when(7),
                    artifact_store=artifact_store,
                )

            # Once immutable evidence exists by the cutoff, the same instant resolves to v2.
            self.assertEqual(
                registry.at_known(
                    A,
                    when(7),
                    knowledge_cutoff=when(8),
                    artifact_store=artifact_store,
                ).version,
                2,
            )

    def test_causal_lookup_requires_resolvable_immutable_metadata_evidence(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            registry = InstrumentRegistry()
            registry.add(
                spot(
                    metadata_evidence=(
                        {
                            "artifact_id": B,
                            "sha256": "sha256:" + "a" * 64,
                            "observed_at": "2026-01-01T00:00:00Z",
                            "rights_id": "provider-metadata-rights",
                        },
                    ),
                )
            )
            with self.assertRaisesRegex(
                InstrumentRegistryError,
                "cannot be integrity verified",
            ):
                registry.at_known(
                    A,
                    when(1),
                    knowledge_cutoff=when(2),
                    artifact_store=artifact_store,
                )

    def test_causal_lookup_uses_immutable_commit_time_not_claimed_observation_only(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            evidence = publish_metadata_evidence(
                artifact_store,
                when(1),
                artifact_id=B,
                committed_at=when(8),
            )
            registry = InstrumentRegistry()
            registry.add(spot(metadata_evidence=(evidence,)))

            with self.assertRaises(InstrumentNotFound):
                registry.at_known(
                    A,
                    when(2),
                    knowledge_cutoff=when(7),
                    artifact_store=artifact_store,
                )
            self.assertEqual(
                registry.at_known(
                    A,
                    when(2),
                    knowledge_cutoff=when(8),
                    artifact_store=artifact_store,
                ).version,
                1,
            )

    def test_causal_lookup_requires_metadata_evidence_and_no_future_effective_query(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            registry = InstrumentRegistry()
            registry.add(spot())
            with self.assertRaisesRegex(InstrumentNotFound, "causally known"):
                registry.at_known(
                    A,
                    when(1),
                    knowledge_cutoff=when(2),
                    artifact_store=artifact_store,
                )
            with self.assertRaisesRegex(InstrumentRegistryError, "later than causal"):
                registry.at_known(
                    A,
                    when(3),
                    knowledge_cutoff=when(2),
                    artifact_store=artifact_store,
                )

    def test_all_metadata_evidence_must_be_committed_before_causal_version_is_visible(self):
        with TemporaryDirectory() as directory:
            artifact_store = ArtifactStore(Path(directory) / "artifacts")
            first = publish_metadata_evidence(
                artifact_store,
                when(1),
                artifact_id=B,
            )
            second = publish_metadata_evidence(
                artifact_store,
                when(3),
                artifact_id=C,
            )
            registry = InstrumentRegistry()
            registry.add(
                spot(
                    metadata_evidence=(first, second),
                )
            )
            with self.assertRaises(InstrumentNotFound):
                registry.at_known(
                    A,
                    when(2),
                    knowledge_cutoff=when(2),
                    artifact_store=artifact_store,
                )
            self.assertEqual(
                registry.at_known(
                    A,
                    when(2),
                    knowledge_cutoff=when(3),
                    artifact_store=artifact_store,
                ).version,
                1,
            )

    def test_uuid_identity_aliases_are_canonicalized_before_registry_use(self):
        braced = spot(instrument_id="{" + A + "}")
        uppercase = spot(instrument_id=A.upper())
        self.assertEqual(braced.instrument_id, A)
        self.assertEqual(uppercase.instrument_id, A)

        registry = InstrumentRegistry()
        registry.add(braced)
        # The alternate UUID spelling is the same immutable identity/version,
        # not a second instrument history.
        registry.add(uppercase)
        self.assertEqual(registry.exact(f"{A}@1").instrument_id, A)
        self.assertEqual(len(registry.versions(A)), 1)

    def test_retired_symbol_can_be_reused_only_after_nonoverlap(self):
        registry = InstrumentRegistry()
        registry.add(spot(symbol="OLD"))
        registry.add(spot(version=2, symbol="NEW", effective_from=when(6)))
        registry.add(
            spot(
                instrument_id=B,
                symbol="OLD",
                effective_from=when(6),
            )
        )
        resolved = registry.resolve("simulated", "simulated-venue", "OLD", when(7))
        self.assertEqual(resolved.instrument_id, B)

    def test_overlapping_symbol_collision_is_rejected(self):
        registry = InstrumentRegistry()
        registry.add(spot(symbol="ABC"))
        with self.assertRaisesRegex(InstrumentConflict, "overlapping"):
            registry.add(
                spot(
                    instrument_id=B,
                    symbol="ABC",
                    effective_from=when(2),
                )
            )

    def test_delisted_version_blocks_new_trading_without_erasing_history(self):
        registry = InstrumentRegistry()
        registry.add(spot())
        registry.add(
            spot(
                version=2,
                effective_from=when(6),
                status="DELISTED",
            )
        )

        self.assertEqual(registry.require_tradable(A, when(5)).version, 1)
        self.assertEqual(registry.at(A, when(7)).status, "DELISTED")
        with self.assertRaisesRegex(InstrumentRegistryError, "DELISTED"):
            registry.require_tradable(A, when(7))

    def test_exact_price_and_quantity_precision_bounds(self):
        instrument = spot()
        self.assertEqual(instrument.validate_price("100.01"), Decimal("100.01"))
        self.assertEqual(instrument.validate_quantity("0.001"), Decimal("0.001"))
        self.assertEqual(instrument.validate_quantity("10"), Decimal("10"))

        with self.assertRaisesRegex(InstrumentRegistryError, "price_tick"):
            instrument.validate_price("100.005")
        with self.assertRaisesRegex(InstrumentRegistryError, "minimum_quantity"):
            instrument.validate_quantity("0.0005")
        with self.assertRaisesRegex(InstrumentRegistryError, "maximum_quantity"):
            instrument.validate_quantity("10.001")
        with self.assertRaisesRegex(InstrumentRegistryError, "exact decimal"):
            instrument.validate_price(100.1)

    def test_calendar_uses_explicit_dst_transition_evidence(self):
        sessions = tuple(
            WeeklySession(day, 9 * 60 + 30, 16 * 60)
            for day in range(5)
        )
        calendar = TradingCalendar(
            calendar_id="NY_REGULAR_2026",
            timezone_id="America/New_York",
            sessions=sessions,
            transitions=(
                OffsetTransition(datetime(2025, 11, 2, 6, tzinfo=timezone.utc), -300),
                OffsetTransition(datetime(2026, 3, 8, 7, tzinfo=timezone.utc), -240),
                OffsetTransition(datetime(2026, 11, 1, 6, tzinfo=timezone.utc), -300),
            ),
        )
        registry = InstrumentRegistry(calendars=(calendar,))
        registry.add(
            spot(
                calendar_id="NY_REGULAR_2026",
                timezone_id="America/New_York",
            )
        )

        self.assertTrue(calendar.is_open(when(1, 5, 14, 30)))
        self.assertFalse(calendar.is_open(when(1, 5, 13, 30)))
        self.assertTrue(calendar.is_open(when(7, 6, 13, 30)))
        self.assertEqual(registry.require_tradable(A, when(7, 6, 13, 30)).version, 1)

    def test_option_deliverable_adjustment_requires_new_version(self):
        registry = InstrumentRegistry()
        registry.add(option(version=1, effective_from=when(1), deliverable_quantity="100"))
        registry.add(option(version=2, effective_from=when(6), deliverable_quantity="150"))

        old = registry.at(A, when(5))
        adjusted = registry.at(A, when(7))

        self.assertEqual(old.deliverable[0].quantity, Decimal("100"))
        self.assertEqual(adjusted.deliverable[0].quantity, Decimal("150"))
        self.assertEqual(old.instrument_id, adjusted.instrument_id)
        self.assertEqual(old.version, 1)
        self.assertEqual(adjusted.version, 2)

    def test_contract_projection_uses_schema_safe_string_numbers_and_utc(self):
        instrument = InstrumentVersion(
            instrument_id=A,
            version=1,
            provider_id="simulated",
            venue_id="simulated-venue",
            provider_symbol="BTC-USD",
            asset_class="CRYPTO_SPOT",
            base_currency="BTC",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="BTC",
            contract_multiplier="1",
            price_tick="0.01",
            quantity_step="0.0001",
            minimum_quantity="0.0001",
            minimum_notional_amount="10",
            minimum_notional_currency="USD",
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=when(1),
        )
        projected = instrument.to_contract_dict()
        self.assertEqual(projected["version"], "1")
        self.assertEqual(projected["price_tick"], "0.01")
        self.assertEqual(projected["minimum_notional"], {"amount": "10", "currency": "USD"})
        self.assertEqual(projected["effective_from"], "2026-01-01T00:00:00Z")

        root = Path(__file__).resolve().parents[2]
        schema_dir = root / "contracts" / "jsonschema"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in schema_dir.glob("*.json")
        }
        registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()]
        )
        market = schemas["market.schema.json"]
        Draft202012Validator(
            {"$ref": f"{market['$id']}#/$defs/InstrumentVersion"},
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(projected)

    def test_metadata_evidence_is_contract_shaped_and_immutable(self):
        evidence = {
            "artifact_id": B,
            "sha256": "sha256:" + "a" * 64,
            "observed_at": "2026-01-01T00:00:00Z",
            "source_uri": "https://example.test/instrument",
            "rights_id": "provider-metadata-rights",
        }
        instrument = spot(metadata_evidence=(evidence,))
        self.assertEqual(instrument.metadata_evidence[0]["artifact_id"], B)
        with self.assertRaises(TypeError):
            instrument.metadata_evidence[0]["rights_id"] = "changed"
        with self.assertRaisesRegex(InstrumentRegistryError, "unknown fields"):
            spot(metadata_evidence=({**evidence, "unexpected": "x"},))
        with self.assertRaisesRegex(InstrumentRegistryError, "sha256"):
            spot(metadata_evidence=({**evidence, "sha256": "bad"},))
        with self.assertRaisesRegex(InstrumentRegistryError, "observed_at"):
            spot(metadata_evidence=({**evidence, "observed_at": "2026-01-01T00:00:00+00:00"},))

    def test_metadata_artifact_uuid_alias_is_canonicalized(self):
        evidence = {
            "artifact_id": "{" + B + "}",
            "sha256": "sha256:" + "a" * 64,
            "observed_at": "2026-01-01T00:00:00Z",
        }
        instrument = spot(metadata_evidence=(evidence,))
        self.assertEqual(instrument.metadata_evidence[0]["artifact_id"], B)

    def test_future_cannot_use_option_payoff(self):
        with self.assertRaisesRegex(
            InstrumentRegistryError,
            "future/perpetual payoff must be LINEAR or INVERSE",
        ):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="futures",
                provider_symbol="ABC-FUT",
                asset_class="FUTURE",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="OPTION",
                underlying_id=f"{B}@1",
                expiry=when(12),
                settlement_method="CASH",
                margin_model_id="future-margin-v1",
            )

    def test_perpetual_requires_funding_and_cannot_invent_expiry(self):
        with self.assertRaisesRegex(InstrumentRegistryError, "funding_schedule"):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="perpetuals",
                provider_symbol="ABC-PERP",
                asset_class="PERPETUAL",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="LINEAR",
                underlying_id=f"{B}@1",
                settlement_method="CASH",
                margin_model_id="perp-margin-v1",
            )

        with self.assertRaisesRegex(InstrumentRegistryError, "must not invent an expiry"):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="perpetuals",
                provider_symbol="ABC-PERP",
                asset_class="PERPETUAL",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="LINEAR",
                underlying_id=f"{B}@1",
                settlement_method="CASH",
                funding_schedule={"interval": "8h", "source": "provider"},
                margin_model_id="perp-margin-v1",
                expiry=when(12),
            )

        perpetual = InstrumentVersion(
            instrument_id=A,
            version=1,
            provider_id="simulated",
            venue_id="perpetuals",
            provider_symbol="ABC-PERP",
            asset_class="PERPETUAL",
            base_currency="ABC",
            quote_currency="USD",
            settlement_currency="USD",
            quantity_unit="contract",
            contract_multiplier="1",
            price_tick="0.01",
            quantity_step="1",
            minimum_quantity="1",
            calendar_id="CONTINUOUS_24_7",
            timezone_id="UTC",
            effective_from=when(1),
            payoff="LINEAR",
            underlying_id=f"{B}@1",
            settlement_method="CASH",
            funding_schedule={
                "interval": "8h",
                "source": {"provider": "simulated", "ids": ["primary"]},
            },
            margin_model_id="perp-margin-v1",
        )
        self.assertEqual(
            perpetual.to_contract_dict()["funding_schedule"],
            {
                "interval": "8h",
                "source": {"provider": "simulated", "ids": ["primary"]},
            },
        )
        with self.assertRaises(TypeError):
            perpetual.funding_schedule["interval"] = "1h"
        with self.assertRaises(TypeError):
            perpetual.funding_schedule["source"]["provider"] = "changed"
        with self.assertRaises(TypeError):
            perpetual.funding_schedule["source"]["ids"][0] = "changed"

    def test_price_bands_are_exact_enforced_and_schema_shaped(self):
        instrument = InstrumentVersion(
            **{
                **spot().__dict__,
                "price_band_low": Decimal("90"),
                "price_band_high": Decimal("110"),
            }
        )
        self.assertEqual(instrument.validate_price("90"), Decimal("90"))
        self.assertEqual(instrument.validate_price("110"), Decimal("110"))
        with self.assertRaisesRegex(InstrumentRegistryError, "below price_band_low"):
            instrument.validate_price("89.99")
        with self.assertRaisesRegex(InstrumentRegistryError, "above price_band_high"):
            instrument.validate_price("110.01")
        projected = instrument.to_contract_dict()
        self.assertEqual(projected["price_bands"], {"low": "90", "high": "110"})

        root = Path(__file__).resolve().parents[2]
        schema_dir = root / "contracts" / "jsonschema"
        schemas = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in schema_dir.glob("*.json")
        }
        registry = Registry().with_resources(
            [(schema["$id"], Resource.from_contents(schema)) for schema in schemas.values()]
        )
        market = schemas["market.schema.json"]
        Draft202012Validator(
            {"$ref": f"{market['$id']}#/$defs/InstrumentVersion"},
            registry=registry,
            format_checker=FormatChecker(),
        ).validate(projected)

    def test_derivative_underlying_uses_exact_versioned_identity(self):
        with self.assertRaisesRegex(InstrumentRegistryError, "instrument_id@version"):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="futures",
                provider_symbol="ABC-FUT",
                asset_class="FUTURE",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="LINEAR",
                underlying_id="ABC",
                expiry=when(12),
                settlement_method="CASH",
                margin_model_id="future-margin-v1",
            )

        with self.assertRaisesRegex(InstrumentRegistryError, "instrument_id@version"):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="futures",
                provider_symbol="ABC-FUT",
                asset_class="FUTURE",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="LINEAR",
                underlying_id=f"{B}@01",
                expiry=when(12),
                settlement_method="CASH",
                margin_model_id="future-margin-v1",
            )

    def test_exact_underlying_reference_is_unambiguous_across_venues(self):
        registry = InstrumentRegistry()
        registry.add(spot(instrument_id=B, symbol="ABC", venue_id="venue-one"))
        registry.add(spot(instrument_id=C, symbol="ABC", venue_id="venue-two"))

        first = registry.exact(f"{B}@1")
        second = registry.exact(f"{C}@1")

        self.assertEqual(first.instrument_id, B)
        self.assertEqual(second.instrument_id, C)
        self.assertNotEqual(first.venue_id, second.venue_id)
        with self.assertRaisesRegex(InstrumentNotFound, "instrument_version"):
            registry.exact(f"{B}@2")

    def test_non_derivative_rejects_all_derivative_only_fields(self):
        with self.assertRaisesRegex(InstrumentRegistryError, "derivative fields"):
            InstrumentVersion(
                **{
                    **spot().__dict__,
                    "settlement_method": "CASH",
                }
            )
        with self.assertRaisesRegex(InstrumentRegistryError, "derivative fields"):
            InstrumentVersion(
                **{
                    **spot().__dict__,
                    "margin_model_id": "wrong",
                }
            )

    def test_invalid_version_calendar_and_derivative_shape_fail_closed(self):
        registry = InstrumentRegistry()
        with self.assertRaisesRegex(InstrumentConflict, "first instrument version"):
            registry.add(spot(version=2))
        with self.assertRaisesRegex(InstrumentRegistryError, "calendar_id is unknown"):
            registry.add(spot(calendar_id="MISSING"))
        with self.assertRaisesRegex(InstrumentRegistryError, "dated derivative requires expiry"):
            InstrumentVersion(
                instrument_id=A,
                version=1,
                provider_id="simulated",
                venue_id="futures",
                provider_symbol="ABC-FUT",
                asset_class="FUTURE",
                base_currency="ABC",
                quote_currency="USD",
                settlement_currency="USD",
                quantity_unit="contract",
                contract_multiplier="1",
                price_tick="0.01",
                quantity_step="1",
                minimum_quantity="1",
                calendar_id="CONTINUOUS_24_7",
                timezone_id="UTC",
                effective_from=when(1),
                payoff="LINEAR",
                underlying_id=f"{B}@1",
                settlement_method="CASH",
                margin_model_id="future-margin-v1",
            )


if __name__ == "__main__":
    unittest.main()
