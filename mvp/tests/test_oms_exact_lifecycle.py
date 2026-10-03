from decimal import Decimal, localcontext, Inexact, Rounded, ROUND_UP
from tempfile import TemporaryDirectory
from pathlib import Path
import unittest
from mvp.autotrade_mvp.order_projection import OrderProjection, OrderProjectionConflict
from mvp.autotrade_mvp.durable_order_projection import DurableOrderBookProjection
from mvp.autotrade_mvp.persistence import JournalStore

NOW = "2026-10-03T00:00:00Z"


class OmsExactLifecycleTests(unittest.TestCase):
    def order(self):
        return OrderProjection(provider_id="SIMULATED", account_id="a", environment="SIMULATION",
            client_order_id="c", instrument="A@1", side="BUY", requested_quantity="1.00000000000000000001")

    def test_partial_fill_late_fill_cancel_and_bust_use_exact_quantities(self):
        order = self.order()
        with localcontext() as c:
            c.prec = 1
            c.rounding = ROUND_UP
            c.traps[Inexact] = c.traps[Rounded] = True
            order.record_fill(fill_id="first", provider_execution_id="e1", quantity="1", price="100")
            self.assertEqual(order.state, "PARTIALLY_FILLED")
            self.assertEqual(order.open_quantity, Decimal("0.00000000000000000001"))
            order.request_cancel(command_id="cancel")
            order.record_fill(fill_id="last", provider_execution_id="e2", quantity="0.00000000000000000001", price="100")
            self.assertEqual(order.state, "FILLED")
            order.confirm_cancel()
            self.assertEqual(order.state, "FILLED_AFTER_CANCEL")
            order.bust_fill("last", provider_revision="bust-v1", correction_fill_id="last-bust")
            self.assertEqual(order.state, "PARTIALLY_FILLED_CANCELLED")
            self.assertEqual(order.open_quantity, Decimal("0.00000000000000000001"))

    def test_report_average_is_context_independent_and_not_fill_authority(self):
        values = []
        for precision in (1, 80):
            order = OrderProjection(provider_id="SIMULATED", account_id="a", environment="SIMULATION",
                client_order_id="c", instrument="A@1", side="BUY", requested_quantity="3")
            order.record_fill(fill_id="f1", provider_execution_id="e1", quantity="1", price="100")
            order.record_fill(fill_id="f2", provider_execution_id="e2", quantity="2", price="101")
            with localcontext() as c:
                c.prec = precision
                c.traps[Inexact] = c.traps[Rounded] = True
                values.append(order.average_fill_price)
                self.assertEqual(order.state, "FILLED")
        self.assertEqual(values, [Decimal("100.666666666666666667")] * 2)

    def test_durable_duplicate_partial_correction_restart_preserves_exact_sum(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "journal.sqlite3")
            def book():
                return DurableOrderBookProjection(store, provider_id="SIMULATED", account_id="a",
                    environment="SIMULATION", host_id="sim", owner_epoch="1")
            oms = book()
            oms.create_order(event_key="create", client_order_id="c", instrument="A@1", side="BUY",
                requested_quantity="1.00000000000000000001", committed_at=NOW)
            with localcontext() as c:
                c.prec = 1
                c.traps[Inexact] = c.traps[Rounded] = True
                first = oms.record_fill(event_key="fill", client_order_id="c", fill_id="f", provider_execution_id="e",
                    quantity="1", price="100", committed_at=NOW)
                again = oms.record_fill(event_key="fill", client_order_id="c", fill_id="f", provider_execution_id="e",
                    quantity="1", price="100", committed_at=NOW)
                self.assertTrue(first.inserted)
                self.assertFalse(again.inserted)
                oms.correct_fill(event_key="correction", client_order_id="c", fill_id="f", quantity="1.00000000000000000001",
                    price="100", provider_revision="v2", correction_fill_id="f-v2", committed_at=NOW)
                self.assertEqual(book().order("c").state, "FILLED")

    def test_hostile_decimal_subtype_never_dispatches_virtual_numeric_methods(self):
        class Hostile(Decimal):
            def is_finite(self):
                raise AssertionError("virtual numeric authority")
        with self.assertRaises((TypeError, ValueError)):
            self.order().record_fill(fill_id="bad", provider_execution_id="bad", quantity=Hostile("1"), price="100")

    def test_replace_rejection_preserves_partial_and_racing_full_fills(self):
        for quantity, expected in (("1", "PARTIALLY_FILLED"),
                                   ("1.00000000000000000001", "FILLED"),
                                   ("2", "OVERFILLED")):
            with self.subTest(quantity=quantity):
                order = self.order()
                order.request_replace(command_id="amend")
                order.record_fill(fill_id="race", provider_execution_id="race", quantity=quantity, price="100")
                before = order.snapshot()
                with self.assertRaisesRegex(OrderProjectionConflict, "does not match"):
                    order.reject_replace(command_id="foreign", reason_code="REJECTED")
                self.assertEqual(before, order.snapshot())
                order.reject_replace(command_id="amend", reason_code="REJECTED")
                self.assertEqual(order.state, expected)
                self.assertEqual(order.requested_quantity, before.requested_quantity)
                self.assertEqual(order.filled_quantity, before.filled_quantity)
                self.assertFalse(order.replace_requested)
                with self.assertRaisesRegex(OrderProjectionConflict, "no pending"):
                    order.reject_replace(command_id="amend", reason_code="REJECTED")

    def test_durable_replace_rejection_replay_duplicate_and_conflict(self):
        with TemporaryDirectory() as d:
            store = JournalStore(Path(d) / "journal.sqlite3")
            def book():
                return DurableOrderBookProjection(store, provider_id="SIMULATED", account_id="a",
                    environment="SIMULATION", host_id="sim", owner_epoch="1")
            oms = book()
            oms.create_order(event_key="create", client_order_id="c", instrument="A@1", side="BUY",
                requested_quantity="2", committed_at=NOW)
            oms.request_replace(event_key="replace", client_order_id="c", command_id="amend", committed_at=NOW)
            oms.record_fill(event_key="fill", client_order_id="c", fill_id="f", provider_execution_id="e",
                quantity="1", price="100", committed_at=NOW)
            before = store.current_journal_sequence()
            with self.assertRaisesRegex(OrderProjectionConflict, "does not match"):
                oms.reject_replace(event_key="wrong", client_order_id="c", command_id="foreign",
                    reason_code="REJECTED", committed_at=NOW)
            self.assertEqual(store.current_journal_sequence(), before)
            args = dict(event_key="rejected", client_order_id="c", command_id="amend",
                        reason_code="REJECTED", committed_at=NOW)
            self.assertTrue(oms.reject_replace(**args).inserted)
            cut = store.current_journal_sequence()
            restored = book()
            self.assertEqual(restored.order("c").state, "PARTIALLY_FILLED")
            self.assertFalse(restored.reject_replace(**args).inserted)
            self.assertEqual(store.current_journal_sequence(), cut)
            with self.assertRaises(OrderProjectionConflict):
                restored.reject_replace(**{**args, "reason_code": "CHANGED"})
            with self.assertRaisesRegex(OrderProjectionConflict, "cannot be reused"):
                restored.request_replace(event_key="replace-reused", client_order_id="c", command_id="amend", committed_at=NOW)
            self.assertEqual(store.current_journal_sequence(), cut)
            restored.request_replace(event_key="replace-next", client_order_id="c", command_id="amend-next", committed_at=NOW)
            self.assertEqual(book().order("c").replace_command_id, "amend-next")
            with self.assertRaisesRegex(OrderProjectionConflict, "does not match"):
                restored.reject_replace(event_key="late-rejection", client_order_id="c", command_id="amend",
                    reason_code="REJECTED", committed_at=NOW)
            self.assertEqual(book().order("c").replace_command_id, "amend-next")


if __name__ == "__main__":
    unittest.main()
