from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from mvp.autotrade_mvp.accounting import (
    AccountingConflict,
    EconomicBook,
    book_equity_fill,
    project_equity_position,
)
from mvp.autotrade_mvp.corporate_action_accounting import (
    commit_authoritative_corporate_action,
)
from mvp.autotrade_mvp.corporate_action_evidence import (
    CorporateActionObservation,
    DurableCorporateActionEvidenceStore,
    resolve_authoritative_corporate_action,
)
from mvp.autotrade_mvp.corporate_actions import CorporateActionBook, EquityState
from mvp.autotrade_mvp.instruments import InstrumentRegistry
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    prepare_authenticated_read_query,
)
from mvp.tests.test_corporate_action_evidence import (
    ENDPOINT,
    INSTRUMENT_ID,
    canonical_instrument,
)
from mvp.tests.test_provider_transport import READ_NOW, verified_read_capability


def sealed_action(
    *,
    external_event_id="corp-1",
    revision="1",
    kind="CASH_DIVIDEND",
    per_share="1.25",
    observed_offset=2,
    effective_offset=1,
    corrects=None,
    numerator="2",
    denominator="1",
):
    binding = prepare_authenticated_read_query(
        capability=verified_read_capability(),
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
    else:
        payload.update({"numerator": numerator, "denominator": denominator})
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
        expected_environment="PAPER",
        allowed_endpoints=frozenset({ENDPOINT}),
        permission_scope="ORDER.READ",
    )


def pure_book(*, quantity="10"):
    current = canonical_instrument()
    return CorporateActionBook(
        EquityState.create(
            symbol="BTCUSDT",
            quantity=quantity,
            total_basis="1000",
            settled_cash="1000",
            unsettled_cash="0",
            currency="USDT",
        ),
        instrument_version=current,
        registry=InstrumentRegistry(versions=(current,)),
    )


def economic_book(store):
    book = DurableProviderEconomicBook(
        store,
        provider_id="BINANCE",
        account_id="acct-1",
        environment="PAPER",
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
        environment="PAPER",
    )


class AtomicCorporateActionFinancialTests(unittest.TestCase):
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
            original = store.commit_command

            def fail(**kwargs):
                raise RuntimeError("injected atomic corporate action failure")

            store.commit_command = fail
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
                store.commit_command = original

            reopened = JournalStore(path)
            self.assertEqual(
                reopened.load_events(
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

    def test_sealed_split_commits_atomically_and_preserves_fifo_basis(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action(kind="SPLIT"))

            before = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )

            self.assertTrue(result.inserted)
            self.assertTrue(result.economically_active)
            self.assertEqual(result.next_state.quantity, Decimal("20"))
            self.assertEqual(result.next_state.total_basis, Decimal("1000"))

            economics.refresh()
            self.assertEqual(economics.position("BTCUSDT"), Decimal("20"))
            self.assertEqual(len(economics.transactions), 2)
            after = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(after.quantity, Decimal("20"))
            self.assertEqual(after.open_cost_basis, before.open_cost_basis)
            self.assertEqual(after.realized_pnl, before.realized_pnl)
            self.assertEqual(len(after.lots), 1)
            self.assertEqual(after.lots[0].quantity, Decimal("20"))
            self.assertEqual(after.lots[0].unit_price, Decimal("50"))
            self.assertEqual(
                len(
                    store.load_events(
                        "corporate_action_evidence",
                        durable_evidence.aggregate_id,
                    )
                ),
                1,
            )

    def test_split_restart_and_exact_retry_do_not_double_apply(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            accepted = resolve_action(sealed_action(kind="SPLIT"))

            first = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )
            self.assertTrue(first.inserted)
            self.assertEqual(economics.position("BTCUSDT"), Decimal("20"))

            reopened = JournalStore(path)
            restarted_evidence = evidence_store(reopened)
            restarted_economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=restarted_evidence,
                economic_book=restarted_economics,
                corporate_book=pure_book(),
                accepted=accepted,
            )

            self.assertFalse(retry.inserted)
            self.assertTrue(retry.economically_active)
            self.assertEqual(restarted_economics.position("BTCUSDT"), Decimal("20"))
            self.assertEqual(len(restarted_economics.transactions), 2)
            projection = project_equity_position(
                EconomicBook(restarted_economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(projection.quantity, Decimal("20"))
            self.assertEqual(projection.open_cost_basis, Decimal("1000"))

    def test_split_correction_reverses_replaces_and_preserves_basis_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "journal.sqlite3"
            store = JournalStore(path)
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            pure = pure_book()
            first = resolve_action(sealed_action(kind="SPLIT"))
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure,
                accepted=first,
            )
            pure.apply(first_result.accepted_event)
            original_split = economics.transactions[-1]

            correction = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    kind="SPLIT",
                    corrects="corp-1",
                    numerator="4",
                    denominator="1",
                    observed_offset=4,
                ),
                corrects="corp-1",
            )
            corrected = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure,
                accepted=correction,
            )

            self.assertTrue(corrected.inserted)
            self.assertEqual(corrected.next_state.quantity, Decimal("40"))
            self.assertEqual(corrected.next_state.total_basis, Decimal("1000"))
            self.assertEqual(len(corrected.transaction_ids), 2)
            self.assertEqual(len(economics.transactions), 4)

            reversal = economics.transactions[-2]
            replacement = economics.transactions[-1]
            self.assertEqual(
                reversal.reverses_transaction_id,
                original_split.transaction_id,
            )
            self.assertEqual(
                replacement.corrects_transaction_id,
                original_split.transaction_id,
            )
            self.assertEqual(
                replacement.economic_effective_at,
                original_split.economic_effective_at,
            )
            self.assertEqual(
                replacement.economic_order_key,
                original_split.economic_order_key,
            )
            self.assertEqual(replacement.observed_at, reversal.observed_at)

            projection = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(projection.quantity, Decimal("40"))
            self.assertEqual(projection.open_cost_basis, Decimal("1000"))
            self.assertEqual(projection.realized_pnl, Decimal("0"))
            self.assertEqual(len(projection.lots), 1)
            self.assertEqual(projection.lots[0].quantity, Decimal("40"))
            self.assertEqual(projection.lots[0].unit_price, Decimal("25"))

            reopened = JournalStore(path)
            restarted_economics = economic_book(reopened)
            retry = commit_authoritative_corporate_action(
                store=reopened,
                evidence_store=evidence_store(reopened),
                economic_book=restarted_economics,
                corporate_book=pure,
                accepted=correction,
            )
            self.assertFalse(retry.inserted)
            self.assertEqual(len(restarted_economics.transactions), 4)
            restarted_projection = project_equity_position(
                EconomicBook(restarted_economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(restarted_projection.quantity, Decimal("40"))
            self.assertEqual(
                restarted_projection.open_cost_basis,
                Decimal("1000"),
            )
            self.assertEqual(
                len(
                    reopened.load_events(
                        "corporate_action_evidence",
                        evidence_store(reopened).aggregate_id,
                    )
                ),
                2,
            )

    def test_split_correction_cannot_move_economic_effective_time(self):
        with TemporaryDirectory() as directory:
            store = JournalStore(Path(directory) / "journal.sqlite3")
            durable_evidence = evidence_store(store)
            economics = economic_book(store)
            pure = pure_book()
            first = resolve_action(sealed_action(kind="SPLIT"))
            first_result = commit_authoritative_corporate_action(
                store=store,
                evidence_store=durable_evidence,
                economic_book=economics,
                corporate_book=pure,
                accepted=first,
            )
            pure.apply(first_result.accepted_event)
            correction = resolve_action(
                sealed_action(
                    external_event_id="corp-2",
                    revision="2",
                    kind="SPLIT",
                    corrects="corp-1",
                    numerator="4",
                    denominator="1",
                    effective_offset=3,
                    observed_offset=4,
                ),
                corrects="corp-1",
            )

            with self.assertRaisesRegex(
                AccountingConflict,
                "cannot change economic effective time",
            ):
                commit_authoritative_corporate_action(
                    store=store,
                    evidence_store=durable_evidence,
                    economic_book=economics,
                    corporate_book=pure,
                    accepted=correction,
                )

            economics.refresh()
            projection = project_equity_position(
                EconomicBook(economics.transactions),
                instrument="BTCUSDT",
                settlement_currency="USDT",
            )
            self.assertEqual(projection.quantity, Decimal("20"))
            self.assertEqual(projection.open_cost_basis, Decimal("1000"))
            self.assertEqual(len(economics.transactions), 2)
            self.assertEqual(
                len(
                    store.load_events(
                        "corporate_action_evidence",
                        durable_evidence.aggregate_id,
                    )
                ),
                1,
            )

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



if __name__ == "__main__":
    unittest.main()
