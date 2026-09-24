import unittest
from decimal import Decimal

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderProjection,
    OrderProjectionConflict,
)


def order(**overrides):
    values = dict(
        client_order_id="c1",
        instrument="ABC",
        side="BUY",
        requested_quantity="2",
    )
    values.update(overrides)
    return OrderProjection(**values)


class OrderProjectionTests(unittest.TestCase):
    def test_fill_before_ack_is_economic_truth_and_ack_adds_no_fill(self):
        item = order()
        self.assertTrue(item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        ))
        before = item.filled_quantity
        item.acknowledge(provider_order_id="p1")
        self.assertEqual(item.state, "PARTIALLY_FILLED")
        self.assertEqual(item.filled_quantity, before)
        self.assertEqual(item.snapshot().fill_count, 1)

    def test_provider_execution_identity_is_unique(self):
        item = order()
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        )
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "provider_execution_id",
        ):
            item.record_fill(
                fill_id="f2",
                provider_execution_id="exec-1",
                quantity="1",
                price="10",
            )
        self.assertEqual(item.filled_quantity, Decimal("1"))

    def test_duplicate_fill_is_idempotent_but_changed_duplicate_conflicts(self):
        item = order()
        kwargs = dict(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        )
        self.assertTrue(item.record_fill(**kwargs))
        self.assertFalse(item.record_fill(**kwargs))
        with self.assertRaises(OrderProjectionConflict):
            item.record_fill(
                fill_id="f1",
                provider_execution_id="exec-1",
                quantity="1.5",
                price="10",
            )

    def test_cancel_request_is_pending_until_provider_confirmation(self):
        item = order(requested_quantity="5")
        item.request_cancel()
        snap = item.snapshot()
        self.assertEqual(snap.state, "CANCEL_REQUESTED")
        self.assertTrue(snap.cancel_requested)
        self.assertFalse(snap.cancel_confirmed)
        self.assertEqual(snap.open_quantity, Decimal("5"))

        item.confirm_cancel()
        confirmed = item.snapshot()
        self.assertEqual(confirmed.state, "CANCELLED")
        self.assertTrue(confirmed.cancel_requested)
        self.assertTrue(confirmed.cancel_confirmed)
        self.assertEqual(confirmed.open_quantity, Decimal("5"))

    def test_fill_during_pending_cancel_remains_live_economic_truth(self):
        item = order(requested_quantity="5")
        item.request_cancel()
        item.record_fill(
            fill_id="f-pending",
            provider_execution_id="exec-pending",
            quantity="2",
            price="10",
        )
        pending = item.snapshot()
        self.assertEqual(pending.state, "PARTIALLY_FILLED_CANCEL_REQUESTED")
        self.assertEqual(pending.filled_quantity, Decimal("2"))
        self.assertEqual(pending.open_quantity, Decimal("3"))
        self.assertFalse(pending.cancel_confirmed)

        item.confirm_cancel()
        confirmed = item.snapshot()
        self.assertEqual(confirmed.state, "PARTIALLY_FILLED_CANCELLED")
        self.assertEqual(confirmed.filled_quantity, Decimal("2"))
        self.assertEqual(confirmed.open_quantity, Decimal("3"))

    def test_overfill_during_pending_cancel_is_explicit(self):
        item = order(requested_quantity="1")
        item.request_cancel()
        item.record_fill(
            fill_id="late-overfill",
            provider_execution_id="exec-late-overfill",
            quantity="1.2",
            price="10",
        )
        self.assertEqual(item.state, "OVERFILLED_DURING_CANCEL")
        self.assertFalse(item.snapshot().cancel_confirmed)

    def test_partial_fill_then_cancel_keeps_executed_quantity(self):
        item = order(requested_quantity="5")
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="2",
            price="11",
        )
        item.cancel()
        snap = item.snapshot()
        self.assertEqual(snap.state, "PARTIALLY_FILLED_CANCELLED")
        self.assertEqual(snap.filled_quantity, Decimal("2"))
        self.assertEqual(snap.open_quantity, Decimal("3"))

    def test_overfill_after_cancel_is_not_hidden_as_partial_cancel(self):
        item = order(requested_quantity="1")
        item.cancel()
        item.record_fill(
            fill_id="late",
            provider_execution_id="exec-late",
            quantity="1.2",
            price="10",
        )
        self.assertEqual(item.state, "OVERFILLED_AFTER_CANCEL")
        self.assertEqual(item.filled_quantity, Decimal("1.2"))

    def test_late_bust_preserves_history_and_reopens_quantity(self):
        item = order(requested_quantity="1")
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
            provider_revision="r1",
        )
        self.assertEqual(item.state, "FILLED")
        self.assertTrue(item.bust_fill("f1", provider_revision="r2-bust"))
        self.assertEqual(item.filled_quantity, Decimal("0"))
        self.assertEqual(item.open_quantity, Decimal("1"))
        self.assertEqual(len(item.fill_history), 2)
        self.assertTrue(item.fill_history[0].active)
        self.assertFalse(item.fill_history[1].active)
        self.assertEqual(
            item.fill_history[0].provider_execution_id,
            item.fill_history[1].provider_execution_id,
        )

    def test_late_corrections_preserve_original_and_revision_history(self):
        item = order()
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
            provider_revision="r1",
        )
        self.assertTrue(item.correct_fill(
            fill_id="f1",
            quantity="1.5",
            price="12",
            provider_revision="r2",
        ))
        self.assertTrue(item.correct_fill(
            fill_id="f1",
            quantity="1.25",
            price="11",
            provider_revision="r3",
        ))
        self.assertEqual(item.filled_quantity, Decimal("1.25"))
        self.assertEqual(item.average_fill_price, Decimal("11"))
        self.assertEqual(len(item.fill_history), 3)
        self.assertEqual(
            [record.provider_revision for record in item.fill_history],
            ["r1", "r2", "r3"],
        )
        self.assertEqual(
            {record.provider_execution_id for record in item.fill_history},
            {"exec-1"},
        )

    def test_old_fill_redelivery_after_correction_is_idempotent_and_does_not_rollback(self):
        item = order()
        original = dict(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
            provider_revision="r1",
        )
        item.record_fill(**original)
        item.correct_fill(
            fill_id="f1",
            quantity="1.5",
            price="12",
            provider_revision="r2",
        )
        self.assertFalse(item.record_fill(**original))
        self.assertEqual(item.filled_quantity, Decimal("1.5"))
        self.assertEqual(item.average_fill_price, Decimal("12"))
        self.assertEqual(
            [record.provider_revision for record in item.fill_history],
            ["r1", "r2"],
        )

    def test_old_fill_redelivery_after_bust_does_not_reactivate_execution(self):
        item = order(requested_quantity="1")
        original = dict(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        )
        item.record_fill(**original)
        item.bust_fill("f1", provider_revision="r2-bust")
        self.assertFalse(item.record_fill(**original))
        self.assertEqual(item.filled_quantity, Decimal("0"))
        self.assertEqual(item.open_quantity, Decimal("1"))
        self.assertEqual(len(item.fill_history), 2)

    def test_repeated_revision_is_idempotent_but_conflict_fails(self):
        item = order()
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        )
        kwargs = dict(
            fill_id="f1",
            quantity="1.5",
            price="12",
            provider_revision="r2",
        )
        self.assertTrue(item.correct_fill(**kwargs))
        self.assertFalse(item.correct_fill(**kwargs))
        with self.assertRaisesRegex(OrderProjectionConflict, "revision"):
            item.correct_fill(
                fill_id="f1",
                quantity="1.6",
                price="12",
                provider_revision="r2",
            )

    def test_reject_cannot_erase_late_provider_fill(self):
        item = order(requested_quantity="1")
        item.acknowledge(provider_order_id="p1", status="REJECTED")
        self.assertEqual(item.state, "REJECTED")
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
        )
        self.assertEqual(item.state, "FILLED_AFTER_REJECT")
        self.assertEqual(item.filled_quantity, Decimal("1"))

    def test_two_oco_fills_are_preserved_and_flagged(self):
        first = order(client_order_id="a", requested_quantity="1", oco_group_id="g")
        second = order(client_order_id="b", requested_quantity="1", oco_group_id="g")
        group = OcoGroupProjection("g")
        group.add(first)
        group.add(second)
        first.record_fill(
            fill_id="fa",
            provider_execution_id="exec-a",
            quantity="1",
            price="10",
        )
        second.record_fill(
            fill_id="fb",
            provider_execution_id="exec-b",
            quantity="1",
            price="11",
        )
        self.assertTrue(group.refresh())
        self.assertEqual(first.state, "OCO_VIOLATION")
        self.assertEqual(second.state, "OCO_VIOLATION")
        self.assertEqual(first.filled_quantity, Decimal("1"))
        self.assertEqual(second.filled_quantity, Decimal("1"))

    def test_snapshot_binds_instrument_side_and_audit_counts(self):
        item = order(instrument="MSFT", side="SELL")
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="100",
        )
        snap = item.snapshot()
        self.assertEqual(snap.instrument, "MSFT")
        self.assertEqual(snap.side, "SELL")
        self.assertEqual(snap.fill_count, 1)
        self.assertEqual(snap.observation_count, 1)

    def test_binary_float_and_invalid_side_are_rejected(self):
        with self.assertRaises(TypeError):
            order(requested_quantity=1.0)
        with self.assertRaises(ValueError):
            order(side="HOLD")
        item = order()
        with self.assertRaises(TypeError):
            item.record_fill(
                fill_id="f1",
                provider_execution_id="exec-1",
                quantity=1.0,
                price="10",
            )


if __name__ == "__main__":
    unittest.main()
