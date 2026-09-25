from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import book_equity_fill
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.perpetual_funding import (
    DurablePerpetualFundingAuthority,
    PerpetualFundingConflict,
    PerpetualFundingError,
    PerpetualFundingObservation,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


FUNDING_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNDERLYING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ENDPOINT = "/fapi/v1/income"
READ_NOW = datetime(2026, 9, 25, 10, 0, tzinfo=timezone.utc)
ARTIFACT_IDS = {
    "DOCUMENTED": "11111111-1111-4111-8111-111111111111",
    "API": "22222222-2222-4222-8222-222222222222",
    "ACCOUNT": "33333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "44444444-4444-4444-8444-444444444444",
}


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def funding_capability():
    observed = READ_NOW - timedelta(minutes=1)
    expires = READ_NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id="acct-1",
            entity_id="entity-1",
            environment="PAPER",
            instrument_version=f"{FUNDING_ID}@1",
            observed_at=observed,
            expires_at=expires,
            supported_order_types=frozenset({"LIMIT"}),
            time_in_force=frozenset({"GTC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-test-v1",
            data_entitlements=frozenset({"ACCOUNT"}),
            evidence_ref={
                "artifact_id": ARTIFACT_IDS[source],
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed.isoformat().replace("+00:00", "Z"),
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        claims=claims,
        observed_at=READ_NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def perpetual_version(
    *,
    multiplier="0.001",
    provider_id="BINANCE",
    symbol="BTCUSDT",
    payoff="LINEAR",
    settlement_currency="USDT",
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=FUNDING_ID,
        version=1,
        provider_id=provider_id,
        venue_id="BINANCE_FUTURES",
        provider_symbol=symbol,
        asset_class="PERPETUAL",
        base_currency="BTC",
        quote_currency="USDT",
        settlement_currency=settlement_currency,
        quantity_unit="contract",
        contract_multiplier=Decimal(multiplier),
        price_tick=Decimal("0.1"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        payoff=payoff,
        underlying_id=f"{UNDERLYING_ID}@1",
        settlement_method="CASH",
        funding_schedule={"interval_hours": 8},
        margin_model_id="binance-usdm-v1",
    )


def sealed_funding(
    *,
    external_event_id="funding-1",
    revision="1",
    rate="0.001",
    contracts="2",
    observed_offset=1,
    corrects=None,
    instrument_id="BTCUSDT",
):
    binding = prepare_authenticated_read_query(
        capability=funding_capability(),
        surface=Surface.ACTIVITIES,
        endpoint=ENDPOINT,
        query={"incomeType": "FUNDING_FEE", "symbol": "BTCUSDT"},
        at=READ_NOW,
        permission_scope="ORDER.READ",
    )
    payload = {
        "external_event_id": external_event_id,
        "provider_revision": revision,
        "funding_period_id": "2026-09-25T10:00:00Z",
        "effective_at": "2026-09-25T10:00:00Z",
        "instrument_id": instrument_id,
        "signed_contracts": contracts,
        "funding_rate": rate,
        "mark_price": "100000",
        "index_price": "100000",
        "price_basis": "MARK",
        "positive_rate_effect": "LONG_PAYS",
        "corrects_external_event_id": corrects,
    }
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        observed_at=READ_NOW + timedelta(seconds=observed_offset),
    )


def normalize(source):
    payload = source.payload
    return PerpetualFundingObservation(
        provider_id=source.provider_id,
        account_id=source.account_id,
        environment=source.environment,
        instrument_id=payload["instrument_id"],
        instrument_version=source.query_binding.instrument_version,
        external_event_id=payload["external_event_id"],
        provider_revision=payload["provider_revision"],
        funding_period_id=payload["funding_period_id"],
        effective_at=_instant(payload["effective_at"]),
        observed_at=_instant(source.observed_at),
        signed_contracts=payload["signed_contracts"],
        funding_rate=payload["funding_rate"],
        mark_price=payload["mark_price"],
        index_price=payload["index_price"],
        price_basis=payload["price_basis"],
        positive_rate_effect=payload["positive_rate_effect"],
        raw_evidence_digest=source.response_sha256,
        corrects_external_event_id=payload["corrects_external_event_id"],
    )


def seed_position(
    book,
    *,
    transaction_id="position-1",
    contracts="2",
    effective_at="2026-09-25T09:00:00Z",
    observed_at="2026-09-25T09:01:00Z",
):
    quantity = Decimal(contracts)
    transaction = book_equity_fill(
        transaction_id=transaction_id,
        cause_event_id="provider-fill:" + transaction_id,
        instrument="BTCUSDT",
        settlement_currency="USDT",
        side="BUY" if quantity > 0 else "SELL",
        quantity=abs(quantity),
        price="100000",
        economic_effective_at=effective_at,
        economic_order_key="provider:BINANCE:execution:" + transaction_id,
        observed_at=observed_at,
    )
    book.append(transaction)


class DurablePerpetualFundingAuthorityTests(unittest.TestCase):
    def authority(self, store, evidence, *, registry=None, seed=True):
        book = DurableProviderEconomicBook(
            store,
            provider_id="BINANCE",
            account_id="acct-1",
            environment="PAPER",
        )
        if seed and not book.transactions:
            seed_position(book)
        resolver = {item.evidence_ref: item for item in evidence}
        authority = DurablePerpetualFundingAuthority(
            store,
            economic_book=book,
            instrument_registry=registry
            or InstrumentRegistry(versions=(perpetual_version(),)),
            evidence_resolver=resolver.__getitem__,
            funding_endpoints=frozenset({ENDPOINT}),
            permission_scope="ORDER.READ",
        )
        return authority, book

    def test_arbitrary_funding_normalizer_cannot_be_injected(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableProviderEconomicBook(
                store,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
            )
            seed_position(book)
            resolver = {evidence.evidence_ref: evidence}

            def forged_normalizer(source):
                valid = normalize(source)
                return PerpetualFundingObservation(
                    provider_id=valid.provider_id,
                    account_id=valid.account_id,
                    environment=valid.environment,
                    instrument_id=valid.instrument_id,
                    instrument_version=valid.instrument_version,
                    external_event_id="forged-event",
                    provider_revision="forged-revision",
                    funding_period_id="forged-period",
                    effective_at=valid.effective_at + timedelta(hours=8),
                    observed_at=valid.observed_at,
                    signed_contracts=Decimal("999"),
                    funding_rate=Decimal("0.99"),
                    mark_price=Decimal("1"),
                    index_price=Decimal("1"),
                    price_basis="INDEX",
                    positive_rate_effect="LONG_RECEIVES",
                    raw_evidence_digest=valid.raw_evidence_digest,
                )

            with self.assertRaises(TypeError):
                DurablePerpetualFundingAuthority(
                    store,
                    economic_book=book,
                    instrument_registry=InstrumentRegistry(
                        versions=(perpetual_version(),)
                    ),
                    evidence_resolver=resolver.__getitem__,
                    normalizer=forged_normalizer,
                    funding_endpoints=frozenset({ENDPOINT}),
                    permission_scope="ORDER.READ",
                )

            self.assertEqual(
                store.load_events_by_aggregate_type("perpetual_funding"),
                [],
            )
            self.assertEqual(len(book.transactions), 1)

    def test_sealed_funding_uses_canonical_registry_and_durable_position_cut(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, book = self.authority(store, [evidence])

            first = authority.apply(evidence.evidence_ref)
            self.assertTrue(first.inserted)
            self.assertEqual(first.cashflow, Decimal("-0.200000"))
            self.assertEqual(first.currency, "USDT")
            self.assertEqual(book.cash("USDT"), Decimal("-200000.200000"))

            events = store.load_events(
                "perpetual_funding", authority.aggregate_id
            )
            self.assertEqual(len(events), 1)
            payload = events[0]["payload"]
            self.assertEqual(payload["position_cut"]["position"], "2")
            self.assertEqual(
                payload["position_cut"]["contributing_transaction_ids"],
                ["position-1"],
            )
            self.assertTrue(
                payload["position_cut"]["digest"].startswith("sha256:")
            )
            self.assertTrue(
                payload["instrument_contract_digest"].startswith("sha256:")
            )

            restarted, restarted_book = self.authority(
                JournalStore(path), [evidence], seed=False
            )
            replay = restarted.apply(evidence.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(replay.active_transaction_id, first.active_transaction_id)
            self.assertEqual(
                restarted_book.cash("USDT"), Decimal("-200000.200000")
            )
            self.assertEqual(
                len(
                    JournalStore(path).load_events(
                        "perpetual_funding", restarted.aggregate_id
                    )
                ),
                1,
            )

    def test_missing_canonical_position_fails_before_funding_mutation(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False
            )
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "canonical position"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.cash("USDT"), Decimal("0"))
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_position_effective_after_funding_cut_cannot_authorize_booking(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False
            )
            seed_position(
                book,
                effective_at="2026-09-25T10:00:01Z",
                observed_at="2026-09-25T10:00:01Z",
            )
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "canonical position"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_position_observed_after_provider_fact_cannot_leak_into_cut(self):
        evidence = sealed_funding(observed_offset=1)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False
            )
            seed_position(
                book,
                effective_at="2026-09-25T09:00:00Z",
                observed_at="2026-09-25T10:00:02Z",
            )
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "canonical position"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_position_history_without_causal_timestamps_fails_closed(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False
            )
            transaction = book_equity_fill(
                transaction_id="legacy-position",
                cause_event_id="legacy-position",
                instrument="BTCUSDT",
                settlement_currency="USDT",
                side="BUY",
                quantity="2",
                price="100000",
            )
            book.append(transaction)
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "causal economic ordering"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_provider_symbol_must_match_exact_registry_version(self):
        evidence = sealed_funding(instrument_id="ETHUSDT")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, _book = self.authority(store, [evidence])
            with self.assertRaisesRegex(
                PerpetualFundingError, "canonical instrument version"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_registry_contract_digest_blocks_silent_metadata_drift_on_replay(self):
        evidence = sealed_funding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, _book = self.authority(store, [evidence])
            authority.apply(evidence.evidence_ref)

            drifted = InstrumentRegistry(
                versions=(perpetual_version(multiplier="0.002"),)
            )
            restarted, _restarted_book = self.authority(
                JournalStore(path),
                [evidence],
                registry=drifted,
                seed=False,
            )
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "changed evidence"
            ):
                restarted.apply(evidence.evidence_ref)

    def test_inverse_contract_remains_fail_closed_without_quantization_policy(self):
        evidence = sealed_funding()
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="1",
                ),
            )
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, _book = self.authority(
                store, [evidence], registry=inverse
            )
            with self.assertRaisesRegex(
                PerpetualFundingError, "quantization policy"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_provider_correction_atomically_reverses_and_replaces_funding(self):
        original = sealed_funding()
        correction = sealed_funding(
            external_event_id="funding-2",
            revision="2",
            rate="0.002",
            observed_offset=2,
            corrects="funding-1",
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, book = self.authority(
                store, [original, correction]
            )
            first = authority.apply(original.evidence_ref)
            corrected = authority.apply(correction.evidence_ref)

            self.assertTrue(corrected.inserted)
            self.assertEqual(corrected.cashflow, Decimal("-0.400000"))
            self.assertIsNotNone(corrected.reversal_transaction_id)
            self.assertNotEqual(
                corrected.active_transaction_id, first.active_transaction_id
            )
            # The seed fill consumes 200000 USDT; funding net is now -0.4.
            self.assertEqual(book.cash("USDT"), Decimal("-200000.400000"))

            restarted, restarted_book = self.authority(
                JournalStore(path),
                [original, correction],
                seed=False,
            )
            replay = restarted.apply(correction.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(
                replay.reversal_transaction_id,
                corrected.reversal_transaction_id,
            )
            self.assertEqual(
                restarted_book.cash("USDT"), Decimal("-200000.400000")
            )


if __name__ == "__main__":
    unittest.main()
