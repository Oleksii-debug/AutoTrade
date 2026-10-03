"""Falsifiers for the canonical durable OMS and atomic financial cut."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.order_projection import OrderProjectionConflict
from mvp.autotrade_mvp.persistence import JournalStore, payload_digest
from mvp.tests.test_atomic_oms_financial_commit import books, seed, atomic_fill, WHEN


class OmsStoreAuthorityTests(unittest.TestCase):
    def test_raw_method_shadow_fails_before_callback_and_financial_write(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            r, e, o = books(store)
            seed(r, o)
            before = store.current_journal_sequence()
            calls = []
            vars(o)["prepare_record_fill_mutation"] = lambda **kwargs: calls.append(kwargs)
            with self.assertRaisesRegex(OrderProjectionConflict, "shadowed"):
                atomic_fill(e, r, o)
            self.assertEqual(calls, [])
            self.assertEqual(store.current_journal_sequence(), before)
            self.assertEqual(e.transactions, ())
            self.assertEqual(r.get("reservation-1").consumed["CASH:USD"], 0)

    def test_store_and_scope_retargeting_and_reentry_fail_closed(self):
        for field, value in (("provider_id", "FOREIGN"), ("account_id", "other"),
                             ("environment", "PAPER"), ("aggregate_id", "other"),
                             ("host_id", "other"), ("owner_epoch", "2")):
            with self.subTest(field=field), TemporaryDirectory() as d:
                store = JournalStore(Path(d) / "a.db")
                r, e, o = books(store)
                seed(r, o)
                before = store.current_journal_sequence()
                object.__setattr__(o, field, value)
                with self.assertRaisesRegex(OrderProjectionConflict, "scope changed"):
                    atomic_fill(e, r, o)
                self.assertEqual(store.current_journal_sequence(), before)
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            _, _, o = books(store)
            foreign = JournalStore(Path(d) / "b.db")
            with self.assertRaisesRegex(OrderProjectionConflict, "already initialized"):
                DurableOrderBookProjection.__init__(o, foreign, provider_id="OTHER", account_id="OTHER",
                    environment="SIMULATION", host_id="OTHER", owner_epoch="2")
            object.__setattr__(o, "store", foreign)
            with self.assertRaisesRegex(OrderProjectionConflict, "selected store changed"):
                o.refresh()
            self.assertEqual(foreign.current_journal_sequence(), 0)

    def test_cached_book_cannot_replace_durable_open_obligations(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            r, _, o = books(store)
            seed(r, o)
            object.__setattr__(o, "_book", object())
            self.assertEqual(o.order("order-1").snapshot().requested_quantity, 1)
            self.assertEqual(len(o.snapshots), 1)

    def test_unrelated_writer_after_oms_preparation_invalidates_whole_fill_cut(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            r, e, o = books(store)
            seed(r, o)
            before = store.current_journal_sequence()
            original = DurableOrderBookProjection.prepare_record_fill_mutation
            def race(selected, **kwargs):
                plan = original(selected, **kwargs)
                payload = {"fact": "competing canonical writer"}
                store.append_event({"event_id": "competing-fact", "event_type": "CompetingFact",
                    "aggregate_type": "competing", "aggregate_id": "independent", "aggregate_version": "1",
                    "committed_at": WHEN, "payload": payload, "payload_hash": payload_digest(payload)})
                return plan
            with patch.object(DurableOrderBookProjection, "prepare_record_fill_mutation", autospec=True, side_effect=race):
                with self.assertRaisesRegex(ValueError, "journal sequence changed"):
                    atomic_fill(e, r, o)
            self.assertEqual(store.current_journal_sequence(), before + 1)
            self.assertEqual(e.transactions, ())
            self.assertEqual(r.get("reservation-1").consumed["CASH:USD"], 0)
            self.assertNotEqual(o.order("order-1").state, "FILLED")

    def test_non_string_raw_state_key_is_rejected_before_its_callbacks(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            _, _, o = books(store)
            calls = []
            class HostileKey:
                def __hash__(self):
                    calls.append("hash")
                    return 0
                def __eq__(self, other):
                    calls.append("eq")
                    return False
            vars(o)[HostileKey()] = "hidden"
            calls.clear()
            with self.assertRaisesRegex(OrderProjectionConflict, "shadowed"):
                o.refresh()
            self.assertEqual(calls, [])

    def test_binding_weakrefs_have_no_authority_eraser_callbacks(self):
        import weakref
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            _, _, o = books(store)
            refs = weakref.getweakrefs(o)
            self.assertTrue(refs)
            self.assertTrue(all(ref.__callback__ is None for ref in refs))

    def test_same_path_physical_store_replacement_fails_closed(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "a.db")
            r, e, o = books(store)
            seed(r, o)
            foreign = JournalStore(Path(d) / "b.db")
            foreign.path.replace(store.path)
            with self.assertRaises((ValueError, RuntimeError)):
                atomic_fill(e, r, o)
            self.assertEqual(JournalStore(store.path).current_journal_sequence(), 0)
