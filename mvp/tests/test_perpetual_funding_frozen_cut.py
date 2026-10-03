"""Provider-free funding correction/restart falsifiers over canonical history."""
from copy import deepcopy
from decimal import Decimal, Inexact, Rounded, ROUND_FLOOR, localcontext
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.tests import test_perpetual_funding as fixtures
from mvp.autotrade_mvp.perpetual_funding import (
    DurablePerpetualFundingAuthority, PerpetualFundingConflict, PerpetualFundingError,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_activity_accounting import DurableProviderEconomicBook


class FundingFrozenCutTests(unittest.TestCase):
    def authority(self, store, evidence, **kwargs):
        return fixtures.DurablePerpetualFundingAuthorityTests().authority(store, evidence, **kwargs)

    def pair(self, **kwargs):
        return fixtures.sealed_funding(), fixtures.sealed_funding(
            external_event_id="funding-2", revision="2", rate="0.002",
            observed_offset=10, corrects="funding-1", **kwargs,
        )

    def test_backdated_later_fill_cannot_change_original_funding_position(self):
        original, correction = self.pair()
        with TemporaryDirectory() as directory:
            path = f"{directory}/journal.sqlite3"
            store = JournalStore(path)
            authority, book = self.authority(store, [original, correction])
            authority.apply(original.evidence_ref)
            frozen = store.load_events("perpetual_funding", authority.aggregate_id)[0]["payload"]["position_cut"]
            fixtures.seed_position(book, transaction_id="late-backdated", contracts="1",
                                   observed_at="2026-09-25T09:30:00Z")
            restarted, restarted_book = self.authority(JournalStore(path), [original, correction], seed=False)
            result = restarted.apply(correction.evidence_ref)
            self.assertEqual(result.cashflow, Decimal("-0.4"))
            events = restarted.store.load_events("perpetual_funding", restarted.aggregate_id)
            self.assertEqual(events[1]["payload"]["position_cut"], frozen)
            self.assertEqual(restarted_book.position("BTCUSDT"), Decimal("3"))
            self.assertFalse(restarted.apply(correction.evidence_ref).inserted)
            self.assertEqual(len(restarted_book.transactions), 5)

    def test_correction_cannot_substitute_position_from_later_visible_fill(self):
        original, correction = self.pair(contracts="3")
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [original, correction])
            authority.apply(original.evidence_ref)
            fixtures.seed_position(book, transaction_id="late-fill", contracts="1")
            before = store.current_journal_sequence(), book.audit_digest()
            with self.assertRaisesRegex(PerpetualFundingConflict, "canonical position"):
                authority.apply(correction.evidence_ref)
            self.assertEqual((store.current_journal_sequence(), book.audit_digest()), before)

    def test_missing_or_corrupt_saved_cut_cannot_be_replaced_by_current_cut(self):
        original, correction = self.pair()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [original, correction])
            authority.apply(original.evidence_ref)
            events = authority._events()
            before = store.current_journal_sequence(), book.audit_digest()
            for change in ("missing", "position", "visibility", "observed", "digest"):
                damaged = deepcopy(events)
                payload = damaged[0]["payload"]
                if change == "missing":
                    payload.pop("position_cut")
                elif change == "position":
                    payload["position_cut"]["position"] = "999"
                elif change == "visibility":
                    payload["position_cut"]["journal_sequence"] = True
                elif change == "observed":
                    payload["position_cut"]["evidence_observed_at"] = "2026-09-25T10:00:11Z"
                else:
                    payload["position_cut"]["digest"] = "sha256:" + "0" * 64
                with self.subTest(change=change), patch.object(DurablePerpetualFundingAuthority, "_events", return_value=damaged):
                    with self.assertRaises(PerpetualFundingConflict):
                        authority.apply(correction.evidence_ref)
                    with self.assertRaises(PerpetualFundingConflict):
                        authority.apply(original.evidence_ref)
                self.assertEqual((store.current_journal_sequence(), book.audit_digest()), before)

    def test_competing_writer_after_frozen_read_cannot_commit_partial_correction(self):
        original, correction = self.pair()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [original, correction])
            authority.apply(original.evidence_ref)
            prepare = DurableProviderEconomicBook.prepare_batch_mutation
            raced = False
            def racing_prepare(owner, transactions, **kwargs):
                nonlocal raced
                if owner is book and not raced:
                    raced = True
                    fixtures.seed_position(owner, transaction_id="competing", contracts="1")
                return prepare(owner, transactions, **kwargs)
            with patch.object(DurableProviderEconomicBook, "prepare_batch_mutation", racing_prepare):
                with self.assertRaisesRegex(PerpetualFundingConflict, "changed after"):
                    authority.apply(correction.evidence_ref)
            self.assertEqual(len(authority._events()), 1)
            self.assertEqual(len(book.transactions), 3)
            self.assertEqual(authority.apply(correction.evidence_ref).cashflow, Decimal("-0.4"))

    def test_exact_boundary_position_remains_ambiguous_without_native_order_proof(self):
        original = fixtures.sealed_funding()
        with TemporaryDirectory() as directory:
            store = JournalStore(f"{directory}/journal.sqlite3")
            authority, book = self.authority(store, [original], seed=False)
            fixtures.seed_position(book, effective_at="2026-09-25T10:00:00Z",
                                   observed_at="2026-09-25T10:00:00Z")
            before = store.current_journal_sequence(), book.audit_digest()
            with self.assertRaisesRegex(PerpetualFundingConflict, "ambiguous ordering"):
                authority.apply(original.evidence_ref)
            self.assertEqual((store.current_journal_sequence(), book.audit_digest()), before)

    def test_canonical_quantity_grid_ignores_order_minimum_but_rejects_fractional_contract(self):
        for contracts, allowed in (("0.5", False), ("-2", True), ("0", True)):
            with self.subTest(contracts=contracts), TemporaryDirectory() as directory:
                source = fixtures.sealed_funding(contracts=contracts)
                authority, _ = self.authority(JournalStore(f"{directory}/journal.sqlite3"), [source], seed=False)
                observation, _ = authority._observation(source.evidence_ref)
                if allowed:
                    self.assertEqual(authority._contract(observation)[0].quantity_step, Decimal("1"))
                else:
                    with self.assertRaisesRegex(PerpetualFundingError, "quantity grid"):
                        authority._contract(observation)

    def test_hostile_decimal_and_ambient_context_do_not_round_position_or_postings(self):
        original = fixtures.sealed_funding(rate="0.12345678901234567890123456789")
        with TemporaryDirectory() as directory:
            authority, book = self.authority(JournalStore(f"{directory}/journal.sqlite3"), [original])
            with localcontext() as context:
                context.prec = 1
                context.rounding = ROUND_FLOOR
                context.traps[Inexact] = True
                context.traps[Rounded] = True
                result = authority.apply(original.evidence_ref)
                self.assertEqual(result.cashflow, Decimal("-24.691357802469135780246913578"))
                self.assertEqual(book.transactions[-1].postings[1].signed_amount,
                                 Decimal("24.691357802469135780246913578"))
        class Hostile(Decimal):
            def is_finite(self): raise AssertionError("virtual decimal read")
            def as_tuple(self): raise AssertionError("virtual decimal tuple")
        observation = fixtures.normalize(fixtures.sealed_funding())
        from dataclasses import replace
        with self.assertRaises(PerpetualFundingError):
            replace(observation, signed_contracts=Hostile("2"))

    def test_registry_instance_shadow_cannot_replace_canonical_contract(self):
        source = fixtures.sealed_funding()
        with TemporaryDirectory() as directory:
            authority, _ = self.authority(JournalStore(f"{directory}/journal.sqlite3"), [source])
            authority.instrument_registry.exact = lambda *_: self.fail("caller registry dispatch")
            authority.instrument_registry.at = lambda *_: self.fail("caller registry dispatch")
            self.assertEqual(authority.apply(source.evidence_ref).cashflow, Decimal("-0.2"))

    def test_financial_store_binding_cannot_be_rebound_after_construction(self):
        source = fixtures.sealed_funding()
        for mutation in ("authority_store", "economic_book", "economic_book_store"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as directory:
                primary_store = JournalStore(f"{directory}/primary.sqlite3")
                authority, book = self.authority(primary_store, [source])
                foreign_store = JournalStore(f"{directory}/foreign.sqlite3")
                foreign_book = DurableProviderEconomicBook(
                    foreign_store,
                    provider_id=book.provider_id,
                    account_id=book.account_id,
                    environment=book.environment,
                )
                fixtures.seed_position(foreign_book, transaction_id="foreign-position")

                primary_before = primary_store.current_journal_sequence()
                foreign_before = foreign_store.current_journal_sequence()

                if mutation == "authority_store":
                    authority.store = foreign_store
                elif mutation == "economic_book":
                    authority.economic_book = foreign_book
                else:
                    book.store = foreign_store

                with self.assertRaisesRegex(
                    PerpetualFundingConflict,
                    "durable store binding changed after construction",
                ):
                    authority.apply(source.evidence_ref)

                self.assertEqual(
                    primary_store.current_journal_sequence(),
                    primary_before,
                )
                self.assertEqual(
                    foreign_store.current_journal_sequence(),
                    foreign_before,
                )
                self.assertEqual(
                    primary_store.load_events(
                        "perpetual_funding",
                        authority.aggregate_id,
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
