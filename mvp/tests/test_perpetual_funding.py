from dataclasses import replace
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
    FundingEvidenceBundle,
    PerpetualFundingConflict,
    PerpetualFundingError,
    PerpetualFundingObservation,
    _source_event_identity,
)
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


FUNDING_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
UNDERLYING_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ENDPOINT = "/fapi/v1/income"
RATE_ENDPOINT = "/fapi/v1/fundingRate"
PRICE_ENDPOINT = "/fapi/v1/premiumIndex"
CUT_ENDPOINT = "/fapi/v1/positionRisk"
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


def funding_capability(environment="SIMULATION"):
    observed = READ_NOW - timedelta(minutes=1)
    expires = READ_NOW + timedelta(minutes=10)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id="acct-1",
            entity_id="entity-1",
            environment=environment,
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
    funding_schedule=None,
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
        funding_schedule={
            "interval_hours": 8,
            "price_basis": "MARK",
            "positive_rate_effect": "LONG_PAYS",
            **({} if funding_schedule is None else funding_schedule),
        },
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
    funding_period_id="2026-09-25T10:00:00Z",
    price_reference_at="2026-09-25T10:00:00Z",
    collateral_currency="USDT",
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
        "funding_period_id": funding_period_id,
        "effective_at": "2026-09-25T10:00:00Z",
        "price_reference_at": price_reference_at,
        "instrument_id": instrument_id,
        "signed_contracts": contracts,
        "funding_rate": rate,
        "mark_price": "100000",
        "index_price": "100000",
        "price_basis": "MARK",
        "collateral_currency": collateral_currency,
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


def sealed_composite_funding(
    *,
    tran_id="9001",
    income="-0.200000",
    rate="0.001",
    position="2",
    pre_cut=(),
    post_cut=(),
    corrects=None,
    environment="PAPER",
    income_time=1790330400000,
    cut_observed_offset=4,
):
    capability = funding_capability(environment=environment)

    def observe(endpoint, query, payload, offset):
        binding = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.ACTIVITIES,
            endpoint=endpoint,
            query=query,
            at=READ_NOW,
            permission_scope="ORDER.READ",
        )
        return observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            observed_at=READ_NOW + timedelta(seconds=offset),
        )

    income_source = observe(
        ENDPOINT,
        {"incomeType": "FUNDING_FEE", "symbol": "BTCUSDT"},
        {
            "symbol": "BTCUSDT",
            "incomeType": "FUNDING_FEE",
            "income": income,
            "asset": "USDT",
            "info": "",
            "time": income_time,
            "tranId": tran_id,
            "tradeId": "",
        },
        1,
    )
    rate_source = observe(
        RATE_ENDPOINT,
        {"symbol": "BTCUSDT"},
        {
            "symbol": "BTCUSDT",
            "fundingTime": "2026-09-25T10:00:00Z",
            "fundingRate": rate,
        },
        2,
    )
    price_source = observe(
        PRICE_ENDPOINT,
        {"symbol": "BTCUSDT"},
        {
            "symbol": "BTCUSDT",
            "time": "2026-09-25T10:00:00Z",
            "markPrice": "100000",
            "indexPrice": "100000",
        },
        3,
    )
    cut_source = observe(
        CUT_ENDPOINT,
        {"symbol": "BTCUSDT"},
        {
            "symbol": "BTCUSDT",
            "fundingTime": "2026-09-25T10:00:00Z",
            "position": position,
            "preCutTransactionIds": list(pre_cut),
            "postCutTransactionIds": list(post_cut),
        },
        cut_observed_offset,
    )
    return FundingEvidenceBundle(
        income=income_source,
        rate=rate_source,
        prices=price_source,
        cut=cut_source,
        corrects_external_event_id=corrects,
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
        price_reference_at=_instant(payload["price_reference_at"]),
        signed_contracts=payload["signed_contracts"],
        funding_rate=payload["funding_rate"],
        mark_price=payload["mark_price"],
        index_price=payload["index_price"],
        price_basis=payload["price_basis"],
        collateral_currency=payload["collateral_currency"],
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
    def authority(
        self, store, evidence, *, registry=None, seed=True, environment=None
    ):
        if environment is None:
            first = evidence[0]
            environment = (
                first.income.environment
                if isinstance(first, FundingEvidenceBundle)
                else first.environment
            )
        book = DurableProviderEconomicBook(
            store,
            provider_id="BINANCE",
            account_id="acct-1",
            environment=environment,
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
            funding_endpoints=frozenset(
                {ENDPOINT, RATE_ENDPOINT, PRICE_ENDPOINT, CUT_ENDPOINT}
            ),
            permission_scope="ORDER.READ",
            funding_evidence_endpoints={
                "income": frozenset({ENDPOINT}),
                "rate": frozenset({RATE_ENDPOINT}),
                "prices": frozenset({PRICE_ENDPOINT}),
                "cut": frozenset({CUT_ENDPOINT}),
            },
        )
        return authority, book

    def test_provider_event_identity_is_instrument_scoped_by_default(self):
        base = normalize(sealed_funding(external_event_id="provider-row-7"))
        same_instrument_retry = replace(base, provider_revision="2")
        other_instrument = replace(
            base,
            instrument_version="dddddddd-dddd-4ddd-8ddd-dddddddddddd@1",
        )
        other_provider_row = replace(base, external_event_id="provider-row-8")

        self.assertEqual(
            _source_event_identity(base),
            _source_event_identity(same_instrument_retry),
        )
        self.assertNotEqual(
            _source_event_identity(base),
            _source_event_identity(other_instrument),
        )
        self.assertNotEqual(
            _source_event_identity(base),
            _source_event_identity(other_provider_row),
        )

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
                    price_reference_at=valid.price_reference_at,
                    signed_contracts=Decimal("999"),
                    funding_rate=Decimal("0.99"),
                    mark_price=Decimal("1"),
                    index_price=Decimal("1"),
                    price_basis="INDEX",
                    collateral_currency=valid.collateral_currency,
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

    def test_late_price_reference_cannot_be_backdated_to_funding_cut(self):
        evidence = sealed_funding(
            observed_offset=30,
            price_reference_at="2026-09-25T10:00:30Z",
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [evidence])
            before = book.audit_digest()

            with self.assertRaisesRegex(
                PerpetualFundingError,
                "exact funding cut",
            ):
                authority.apply(evidence.evidence_ref)

            self.assertEqual(
                store.load_events_by_aggregate_type("perpetual_funding"),
                [],
            )
            self.assertEqual(book.audit_digest(), before)
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

    def test_provider_reported_funding_asset_must_match_immutable_contract(self):
        evidence = sealed_funding(collateral_currency="USD")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [evidence])
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "immutable contract settlement currency",
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_missing_provider_collateral_currency_fails_closed(self):
        source = sealed_funding()
        payload = dict(source.payload)
        payload.pop("collateral_currency")
        binding = source.query_binding
        malformed = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=json.dumps(
                payload, sort_keys=True, separators=(",", ":")
            ).encode("utf-8"),
            observed_at=_instant(source.observed_at),
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [malformed])
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingError, "shape is not canonical"
            ):
                authority.apply(malformed.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_paper_official_income_row_alone_cannot_book(self):
        composite = sealed_composite_funding()
        income = composite.income
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableProviderEconomicBook(
                store,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
            )
            seed_position(book)
            authority = DurablePerpetualFundingAuthority(
                store,
                economic_book=book,
                instrument_registry=InstrumentRegistry(
                    versions=(perpetual_version(),)
                ),
                evidence_resolver={income.evidence_ref: income}.__getitem__,
                funding_endpoints=frozenset(
                    {ENDPOINT, RATE_ENDPOINT, PRICE_ENDPOINT, CUT_ENDPOINT}
                ),
                permission_scope="ORDER.READ",
            )
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingError,
                "separately authenticated income, rate, price and cut evidence",
            ):
                authority.apply(income.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id), []
            )

    def test_composite_position_cut_cannot_learn_from_later_evidence_sources(self):
        evidence = sealed_composite_funding(cut_observed_offset=1)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False, environment="PAPER"
            )
            seed_position(book)
            seed_position(
                book,
                transaction_id="late-local-knowledge",
                contracts="1",
                effective_at="2026-09-25T09:30:00Z",
                observed_at="2026-09-25T10:00:02Z",
            )
            result = authority.apply(evidence.evidence_ref)
            self.assertTrue(result.inserted)
            self.assertEqual(result.cashflow, Decimal("-0.200000"))
            event = store.load_events(
                "perpetual_funding", authority.aggregate_id
            )[0]
            self.assertEqual(
                event["payload"]["position_cut"]["position"],
                "2",
            )
            self.assertEqual(
                event["payload"]["position_cut"]["evidence_observed_at"],
                "2026-09-25T10:00:01Z",
            )
            self.assertNotIn(
                "late-local-knowledge",
                event["payload"]["position_cut"]["contributing_transaction_ids"],
            )

    def test_composite_official_evidence_books_and_replays_deterministically(self):
        evidence = sealed_composite_funding()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            authority, book = self.authority(
                JournalStore(path), [evidence], environment="PAPER"
            )
            first = authority.apply(evidence.evidence_ref)
            self.assertTrue(first.inserted)
            self.assertEqual(first.cashflow, Decimal("-0.200000"))
            events = authority.store.load_events(
                "perpetual_funding", authority.aggregate_id
            )
            self.assertEqual(len(events), 1)
            payload = events[0]["payload"]
            self.assertIsNotNone(payload["composite_evidence_digest"])
            self.assertEqual(payload["provider_income"], "-0.200000")
            self.assertTrue(payload["position_cut"]["causal_digest"].startswith("sha256:"))

            restarted, restarted_book = self.authority(
                JournalStore(path), [evidence], seed=False, environment="PAPER"
            )
            replay = restarted.apply(evidence.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(replay.active_transaction_id, first.active_transaction_id)
            self.assertEqual(restarted_book.cash("USDT"), book.cash("USDT"))

    def test_composite_income_time_must_match_registered_funding_cut(self):
        evidence = sealed_composite_funding(income_time=1790330400001)
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [evidence], environment="PAPER")
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingError, "income event is not bound"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)

    def test_cut_evidence_rejects_unknown_same_cut_transaction_identity(self):
        evidence = sealed_composite_funding(pre_cut=("ghost-same-cut",))
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [evidence], environment="PAPER")
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "exactly match canonical same-cut"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)

    def test_composite_role_endpoint_policy_fails_closed(self):
        evidence = sealed_composite_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            book = DurableProviderEconomicBook(
                store,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="PAPER",
            )
            seed_position(book)
            authority = DurablePerpetualFundingAuthority(
                store,
                economic_book=book,
                instrument_registry=InstrumentRegistry(
                    versions=(perpetual_version(),)
                ),
                evidence_resolver={evidence.evidence_ref: evidence}.__getitem__,
                funding_endpoints=frozenset(
                    {ENDPOINT, RATE_ENDPOINT, PRICE_ENDPOINT, CUT_ENDPOINT}
                ),
                permission_scope="ORDER.READ",
                funding_evidence_endpoints={
                    "income": frozenset({ENDPOINT}),
                    "rate": frozenset({PRICE_ENDPOINT}),
                    "prices": frozenset({RATE_ENDPOINT}),
                    "cut": frozenset({CUT_ENDPOINT}),
                },
            )
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingError, "rate funding evidence endpoint is not allowed"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)

    def test_composite_correction_is_atomic_restart_safe_and_idempotent(self):
        original = sealed_composite_funding(
            tran_id="9001", income="-0.200000", rate="0.001"
        )
        correction = sealed_composite_funding(
            tran_id="9002",
            income="-0.400000",
            rate="0.002",
            corrects="9001",
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            authority, book = self.authority(
                JournalStore(path),
                [original, correction],
                environment="PAPER",
            )
            first = authority.apply(original.evidence_ref)
            corrected = authority.apply(correction.evidence_ref)
            self.assertTrue(first.inserted)
            self.assertTrue(corrected.inserted)
            self.assertIsNotNone(corrected.reversal_transaction_id)
            self.assertEqual(corrected.cashflow, Decimal("-0.400000"))
            self.assertEqual(book.cash("USDT"), Decimal("-200000.400000"))

            restarted, restarted_book = self.authority(
                JournalStore(path),
                [original, correction],
                seed=False,
                environment="PAPER",
            )
            replay = restarted.apply(correction.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(
                replay.active_transaction_id, corrected.active_transaction_id
            )
            self.assertEqual(
                replay.reversal_transaction_id,
                corrected.reversal_transaction_id,
            )
            self.assertEqual(
                restarted_book.cash("USDT"), Decimal("-200000.400000")
            )

    def test_composite_correction_cannot_reinterpret_same_cut_causality(self):
        original = sealed_composite_funding(
            tran_id="9001",
            position="3",
            income="-0.300000",
            pre_cut=("same-cut",),
        )
        correction = sealed_composite_funding(
            tran_id="9002",
            position="2",
            income="-0.400000",
            rate="0.002",
            post_cut=("same-cut",),
            corrects="9001",
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [original, correction], seed=False, environment="PAPER"
            )
            seed_position(book, contracts="2")
            seed_position(
                book,
                transaction_id="same-cut",
                contracts="1",
                effective_at="2026-09-25T10:00:00Z",
                observed_at="2026-09-25T10:00:01Z",
            )
            authority.apply(original.evidence_ref)
            before = book.audit_digest()
            sequence = store.current_journal_sequence()
            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "cannot reinterpret immutable funding-period position cut",
            ):
                authority.apply(correction.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(store.current_journal_sequence(), sequence)

    def test_composite_provider_income_conflict_rejects_before_mutation(self):
        evidence = sealed_composite_funding(income="-0.19")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [evidence], environment="PAPER")
            before = book.audit_digest()
            sequence = store.current_journal_sequence()
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "does not reconcile"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(store.current_journal_sequence(), sequence)

    def test_same_timestamp_pre_cut_evidence_includes_execution(self):
        evidence = sealed_composite_funding(
            position="3", income="-0.300000", pre_cut=("same-cut",)
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False, environment="PAPER"
            )
            seed_position(book, contracts="2")
            seed_position(
                book,
                transaction_id="same-cut",
                contracts="1",
                effective_at="2026-09-25T10:00:00Z",
                observed_at="2026-09-25T10:00:01Z",
            )
            result = authority.apply(evidence.evidence_ref)
            self.assertEqual(result.cashflow, Decimal("-0.300000"))
            payload = store.load_events(
                "perpetual_funding", authority.aggregate_id
            )[0]["payload"]
            self.assertIn(
                "same-cut",
                payload["position_cut"]["contributing_transaction_ids"],
            )

    def test_same_timestamp_post_cut_evidence_excludes_execution(self):
        evidence = sealed_composite_funding(
            position="2", income="-0.200000", post_cut=("same-cut",)
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], seed=False, environment="PAPER"
            )
            seed_position(book, contracts="2")
            seed_position(
                book,
                transaction_id="same-cut",
                contracts="1",
                effective_at="2026-09-25T10:00:00Z",
                observed_at="2026-09-25T10:00:01Z",
            )
            result = authority.apply(evidence.evidence_ref)
            self.assertEqual(result.cashflow, Decimal("-0.200000"))

    def test_same_timestamp_composite_without_ordering_fails_closed(self):
        evidence = sealed_composite_funding(position="2")
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, book = self.authority(
                store, [evidence], seed=False, environment="PAPER"
            )
            seed_position(book, contracts="2")
            seed_position(
                book,
                transaction_id="same-cut",
                contracts="1",
                effective_at="2026-09-25T10:00:00Z",
                observed_at="2026-09-25T10:00:01Z",
            )
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "ambiguous position ordering"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)

            restarted, restarted_book = self.authority(
                JournalStore(path), [evidence], seed=False, environment="PAPER"
            )
            with self.assertRaisesRegex(
                PerpetualFundingConflict, "ambiguous position ordering"
            ):
                restarted.apply(evidence.evidence_ref)
            self.assertEqual(restarted_book.audit_digest(), before)

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

    def test_inverse_contract_settles_exact_fraction_at_versioned_quantum(self):
        evidence = sealed_funding(collateral_currency="BTC")
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="100",
                    funding_schedule={
                        "interval_hours": 8,
                        "settlement_quantum": "0.00000001",
                        "settlement_rounding": "HALF_EVEN",
                    },
                ),
            )
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, book = self.authority(
                store, [evidence], registry=inverse
            )

            first = authority.apply(evidence.evidence_ref)
            self.assertTrue(first.inserted)
            self.assertEqual(first.currency, "BTC")
            self.assertEqual(first.cashflow, Decimal("-0.000002"))
            self.assertEqual(book.cash("BTC"), Decimal("-0.000002"))

            restarted, restarted_book = self.authority(
                JournalStore(path),
                [evidence],
                registry=inverse,
                seed=False,
            )
            replay = restarted.apply(evidence.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(replay.active_transaction_id, first.active_transaction_id)
            self.assertEqual(replay.cashflow, Decimal("-0.000002"))
            self.assertEqual(restarted_book.cash("BTC"), Decimal("-0.000002"))

    def test_inverse_half_even_quantization_is_deterministic(self):
        evidence = sealed_funding(
            rate="0.00075",
            collateral_currency="BTC",
        )
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="1",
                    funding_schedule={
                        "interval_hours": 8,
                        "settlement_quantum": "0.00000001",
                        "settlement_rounding": "HALF_EVEN",
                    },
                ),
            )
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], registry=inverse
            )
            result = authority.apply(evidence.evidence_ref)
            self.assertEqual(result.cashflow, Decimal("-0.00000002"))
            self.assertEqual(book.cash("BTC"), Decimal("-0.00000002"))

    def test_inverse_contract_rejects_unqualified_rounding_policy_before_mutation(self):
        evidence = sealed_funding(collateral_currency="BTC")
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="1",
                    funding_schedule={
                        "interval_hours": 8,
                        "settlement_quantum": "0.00000001",
                        "settlement_rounding": "UP",
                    },
                ),
            )
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(
                store, [evidence], registry=inverse
            )
            before = book.audit_digest()
            with self.assertRaisesRegex(
                PerpetualFundingError, "HALF_EVEN or DOWN"
            ):
                authority.apply(evidence.evidence_ref)
            self.assertEqual(book.audit_digest(), before)
            self.assertEqual(
                store.load_events("perpetual_funding", authority.aggregate_id),
                [],
            )

    def test_restart_correction_rejects_reused_version_with_changed_contract(self):
        original = sealed_funding(collateral_currency="BTC")
        correction = sealed_funding(
            external_event_id="funding-corrected", revision="2", rate="0.002",
            observed_offset=2, corrects="funding-1", collateral_currency="BTC",
        )

        def registry(quantum="0.00000001", rounding="HALF_EVEN"):
            return InstrumentRegistry(versions=(perpetual_version(
                payoff="INVERSE", settlement_currency="BTC", multiplier="100",
                funding_schedule={"interval_hours": 8, "settlement_quantum": quantum,
                                  "settlement_rounding": rounding},
            ),))

        for changed in (registry("0.0001"), registry(rounding="DOWN")):
            with self.subTest(contract=changed), TemporaryDirectory() as directory:
                path = f"{directory}/journal.sqlite3"
                original_authority, _ = self.authority(
                    JournalStore(path), [original, correction], registry=registry()
                )
                original_authority.apply(original.evidence_ref)
                reopened = JournalStore(path)
                changed_authority, changed_book = self.authority(
                    reopened, [original, correction], registry=changed, seed=False
                )
                before_sequence = reopened.current_journal_sequence()
                before_digest = changed_book.audit_digest()
                with self.assertRaisesRegex(PerpetualFundingConflict, "immutable instrument contract"):
                    changed_authority.apply(correction.evidence_ref)
                self.assertEqual(reopened.current_journal_sequence(), before_sequence)
                self.assertEqual(changed_book.audit_digest(), before_digest)
                restored, book = self.authority(
                    JournalStore(path), [original, correction], registry=registry(), seed=False
                )
                result = restored.apply(correction.evidence_ref)
                self.assertTrue(result.inserted)
                self.assertEqual(book.cash("BTC"), Decimal("-0.00000400"))
                self.assertFalse(restored.apply(correction.evidence_ref).inserted)

    def test_legacy_funding_event_without_contract_identity_cannot_authorize_correction(self):
        original = sealed_funding()
        correction = sealed_funding(
            external_event_id="funding-corrected", revision="2", rate="0.002",
            observed_offset=2, corrects="funding-1",
        )
        with TemporaryDirectory() as directory:
            source = JournalStore(f"{directory}/source.sqlite3")
            authority, source_book = self.authority(source, [original, correction])
            authority.apply(original.evidence_ref)
            saved, = source.load_events("perpetual_funding", authority.aggregate_id)
            for invalid_digest in (None, "", "sha256:invalid"):
                with self.subTest(digest=invalid_digest), TemporaryDirectory() as legacy_dir:
                    legacy = JournalStore(f"{legacy_dir}/legacy.sqlite3")
                    legacy_authority, legacy_book = self.authority(
                        legacy, [original, correction], seed=False
                    )
                    for transaction in source_book.transactions:
                        legacy_book.append(transaction)
                    payload = dict(saved["payload"])
                    if invalid_digest is None:
                        payload.pop("instrument_contract_digest")
                    else:
                        payload["instrument_contract_digest"] = invalid_digest
                    envelope = {key: saved[key] for key in (
                        "event_id", "event_type", "aggregate_type", "aggregate_id",
                        "aggregate_version", "committed_at",
                    )}
                    envelope["aggregate_version"] = str(envelope["aggregate_version"])
                    envelope.update(payload=payload, payload_hash=payload_digest(payload))
                    legacy.append_event(envelope)
                    before = legacy.current_journal_sequence()
                    before_digest = legacy_book.audit_digest()
                    restarted, restarted_book = self.authority(
                        JournalStore(legacy.path), [original, correction], seed=False
                    )
                    with self.assertRaisesRegex(PerpetualFundingConflict, "immutable instrument contract"):
                        restarted.apply(correction.evidence_ref)
                    self.assertEqual(legacy.current_journal_sequence(), before)
                    self.assertEqual(restarted_book.audit_digest(), before_digest)

    def test_same_timestamp_position_cut_is_ambiguous_before_booking_or_correction(self):
        for correcting in (False, True):
            with self.subTest(correcting=correcting), TemporaryDirectory() as directory:
                original = sealed_funding()
                target = sealed_funding(
                    external_event_id="funding-corrected" if correcting else "funding-1",
                    revision="2" if correcting else "1", contracts="3", observed_offset=2,
                    corrects="funding-1" if correcting else None,
                )
                path = f"{directory}/journal.sqlite3"
                store = JournalStore(path)
                authority, book = self.authority(store, [original, target])
                if correcting:
                    authority.apply(original.evidence_ref)
                seed_position(
                    book, transaction_id="opaque-execution-999", contracts="1",
                    effective_at="2026-09-25T10:00:00Z", observed_at="2026-09-25T10:00:01Z",
                )
                before_sequence = store.current_journal_sequence()
                before_digest = book.audit_digest()
                for restart in (False, True):
                    if restart:
                        authority, book = self.authority(
                            JournalStore(path), [original, target], seed=False
                        )
                    with self.assertRaisesRegex(PerpetualFundingConflict, "ambiguous.*funding cut"):
                        authority.apply(target.evidence_ref)
                    self.assertEqual(store.current_journal_sequence(), before_sequence)
                    self.assertEqual(book.audit_digest(), before_digest)

    def test_same_effective_cut_cannot_double_book_with_different_period_id(self):
        first = sealed_funding(
            external_event_id="funding-cut-a",
            funding_period_id="provider-period-a",
            observed_offset=1,
        )
        duplicate_cut = sealed_funding(
            external_event_id="funding-cut-b",
            funding_period_id="provider-period-b",
            observed_offset=2,
        )
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [first, duplicate_cut])
            authority.apply(first.evidence_ref)
            before_sequence = store.current_journal_sequence()
            before_digest = book.audit_digest()

            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "funding period already has a canonical provider event",
            ):
                authority.apply(duplicate_cut.evidence_ref)

            self.assertEqual(store.current_journal_sequence(), before_sequence)
            self.assertEqual(book.audit_digest(), before_digest)

    def test_correction_observation_cannot_predate_predecessor_after_restart(self):
        original = sealed_funding(observed_offset=1)
        first_correction = sealed_funding(
            external_event_id="funding-correction-1",
            revision="2",
            rate="0.002",
            observed_offset=3,
            corrects="funding-1",
        )
        regressed_correction = sealed_funding(
            external_event_id="funding-correction-2",
            revision="3",
            rate="0.003",
            observed_offset=2,
            corrects="funding-correction-1",
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            authority, _ = self.authority(
                JournalStore(path),
                [original, first_correction, regressed_correction],
            )
            authority.apply(original.evidence_ref)
            authority.apply(first_correction.evidence_ref)

            reopened = JournalStore(path)
            restarted, restarted_book = self.authority(
                reopened,
                [original, first_correction, regressed_correction],
                seed=False,
            )
            before_sequence = reopened.current_journal_sequence()
            before_digest = restarted_book.audit_digest()

            with self.assertRaisesRegex(
                PerpetualFundingConflict,
                "correction observation cannot predate",
            ):
                restarted.apply(regressed_correction.evidence_ref)

            self.assertEqual(reopened.current_journal_sequence(), before_sequence)
            self.assertEqual(restarted_book.audit_digest(), before_digest)

    def test_inverse_provider_correction_reverses_quantized_cashflow(self):
        original = sealed_funding(collateral_currency="BTC")
        correction = sealed_funding(
            external_event_id="funding-inverse-2",
            revision="2",
            rate="0.002",
            observed_offset=2,
            corrects="funding-1",
            collateral_currency="BTC",
        )
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="100",
                    funding_schedule={
                        "interval_hours": 8,
                        "settlement_quantum": "0.00000001",
                        "settlement_rounding": "HALF_EVEN",
                    },
                ),
            )
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            authority, book = self.authority(
                JournalStore(path),
                [original, correction],
                registry=inverse,
            )

            first = authority.apply(original.evidence_ref)
            corrected = authority.apply(correction.evidence_ref)

            self.assertEqual(first.cashflow, Decimal("-0.00000200"))
            self.assertTrue(corrected.inserted)
            self.assertEqual(corrected.cashflow, Decimal("-0.00000400"))
            self.assertIsNotNone(corrected.reversal_transaction_id)
            self.assertNotEqual(
                corrected.active_transaction_id,
                first.active_transaction_id,
            )
            self.assertEqual(book.cash("BTC"), Decimal("-0.00000400"))

            restarted, restarted_book = self.authority(
                JournalStore(path),
                [original, correction],
                registry=inverse,
                seed=False,
            )
            replay = restarted.apply(correction.evidence_ref)
            self.assertFalse(replay.inserted)
            self.assertEqual(
                replay.reversal_transaction_id,
                corrected.reversal_transaction_id,
            )
            self.assertEqual(replay.cashflow, Decimal("-0.00000400"))
            self.assertEqual(
                restarted_book.cash("BTC"),
                Decimal("-0.00000400"),
            )

    def test_inverse_subquantum_zero_retains_event_and_correction_lineage(self):
        original = sealed_funding(
            rate="0.0001",
            collateral_currency="BTC",
        )
        correction = sealed_funding(
            external_event_id="funding-subquantum-2",
            revision="2",
            rate="0.001",
            observed_offset=2,
            corrects="funding-1",
            collateral_currency="BTC",
        )
        inverse = InstrumentRegistry(
            versions=(
                perpetual_version(
                    payoff="INVERSE",
                    settlement_currency="BTC",
                    multiplier="1",
                    funding_schedule={
                        "interval_hours": 8,
                        "settlement_quantum": "0.00000001",
                        "settlement_rounding": "HALF_EVEN",
                    },
                ),
            )
        )
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            authority, book = self.authority(
                JournalStore(path),
                [original, correction],
                registry=inverse,
            )

            first = authority.apply(original.evidence_ref)
            self.assertTrue(first.inserted)
            self.assertEqual(first.cashflow, Decimal("0"))
            self.assertEqual(book.cash("BTC"), Decimal("0"))
            self.assertEqual(
                len(
                    authority.store.load_events(
                        "perpetual_funding",
                        authority.aggregate_id,
                    )
                ),
                1,
            )

            restarted, restarted_book = self.authority(
                JournalStore(path),
                [original, correction],
                registry=inverse,
                seed=False,
            )
            duplicate = restarted.apply(original.evidence_ref)
            self.assertFalse(duplicate.inserted)
            self.assertEqual(
                duplicate.active_transaction_id,
                first.active_transaction_id,
            )
            self.assertEqual(restarted_book.cash("BTC"), Decimal("0"))

            corrected = restarted.apply(correction.evidence_ref)
            self.assertTrue(corrected.inserted)
            self.assertEqual(corrected.cashflow, Decimal("-0.00000002"))
            self.assertIsNotNone(corrected.reversal_transaction_id)
            self.assertEqual(
                restarted_book.cash("BTC"),
                Decimal("-0.00000002"),
            )

            final, final_book = self.authority(
                JournalStore(path),
                [original, correction],
                registry=inverse,
                seed=False,
            )
            correction_replay = final.apply(correction.evidence_ref)
            self.assertFalse(correction_replay.inserted)
            self.assertEqual(
                correction_replay.active_transaction_id,
                corrected.active_transaction_id,
            )
            self.assertEqual(
                final_book.cash("BTC"),
                Decimal("-0.00000002"),
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
