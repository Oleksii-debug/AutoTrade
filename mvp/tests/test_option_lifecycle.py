from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from tempfile import TemporaryDirectory
from typing import Mapping
import unittest

from mvp.autotrade_mvp.accounting import book_equity_fill
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
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


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
) -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=OPTION_ID,
        version=version,
        provider_id="BYBIT",
        venue_id="OPTIONS",
        provider_symbol="ABC-202612-C50",
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
        underlying_id=f"{UNDERLYING_ID}@1",
        expiry=utc(12, 18, 21),
        delivery_cutoff=utc(12, 18, 20),
        settlement_method=settlement_method,
        margin_model_id="option-margin-v1",
        strike=Decimal(strike),
        option_right="CALL",
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

    def _authority(self, *, registry, economic_book):
        def resolve(reference):
            return self._evidence[reference]

        return DurableOptionLifecycleAuthority(
            self.store,
            registry=registry,
            economic_book=economic_book,
            evidence_resolver=resolve,
            lifecycle_endpoints=frozenset({LIFECYCLE_ENDPOINT}),
            permission_scope=LIFECYCLE_SCOPE,
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
            "autotrade.option-lifecycle.sealed-json",
        )
        self.assertEqual(evidence["parser_version"], "1.0.0")
        self.assertRegex(
            evidence["parser_contract_digest"],
            r"^sha256:[0-9a-f]{64}$",
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
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
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

    def test_short_assignment_has_exact_opposite_obligations(self):
        self.seed_option_position("-1")
        result = self.authority.apply(
            self.evidence(
                event_kind="ASSIGNMENT",
                signed_contracts="-1",
            )
        )

        self.assertTrue(result.inserted)
        self.assertEqual(self.book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(self.book.position("ABC"), Decimal("-100"))
        self.assertEqual(self.book.cash("USD"), Decimal("5001"))

    def test_correction_reusing_provider_revision_fails_closed(self):
        self.seed_option_position("-2")
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
        self.assertEqual(self.book.position("ABC"), Decimal("-100"))
        # The -2 seed is itself canonical economics: selling two contracts at\n        # 1 USD credits 2 USD before the -1 assignment adds 5000 USD. Rejected\n        # correction evidence must leave that exact 5002 USD state unchanged.\n        self.assertEqual(self.book.cash("USD"), Decimal("5002"))\n        self.assertEqual(\n            len(self.store.load_events("option_lifecycle", self.authority.aggregate_id)),\n            1,\n        )

    def test_correction_atomically_reverses_and_replaces_economics(self):
        self.seed_option_position("-2")
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
        self.assertEqual(self.book.position("ABC"), Decimal("-200"))
        self.assertEqual(self.book.cash("USD"), Decimal("10002"))
        self.assertEqual(len(self.book.transactions), 4)

        _seed, original, reversal, replacement = self.book.transactions
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
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
        )
        self.assertEqual(restarted.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(restarted.position("ABC"), Decimal("-200"))
        self.assertEqual(restarted.cash("USD"), Decimal("10002"))
        self.assertEqual(len(restarted.transactions), 4)

    def test_adjusted_deliverable_without_explicit_exercise_cash_fails_closed(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(deliverable_quantity="150"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
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

    def test_cash_expiry_is_derived_from_bound_price_evidence(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(settlement_method="CASH", strike="100"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )

        result = authority.apply(
            self.evidence(
                event_kind="EXPIRY",
                underlying_price="112",
            )
        )

        self.assertTrue(result.inserted)
        self.assertEqual(book.position(f"{OPTION_ID}@1"), Decimal("0"))
        self.assertEqual(book.cash("USD"), Decimal("1199"))
        self.assertEqual(len(book.transactions), 2)

    def test_otm_cash_expiry_is_durable_lifecycle_fact_without_zero_transaction(self):
        self.seed_option_position("1")
        registry = InstrumentRegistry(
            versions=(option_version(settlement_method="CASH", strike="100"),)
        )
        book = DurableProviderEconomicBook(
            self.store,
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
        )
        authority = self._authority(
            registry=registry,
            economic_book=book,
        )
        observation = self.evidence(
            event_kind="EXPIRY",
            underlying_price="90",
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
            provider_id="BYBIT",
            account_id="paper-1",
            environment="PAPER",
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
