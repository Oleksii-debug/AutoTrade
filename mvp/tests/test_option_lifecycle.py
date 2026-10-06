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
import mvp.autotrade_mvp.option_lifecycle as option_lifecycle_module
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
        provider_id="TEST_PROVIDER",
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
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        self._evidence = {}
        self.authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
        )

    def _authority(
        self,
        *,
        registry,
        economic_book,
        provider_environment=None,
    ):
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
        environment="SIMULATION",
    ):
        claim_time = observed_at - timedelta(minutes=2)
        expires_at = observed_at + timedelta(minutes=10)
        claims = tuple(
            CapabilityClaim(
                source=source,
                provider_id=provider_id,
                account_id="paper-1",
                entity_id="entity-1",
                environment=environment,
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
        provider_id: str = "TEST_PROVIDER",
        venue_id: str = "OPTIONS",
        endpoint: str = LIFECYCLE_ENDPOINT,
        permission_scope: str = LIFECYCLE_SCOPE,
        environment: str = "SIMULATION",
    ) -> str:
        capability = self._capability(
            provider_id=provider_id,
            instrument_version=instrument_version,
            observed_at=observed_at,
            permission_scope=permission_scope,
            environment=environment,
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

    def test_lifecycle_financial_decimals_reject_subclasses_before_virtual_dispatch(self):
        class HostileDecimal(Decimal):
            def is_finite(self):
                raise AssertionError("hostile Decimal subclass must not dispatch")

        base = {
            "provider_id": "TEST_PROVIDER",
            "account_id": "paper-1",
            "environment": "SIMULATION",
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
                self.store, provider_id="TEST_PROVIDER", account_id="paper-1", environment="SIMULATION",
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
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
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

    def test_paper_and_live_lifecycle_reject_test_injected_response_before_financial_mutation(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment):
                book = DurableProviderEconomicBook(
                    self.store,
                    provider_id="TEST_PROVIDER",
                    account_id="paper-1",
                    environment=environment,
                )
                book.append(
                    book_equity_fill(
                        transaction_id=f"origin-firebreak-seed-{environment.lower()}",
                        cause_event_id=f"origin-firebreak-seed-cause-{environment.lower()}",
                        instrument=f"{OPTION_ID}@1",
                        settlement_currency="USD",
                        side="BUY",
                        quantity=Decimal("1"),
                        price=Decimal("1"),
                    )
                )
                authority = self._authority(
                    registry=self.registry,
                    economic_book=book,
                )
                reference = self.evidence(
                    external_event_id=f"{environment.lower()}-test-injected-life",
                    environment=environment,
                )
                before_lifecycle = tuple(
                    self.store.load_events("option_lifecycle", authority.aggregate_id)
                )
                before_economic = tuple(book.transactions)

                with self.assertRaisesRegex(
                    OptionLifecycleError,
                    "durable PROVIDER_ORIGIN evidence",
                ):
                    authority.apply(reference)

                self.assertEqual(
                    tuple(
                        self.store.load_events(
                            "option_lifecycle",
                            authority.aggregate_id,
                        )
                    ),
                    before_lifecycle,
                )
                self.assertEqual(tuple(book.transactions), before_economic)
                self.assertEqual(
                    book.position(f"{OPTION_ID}@1"),
                    Decimal("1"),
                )

    def test_paper_and_live_firebreak_precedes_arbitrary_evidence_resolver(self):
        for environment in ("PAPER", "LIVE"):
            with self.subTest(environment=environment):
                book = DurableProviderEconomicBook(
                    self.store,
                    provider_id="TEST_PROVIDER",
                    account_id="paper-1",
                    environment=environment,
                )
                resolver_calls = []

                def mutate_financial_state_if_called(_reference):
                    resolver_calls.append(environment)
                    book.append(
                        book_equity_fill(
                            transaction_id=(
                                f"resolver-side-effect-{environment.lower()}"
                            ),
                            cause_event_id=(
                                f"resolver-side-effect-cause-{environment.lower()}"
                            ),
                            instrument=f"{OPTION_ID}@1",
                            settlement_currency="USD",
                            side="BUY",
                            quantity=Decimal("1"),
                            price=Decimal("1"),
                        )
                    )
                    raise AssertionError(
                        "PAPER/LIVE firebreak must precede evidence resolver"
                    )

                authority = DurableOptionLifecycleAuthority(
                    self.store,
                    registry=self.registry,
                    economic_book=book,
                    evidence_resolver=mutate_financial_state_if_called,
                    lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
                    permission_scope=LIFECYCLE_SCOPE,
                    provider_environment="TESTNET",
                )
                before_economic = tuple(book.transactions)
                before_lifecycle = tuple(
                    self.store.load_events(
                        "option_lifecycle",
                        authority.aggregate_id,
                    )
                )

                with self.assertRaisesRegex(
                    OptionLifecycleError,
                    "durable PROVIDER_ORIGIN evidence",
                ):
                    authority.apply(
                        f"resolver-firebreak-{environment.lower()}"
                    )

                self.assertEqual(resolver_calls, [])
                self.assertEqual(tuple(book.transactions), before_economic)
                self.assertEqual(
                    tuple(
                        self.store.load_events(
                            "option_lifecycle",
                            authority.aggregate_id,
                        )
                    ),
                    before_lifecycle,
                )

    def test_provider_environment_is_canonical_and_changes_aggregate_identity(self):
        testnet = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="testnet",
        )
        demo = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="DEMO",
        )
        self.assertEqual(testnet.provider_environment, "TESTNET")
        self.assertEqual(demo.provider_environment, "DEMO")
        self.assertNotEqual(testnet.aggregate_id, demo.aggregate_id)

    def test_provider_environment_mutation_fails_before_lifecycle_read(self):
        authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="TESTNET",
        )
        authority.provider_environment = "DEMO"
        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "provider_environment authority changed",
        ):
            authority._events()

    def test_neutral_simulation_evidence_cannot_satisfy_provider_environment_authority(self):
        self.seed_option_position("1")
        reference = self.evidence(
            external_event_id="neutral-provider-domain",
            provider_revision="neutral-provider-domain-r1",
        )
        authority = self._authority(
            registry=self.registry,
            economic_book=self.book,
            provider_environment="TESTNET",
        )
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "provider_environment scope does not match lifecycle authority",
        ):
            authority.apply(reference)

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def test_qualified_provider_read_wrapper_is_not_lifecycle_authority(self):
        reference = "qualified-provider-read-wrapper"
        forged = object.__new__(QualifiedProviderResponseObservation)
        self._evidence[reference] = forged
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events(
                "option_lifecycle",
                self.authority.aggregate_id,
            )
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "exact sealed ProviderResponseObservation",
        ):
            self.authority.apply(reference)

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(
                self.store.load_events(
                    "option_lifecycle",
                    self.authority.aggregate_id,
                )
            ),
            before_lifecycle,
        )

    def test_simulation_resolver_cannot_rebind_lifecycle_observation_parser(self):
        reference = self.evidence(
            external_event_id="resolver-parser-rebind-life",
        )
        original_parser = (
            option_lifecycle_module._canonical_observation_from_sealed_response
        )

        def rebind_parser_then_resolve(evidence_ref):
            option_lifecycle_module._canonical_observation_from_sealed_response = (
                lambda _source: self.forged_observation()
            )
            return self._evidence[evidence_ref]

        authority = DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry,
            economic_book=self.book,
            evidence_resolver=rebind_parser_then_resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
        )
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )
        try:
            with self.assertRaisesRegex(
                OptionLifecycleError,
                "evidence authority changed during resolution",
            ):
                authority.apply(reference)
        finally:
            option_lifecycle_module._canonical_observation_from_sealed_response = (
                original_parser
            )

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def test_simulation_resolver_cannot_rebind_response_scope_validator(self):
        reference = self.evidence(
            external_event_id="resolver-scope-rebind-life",
        )
        source_type = type(self._evidence[reference])
        original_require_scope = source_type.require_scope

        def rebind_scope_validator_then_resolve(evidence_ref):
            source_type.require_scope = lambda *_args, **_kwargs: None
            return self._evidence[evidence_ref]

        authority = DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry,
            economic_book=self.book,
            evidence_resolver=rebind_scope_validator_then_resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
        )
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )
        try:
            with self.assertRaisesRegex(
                OptionLifecycleError,
                "evidence authority changed during resolution",
            ):
                authority.apply(reference)
        finally:
            source_type.require_scope = original_require_scope

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def test_simulation_resolver_cannot_swap_same_scope_economic_book(self):
        reference = self.evidence(
            external_event_id="resolver-book-swap-life",
        )
        replacement_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        holder = {}

        def swap_book_then_resolve(evidence_ref):
            holder["authority"].economic_book = replacement_book
            return self._evidence[evidence_ref]

        authority = DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry,
            economic_book=self.book,
            evidence_resolver=swap_book_then_resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
        )
        holder["authority"] = authority
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "option lifecycle authority changed during evidence resolution",
        ):
            authority.apply(reference)

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def test_simulation_resolver_cannot_swap_instrument_registry(self):
        reference = self.evidence(
            external_event_id="resolver-registry-swap-life",
        )
        replacement_registry = InstrumentRegistry(versions=(option_version(),))
        holder = {}

        def swap_registry_then_resolve(evidence_ref):
            holder["authority"].registry = replacement_registry
            return self._evidence[evidence_ref]

        authority = DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry,
            economic_book=self.book,
            evidence_resolver=swap_registry_then_resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
        )
        holder["authority"] = authority
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "option lifecycle authority changed during evidence resolution",
        ):
            authority.apply(reference)

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def test_simulation_resolver_cannot_retarget_permission_scope(self):
        forged_scope = "ACCOUNT.FORGED"
        reference = self.evidence(
            external_event_id="resolver-permission-retarget-life",
            permission_scope=forged_scope,
        )
        holder = {}

        def retarget_permission_then_resolve(evidence_ref):
            holder["authority"].permission_scope = forged_scope
            return self._evidence[evidence_ref]

        authority = DurableOptionLifecycleAuthority(
            self.store,
            registry=self.registry,
            economic_book=self.book,
            evidence_resolver=retarget_permission_then_resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
        )
        holder["authority"] = authority
        before_economic = tuple(self.book.transactions)
        before_lifecycle = tuple(
            self.store.load_events("option_lifecycle", authority.aggregate_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "option lifecycle authority changed during evidence resolution",
        ):
            authority.apply(reference)

        self.assertEqual(tuple(self.book.transactions), before_economic)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            before_lifecycle,
        )

    def forged_observation(self) -> OptionLifecycleObservation:
        return OptionLifecycleObservation(
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
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
            "autotrade.option-lifecycle.sealed-json",
        )
        self.assertEqual(evidence["parser_version"], "1.1.0")
        self.assertRegex(
            evidence["parser_contract_digest"],
            r"^sha256:[0-9a-f]{64}$",
        )

    def test_fractional_lifecycle_quantity_off_canonical_grid_fails_before_mutation(self):
        self.seed_option_position("0.5")
        before_transactions = tuple(self.book.transactions)

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "canonical instrument quantity_step",
        ):
            self.authority.apply(self.evidence(signed_contracts="0.5"))

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )

    def test_explicit_smaller_canonical_quantity_step_accepts_aligned_lifecycle_quantity(self):
        registry = InstrumentRegistry(
            versions=(
                option_version(
                    quantity_step="0.25",
                    minimum_quantity="0.25",
                ),
            )
        )
        authority = self._authority(
            registry=registry,
            economic_book=self.book,
        )
        self.seed_option_position("0.5")

        result = authority.apply(self.evidence(signed_contracts="0.5"))

        self.assertTrue(result.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("50"))

    def test_lifecycle_quantity_does_not_inherit_order_entry_minimum(self):
        registry = InstrumentRegistry(
            versions=(
                option_version(
                    quantity_step="0.25",
                    minimum_quantity="1",
                ),
            )
        )
        authority = self._authority(
            registry=registry,
            economic_book=self.book,
        )
        self.seed_option_position("0.5")

        result = authority.apply(self.evidence(signed_contracts="0.5"))

        self.assertTrue(result.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("50"))

    def test_lifecycle_quantity_does_not_inherit_order_entry_maximum(self):
        registry = InstrumentRegistry(
            versions=(
                option_version(
                    quantity_step="1",
                    minimum_quantity="1",
                    maximum_quantity="2",
                ),
            )
        )
        authority = self._authority(
            registry=registry,
            economic_book=self.book,
        )
        self.seed_option_position("3")

        result = authority.apply(self.evidence(signed_contracts="3"))

        self.assertTrue(result.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("300"))

    def test_lifecycle_observation_decimal_identity_ignores_ambient_context(self):
        observation = OptionLifecycleObservation(
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
            venue_id="OPTIONS",
            instrument_version=f"{OPTION_ID}@1",
            external_event_id="identity-context",
            event_kind="EXERCISE",
            signed_contracts=Decimal("12345678901234567890.125"),
            effective_at=utc(12, 18, 19),
            observed_at=utc(12, 18, 19, 1),
            raw_evidence_digest="sha256:" + "c" * 64,
            provider_revision="provider-context-r1",
            underlying_price=Decimal("98765432109876543210.375"),
        )
        payloads = []
        for precision in (6, 10, 28, 80):
            with localcontext() as context:
                context.prec = precision
                payloads.append(canonical_option_lifecycle_observation(observation))

        self.assertTrue(all(payload == payloads[0] for payload in payloads[1:]))
        self.assertEqual(
            payloads[0]["signed_contracts"],
            "12345678901234567890.125",
        )
        self.assertEqual(
            payloads[0]["underlying_price"],
            "98765432109876543210.375",
        )

    def test_quantity_grid_admission_is_independent_of_ambient_decimal_context(self):
        # Built-in abs(Decimal) is context-sensitive. Under this precision the
        # off-grid .5 tail would round away if ambient Decimal context were
        # allowed to become financial authority.
        off_grid = "100000000000000000000000000000.5"
        with localcontext() as context:
            context.prec = 2
            with self.assertRaisesRegex(
                OptionLifecycleError,
                "canonical instrument quantity_step",
            ):
                self.authority.apply(
                    self.evidence(signed_contracts=off_grid)
                )

        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(self.book.transactions, ())

    def test_correction_reproves_quantity_grid_before_any_reversal(self):
        self.seed_option_position("-2")
        self.seed_underlying_position("200")
        first = self.evidence(
            external_event_id="assignment-grid-r1",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
            provider_revision="provider-r1",
        )
        self.authority.apply(first)
        before_transactions = tuple(self.book.transactions)
        before_position = self.book.position(f"{OPTION_ID}@1")
        before_cash = self.book.cash("USD")

        correction = self.evidence(
            external_event_id="assignment-grid-r2",
            event_kind="ASSIGNMENT",
            signed_contracts="-1.5",
            observed_at=utc(12, 18, 19, 2),
            provider_revision="provider-r2",
            corrects_external_event_id="assignment-grid-r1",
        )
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "canonical instrument quantity_step",
        ):
            self.authority.apply(correction)

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), before_position)
        self.assertEqual(self.book.cash("USD"), before_cash)
        self.assertEqual(
            len(self.store.load_events("option_lifecycle", self.authority.aggregate_id)),
            1,
        )

    def test_correction_rejects_changed_instrument_grid_authority_before_reversal(self):
        self.seed_option_position("-2")
        self.seed_underlying_position("200")
        first = self.evidence(
            external_event_id="assignment-grid-authority-r1",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
            provider_revision="provider-r1",
        )
        self.authority.apply(first)

        before_transactions = tuple(self.book.transactions)
        before_position = self.book.position(f"{OPTION_ID}@1")
        before_cash = self.book.cash("USD")

        changed_registry = InstrumentRegistry(
            versions=(
                option_version(
                    quantity_step="0.5",
                    minimum_quantity="0.5",
                ),
            )
        )
        restarted_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        changed_authority = self._authority(
            registry=changed_registry,
            economic_book=restarted_book,
        )
        correction = self.evidence(
            external_event_id="assignment-grid-authority-r2",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
            observed_at=utc(12, 18, 19, 2),
            provider_revision="provider-r2",
            corrects_external_event_id="assignment-grid-authority-r1",
        )

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "instrument version authority",
        ):
            changed_authority.apply(correction)

        self.assertEqual(tuple(restarted_book.transactions), before_transactions)
        self.assertEqual(
            restarted_book.position(f"{OPTION_ID}@1"),
            before_position,
        )
        self.assertEqual(restarted_book.cash("USD"), before_cash)
        self.assertEqual(
            len(
                self.store.load_events(
                    "option_lifecycle",
                    changed_authority.aggregate_id,
                )
            ),
            1,
        )

    def test_retry_under_changed_quantity_grid_is_not_equivalent(self):
        self.seed_option_position("1")
        reference = self.evidence()
        first = self.authority.apply(reference)
        self.assertTrue(first.inserted)
        before_transactions = tuple(self.book.transactions)

        changed_registry = InstrumentRegistry(
            versions=(
                option_version(
                    quantity_step="0.5",
                    minimum_quantity="0.5",
                ),
            )
        )
        restarted_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        changed_authority = self._authority(
            registry=changed_registry,
            economic_book=restarted_book,
        )

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "reused with changed evidence",
        ):
            changed_authority.apply(reference)

        self.assertEqual(tuple(restarted_book.transactions), before_transactions)
        self.assertEqual(
            len(
                self.store.load_events(
                    "option_lifecycle",
                    changed_authority.aggregate_id,
                )
            ),
            1,
        )

    def test_lifecycle_cannot_consume_contracts_absent_from_canonical_position(self):
        reference = self.evidence()
        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "exceed canonical option position",
        ):
            self.authority.apply(reference)
        self.assertEqual(self.book.transactions, ())
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )

    def test_stale_facade_cannot_authorize_option_position_consumption(self):
        self.seed_option_position("1")
        stale_position = self.book.position(f"{OPTION_ID}@1")
        self.assertEqual(stale_position, Decimal("1"))

        competing_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        competing_book.append(
            book_equity_fill(
                transaction_id="competing-option-consume",
                cause_event_id="competing-option-consume-cause",
                instrument=f"{OPTION_ID}@1",
                settlement_currency="USD",
                side="SELL",
                quantity=Decimal("1"),
                price=Decimal("1"),
            )
        )
        self.assertEqual(competing_book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("1"))

        before_economic = tuple(
            self.store.load_events("economic_book", self.book.book_id)
        )
        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "exceed canonical option position",
        ):
            self.authority.apply(self.evidence(external_event_id="stale-cut-life"))

        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(
            tuple(self.store.load_events("economic_book", self.book.book_id)),
            before_economic,
        )

    def test_economic_writer_between_position_cut_and_prepare_fails_closed(self):
        self.seed_option_position("1")
        competing_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        original_prepare = DurableProviderEconomicBook.prepare_batch_mutation
        raced = False

        def racing_prepare(book, transactions, **kwargs):
            nonlocal raced
            if book is self.book and not raced:
                raced = True
                competing_book.append(
                    book_equity_fill(
                        transaction_id="racing-option-consume",
                        cause_event_id="racing-option-consume-cause",
                        instrument=f"{OPTION_ID}@1",
                        settlement_currency="USD",
                        side="SELL",
                        quantity=Decimal("1"),
                        price=Decimal("1"),
                    )
                )
            return original_prepare(book, transactions, **kwargs)

        with patch.object(
            DurableProviderEconomicBook,
            "prepare_batch_mutation",
            new=racing_prepare,
        ):
            with self.assertRaisesRegex(
                OptionLifecycleConflict,
                "changed after lifecycle position validation",
            ):
                self.authority.apply(
                    self.evidence(external_event_id="race-cut-life")
                )

        self.assertTrue(raced)
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        current = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        self.assertEqual(current.position(f"{OPTION_ID}@1"), Decimal("0"))

    def test_physical_exercise_is_exactly_once_across_restart(self):
        self.seed_option_position("1")
        observation = self.evidence()

        first = self.authority.apply(observation)
        self.assertTrue(first.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("100"))
        self.assertEqual(self.book.cash("USD"), Decimal("-5001"))
        self.assertEqual(len(self.book.transactions), 2)

        restarted_book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        restarted = self._authority(
            registry=self.registry,
            economic_book=restarted_book,
        )
        second = restarted.apply(observation)

        self.assertFalse(second.inserted)
        self.assertEqual(second.lifecycle_event_id, first.lifecycle_event_id)
        self.assertEqual(restarted_book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(restarted_book.position("ABC"), Decimal("100"))
        self.assertEqual(restarted_book.cash("USD"), Decimal("-5001"))
        self.assertEqual(len(restarted_book.transactions), 2)
        self.assertEqual(
            len(self.store.load_events("option_lifecycle", restarted.aggregate_id)),
            1,
        )

    def test_same_external_identity_with_changed_economics_conflicts(self):
        self.seed_option_position("1")
        first = self.evidence()
        self.authority.apply(first)

        changed = self.evidence(signed_contracts="2")
        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "reused with changed evidence",
        ):
            self.authority.apply(changed)

        self.assertEqual(self.book.position("ABC"), Decimal("100"))
        self.assertEqual(self.book.cash("USD"), Decimal("-5001"))
        self.assertEqual(len(self.book.transactions), 2)

    def test_scope_mismatch_fails_before_any_journal_mutation(self):
        with self.assertRaisesRegex(OptionLifecycleError, "scope mismatch"):
            self.authority.apply(self.evidence(provider_id="ALPACA"))

        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(
            self.store.load_events("economic_book", self.book.book_id),
            [],
        )

    def test_long_put_exercise_that_requires_borrow_fails_before_mutation(self):
        put_registry = InstrumentRegistry(
            versions=(option_version(option_right="PUT"),)
        )
        authority = self._authority(
            registry=put_registry,
            economic_book=self.book,
        )
        self.seed_option_position("1")
        before_transactions = tuple(self.book.transactions)

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "without atomic borrow authority",
        ):
            authority.apply(
                self.evidence(
                    external_event_id="put-exercise-needs-borrow",
                    event_kind="EXERCISE",
                    signed_contracts="1",
                )
            )

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("1"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("-1"))
        self.assertEqual(
            self.store.load_events("option_lifecycle", authority.aggregate_id),
            [],
        )

    def test_short_assignment_that_widens_underlying_short_fails_before_mutation(self):
        self.seed_option_position("-1")
        before_transactions = tuple(self.book.transactions)
        before_economic = tuple(
            self.store.load_events("economic_book", self.book.book_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "without atomic borrow authority",
        ):
            self.authority.apply(
                self.evidence(
                    event_kind="ASSIGNMENT",
                    signed_contracts="-1",
                )
            )

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(
            tuple(self.store.load_events("economic_book", self.book.book_id)),
            before_economic,
        )
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("-1"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("1"))

    def test_short_assignment_covered_by_owned_underlying_has_exact_obligations(self):
        self.seed_option_position("-1")
        self.seed_underlying_position("100")
        result = self.authority.apply(
            self.evidence(
                event_kind="ASSIGNMENT",
                signed_contracts="-1",
            )
        )

        self.assertTrue(result.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("4901"))

    def test_partially_covered_assignment_cannot_create_residual_short(self):
        self.seed_option_position("-1")
        self.seed_underlying_position("50")
        before_transactions = tuple(self.book.transactions)

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "without atomic borrow authority",
        ):
            self.authority.apply(
                self.evidence(
                    external_event_id="partial-cover-assignment",
                    event_kind="ASSIGNMENT",
                    signed_contracts="-1",
                )
            )

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("-1"))
        self.assertEqual(self.book.position("ABC"), Decimal("50"))
        self.assertEqual(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id),
            [],
        )

    def test_expiry_correction_to_covered_assignment_uses_current_inventory(self):
        self.seed_option_position("-1")
        self.seed_underlying_position("100")
        first = self.evidence(
            external_event_id="expiry-r1",
            event_kind="EXPIRY",
            signed_contracts="-1",
            provider_revision="provider-r1",
        )
        self.authority.apply(first)

        correction = self.evidence(
            external_event_id="assignment-after-expiry-r2",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
            observed_at=utc(12, 18, 19, 2),
            provider_revision="provider-r2",
            corrects_external_event_id="expiry-r1",
        )
        corrected = self.authority.apply(correction)

        self.assertTrue(corrected.inserted)
        self.assertEqual(len(corrected.reversal_transaction_ids), 1)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("4901"))

    def test_expiry_correction_to_uncovered_assignment_remains_fail_closed(self):
        self.seed_option_position("-1")
        first = self.evidence(
            external_event_id="expiry-uncovered-r1",
            event_kind="EXPIRY",
            signed_contracts="-1",
            provider_revision="provider-r1",
        )
        self.authority.apply(first)
        before_transactions = tuple(self.book.transactions)
        before_events = tuple(
            self.store.load_events("option_lifecycle", self.authority.aggregate_id)
        )

        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "without atomic borrow authority",
        ):
            self.authority.apply(
                self.evidence(
                    external_event_id="assignment-uncovered-r2",
                    event_kind="ASSIGNMENT",
                    signed_contracts="-1",
                    observed_at=utc(12, 18, 19, 2),
                    provider_revision="provider-r2",
                    corrects_external_event_id="expiry-uncovered-r1",
                )
            )

        self.assertEqual(tuple(self.book.transactions), before_transactions)
        self.assertEqual(
            tuple(self.store.load_events("option_lifecycle", self.authority.aggregate_id)),
            before_events,
        )
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))

    def test_correction_reusing_provider_revision_fails_closed(self):
        self.seed_option_position("-2")
        self.seed_underlying_position("200")
        first = self.evidence(
            external_event_id="assignment-r1",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
            provider_revision="provider-r1",
        )
        self.authority.apply(first)

        correction = self.evidence(
            external_event_id="assignment-r2",
            event_kind="ASSIGNMENT",
            signed_contracts="-2",
            observed_at=utc(12, 18, 19, 2),
            provider_revision="provider-r1",
            corrects_external_event_id="assignment-r1",
        )
        with self.assertRaisesRegex(
            OptionLifecycleConflict,
            "new provider revision",
        ):
            self.authority.apply(correction)

        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("-1"))
        self.assertEqual(self.book.position("ABC"), Decimal("100"))
        self.assertEqual(self.book.cash("USD"), Decimal("4802"))
        self.assertEqual(
            len(self.store.load_events("option_lifecycle", self.authority.aggregate_id)),
            1,
        )

    def test_correction_atomically_reverses_and_replaces_economics(self):
        self.seed_option_position("-2")
        self.seed_underlying_position("200")
        first = self.evidence(
            external_event_id="assignment-r1",
            event_kind="ASSIGNMENT",
            signed_contracts="-1",
        )
        first_result = self.authority.apply(first)
        original_id = first_result.active_transaction_ids[0]

        correction = self.evidence(
            external_event_id="assignment-r2",
            event_kind="ASSIGNMENT",
            signed_contracts="-2",
            observed_at=utc(12, 18, 19, 2),
            provider_revision="provider-r2",
            corrects_external_event_id="assignment-r1",
        )
        corrected = self.authority.apply(correction)

        self.assertTrue(corrected.inserted)
        self.assertEqual(len(corrected.reversal_transaction_ids), 1)
        self.assertEqual(len(corrected.active_transaction_ids), 1)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("9802"))
        self.assertEqual(len(self.book.transactions), 5)

        _option_seed, _underlying_seed, original, reversal, replacement = (
            self.book.transactions
        )
        self.assertEqual(original.transaction_id, original_id)
        self.assertEqual(reversal.reverses_transaction_id, original_id)
        self.assertEqual(replacement.corrects_transaction_id, original_id)
        self.assertEqual(reversal.observed_at, replacement.observed_at)
        self.assertEqual(
            original.economic_effective_at,
            replacement.economic_effective_at,
        )
        self.assertEqual(
            original.economic_order_key,
            replacement.economic_order_key,
        )

        restarted = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        self.assertEqual(restarted.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(restarted.position("ABC"), Decimal("0"))
        self.assertEqual(restarted.cash("USD"), Decimal("9802"))
        self.assertEqual(len(restarted.transactions), 5)

    def test_adjusted_deliverable_without_explicit_exercise_cash_fails_closed(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(deliverable_quantity="150"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "explicit canonical exercise cash evidence",
        ):
            authority.apply(self.evidence())

        self.assertEqual(
            self.store.load_events("option_lifecycle", authority.aggregate_id),
            [],
        )
        self.assertEqual(
            len(self.store.load_events("economic_book", book.book_id)),
            1,
        )

    def test_cash_expiry_uses_bound_final_settlement_not_generic_price(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(settlement_method="CASH", strike="100"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )

        result = authority.apply(
            self.evidence(
                event_kind="EXPIRY",
                # Deliberately inconsistent generic price: if this were used,
                # the call would be OTM and settle to zero. Financial authority
                # is the sealed final provider settlement amount instead.
                underlying_price="90",
                cash_settlement_amount="1200",
            )
        )

        self.assertTrue(result.inserted)
        self.assertEqual(book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(book.cash("USD"), Decimal("1199"))
        self.assertEqual(len(book.transactions), 2)

    def test_cash_settlement_without_final_provider_amount_fails_before_mutation(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(settlement_method="CASH", strike="100"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )
        before = tuple(book.transactions)

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "final cash_settlement_amount",
        ):
            authority.apply(
                self.evidence(
                    event_kind="EXPIRY",
                    underlying_price="112",
                    cash_settlement_amount=None,
                )
            )

        self.assertEqual(tuple(book.transactions), before)
        self.assertEqual(
            self.store.load_events("option_lifecycle", authority.aggregate_id),
            [],
        )

    def test_otm_cash_expiry_is_durable_lifecycle_fact_without_zero_transaction(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(settlement_method="CASH", strike="100"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )
        observation = self.evidence(
            event_kind="EXPIRY",
            underlying_price="112",
            cash_settlement_amount="0",
        )

        first = authority.apply(observation)
        second = authority.apply(observation)

        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(len(first.active_transaction_ids), 1)
        self.assertEqual(book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(len(book.transactions), 2)
        self.assertEqual(
            len(self.store.load_events("option_lifecycle", authority.aggregate_id)),
            1,
        )

    def test_physical_expiry_does_not_invent_exercise_or_assignment(self):
        self.seed_option_position("1")
        result = self.authority.apply(
            self.evidence(
                event_kind="EXPIRY",
                signed_contracts="1",
            )
        )

        self.assertTrue(result.inserted)
        self.assertEqual(len(result.active_transaction_ids), 1)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("0"))
        self.assertEqual(self.book.cash("USD"), Decimal("-1"))
        self.assertEqual(len(self.book.transactions), 2)

    def test_stale_instrument_version_is_rejected_at_event_time(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry()
        registry.add(option_version())
        registry.add(
            option_version(
                version=2,
                effective_from=utc(12, 18, 18),
                deliverable_quantity="100",
            )
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="TEST_PROVIDER",
            account_id="paper-1",
            environment="SIMULATION",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )

        with self.assertRaisesRegex(
            OptionLifecycleError,
            "not the version effective",
        ):
            authority.apply(self.evidence(instrument_version=f"{OPTION_ID}@1"))

        self.assertEqual(len(book.transactions), 1)

    def test_unsupported_lifecycle_event_fails_closed(self):
        with self.assertRaisesRegex(
            OptionLifecycleError,
            "unsupported option lifecycle event",
        ):
            self.authority.apply(self.evidence(event_kind="CASH_IN_LIEU"))


if __name__ == "__main__":
    unittest.main()
