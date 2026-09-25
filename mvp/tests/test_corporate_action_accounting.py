from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import AccountingConflict
from mvp.autotrade_mvp.corporate_action_accounting import (
    ProviderCorporateActionEvidence,
    commit_provider_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateActionBook, CorporateEvent, EquityState
from mvp.autotrade_mvp.instruments import InstrumentRegistry, InstrumentVersion
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


INSTRUMENT_ID = "11111111-1111-4111-8111-111111111111"


def instrument() -> InstrumentVersion:
    return InstrumentVersion(
        instrument_id=INSTRUMENT_ID,
        version=1,
        provider_id="PROVIDER-A",
        venue_id="VENUE-A",
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


def corporate_book() -> CorporateActionBook:
    current = instrument()
    registry = InstrumentRegistry(versions=(current,))
    return CorporateActionBook(
        EquityState.create(
            symbol="AAA",
            quantity="10",
            total_basis="1000",
            settled_cash="1000",
            unsettled_cash="0",
            currency="USD",
        ),
        instrument_version=current,
        registry=registry,
    )


def economic_book(store: JournalStore) -> DurableProviderEconomicBook:
    return DurableProviderEconomicBook(
        store,
        provider_id="PROVIDER-A",
        account_id="acct-1",
        environment="PAPER",
    )


def evidence(
    *,
    revision="r1",
    supersedes_revision=None,
    per_share="1.50",
    raw_digest="sha256:" + ("a" * 64),
    complete=True,
    kind="CASH_DIVIDEND",
    payload=None,
    effective_at="2026-09-25T09:00:00Z",
    observed_at="2026-09-25T10:00:00Z",
):
    if payload is None:
        payload = (
            {"per_share": per_share, "currency": "USD"}
            if kind == "CASH_DIVIDEND"
            else {"numerator": "2", "denominator": "1"}
        )
    return ProviderCorporateActionEvidence(
        provider_id="PROVIDER-A",
        account_id="acct-1",
        environment="PAPER",
        external_action_id="action-1",
        revision=revision,
        supersedes_revision=supersedes_revision,
        instrument_id=INSTRUMENT_ID,
        instrument_version=1,
        kind=kind,
        effective_at=effective_at,
        observed_at=observed_at,
        payload=payload,
        raw_evidence_sha256=raw_digest,
        evidence_refs=("provider-response:action-1",),
        source_sequence=1,
        ex_at=effective_at if kind == "CASH_DIVIDEND" else None,
        pay_at="2026-09-26T09:00:00Z" if kind == "CASH_DIVIDEND" else None,
        announcement_at="2026-09-20T09:00:00Z",
        record_at="2026-09-24T09:00:00Z",
        complete=complete,
    )


class CorporateActionAuthorityTests(unittest.TestCase):
    def test_provider_dividend_evidence_and_economics_commit_atomically(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            pure = corporate_book()

            result = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )

            self.assertTrue(result.inserted)
            self.assertEqual(result.next_state.unsettled_cash, Decimal("15.00"))
            self.assertEqual(len(result.transaction_ids), 1)
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USD", "USD"),
                Decimal("15.00"),
            )
            self.assertEqual(
                economics.balance("CORPORATE_ACTION_INCOME:USD", "USD"),
                Decimal("-15.00"),
            )
            source = store.load_events(
                "corporate_action_source",
                next(
                    row["aggregate_id"]
                    for row in store.load_events_after_journal_sequence(0, limit=100)
                    if row["event_type"] == "CorporateActionEvidenceAccepted"
                ),
            )
            self.assertEqual(len(source), 1)
            self.assertEqual(source[0]["payload"]["raw_evidence_sha256"], "sha256:" + ("a" * 64))

    def test_exact_retry_after_restart_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            first_store = JournalStore(path)
            first_book = economic_book(first_store)
            pure = corporate_book()
            first = commit_provider_corporate_action(
                store=first_store,
                economic_book=first_book,
                corporate_book=pure,
                evidence=evidence(),
            )
            self.assertTrue(first.inserted)

            reopened = JournalStore(path)
            restarted_book = economic_book(reopened)
            retry = commit_provider_corporate_action(
                store=reopened,
                economic_book=restarted_book,
                corporate_book=pure,
                evidence=evidence(),
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(restarted_book.transactions), 1)
            self.assertEqual(
                restarted_book.balance("UNSETTLED_CASH:USD", "USD"),
                Decimal("15.00"),
            )

    def test_same_revision_with_changed_raw_evidence_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            pure = corporate_book()
            commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "revision identity conflicts",
            ):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=economics,
                    corporate_book=pure,
                    evidence=evidence(raw_digest="sha256:" + ("b" * 64)),
                )
            self.assertEqual(len(economics.transactions), 1)

    def test_revision_requires_explicit_latest_supersession(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            pure = corporate_book()
            first = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )
            pure.apply(first.event)

            with self.assertRaisesRegex(
                AccountingConflict,
                "explicitly supersede",
            ):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=economics,
                    corporate_book=pure,
                    evidence=evidence(
                        revision="r2",
                        per_share="2",
                        raw_digest="sha256:" + ("b" * 64),
                        observed_at="2026-09-25T11:00:00Z",
                    ),
                )

    def test_dividend_revision_atomically_reverses_and_replaces_economics(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = corporate_book()
            first = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )
            pure.apply(first.event)

            corrected = evidence(
                revision="r2",
                supersedes_revision="r1",
                per_share="2",
                raw_digest="sha256:" + ("b" * 64),
                observed_at="2026-09-25T11:00:00Z",
            )
            result = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=corrected,
            )

            self.assertTrue(result.inserted)
            self.assertEqual(result.next_state.unsettled_cash, Decimal("20"))
            self.assertEqual(len(result.transaction_ids), 2)
            self.assertEqual(len(economics.transactions), 3)
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USD", "USD"),
                Decimal("20.00"),
            )
            self.assertEqual(
                economics.balance("CORPORATE_ACTION_INCOME:USD", "USD"),
                Decimal("-20.00"),
            )
            replacement = economics.transactions[-1]
            self.assertIsNotNone(replacement.corrects_transaction_id)
            self.assertEqual(
                economics.transactions[-2].reverses_transaction_id,
                replacement.corrects_transaction_id,
            )

            reopened = JournalStore(path)
            restarted = economic_book(reopened)
            self.assertEqual(
                restarted.balance("UNSETTLED_CASH:USD", "USD"),
                Decimal("20.00"),
            )
            self.assertEqual(len(restarted.transactions), 3)

    def test_corrected_revision_exact_retry_does_not_reverse_twice(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = corporate_book()
            first = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )
            pure.apply(first.event)
            corrected = evidence(
                revision="r2",
                supersedes_revision="r1",
                per_share="2",
                raw_digest="sha256:" + ("b" * 64),
                observed_at="2026-09-25T11:00:00Z",
            )
            result = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=corrected,
            )
            pure = CorporateActionBook.replay(
                result.transition.before,
                instrument_version=instrument(),
                registry=InstrumentRegistry(versions=(instrument(),)),
                events=(result.event,),
            ) if False else pure

            reopened = JournalStore(path)
            retry_book = economic_book(reopened)
            retry = commit_provider_corporate_action(
                store=reopened,
                economic_book=retry_book,
                corporate_book=pure,
                evidence=corrected,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(retry_book.transactions), 3)
            self.assertEqual(
                retry_book.balance("UNSETTLED_CASH:USD", "USD"),
                Decimal("20.00"),
            )

    def test_changed_effective_time_revision_fails_closed(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            pure = corporate_book()
            first = commit_provider_corporate_action(
                store=store,
                economic_book=economics,
                corporate_book=pure,
                evidence=evidence(),
            )
            pure.apply(first.event)
            with self.assertRaisesRegex(
                AccountingConflict,
                "cannot change economic effective time",
            ):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=economics,
                    corporate_book=pure,
                    evidence=evidence(
                        revision="r2",
                        supersedes_revision="r1",
                        raw_digest="sha256:" + ("b" * 64),
                        effective_at="2026-09-25T09:30:00Z",
                        observed_at="2026-09-25T11:00:00Z",
                    ),
                )

    def test_unsupported_split_mapping_fails_before_durable_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            with self.assertRaisesRegex(
                AccountingConflict,
                "no qualified durable corporate-action accounting mapping",
            ):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=economics,
                    corporate_book=corporate_book(),
                    evidence=evidence(kind="SPLIT"),
                )
            self.assertEqual(economics.transactions, ())
            self.assertEqual(
                [
                    event
                    for event in store.load_events_after_journal_sequence(0, limit=100)
                    if event["event_type"] == "CorporateActionEvidenceAccepted"
                ],
                [],
            )

    def test_incomplete_or_unhashed_evidence_is_rejected_at_boundary(self):
        with self.assertRaisesRegex(ValueError, "incomplete"):
            evidence(complete=False)
        with self.assertRaisesRegex(ValueError, "sha256"):
            evidence(raw_digest="provider-response")

    def test_caller_authored_corporate_event_is_not_accepted_as_authority(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            event = CorporateEvent.create(
                event_id="locally-authored",
                instrument_id=INSTRUMENT_ID,
                instrument_version=1,
                kind="CASH_DIVIDEND",
                effective_date=datetime(2026, 9, 25, tzinfo=timezone.utc).date(),
                effective_at=datetime(2026, 9, 25, 9, tzinfo=timezone.utc),
                source_revision="fake",
                payload={"per_share": "100", "currency": "USD"},
            )
            with self.assertRaisesRegex(TypeError, "ProviderCorporateActionEvidence"):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=economic_book(store),
                    corporate_book=corporate_book(),
                    evidence=event,
                )

    def test_scope_mismatch_fails_before_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            wrong = DurableProviderEconomicBook(
                store,
                provider_id="PROVIDER-A",
                account_id="other",
                environment="PAPER",
            )
            with self.assertRaisesRegex(ValueError, "scope"):
                commit_provider_corporate_action(
                    store=store,
                    economic_book=wrong,
                    corporate_book=corporate_book(),
                    evidence=evidence(),
                )
            self.assertEqual(wrong.transactions, ())

    def test_failure_before_atomic_commit_leaves_source_and_economics_absent(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            original = store.commit_command

            def fail(**kwargs):
                raise RuntimeError("injected corporate action failure")

            store.commit_command = fail
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    commit_provider_corporate_action(
                        store=store,
                        economic_book=economics,
                        corporate_book=corporate_book(),
                        evidence=evidence(),
                    )
            finally:
                store.commit_command = original

            reopened = JournalStore(Path(directory) / "journal.sqlite3")
            self.assertEqual(economic_book(reopened).transactions, ())
            self.assertEqual(
                [
                    event
                    for event in reopened.load_events_after_journal_sequence(0, limit=100)
                    if event["event_type"] == "CorporateActionEvidenceAccepted"
                ],
                [],
            )


if __name__ == "__main__":
    unittest.main()
