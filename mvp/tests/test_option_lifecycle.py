from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from tempfile import TemporaryDirectory
from typing import Mapping
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.accounting import AccountingConflict, book_equity_fill
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.instruments import (
    DeliverableLeg,
    InstrumentRegistry,
    InstrumentVersion,
)
from mvp.autotrade_mvp.option_lifecycle import (
    DurableOptionLifecycleAuthority,
    OptionLifecycleConflict,
    OptionLifecycleError,
    OptionLifecycleObservation,
    canonical_option_lifecycle_observation,
    _project_position_after_reversal,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import (
    DurableProviderEconomicBook,
    EconomicBookCut,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_route_reads import QualifiedProviderResponseObservation


OPTION_ID = "11111111-1111-1111-1111-111111111111"
UNDERLYING_ID = "22222222-2222-2222-2222-222222222222"
LIFECYCLE_ENDPOINT = "/v5/account/option-lifecycle"
LIFECYCLE_SCOPE = "ACCOUNT.READ"
_CAPABILITY_ARTIFACTS = {
    "DOCUMENTED": "11111111-1111-4111-8111-111111111111",
    "API": "22222222-2222-4222-8222-222222222222",
    "ACCOUNT": "33333333-3333-4333-8333-333333333333",
    "INSTRUMENT": "44444444-4444-4444-8444-444444444444",
}


def utc(month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


def option_version(
    *,
    version: int = 1,
    effective_from: datetime = utc(1, 1, 0),
    settlement_method: str = "PHYSICAL",
    deliverable_quantity: str = "100",
    strike: str = "50",
    quantity_unit: str = "contract",
    quantity_step: str = "1",
    minimum_quantity: str = "1",
    maximum_quantity: str | None = None,
    option_right: str = "CALL",
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=OPTION_ID,
        version=version,
        provider_id="BYBIT",
        venue_id="OPTIONS",
        provider_symbol=(
            "ABC-202612-C50" if option_right == "CALL" else "ABC-202612-P50"
        ),
        asset_class="OPTION",
        base_currency="ABC",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit=quantity_unit,
        contract_multiplier=Decimal("100"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal(quantity_step),
        minimum_quantity=Decimal(minimum_quantity),
        maximum_quantity=(
            None if maximum_quantity is None else Decimal(maximum_quantity)
        ),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=effective_from,
        status="ACTIVE",
        payoff="OPTION",
        underlying_id=f"{UNDERLYING_ID}@1",
        expiry=utc(12, 18, 21),
        delivery_cutoff=utc(12, 18, 20),
        settlement_method=settlement_method,
        margin_model_id="option-margin-v1",
        strike=Decimal(strike),
        option_right=option_right,
        exercise_style="AMERICAN",
        deliverable=(DeliverableLeg("ABC", Decimal(deliverable_quantity)),),
    )


class DurableOptionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = JournalStore(self.directory.name + "/journal.sqlite3")
        self.registry = InstrumentRegistry(versions=(option_version(),))
        self.book = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
        )
        self._evidence = {}
        self.authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
        )

    def _authority(self, *, registry, economic_book, provider_environment=None):
        def resolve(reference):
            return self._evidence[reference]

        return DurableOptionLifecycleAuthority(
            self.store,
            registry=registry,
            economic_book=economic_book,
            evidence_resolver=resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
            provider_environment=provider_environment,
        )

    @staticmethod
    def _normalize_provider_lifecycle(source):
        payload = source.payload
        if not isinstance(payload, Mapping):
            raise ValueError("lifecycle provider payload must be an object")
        required = {
            "venue_id",
            "external_event_id",
            "event_kind",
            "signed_contracts",
            "effective_at",
            "provider_revision",
            "underlying_price",
            "cash_settlement_amount",
            "corrects_external_event_id",
        }
        if set(payload) != required:
            raise ValueError("lifecycle provider payload shape is not canonical")

        def instant(value):
            if not isinstance(value, str) or not value.endswith("Z"):
                raise ValueError("lifecycle timestamp must be canonical UTC text")
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc)

        return OptionLifecycleObservation(
            provider_id=source.provider_id,
            account_id=source.account_id,
            environment=source.environment,
            venue_id=payload["venue_id"],
            instrument_version=source.query_binding.instrument_version,
            external_event_id=payload["external_event_id"],
            event_kind=payload["event_kind"],
            signed_contracts=Decimal(payload["signed_contracts"]),
            effective_at=instant(payload["effective_at"]),
            observed_at=instant(source.observed_at),
            raw_evidence_digest=source.response_sha256,
            provider_revision=payload["provider_revision"],
            underlying_price=(
                None
                if payload["underlying_price"] is None
                else Decimal(payload["underlying_price"])
            ),
            cash_settlement_amount=(
                None
                if payload["cash_settlement_amount"] is None
                else Decimal(payload["cash_settlement_amount"])
            ),
            corrects_external_event_id=payload["corrects_external_event_id"],
        )

    def _capability(
        self,
        *,
        provider_id,
        instrument_version,
        observed_at,
        permission_scope=LIFECYCLE_SCOPE,
    ):
        claim_time = observed_at - timedelta(minutes=2)
        expires_at = observed_at + timedelta(minutes=10)
        claims = tuple(
            CapabilityClaim(
                source=source,
                provider_id=provider_id,
                account_id="paper-1",
                entity_id="entity-1",
                environment="PAPER",
                instrument_version=instrument_version,
                observed_at=claim_time,
                expires_at=expires_at,
                supported_order_types=frozenset({"LIMIT"}),
                time_in_force=frozenset({"GTC"}),
                permission_scopes=frozenset({permission_scope}),
                position_mode="NET",
                native_protection=frozenset(),
                rate_limit_policy_id="option-lifecycle-test-v1",
                data_entitlements=frozenset({"ACCOUNT"}),
                evidence_ref={
                    "artifact_id": _CAPABILITY_ARTIFACTS[source],
                    "sha256": "sha256:" + "a" * 64,
                    "observed_at": claim_time.isoformat().replace("+00:00", "Z"),
                },
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        )
        return derive_capability_snapshot(
            snapshot_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            claims=claims,
            observed_at=observed_at - timedelta(minutes=1),
            evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
        )

    def evidence(
        self,
        *,
        external_event_id: str = "life-1",
        event_kind: str = "EXERCISE",
        signed_contracts: str = "1",
        observed_at: datetime = utc(12, 18, 19, 1),
        provider_revision: str = "provider-r1",
        underlying_price: str | None = None,
        cash_settlement_amount: str | None = None,
        corrects_external_event_id: str | None = None,
        instrument_version: str = f"{OPTION_ID}@1",
        provider_id: str = "BYBIT",
        venue_id: str = "OPTIONS",
        endpoint: str = LIFECYCLE_ENDPOINT,
        permission_scope: str = LIFECYCLE_SCOPE,
    ) -> str:
        capability = self._capability(
            provider_id=provider_id,
            instrument_version=instrument_version,
            observed_at=observed_at,
            permission_scope=permission_scope,
        )
        binding = prepare_authenticated_read_query(
            capability=capability,
            surface=Surface.ACTIVITIES,
            endpoint=endpoint,
            query={"kind": "OPTION_LIFECYCLE"},
            at=observed_at - timedelta(seconds=30),
            permission_scope=permission_scope,
        )
        payload = {
            "venue_id": venue_id,
            "external_event_id": external_event_id,
            "event_kind": event_kind,
            "signed_contracts": signed_contracts,
            "effective_at": utc(12, 18, 19).isoformat().replace("+00:00", "Z"),
            "provider_revision": provider_revision,
            "underlying_price": underlying_price,
            "cash_settlement_amount": cash_settlement_amount,
            "corrects_external_event_id": corrects_external_event_id,
        }
        source = observe_authenticated_json_response(
            query_binding=binding,
            http_status=200,
            response_bytes=json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8"),
            observed_at=observed_at,
        )
        self._evidence[source.evidence_ref] = source
        return source.evidence_ref

    def test_unissued_qualified_provider_response_cannot_cross_lifecycle_boundary(self):
        forged = object.__new__(QualifiedProviderResponseObservation)
        self._evidence["forged-qualified"] = forged
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "qualified provider lifecycle evidence authority is unavailable",
        ):
            self.authority._observation_from_evidence("forged-qualified")
        self.assertEqual(self.authority._events(), [])
        self.assertEqual(self.book.transactions, ())

    def test_delivery_parser_identity_is_not_accepted_as_lifecycle_authority(self):
        neutral_ref = self.evidence(external_event_id="delivery-parser-refusal")
        neutral_source = self._evidence[neutral_ref]
        forged = object.__new__(QualifiedProviderResponseObservation)
        object.__setattr__(forged, "observation", neutral_source)
        object.__setattr__(forged, "query_binding", neutral_source.query_binding)
        self._evidence["qualified-delivery"] = forged

        with (
            patch.object(
                QualifiedProviderResponseObservation,
                "evidence_ref",
                new_callable=property,
                return_value="qualified-delivery",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "parser_identity",
                new_callable=property,
                return_value="BYBIT_OPTION_DELIVERY_V5_JSON_V1",
            ),
        ):
            with self.assertRaisesRegex(
                OptionLifecycleError,
                "qualified provider lifecycle evidence parser identity is unsupported",
            ):
                self.authority._observation_from_evidence("qualified-delivery")
        self.assertEqual(self.authority._events(), [])
        self.assertEqual(self.book.transactions, ())

    def test_qualified_lifecycle_parser_identity_is_admitted_only_as_the_canonical_lifecycle_contract(self):
        neutral_ref = self.evidence(external_event_id="qualified-lifecycle-ref")
        neutral_source = self._evidence[neutral_ref]
        forged = object.__new__(QualifiedProviderResponseObservation)
        object.__setattr__(forged, "observation", neutral_source)
        object.__setattr__(forged, "query_binding", neutral_source.query_binding)
        self._evidence["qualified-lifecycle"] = forged

        with (
            patch.object(
                QualifiedProviderResponseObservation,
                "evidence_ref",
                new_callable=property,
                return_value="qualified-lifecycle",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "parser_identity",
                new_callable=property,
                return_value="autotrade.option-lifecycle.sealed-json",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "provider_environment",
                new_callable=property,
                return_value="TESTNET",
            ),
        ):
            observation, provider_evidence, qualified = (
                self.authority._observation_from_evidence("qualified-lifecycle")
            )

        self.assertEqual(observation.provider_id, "BYBIT")
        self.assertIs(provider_evidence, neutral_source)
        self.assertIs(qualified, forged)
        self.assertEqual(observation.raw_evidence_digest, neutral_source.response_sha256)
        self.assertEqual(observation.provider_environment, "TESTNET")
        self.assertEqual(
            canonical_option_lifecycle_observation(observation)["provider_environment"],
            "TESTNET",
        )

    def test_qualified_lifecycle_apply_persists_full_provider_q_provenance(self):
        self.seed_option_position("1")
        neutral_ref = self.evidence(
            external_event_id="qualified-lifecycle-apply",
            provider_revision="qualified-r1",
        )
        neutral_source = self._evidence[neutral_ref]

        class QualifiedBinding:
            query_digest = "sha256:" + "a" * 64
            provider_environment = "TESTNET"
            authority_journal_sequence_cut = 17
            adapter_code_sha = "b" * 40
            packaged_artifact_digest = "sha256:" + "c" * 64

        forged = object.__new__(QualifiedProviderResponseObservation)
        object.__setattr__(forged, "observation", neutral_source)
        object.__setattr__(forged, "query_binding", QualifiedBinding())
        self._evidence["qualified-lifecycle-apply"] = forged
        authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="TESTNET",
        )

        patches = (
            patch.object(
                QualifiedProviderResponseObservation,
                "evidence_ref",
                new_callable=property,
                return_value="qualified-lifecycle-apply",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "parser_identity",
                new_callable=property,
                return_value="autotrade.option-lifecycle.sealed-json",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "qualification_id",
                new_callable=property,
                return_value="provider-qualification:sha256:" + "d" * 64,
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "route_semantics_digest",
                new_callable=property,
                return_value="sha256:" + "e" * 64,
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "endpoint_rule_digest",
                new_callable=property,
                return_value="sha256:" + "f" * 64,
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "qualified_route_rule_digest",
                new_callable=property,
                return_value="sha256:" + "0" * 64,
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "data_entitlement",
                new_callable=property,
                return_value="ACCOUNT",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "provider_environment",
                new_callable=property,
                return_value="TESTNET",
            ),
        )
        for item in patches:
            item.start()
        try:
            result = authority.apply("qualified-lifecycle-apply")
        finally:
            for item in reversed(patches):
                item.stop()

        self.assertTrue(result.inserted)
        event = self.store.load_events(
            "option_lifecycle",
            authority.aggregate_id,
        )[0]["payload"]
        evidence = event["provider_evidence"]
        self.assertEqual(
            event["provider_evidence_ref"],
            "qualified-lifecycle-apply",
        )
        self.assertEqual(evidence["evidence_ref"], "qualified-lifecycle-apply")
        self.assertEqual(
            evidence["qualification_id"],
            "provider-qualification:sha256:" + "d" * 64,
        )
        self.assertEqual(evidence["route_semantics_digest"], "sha256:" + "e" * 64)
        self.assertEqual(evidence["endpoint_rule_digest"], "sha256:" + "f" * 64)
        self.assertEqual(
            evidence["qualified_route_rule_digest"],
            "sha256:" + "0" * 64,
        )
        self.assertEqual(evidence["data_entitlement"], "ACCOUNT")
        self.assertEqual(event["provider_environment"], "TESTNET")
        self.assertEqual(evidence["provider_environment"], "TESTNET")
        self.assertEqual(evidence["authority_journal_sequence_cut"], 17)

    def test_qualified_provider_environment_mismatch_fails_before_economic_mutation(self):
        self.seed_option_position("1")
        neutral_ref = self.evidence(
            external_event_id="qualified-domain-mismatch",
            provider_revision="qualified-domain-r1",
        )
        neutral_source = self._evidence[neutral_ref]
        forged = object.__new__(QualifiedProviderResponseObservation)
        object.__setattr__(forged, "observation", neutral_source)

        class QualifiedBinding:
            provider_environment = "TESTNET"

        object.__setattr__(forged, "query_binding", QualifiedBinding())
        self._evidence["qualified-domain-mismatch"] = forged
        authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="DEMO",
        )

        with (
            patch.object(
                QualifiedProviderResponseObservation,
                "evidence_ref",
                new_callable=property,
                return_value="qualified-domain-mismatch",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "parser_identity",
                new_callable=property,
                return_value="autotrade.option-lifecycle.sealed-json",
            ),
            patch.object(
                QualifiedProviderResponseObservation,
                "provider_environment",
                new_callable=property,
                return_value="TESTNET",
            ),
        ):
            with self.assertRaisesRegex(
                OptionLifecycleError,
                "provider_environment scope does not match lifecycle authority",
            ):
                authority.apply("qualified-domain-mismatch")

        self.assertEqual(self.book.transactions, ())
        self.assertEqual(authority._events(), [])

    def test_qualified_provider_environment_changes_aggregate_identity(self):
        testnet = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="TESTNET",
        )
        demo = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="DEMO",
        )
        self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)

        self.assertEqual(evidence["adapter_code_sha"], "b" * 40)
        self.assertEqual(
            evidence["packaged_artifact_digest"],
            "sha256:" + "c" * 64,
        )

    def test_qualified_provider_environment_mismatch_fails_before_economic_mutation_before_virtual_dispatch(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("hostile Decimal subclass must not dispatch")

        base = {
            "provider_id": "BYBIT",
            "account_id": "paper-1",
            "environment": "PAPER",
            "venue_id": "OPTIONS",
            "instrument_version": f"{OPTION_ID}@1",
            "external_event_id": "hostile-life",
            "event_kind": "EXERCISE",
            "signed_contracts": Decimal("1"),
            "effective_at": utc(12, 18, 19),
            "observed_at": utc(12, 18, 19, 1),
            "raw_evidence_digest": "sha256:" + "e" * 64,
            "provider_revision": "hostile-r1",
        }
        for field in (
            "signed_contracts",
            "underlying_price",
            "cash_settlement_amount",
        ):
            with self.subTest(field=field):
                values = dict(base)
                values[field] = HostileDecimal("1.25")
                with self.assertRaisesRegex(
                    OptionLifecycleError,
                    "bounded exact decimal input",
                ):
                    OptionLifecycleObservation(**values)

    def test_oversized_provider_numeric_text_fails_before_mutation(self):
        reference = self.evidence(
            external_event_id="oversized-life",
            signed_contracts="1" * 2048,
        )
        self.assertEqual(self.book.transactions, ())
        self.assertEqual(self.authority._events(), [])
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "invalid financial values",
        ):
            self.authority.apply(reference)
        self.assertEqual(self.book.transactions, ())
        self.assertEqual(self.authority._events(), [])

    def test_authority_rejects_economic_book_subclass_before_virtual_dispatch(self):
        class HostileEconomicBook(DurableProviderEconomicBook):
            def read_cut(self):
                raise AssertionError("hostile economic read-cut dispatch")

        with self.assertRaisesRegex(
            TypeError, "economic_book must be exact DurableProviderEconomicBook",
        ):
            HostileEconomicBook(
                self.store, provider_id="BYBIT", account_id="paper-1", environment="PAPER",
            )

    def test_lifecycle_rejects_post_construction_economic_method_shadow(self):
        self.seed_option_position("1")
        evidence_ref = self.evidence(external_event_id="shadowed-owner")
        with self.assertRaisesRegex(AccountingConflict, "authority state is immutable"):
            self.book.read_cut = lambda: (_ for _ in ()).throw(
                AssertionError("shadowed economic cut dispatch")
            )
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(
            self.book.position(f"{OPTION_ID}@1"),
            Decimal("1"),
        )

    def test_evidence_callback_cannot_retarget_economic_book_scope(self):
        self.seed_option_position("1")
        evidence_ref = self.evidence(external_event_id="retarget-owner")
        original_resolver = self.authority.evidence_resolver

        def hostile_resolver(reference):
            source = original_resolver(reference)
            self.book.account_id = "attacker-account"
            return source

        self.authority.evidence_resolver = hostile_resolver
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "provider lifecycle evidence could not be resolved",
        ):
            self.authority.apply(evidence_ref)
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(self.book.account_id, "paper-1")

    def test_correction_position_projection_is_context_independent(self):
        instrument = f"{OPTION_ID}@1"
        current = book_equity_fill(
            transaction_id="projection-current",
            cause_event_id="projection-current-cause",
            instrument=instrument,
            settlement_currency="USD",
            side="BUY",
            quantity=Decimal("123456789012345678901234567890"),
            price=Decimal("1"),
        )
        prior_retirement = book_equity_fill(
            transaction_id="projection-prior",
            cause_event_id="projection-prior-cause",
            instrument=instrument,
            settlement_currency="USD",
            side="SELL",
            quantity=Decimal("1"),
            price=Decimal("1"),
        )
        cut = EconomicBookCut(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            transactions=(current,),
            book_digest="sha256:" + "a" * 64,
            aggregate_version=1,
        )

        values = []
        for precision, rounding in (
            (6, ROUND_FLOOR),
            (80, ROUND_CEILING),
        ):
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                values.append(
                    _project_position_after_reversal(
                        economic_cut=cut,
                        instrument=instrument,
                        old_active_transactions=(prior_retirement,),
                    )
                )
        self.assertEqual(values[0], values[1])
        self.assertEqual(
            values[0],
            Decimal("123456789012345678901234567891"),
        )

    def test_physical_exercise_strike_cash_is_context_independent(self):
        high_precision_version = option_version(
            strike="12345678901234567890.123456789",
        )
        registry = InstrumentRegistry(versions=(high_precision_version,))
        authority = self._authority(
            registry=registry,
            economic_book=self.book,
        )
        self.seed_option_position("2")

        cash_amounts = []
        contexts = (
            (6, ROUND_FLOOR, "context-low", "context-low-r1"),
            (80, ROUND_CEILING, "context-high", "context-high-r1"),
        )
        for precision, rounding, event_id, revision in contexts:
            evidence_ref = self.evidence(
                external_event_id=event_id,
                provider_revision=revision,
            )
            with localcontext() as context:
                context.prec = precision
                context.rounding = rounding
                result = authority.apply(evidence_ref)
            by_id = {
                item.transaction_id: item
                for item in self.book.transactions
            }
            transaction = by_id[result.active_transaction_ids[0]]
            cash_amounts.append(
                next(
                    posting.signed_amount
                    for posting in transaction.postings
                    if posting.asset_or_currency == "USD"
                )
            )

        self.assertEqual(cash_amounts[0], cash_amounts[1])
        self.assertEqual(
            cash_amounts[0],
            Decimal("-1234567890123456789012.3456789"),
        )

    def seed_option_position(self, signed_contracts: str) -> None:
        quantity = Decimal(signed_contracts)
        if quantity == 0:
            raise ValueError("seed position must be non-zero")
        side = "BUY" if quantity > 0 else "SELL"
        self.book.append(
            book_equity_fill(
                transaction_id=f"seed-option-{side.lower()}-{abs(quantity)}",
                cause_event_id=f"seed-option-cause-{side.lower()}-{abs(quantity)}",
                instrument=f"{OPTION_ID}@1",
                settlement_currency="USD",
                side=side,
                quantity=abs(quantity),
                price=Decimal("1"),
            )
        )

    def seed_underlying_position(self, signed_quantity: str) -> None:
        quantity = Decimal(signed_quantity)
        if quantity == 0:
            raise ValueError("seed position must be non-zero")
        side = "BUY" if quantity > 0 else "SELL"
        self.book.append(
            book_equity_fill(
                transaction_id=f"seed-underlying-{side.lower()}-{abs(quantity)}",
                cause_event_id=f"seed-underlying-cause-{side.lower()}-{abs(quantity)}",
                instrument="ABC",
                settlement_currency="USD",
                side=side,
                quantity=abs(quantity),
                price=Decimal("1"),
            )
        )

    def forged_observation(self) -> OptionLifecycleObservation:
        return OptionLifecycleObservation(
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
            venue_id="OPTIONS",
            instrument_version=f"{OPTION_ID}@1",
            external_event_id="forged-life",
            event_kind="EXERCISE",
            signed_contracts=Decimal("1"),
            effective_at=utc(12, 18, 19),
            observed_at=utc(12, 18, 19, 1),
            raw_evidence_digest="sha256:" + "f" * 64,
            provider_revision="forged-r1",
        )

    def test_direct_fabricated_lifecycle_fact_cannot_mutate_financial_state(self):
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "evidence_ref",
        ):
            self.authority.apply(self.forged_observation())

        self.assertEqual(self.book.transactions, ())
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(
            self.store.load_events("economic_book", self.book.book_id),
            [],
        )

    def test_unresolved_or_mismatched_provider_evidence_cannot_mutate(self):
        cases = (
            ("unresolved", "provider-read:sha256:" + "0" * 64),
            ("wrong-endpoint", self.evidence(endpoint="/v5/account/other")),
            (
                "wrong-permission",
                self.evidence(permission_scope="OPTION.LIFECYCLE.READ"),
            ),
            ("wrong-provider", self.evidence(provider_id="ALPACA")),
        )
        for name, reference in cases:
            with self.subTest(case=name):
                before_lifecycle = tuple(
                    self.store.load_events(
                        "option_lifecycle",
                        self.authority.aggregate_id,
                    )
                )
                before_economic = tuple(
                    self.store.load_events("economic_book", self.book.book_id)
                )
                with self.assertRaises(OptionLifecycleError):
                    self.authority.apply(reference)
                self.assertEqual(
                    tuple(
                        self.store.load_events(
                            "option_lifecycle",
                            self.authority.aggregate_id,
                        )
                    ),
                    before_lifecycle,
                )
                self.assertEqual(
                    tuple(
                        self.store.load_events("economic_book", self.book.book_id)
                    ),
                    before_economic,
                )

    def test_arbitrary_normalizer_cannot_be_injected_as_financial_authority(self):
        reference = self.evidence()
        source = self._evidence[reference]

        def forged_normalizer(_source):
            valid = self._normalize_provider_lifecycle(source)
            return OptionLifecycleObservation(
                provider_id=valid.provider_id,
                account_id=valid.account_id,
                environment=valid.environment,
                venue_id="FORGED_VENUE",
                instrument_version=valid.instrument_version,
                external_event_id="forged-external-id",
                event_kind="ASSIGNMENT",
                signed_contracts=Decimal("-99"),
                effective_at=valid.effective_at + timedelta(days=1),
                observed_at=valid.observed_at,
                raw_evidence_digest=valid.raw_evidence_digest,
                provider_revision="forged-revision",
                underlying_price=Decimal("999999"),
                corrects_external_event_id="forged-correction",
            )

        with self.assertRaises(TypeError):
            DurableOptionLifecycleAuthority(
                self.store,
                registry=self.registry,
                economic_book=self.book,
                evidence_resolver=lambda ref: self._evidence[ref],
                normalizer=forged_normalizer,
                lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
                permission_scope=LIFECYCLE_SCOPE,
            )

        self.assertEqual(self.book.transactions, ())
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(reference, source.evidence_ref)

    def test_durable_evidence_binds_exact_parser_contract(self):
        self.seed_option_position("1")
        reference = self.evidence()

        self.authority.apply(reference)

        events = self.store.load_events(
            "option_lifecycle",
            self.authority.aggregate_id,
        )
        self.assertEqual(len(events), 1)
        evidence = events[0]["payload"]["provider_evidence"]
        self.assertEqual(
            evidence["parser_id"],