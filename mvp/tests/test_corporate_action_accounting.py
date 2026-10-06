from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import mvp.autotrade_mvp.corporate_action_accounting as corporate_action_accounting_module
from mvp.autotrade_mvp.accounting import AccountingConflict, book_equity_fill
from mvp.autotrade_mvp.corporate_action_accounting import (
    commit_authoritative_corporate_action,
    corporate_action_reconciliation_inputs,
)
from mvp.autotrade_mvp.corporate_action_evidence import (
    CorporateActionEvidenceError,
    CorporateActionObservation,
    DurableCorporateActionEvidenceStore,
    resolve_authoritative_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateActionBook, EquityState
from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.reconciliation import (
    CoverageSurfaceEvidence,
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_corporate_action_evidence import (
    ENDPOINT,
    INSTRUMENT_ID,
    canonical_instrument,
    simulation_read_capability,
)
from mvp.tests.test_provider_transport import READ_NOW


def sealed_action(
    *,
    external_event_id="corp-1",
    revision="1",
    kind="CASH_DIVIDEND",
    per_share="1.25",
    numerator="2",
    denominator="1",
    cash_per_share="100",
    observed_offset=2,
    effective_offset=1,
    corrects=None,
):
    binding = prepare_authenticated_read_query(
        capability=simulation_read_capability(),
        surface=Surface.ACTIVITIES,
        endpoint=ENDPOINT,
        query={"symbol": "BTCUSDT"},
        at=READ_NOW,
        permission_scope="ORDER.READ",
    )
    payload = {
        "external_event_id": external_event_id,
        "provider_revision": revision,
        "instrument_id": INSTRUMENT_ID,
        "instrument_version": 1,
        "effective_at": (
            READ_NOW + timedelta(seconds=effective_offset)
        ).isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "source_sequence": 7,
        "complete": True,
    }
    if corrects is not None:
        payload["corrects_external_event_id"] = corrects
    if kind == "CASH_DIVIDEND":
        payload.update({"per_share": per_share, "currency": "USDT"})
    elif kind == "SPLIT":
        payload.update(
            {"numerator": numerator, "denominator": denominator}
        )
    elif kind in {"MERGER_CASH", "DELIST"}:
        payload.update(
            {"cash_per_share": cash_per_share, "currency": "USDT"}
        )
    return observe_authenticated_json_response(
        query_binding=binding,
        http_status=200,
        response_bytes=json.dumps(
            payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        observed_at=READ_NOW + timedelta(seconds=observed_offset),
    )


def resolve_action(source, *, corrects=None):
    if corrects is not None and source.payload.get(
        "corrects_external_event_id"
    ) != corrects:
        raise ValueError(
            "correction identity must be present in sealed provider evidence"
        )
    current = canonical_instrument()
    return resolve_authoritative_corporate_action(
        source.evidence_ref,
        evidence_resolver={source.evidence_ref: source}.__getitem__,
        instrument_registry=InstrumentRegistry(versions=(current,)),
        expected_provider_id="BINANCE",
        expected_account_id="acct-1",
        expected_environment="SIMULATION",
        allowed_endpoints=frozenset({ENDPOINT}),
        permission_scope="ORDER.READ",
    )


def pure_book(
    *,
    quantity="10",
    borrowed_quantity="0",
    recalled_quantity="0",
):
    current = canonical_instrument()
    return CorporateActionBook(
        EquityState.create(
            symbol="BTCUSDT",
            quantity=quantity,
            total_basis="1000",
            settled_cash="1000",
            unsettled_cash="0",
            currency="USDT",
            borrowed_quantity=borrowed_quantity,
            recalled_quantity=recalled_quantity,
        ),
        instrument_version=current,
        registry=InstrumentRegistry(versions=(current,)),
    )


def economic_book(store):
    book = DurableProviderEconomicBook(
        store,
        provider_id="BINANCE",
        account_id="acct-1",
        environment="SIMULATION",
    )
    has_position_seed = any(
        any(
            posting.ledger_account == "POSITION:BTCUSDT"
            and posting.asset_or_currency == "BTCUSDT"
            for posting in transaction.postings
        )
        for transaction in book.transactions
    )
    if not has_position_seed:
        book.append(
            book_equity_fill(
                transaction_id="canonical-equity-position-seed",
                cause_event_id="provider-fill:canonical-equity-position-seed",
                instrument="BTCUSDT",
                settlement_currency="USDT",
                side="BUY",
                quantity="10",
                price="100",
                economic_effective_at=(
                    READ_NOW - timedelta(minutes=2)
                ).isoformat().replace("+00:00", "Z"),
                economic_order_key="provider:BINANCE:execution:position-seed",
                observed_at=(
                    READ_NOW - timedelta(minutes=1)
                ).isoformat().replace("+00:00", "Z"),
            )
        )
    return book


def evidence_store(store):
    return DurableCorporateActionEvidenceStore(
        store,
        provider_id="BINANCE",
        account_id="acct-1",
        environment="SIMULATION",
    )


class AtomicCorporateActionFinancialTests(unittest.TestCase):
    def test_exact_forged_action_cannot_enter_atomic_financial_composition(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            issued = resolve_action(sealed_action())
            forged = type(issued)(**issued.__dict__)

            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "lacks canonical resolver issuance authority",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure_book(),
                    accepted=forged,
                )

            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            self.assertEqual(len(economics.transactions), 1)

    def test_financial_collaborator_subclasses_are_rejected_before_dispatch(self):
        calls = []

        class HostileEconomicBook(DurableProviderEconomicBook):
            def __getattribute__(self, name):
                calls.append(("economic", name))
                raise AssertionError("economic subclass dispatched")

        class HostileCorporateBook(CorporateActionBook):
            def __getattribute__(self, name):
                calls.append(("corporate", name))
                raise AssertionError("corporate subclass dispatched")

        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())

            with self.assertRaisesRegex(
                TypeError,
                "exact DurableProviderEconomicBook",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=object.__new__(HostileEconomicBook),
                    corporate_book=pure_book(),
                    accepted=accepted,
                )
            self.assertEqual(calls, [])

            economics = economic_book(store)
            with self.assertRaisesRegex(TypeError, "exact CorporateActionBook"):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=object.__new__(HostileCorporateBook),
                    accepted=accepted,
                )
            self.assertEqual(calls, [])

    def test_sealed_dividend_source_and_economics_commit_together(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            accepted = resolve_action(sealed_action())
            durable_evidence = evidence_store(store)
            economics = economic_book(store)

            result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )

            self.assertTrue(result.inserted)
            self.assertEqual(result.next_state.unsettled_cash, Decimal("12.50"))
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("12.50"),
            )
            self.assertEqual(
                economics.balance("CORPORATE_ACTION_INCOME:USDT", "USDT"),
                Decimal("-12.50"),
            )
            self.assertEqual(
                len(
                    store.load_events(
                        "corporate_action_evidence",
                        durable_evidence.aggregate_id,
                    )
                ),
                1,
            )
            self.assertEqual(len(economics.transactions), 2)

    def test_prepared_evidence_does_not_mutate_until_shared_commit(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())
            plan = durable_evidence.prepare_record_mutation(accepted)
            self.assertFalse(plan.already_committed)
            self.assertIsNotNone(plan.envelope)
            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )

    def test_precommit_failure_leaves_neither_source_nor_economics(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action())
            original = JournalStore.commit_command

            def fail(selected_store, **kwargs):
                if kwargs.get("actor") == "corporate-action-financial-integration":
                    raise RuntimeError("injected atomic corporate action failure")
                return original(selected_store, **kwargs)

            JournalStore.commit_command = fail
            try:
                with self.assertRaisesRegex(RuntimeError, "injected"):
                    commit_authoritative_corporate_action(
                        store=store,
                        evidence_store=durable_evidence,
                        economic_book=economics,
                        corporate_book=pure_book(),
                        accepted=accepted,
                    )
            finally:
                JournalStore.commit_command = original

            reopened = JournalStore(path)
            self.assertEqual(
                JournalStore.load_events(
                    reopened,
                    "corporate_action_evidence",
                    evidence_store(reopened).aggregate_id,
                ),
                [],
            )
            self.assertEqual(len(economic_book(reopened).transactions), 1)

    def test_exact_restart_retry_is_idempotent(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            accepted = resolve_action(sealed_action())
            first = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economic_book(store),
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertTrue(first.inserted)

            reopened = JournalStore(path)
            economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(economics.transactions), 2)
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("12.50"),
            )

    def test_fresh_provider_revision_reverses_and_replaces_dividend(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = pure_book()
            first = resolve_action(sealed_action())
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=first,
            )
            pure.apply(first_result.accepted_event)

            corrected = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    per_share="2.00",
                    observed_offset=4,
                    corrects="corp-1",
                ),
                corrects="corp-1",
            )
            result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=corrected,
            )

            self.assertTrue(result.inserted)
            self.assertEqual(result.next_state.unsettled_cash, Decimal("20.00"))
            self.assertEqual(len(result.transaction_ids), 2)
            self.assertEqual(len(economics.transactions), 4)
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("20.00"),
            )
            reversal = economics.transactions[-2]
            replacement = economics.transactions[-1]
            self.assertEqual(
                reversal.reverses_transaction_id,
                replacement.corrects_transaction_id,
            )

            reopened = JournalStore(path)
            restarted = economic_book(reopened)
            self.assertEqual(len(restarted.transactions), 4)
            self.assertEqual(
                restarted.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("20.00"),
            )

    def test_correction_exact_retry_does_not_reverse_twice(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            economics = economic_book(store)
            pure = pure_book()
            first = resolve_action(sealed_action())
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=first,
            )
            pure.apply(first_result.accepted_event)
            corrected = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    per_share="2.00",
                    observed_offset=4,
                    corrects="corp-1",
                ),
                corrects="corp-1",
            )
            commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=corrected,
            )

            reopened = JournalStore(path)
            retry_economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=retry_economics,
                corporate_book=pure,
                accepted=corrected,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(retry_economics.transactions), 4)
            self.assertEqual(
                retry_economics.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("20.00"),
            )

    def test_correction_cannot_move_economic_effective_time(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            economics = economic_book(store)
            pure = pure_book()
            first = resolve_action(sealed_action())
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economics,
                corporate_book=pure,
                accepted=first,
            )
            pure.apply(first_result.accepted_event)
            moved = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    per_share="2",
                    effective_offset=3,
                    observed_offset=4,
                    corrects="corp-1",
                ),
                corrects="corp-1",
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "cannot change economic effective time",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=evidence_store(store),
                    economic_book=economics,
                    corporate_book=pure,
                    accepted=moved,
                )

    def test_pre_effective_announcement_is_retained_then_activates_exactly_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            announced = resolve_action(
                sealed_action(
                    observed_offset=1,
                    effective_offset=5,
                )
            )
            pending = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=announced,
            )
            self.assertTrue(pending.inserted)
            self.assertFalse(pending.economically_active)
            self.assertEqual(
                len(
                    store.load_events(
                        "corporate_action_evidence",
                        durable_evidence.aggregate_id,
                    )
                ),
                1,
            )
            self.assertEqual(len(economics.transactions), 1)
            self.assertEqual(
                economics.balance("UNSETTLED_CASH:USDT", "USDT"),
                Decimal("0"),
            )

            reopened = JournalStore(path)
            restarted_economics = economic_book(reopened)
            activated = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted_economics,
                corporate_book=pure_book(),
                accepted=announced,
                activation_at=announced.event.effective_at,
            )
            self.assertTrue(activated.economically_active)
            self.assertEqual(len(restarted_economics.transactions), 2)
            self.assertEqual(
                restarted_economics.balance(
                    "UNSETTLED_CASH:USDT",
                    "USDT",
                ),
                Decimal("12.50"),
            )
            self.assertEqual(
                restarted_economics.transactions[-1].observed_at,
                announced.event.effective_at.isoformat().replace("+00:00", "Z"),
            )

            exact_retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted_economics,
                corporate_book=pure_book(),
                accepted=announced,
                activation_at=announced.event.effective_at,
            )
            self.assertFalse(exact_retry.inserted)
            self.assertEqual(len(restarted_economics.transactions), 2)

    def test_split_books_canonical_quantity_adjustment_and_restarts_idempotently(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            split = resolve_action(
                sealed_action(
                    kind="SPLIT",
                    numerator="2",
                    denominator="1",
                )
            )

            applied = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=split,
            )
            self.assertTrue(applied.inserted)
            self.assertTrue(applied.economically_active)
            self.assertEqual(applied.next_state.quantity, Decimal("20"))
            self.assertEqual(applied.next_state.total_basis, Decimal("1000"))
            self.assertEqual(applied.next_state.unsettled_cash, Decimal("0"))
            self.assertEqual(
                economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("20"),
            )
            self.assertEqual(len(economics.transactions), 2)
            split_transaction = economics.transactions[-1]
            self.assertEqual(
                split_transaction.economic_effective_at,
                split.event.effective_at.isoformat().replace("+00:00", "Z"),
            )
            self.assertTrue(
                any(
                    posting.ledger_account.startswith(
                        "CORPORATE_ACTION_SPLIT_CLEARING:BTCUSDT:2:1"
                    )
                    for posting in split_transaction.postings
                )
            )

            reopened = JournalStore(path)
            restarted_economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted_economics,
                corporate_book=pure_book(),
                accepted=split,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(
                restarted_economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("20"),
            )
            self.assertEqual(len(restarted_economics.transactions), 2)

    def test_split_revision_reverses_original_and_replaces_same_economic_cut(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            first = resolve_action(
                sealed_action(
                    external_event_id="corp-split-1",
                    revision="1",
                    kind="SPLIT",
                    numerator="2",
                    denominator="1",
                )
            )
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=first,
            )
            self.assertEqual(first_result.next_state.quantity, Decimal("20"))

            current_book = pure_book()
            current_book.apply(first.event)
            correction = resolve_action(
                sealed_action(
                    external_event_id="corp-split-2",
                    revision="2",
                    kind="SPLIT",
                    numerator="3",
                    denominator="1",
                    observed_offset=4,
                    corrects="corp-split-1",
                ),
                corrects="corp-split-1",
            )
            corrected = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=current_book,
                accepted=correction,
            )
            self.assertEqual(corrected.next_state.quantity, Decimal("30"))
            self.assertEqual(corrected.next_state.total_basis, Decimal("1000"))
            self.assertEqual(
                economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("30"),
            )
            self.assertEqual(len(economics.transactions), 4)
            original, reversal, replacement = economics.transactions[-3:]
            self.assertEqual(reversal.reverses_transaction_id, original.transaction_id)
            self.assertEqual(
                replacement.corrects_transaction_id,
                original.transaction_id,
            )
            self.assertEqual(
                replacement.economic_effective_at,
                original.economic_effective_at,
            )
            self.assertEqual(replacement.observed_at, reversal.observed_at)

            retry = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=current_book,
                accepted=correction,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(economics.transactions), 4)

    def test_split_revision_to_one_to_one_reverses_original_without_replacement(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            first = resolve_action(
                sealed_action(
                    external_event_id="corp-split-cancelled-1",
                    revision="1",
                    kind="SPLIT",
                    numerator="2",
                    denominator="1",
                )
            )
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=first,
            )
            self.assertEqual(first_result.next_state.quantity, Decimal("20"))
            self.assertEqual(
                economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("20"),
            )

            current_book = pure_book()
            current_book.apply(first.event)
            correction = resolve_action(
                sealed_action(
                    external_event_id="corp-split-cancelled-2",
                    revision="2",
                    kind="SPLIT",
                    numerator="1",
                    denominator="1",
                    observed_offset=4,
                    corrects="corp-split-cancelled-1",
                ),
                corrects="corp-split-cancelled-1",
            )
            corrected = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=current_book,
                accepted=correction,
            )
            self.assertTrue(corrected.inserted)
            self.assertTrue(corrected.economically_active)
            self.assertEqual(corrected.next_state.quantity, Decimal("10"))
            self.assertEqual(corrected.next_state.total_basis, Decimal("1000"))
            self.assertEqual(
                economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("10"),
            )
            self.assertEqual(len(economics.transactions), 3)
            original = economics.transactions[-2]
            reversal = economics.transactions[-1]
            self.assertEqual(
                reversal.reverses_transaction_id,
                original.transaction_id,
            )
            self.assertFalse(
                any(
                    item.corrects_transaction_id == original.transaction_id
                    for item in economics.transactions
                )
            )

            reopened = JournalStore(path)
            restarted_economics = economic_book(reopened)
            retry_book = pure_book()
            retry_book.apply(first.event)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted_economics,
                corporate_book=retry_book,
                accepted=correction,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(
                restarted_economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("10"),
            )
            self.assertEqual(len(restarted_economics.transactions), 3)

    def test_short_split_fails_closed_until_borrow_obligation_mapping_is_qualified(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = DurableProviderEconomicBook(
                store,
                provider_id="BINANCE",
                account_id="acct-1",
                environment="SIMULATION",
            )
            economics.append(
                book_equity_fill(
                    transaction_id="canonical-short-position-seed",
                    cause_event_id="provider-fill:canonical-short-position-seed",
                    instrument="BTCUSDT",
                    settlement_currency="USDT",
                    side="SELL",
                    quantity="10",
                    price="100",
                    economic_effective_at=(
                        READ_NOW - timedelta(minutes=2)
                    ).isoformat().replace("+00:00", "Z"),
                    economic_order_key="provider:BINANCE:execution:short-seed",
                    observed_at=(
                        READ_NOW - timedelta(minutes=1)
                    ).isoformat().replace("+00:00", "Z"),
                )
            )
            split = resolve_action(
                sealed_action(
                    kind="SPLIT",
                    numerator="2",
                    denominator="1",
                )
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "borrowed/short positions is not qualified",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure_book(
                        quantity="-10",
                        borrowed_quantity="10",
                    ),
                    accepted=split,
                )
            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            self.assertEqual(
                economics.balance("POSITION:BTCUSDT", "BTCUSDT"),
                Decimal("-10"),
            )
            self.assertEqual(len(economics.transactions), 1)

    def test_non_integer_split_ratio_fails_before_source_or_financial_mutation(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            split = resolve_action(
                sealed_action(
                    kind="SPLIT",
                    numerator="1.5",
                    denominator="1",
                )
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "canonical durable quantity adjustment",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure_book(),
                    accepted=split,
                )
            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            self.assertEqual(len(economics.transactions), 1)

    def test_unsupported_merger_has_no_source_or_financial_side_effect(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            merger = resolve_action(
                sealed_action(kind="MERGER_CASH"),
            )
            with self.assertRaisesRegex(
                AccountingConflict,
                "no qualified durable corporate-action accounting mapping",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure_book(),
                    accepted=merger,
                )
            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            self.assertEqual(len(economics.transactions), 1)

    def test_retained_evidence_can_activate_without_rewriting_source_event(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())
            retained = durable_evidence.record(accepted)
            self.assertTrue(retained.inserted)
            economics = economic_book(store)

            activated = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertTrue(activated.economically_active)
            self.assertEqual(
                len(
                    store.load_events(
                        "corporate_action_evidence",
                        durable_evidence.aggregate_id,
                    )
                ),
                1,
            )
            self.assertEqual(len(economics.transactions), 2)

    def test_caller_quantity_cannot_override_canonical_entitlement_position(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action())

            with self.assertRaisesRegex(
                AccountingConflict,
                "canonical durable position",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure_book(quantity="9"),
                    accepted=accepted,
                )

            self.assertEqual(
                store.load_events(
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            self.assertEqual(len(economics.transactions), 1)

    def test_journal_advance_after_entitlement_proof_fails_closed(self):
        from mvp.autotrade_mvp import corporate_action_accounting as accounting_module

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action())
            original = accounting_module._canonical_entitlement_position_proof
            injected = False

            def prove_then_advance(*args, **kwargs):
                nonlocal injected
                proof = original(*args, **kwargs)
                if not injected:
                    injected = True
                    store.append_event(
                        {
                            "event_id": "cas-interference-1",
                            "event_type": "QualificationInterference",
                            "aggregate_type": "qualification_interference",
                            "aggregate_id": "qualification-interference:1",
                            "aggregate_version": "1",
                            "committed_at": accepted.observed_at,
                            "payload": {"kind": "UNRELATED_JOURNAL_ADVANCE"},
                            "payload_hash": payload_digest(
                                {"kind": "UNRELATED_JOURNAL_ADVANCE"}
                            ),
                        }
                    )
                return proof

            accounting_module._canonical_entitlement_position_proof = prove_then_advance
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "journal sequence changed after financial evidence validation",
                ):
                    commit_authoritative_corporate_action(
                        store=store,
                        evidence_store=durable_evidence,
                        economic_book=economics,
                        corporate_book=pure_book(),
                        accepted=accepted,
                    )
            finally:
                accounting_module._canonical_entitlement_position_proof = original

            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            economics.refresh()
            self.assertEqual(len(economics.transactions), 1)

            retry = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertTrue(retry.inserted)
            self.assertEqual(len(economics.transactions), 2)

    def test_post_planning_journal_shadow_fails_before_financial_mutation(self):
        from mvp.autotrade_mvp import corporate_action_accounting as accounting_module

        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action())
            original = accounting_module._economic_transactions
            injected = False

            def plan_then_shadow(*args, **kwargs):
                nonlocal injected
                transactions = original(*args, **kwargs)
                if not injected:
                    injected = True
                    store.commit_command = lambda **_kwargs: (
                        "forged-command",
                        True,
                        {},
                    )
                return transactions

            accounting_module._economic_transactions = plan_then_shadow
            try:
                with self.assertRaisesRegex(TypeError, "shadowed"):
                    commit_authoritative_corporate_action(
                        store=store,
                        evidence_store=durable_evidence,
                        economic_book=economics,
                        corporate_book=pure_book(),
                        accepted=accepted,
                    )
            finally:
                accounting_module._economic_transactions = original
                if "commit_command" in store.__dict__:
                    del store.commit_command

            self.assertEqual(
                JournalStore.load_events(
                    store,
                    "corporate_action_evidence",
                    durable_evidence.aggregate_id,
                ),
                [],
            )
            economics.refresh()
            self.assertEqual(len(economics.transactions), 1)

    def test_exact_retry_ignores_later_unrelated_journal_tail(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            accepted = resolve_action(sealed_action())
            first = commit_authoritative_corporate_action(
                store=store,
                evidence_store=evidence_store(store),
                economic_book=economic_book(store),
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertTrue(first.inserted)

            payload = {"kind": "POST_COMMIT_UNRELATED_ADVANCE"}
            store.append_event(
                {
                    "event_id": "post-commit-interference-1",
                    "event_type": "QualificationInterference",
                    "aggregate_type": "qualification_interference",
                    "aggregate_id": "qualification-interference:2",
                    "aggregate_version": "1",
                    "committed_at": accepted.observed_at,
                    "payload": payload,
                    "payload_hash": payload_digest(payload),
                }
            )

            reopened = JournalStore(path)
            economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(economics.transactions), 2)


    def test_reconciliation_projection_matches_exact_retained_provider_revision(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())
            retained = durable_evidence.record(accepted)
            self.assertTrue(retained.inserted)

            local_ids, provider_activities = corporate_action_reconciliation_inputs(
                durable_evidence,
                provider_actions=(accepted,),
            )

            self.assertEqual(len(local_ids), 1)
            self.assertEqual(
                tuple(activity.activity_id for activity in provider_activities),
                local_ids,
            )
            self.assertEqual(
                provider_activities[0].activity_type,
                "CORPORATE_ACTION:CASH_DIVIDEND",
            )
            self.assertEqual(
                provider_activities[0].instrument,
                INSTRUMENT_ID,
            )
            self.assertEqual(provider_activities[0].currency, "USDT")

    def test_revised_provider_action_becomes_existing_reconciliation_mismatch(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            retained_action = resolve_action(sealed_action(revision="1"))
            durable_evidence.record(retained_action)

            revised_action = resolve_action(
                sealed_action(
                    external_event_id="corp-1",
                    revision="2",
                    per_share="2.00",
                    observed_offset=4,
                )
            )
            local_ids, provider_activities = corporate_action_reconciliation_inputs(
                durable_evidence,
                provider_actions=(revised_action,),
            )
            self.assertEqual(len(local_ids), 1)
            self.assertEqual(len(provider_activities), 1)
            self.assertNotEqual(local_ids[0], provider_activities[0].activity_id)

            coverage_start = (
                READ_NOW - timedelta(minutes=1)
            ).isoformat().replace("+00:00", "Z")
            coverage_end = (
                READ_NOW + timedelta(minutes=10)
            ).isoformat().replace("+00:00", "Z")
            result = reconcile_account(
                provider_id="BINANCE",
                account_id="acct-1",
                environment="SIMULATION",
                local_cash={},
                provider_cash={},
                local_positions={},
                provider_positions={},
                local_execution_ids=(),
                provider_fills=(),
                snapshot_consistency=SnapshotConsistencyEvidence(
                    provider_id="BINANCE",
                    account_id="acct-1",
                    environment="SIMULATION",
                    mode="ATOMIC",
                    query_started_at=READ_NOW.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    query_completed_at=(
                        READ_NOW + timedelta(seconds=5)
                    ).isoformat().replace("+00:00", "Z"),
                ),
                coverage_start=coverage_start,
                coverage_end=coverage_end,
                pagination_complete=True,
                local_provider_activity_ids=local_ids,
                provider_activities=provider_activities,
                provider_activity_provider_id="BINANCE",
                provider_activity_account_id="acct-1",
                activity_coverage=CoverageSurfaceEvidence(
                    provider_id="BINANCE",
                    account_id="acct-1",
                    environment="SIMULATION",
                    surface="ACTIVITIES",
                    coverage_start=coverage_start,
                    coverage_end=coverage_end,
                    pagination_complete=True,
                    consistency_horizon_satisfied=True,
                    provider_semantics_exclude_execution=True,
                ),
                require_activity_reconciliation=True,
            )

            self.assertFalse(result.complete)
            self.assertEqual(
                result.missing_local_provider_activity_ids,
                local_ids,
            )
            self.assertEqual(
                result.unexpected_provider_activity_ids,
                (provider_activities[0].activity_id,),
            )
            self.assertIn("ACCOUNT", result.blocking_resources)
            self.assertIn(
                f"INSTRUMENT:{INSTRUMENT_ID}",
                result.blocking_resources,
            )

    def test_reconciliation_projection_rejects_forged_action_before_activity_ingress(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())
            forged = type(accepted)(**accepted.__dict__)

            with self.assertRaisesRegex(
                CorporateActionEvidenceError,
                "lacks canonical resolver issuance authority",
            ):
                corporate_action_reconciliation_inputs(
                    durable_evidence,
                    provider_actions=(forged,),
                )

    def test_reconciliation_composition_ignores_late_authority_global_decoys(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            accepted = resolve_action(sealed_action())
            durable_evidence.record(accepted)
            forged = type(accepted)(**accepted.__dict__)
            calls = []

            originals = {
                "projection": corporate_action_accounting_module.authoritative_corporate_action_projection,
                "activity_type": corporate_action_accounting_module.ProviderActivityEvidence,
                "store_type": corporate_action_accounting_module.DurableCorporateActionEvidenceStore,
                "identity": corporate_action_accounting_module._corporate_action_reconciliation_id,
            }

            def decoy_projection(_value):
                calls.append("projection")
                return {
                    "provider_id": "BINANCE",
                    "account_id": "acct-1",
                    "environment": "SIMULATION",
                    "external_event_id": "forged",
                    "provider_revision": "forged",
                    "provenance_digest": "sha256:" + "f" * 64,
                    "corrects_external_event_id": None,
                    "payload": {},
                    "kind": "CASH_DIVIDEND",
                    "observed_at": READ_NOW.isoformat().replace("+00:00", "Z"),
                    "instrument_id": INSTRUMENT_ID,
                }

            def decoy_identity(_payload):
                calls.append("identity")
                return "forged-id"

            corporate_action_accounting_module.authoritative_corporate_action_projection = decoy_projection
            corporate_action_accounting_module.ProviderActivityEvidence = object
            corporate_action_accounting_module.DurableCorporateActionEvidenceStore = object
            corporate_action_accounting_module._corporate_action_reconciliation_id = decoy_identity
            try:
                local_ids, provider_activities = (
                    corporate_action_reconciliation_inputs(
                        durable_evidence,
                        provider_actions=(accepted,),
                    )
                )
                self.assertEqual(
                    local_ids,
                    tuple(
                        activity.activity_id
                        for activity in provider_activities
                    ),
                )
                with self.assertRaisesRegex(
                    CorporateActionEvidenceError,
                    "lacks canonical resolver issuance authority",
                ):
                    corporate_action_reconciliation_inputs(
                        durable_evidence,
                        provider_actions=(forged,),
                    )
            finally:
                corporate_action_accounting_module.authoritative_corporate_action_projection = originals[
                    "projection"
                ]
                corporate_action_accounting_module.ProviderActivityEvidence = originals[
                    "activity_type"
                ]
                corporate_action_accounting_module.DurableCorporateActionEvidenceStore = originals[
                    "store_type"
                ]
                corporate_action_accounting_module._corporate_action_reconciliation_id = originals[
                    "identity"
                ]

            self.assertEqual(calls, [])

    def test_correction_lineage_is_part_of_reconciliation_identity(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            original = resolve_action(sealed_action(external_event_id="corp-1"))
            durable_evidence.record(original)
            correction = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    per_share="2.00",
                    observed_offset=4,
                    corrects="corp-1",
                ),
                corrects="corp-1",
            )
            durable_evidence.record(correction)

            local_ids, provider_activities = corporate_action_reconciliation_inputs(
                durable_evidence,
                provider_actions=(original, correction),
            )
            self.assertEqual(
                set(local_ids),
                {activity.activity_id for activity in provider_activities},
            )

            wrong_lineage = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    per_share="2.00",
                    observed_offset=4,
                    corrects="different-original",
                ),
                corrects="different-original",
            )
            _, wrong_provider = corporate_action_reconciliation_inputs(
                durable_evidence,
                provider_actions=(original, wrong_lineage),
            )
            self.assertNotEqual(
                {
                    activity.activity_id
                    for activity in provider_activities
                    if activity.activity_type == "CORPORATE_ACTION:CASH_DIVIDEND"
                },
                {
                    activity.activity_id
                    for activity in wrong_provider
                    if activity.activity_type == "CORPORATE_ACTION:CASH_DIVIDEND"
                },
            )



if __name__ == "__main__":
    unittest.main()
