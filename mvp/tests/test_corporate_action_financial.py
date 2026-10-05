from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    ScopedEconomicBook,
    book_equity_fill,
    project_equity_position,
)
from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.corporate_action_financial import (
    CorporateActionAdmissionError,
    CorporateActionSourceLocator,
    accept_corporate_action_observation,
    book_accepted_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateEvent, EquityState
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)


NOW = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
EFFECTIVE = datetime(2026, 10, 5, 10, 5, tzinfo=timezone.utc)
AS_OF = datetime(2026, 10, 5, 10, 10, tzinfo=timezone.utc)
INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"
INSTRUMENT_REF = f"{INSTRUMENT_ID}@1"
ACCOUNT_ID = "acct-1"


def instrument(*, provider_id="ALPACA"):
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id=provider_id,
        venue_id="NASDAQ",
        provider_symbol="AAA",
        asset_class="CASH_EQUITY",
        base_currency="AAA",
        quote_currency="USD",
        settlement_currency="USD",
        quantity_unit="AAA",
        contract_multiplier=Decimal("1"),
        price_tick=Decimal("0.01"),
        quantity_step=Decimal("1"),
        minimum_quantity=Decimal("1"),
        calendar_id="CONTINUOUS_24_7",
        timezone_id="UTC",
        effective_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="ACTIVE",
    )


def state(**overrides):
    values = dict(
        symbol="AAA",
        quantity="10",
        total_basis="1000",
        settled_cash="1000",
        unsettled_cash="0",
        currency="USD",
    )
    values.update(overrides)
    return EquityState.create(**values)


def event(
    *,
    event_id="ca-1",
    kind="CASH_DIVIDEND",
    source_revision="provider-r1",
    source_sequence=7,
    effective_at=EFFECTIVE,
    payload=None,
):
    if payload is None:
        payload = (
            {"per_share": "1.50", "currency": "USD"}
            if kind == "CASH_DIVIDEND"
            else {"numerator": "2", "denominator": "1"}
        )
    return CorporateEvent.create(
        event_id=event_id,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        kind=kind,
        effective_date=effective_at.date(),
        effective_at=effective_at,
        source_revision=source_revision,
        source_sequence=source_sequence,
        payload=payload,
    )


def claim(source, *, instrument_version=INSTRUMENT_REF, account_id=ACCOUNT_ID):
    observed = NOW - timedelta(minutes=1)
    suffix = {
        "DOCUMENTED": "1",
        "API": "2",
        "ACCOUNT": "3",
        "INSTRUMENT": "4",
    }[source]
    return CapabilityClaim(
        source=source,
        provider_id="ALPACA",
        account_id=account_id,
        entity_id="entity-1",
        environment="PAPER",
        instrument_version=instrument_version,
        observed_at=observed,
        expires_at=NOW + timedelta(hours=1),
        supported_order_types=frozenset({"LIMIT"}),
        time_in_force=frozenset({"DAY"}),
        permission_scopes=frozenset({"ACCOUNT.READ"}),
        position_mode="NET",
        native_protection=frozenset(),
        rate_limit_policy_id="alpaca-test-v1",
        data_entitlements=frozenset({"ACTIVITIES"}),
        evidence_ref={
            "artifact_id": f"33333333-3333-4333-8333-33333333333{suffix}",
            "sha256": "sha256:" + suffix * 64,
            "observed_at": observed.isoformat().replace("+00:00", "Z"),
        },
    )


def capability(*, instrument_version=INSTRUMENT_REF, account_id=ACCOUNT_ID):
    return derive_capability_snapshot(
        snapshot_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        claims=tuple(
            claim(
                source,
                instrument_version=instrument_version,
                account_id=account_id,
            )
            for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
        ),
        observed_at=NOW,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def dividend_payload(
    *,
    event_id="ca-1",
    revision="provider-r1",
    sequence=7,
    per_share="1.50",
    effective_at="2026-10-05T10:05:00Z",
):
    return (
        '{"activities":[{"id":"%s","revision":"%s","type":"cash_dividend",'
        '"effective_at":"%s","sequence":%d,"per_share":"%s","currency":"USD",'
        '"announcement_at":"2026-09-25T13:00:00Z","record_date":"2026-10-03",'
        '"ex_date":"2026-10-05","pay_date":"2026-10-08"}]}'
        % (event_id, revision, effective_at, sequence, per_share)
    ).encode("utf-8")


def split_payload(
    *,
    event_id="split-1",
    revision="split-r1",
    sequence=10,
    numerator="2",
    denominator="1",
):
    return (
        '{"activities":[{"id":"%s","revision":"%s","type":"split",'
        '"effective_at":"2026-10-05T10:05:00Z","sequence":%d,'
        '"numerator":"%s","denominator":"%s",'
        '"announcement_at":"2026-09-25T13:00:00Z"}]}'
        % (event_id, revision, sequence, numerator, denominator)
    ).encode("utf-8")


def observation(
    *,
    surface=Surface.ACTIVITIES,
    instrument_version=INSTRUMENT_REF,
    account_id=ACCOUNT_ID,
    response_bytes=None,
    observed_at=NOW + timedelta(seconds=1),
):
    if response_bytes is None:
        response_bytes = dividend_payload()
    binding = prepare_authenticated_read_query(
        capability=capability(
            instrument_version=instrument_version,
            account_id=account_id,
        ),
        surface=surface,
        endpoint="/v2/account/activities/corporate-actions",
        query={"symbol": "AAA"},
        at=NOW,
        permission_scope="ACCOUNT.READ",
    )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=response_bytes,
        observed_at=observed_at,
    )


def dividend_locator():
    root = ("activities", 0)
    return CorporateActionSourceLocator(
        event_id=root + ("id",),
        source_revision=root + ("revision",),
        kind=root + ("type",),
        effective_at=root + ("effective_at",),
        source_sequence=root + ("sequence",),
        payload={
            "per_share": root + ("per_share",),
            "currency": root + ("currency",),
        },
        announcement_at=root + ("announcement_at",),
        record_date=root + ("record_date",),
        ex_date=root + ("ex_date",),
        pay_date=root + ("pay_date",),
    )


def split_locator():
    root = ("activities", 0)
    return CorporateActionSourceLocator(
        event_id=root + ("id",),
        source_revision=root + ("revision",),
        kind=root + ("type",),
        effective_at=root + ("effective_at",),
        source_sequence=root + ("sequence",),
        payload={
            "numerator": root + ("numerator",),
            "denominator": root + ("denominator",),
        },
        announcement_at=root + ("announcement_at",),
    )


def accepted(
    *,
    corporate_event=None,
    provider_observation=None,
    source_locator=None,
    current_instrument=None,
    registry=None,
):
    current_instrument = current_instrument or instrument()
    registry = registry or InstrumentRegistry(versions=(current_instrument,))
    corporate_event = corporate_event or event()
    provider_observation = provider_observation or observation()
    source_locator = source_locator or dividend_locator()
    return accept_corporate_action_observation(
        observation=provider_observation,
        event=corporate_event,
        source_locator=source_locator,
        instrument_version=current_instrument,
        registry=registry,
    )


def seeded_book():
    book = ScopedEconomicBook(
        environment="PAPER",
        account_id=ACCOUNT_ID,
    )
    book.append(
        book_equity_fill(
            transaction_id="fill-1",
            cause_event_id="fill-event-1",
            instrument="AAA",
            settlement_currency="USD",
            side="BUY",
            quantity="10",
            price="100",
        )
    )
    return book


class CorporateActionFinancialTests(unittest.TestCase):
    def test_accepted_action_cannot_be_reconstructed_with_public_dataclass_replace(self):
        value = accepted()
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "authenticated provider evidence",
        ):
            replace(value, response_sha256="sha256:" + "0" * 64)

    def test_acceptance_binds_source_paths_and_exact_provider_evidence(self):
        obs = observation()
        value = accepted(provider_observation=obs)
        self.assertEqual(value.provider_id, "ALPACA")
        self.assertEqual(value.account_id, ACCOUNT_ID)
        self.assertEqual(value.environment, "PAPER")
        self.assertEqual(value.instrument_version_ref, INSTRUMENT_REF)
        self.assertEqual(value.query_digest, obs.query_binding.query_digest)
        self.assertEqual(value.response_sha256, obs.response_sha256)
        self.assertEqual(value.evidence_ref, obs.evidence_ref)
        self.assertEqual(value.observed_at, obs.observed_at)
        self.assertEqual(value.ex_date, date(2026, 10, 5))
        self.assertEqual(value.pay_date, date(2026, 10, 8))
        self.assertTrue(value.source_locator_digest.startswith("sha256:"))
        self.assertTrue(value.provenance_digest.startswith("sha256:"))

    def test_arbitrary_event_cannot_be_attached_to_unrelated_authenticated_bytes(self):
        unrelated = observation(
            response_bytes=dividend_payload(event_id="different-event")
        )
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "event_id does not match",
        ):
            accepted(provider_observation=unrelated)

    def test_changed_economic_field_must_exist_in_authenticated_payload(self):
        fabricated = event(payload={"per_share": "9.99", "currency": "USD"})
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "payload.per_share does not match",
        ):
            accepted(corporate_event=fabricated)

    def test_source_locator_must_cover_exact_normalized_payload(self):
        root = ("activities", 0)
        incomplete = CorporateActionSourceLocator(
            event_id=root + ("id",),
            source_revision=root + ("revision",),
            kind=root + ("type",),
            effective_at=root + ("effective_at",),
            source_sequence=root + ("sequence",),
            payload={"currency": root + ("currency",)},
            ex_date=root + ("ex_date",),
            pay_date=root + ("pay_date",),
        )
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "cover every normalized",
        ):
            accepted(source_locator=incomplete)

    def test_authenticated_non_activity_read_cannot_be_relabelled_as_corporate_action(self):
        obs = observation(surface=Surface.AUTHENTICATED_READ)
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "ACTIVITIES",
        ):
            accepted(provider_observation=obs)

    def test_observation_instrument_scope_must_match_registered_instrument(self):
        obs = observation(
            instrument_version="22222222-2222-4222-8222-222222222222@1"
        )
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "exact instrument version",
        ):
            accepted(provider_observation=obs)

    def test_source_revision_and_sequence_are_provider_bound(self):
        wrong_revision = event(source_revision="invented")
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "source_revision does not match",
        ):
            accepted(corporate_event=wrong_revision)

        wrong_sequence = event(source_sequence=8)
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "source_sequence does not match",
        ):
            accepted(corporate_event=wrong_sequence)

    def test_cash_dividend_requires_source_bound_ex_and_pay_dates(self):
        root = ("activities", 0)
        locator = CorporateActionSourceLocator(
            event_id=root + ("id",),
            source_revision=root + ("revision",),
            kind=root + ("type",),
            effective_at=root + ("effective_at",),
            source_sequence=root + ("sequence",),
            payload={
                "per_share": root + ("per_share",),
                "currency": root + ("currency",),
            },
        )
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "source-bound ex_date and pay_date",
        ):
            accepted(source_locator=locator)

    def test_future_effective_event_is_zero_mutation(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        value = accepted(current_instrument=current, registry=registry)
        book = seeded_book()
        before = book.transactions
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "future corporate action",
        ):
            book_accepted_corporate_action(
                accepted=value,
                state=state(),
                instrument_version=current,
                registry=registry,
                book=book,
                as_of=EFFECTIVE - timedelta(seconds=1),
            )
        self.assertEqual(book.transactions, before)

    def test_observation_after_cut_is_zero_mutation(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        obs = observation(observed_at=NOW + timedelta(minutes=30))
        value = accepted(
            provider_observation=obs,
            current_instrument=current,
            registry=registry,
        )
        book = seeded_book()
        before = book.transactions
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "before provider observation",
        ):
            book_accepted_corporate_action(
                accepted=value,
                state=state(),
                instrument_version=current,
                registry=registry,
                book=book,
                as_of=NOW + timedelta(minutes=20),
            )
        self.assertEqual(book.transactions, before)

    def test_split_posts_canonical_position_delta_without_changing_fifo_basis(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        split_event = event(
            event_id="split-1",
            kind="SPLIT",
            source_revision="split-r1",
            source_sequence=10,
        )
        value = accepted(
            corporate_event=split_event,
            provider_observation=observation(response_bytes=split_payload()),
            source_locator=split_locator(),
            current_instrument=current,
            registry=registry,
        )
        book = seeded_book()
        result = book_accepted_corporate_action(
            accepted=value,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        self.assertTrue(result.appended)
        self.assertEqual(book.position("AAA"), Decimal("20"))
        projection = project_equity_position(
            book._book,
            instrument="AAA",
            settlement_currency="USD",
        )
        self.assertEqual(projection.quantity, Decimal("20"))
        self.assertEqual(projection.open_cost_basis, Decimal("1000"))

    def test_cash_dividend_posts_unsettled_entitlement_and_income_once(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        value = accepted(current_instrument=current, registry=registry)
        book = seeded_book()
        first = book_accepted_corporate_action(
            accepted=value,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        self.assertTrue(first.appended)
        self.assertEqual(
            book.balance("UNSETTLED_CASH:USD", "USD"),
            Decimal("15.00"),
        )
        self.assertEqual(
            book.balance("CORPORATE_ACTION_INCOME:USD", "USD"),
            Decimal("-15.00"),
        )
        self.assertIn(
            value.provenance_digest.removeprefix("sha256:"),
            first.transaction.economic_order_key,
        )

        retry = book_accepted_corporate_action(
            accepted=value,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        self.assertFalse(retry.appended)
        self.assertEqual(
            book.balance("UNSETTLED_CASH:USD", "USD"),
            Decimal("15.00"),
        )

    def test_same_semantic_provider_event_from_later_response_is_exactly_once(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        action = event()
        first = accepted(
            corporate_event=action,
            provider_observation=observation(
                response_bytes=dividend_payload(),
                observed_at=NOW + timedelta(seconds=1),
            ),
            current_instrument=current,
            registry=registry,
        )
        later = accepted(
            corporate_event=action,
            provider_observation=observation(
                response_bytes=(
                    dividend_payload()[:-1]
                    + b',"next_page_token":"page-2"}'
                ),
                observed_at=NOW + timedelta(minutes=2),
            ),
            current_instrument=current,
            registry=registry,
        )
        self.assertEqual(first.semantic_digest, later.semantic_digest)
        self.assertNotEqual(first.provenance_digest, later.provenance_digest)

        book = seeded_book()
        first_result = book_accepted_corporate_action(
            accepted=first,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        later_result = book_accepted_corporate_action(
            accepted=later,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        self.assertTrue(first_result.appended)
        self.assertFalse(later_result.appended)
        self.assertEqual(first_result.transaction, later_result.transaction)

    def test_changed_revision_requires_explicit_correction_and_preserves_first_booking(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        first = accepted(
            current_instrument=current,
            registry=registry,
        )
        changed_event = event(
            source_revision="provider-r2",
            payload={"per_share": "2.00", "currency": "USD"},
        )
        changed = accepted(
            corporate_event=changed_event,
            provider_observation=observation(
                response_bytes=dividend_payload(
                    revision="provider-r2",
                    per_share="2.00",
                ),
                observed_at=NOW + timedelta(minutes=2),
            ),
            current_instrument=current,
            registry=registry,
        )
        self.assertNotEqual(first.semantic_digest, changed.semantic_digest)

        book = seeded_book()
        book_accepted_corporate_action(
            accepted=first,
            state=state(),
            instrument_version=current,
            registry=registry,
            book=book,
            as_of=AS_OF,
        )
        with self.assertRaisesRegex(
            AccountingConflict,
            "explicit reversal/replacement",
        ):
            book_accepted_corporate_action(
                accepted=changed,
                state=state(),
                instrument_version=current,
                registry=registry,
                book=book,
                as_of=AS_OF,
            )
        self.assertEqual(
            book.balance("UNSETTLED_CASH:USD", "USD"),
            Decimal("15.00"),
        )

    def test_account_or_environment_scope_mismatch_is_zero_mutation(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        value = accepted(current_instrument=current, registry=registry)
        for environment, account_id in (
            ("PAPER", "acct-2"),
            ("LIVE", ACCOUNT_ID),
        ):
            with self.subTest(environment=environment, account_id=account_id):
                wrong = ScopedEconomicBook(
                    environment=environment,
                    account_id=account_id,
                )
                with self.assertRaisesRegex(
                    CorporateActionAdmissionError,
                    "account/environment",
                ):
                    book_accepted_corporate_action(
                        accepted=value,
                        state=state(),
                        instrument_version=current,
                        registry=registry,
                        book=wrong,
                        as_of=AS_OF,
                    )
                self.assertEqual(wrong.transactions, ())

    def test_unsupported_merger_cash_fails_closed_before_accounting_mutation(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        merger_event = event(
            event_id="merger-1",
            kind="MERGER_CASH",
            source_revision="merger-r1",
            payload={"cash_per_share": "110", "currency": "USD"},
        )
        root = ("activities", 0)
        merger_locator = CorporateActionSourceLocator(
            event_id=root + ("id",),
            source_revision=root + ("revision",),
            kind=root + ("type",),
            effective_at=root + ("effective_at",),
            source_sequence=root + ("sequence",),
            payload={
                "cash_per_share": root + ("cash_per_share",),
                "currency": root + ("currency",),
            },
        )
        merger_bytes = (
            b'{"activities":[{"id":"merger-1","revision":"merger-r1",'
            b'"type":"merger_cash","effective_at":"2026-10-05T10:05:00Z",'
            b'"sequence":7,"cash_per_share":"110","currency":"USD"}]}'
        )
        value = accepted(
            corporate_event=merger_event,
            provider_observation=observation(response_bytes=merger_bytes),
            source_locator=merger_locator,
            current_instrument=current,
            registry=registry,
        )
        book = seeded_book()
        before = book.transactions
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "no canonical durable accounting semantics",
        ):
            book_accepted_corporate_action(
                accepted=value,
                state=state(),
                instrument_version=current,
                registry=registry,
                book=book,
                as_of=AS_OF,
            )
        self.assertEqual(book.transactions, before)

    def test_mutating_accepted_event_payload_after_admission_fails_before_booking(self):
        current = instrument()
        registry = InstrumentRegistry(versions=(current,))
        value = accepted(current_instrument=current, registry=registry)
        value.event.payload["per_share"] = "999"
        book = seeded_book()
        before = book.transactions
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "changed after provenance admission",
        ):
            book_accepted_corporate_action(
                accepted=value,
                state=state(),
                instrument_version=current,
                registry=registry,
                book=book,
                as_of=AS_OF,
            )
        self.assertEqual(book.transactions, before)

    def test_general_authenticated_or_adjusted_data_surface_cannot_enter_action_path(self):
        adjusted = (
            b'{"activities":[{"id":"ca-1","revision":"provider-r1",'
            b'"type":"cash_dividend","effective_at":"2026-10-05T10:05:00Z",'
            b'"sequence":7,"per_share":"1.50","currency":"USD",'
            b'"announcement_at":"2026-09-25T13:00:00Z","record_date":"2026-10-03",'
            b'"ex_date":"2026-10-05","pay_date":"2026-10-08","adjusted_close":"123"}]}'
        )
        obs = observation(
            surface=Surface.AUTHENTICATED_READ,
            response_bytes=adjusted,
        )
        with self.assertRaisesRegex(
            CorporateActionAdmissionError,
            "ACTIVITIES",
        ):
            accepted(provider_observation=obs)


if __name__ == "__main__":
    unittest.main()
