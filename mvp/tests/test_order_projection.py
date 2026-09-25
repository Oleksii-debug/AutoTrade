import unittest
from decimal import Decimal

from mvp.autotrade_mvp.order_projection import (
    OcoGroupProjection,
    OrderBookProjection,
    OrderProjection,
    OrderProjectionConflict,
)


def order(**overrides):
    values = dict(
        provider_id="PROVIDER-A",
        account_id="acct-1",
        environment="PAPER",
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
        item.request_cancel(command_id="cancel-command")
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
        item.request_cancel(command_id="cancel-command")
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
        item.request_cancel(command_id="cancel-command")
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

    def test_full_fill_then_late_cancel_confirmation_remains_explicit_full_fill(self):
        item = order(requested_quantity="1")
        item.record_fill(
            fill_id="full-before-cancel",
            provider_execution_id="exec-full-before-cancel",
            quantity="1",
            price="10",
        )
        self.assertEqual(item.state, "FILLED")

        item.confirm_cancel()

        snapshot = item.snapshot()
        self.assertEqual(snapshot.state, "FILLED_AFTER_CANCEL")
        self.assertEqual(snapshot.filled_quantity, Decimal("1"))
        self.assertEqual(snapshot.open_quantity, Decimal("0"))
        self.assertTrue(snapshot.cancel_confirmed)

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
        # A group query must not mutate either order. Group-level OCO truth is
        # rendered by the canonical aggregate projection.
        self.assertEqual(first.state, "FILLED")
        self.assertEqual(second.state, "FILLED")
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

    def test_overfill_quantity_is_explicit_in_snapshot(self):
        item = order(requested_quantity="1")
        item.record_fill(
            fill_id="f-over",
            provider_execution_id="exec-over",
            quantity="1.25",
            price="10",
        )
        snap = item.snapshot()
        self.assertEqual(snap.state, "OVERFILLED")
        self.assertEqual(snap.open_quantity, Decimal("0"))
        self.assertEqual(snap.overfill_quantity, Decimal("0.25"))

    def test_correction_can_have_separate_immutable_observation_identity(self):
        item = order(requested_quantity="2")
        item.record_fill(
            fill_id="f1",
            provider_execution_id="exec-1",
            quantity="1",
            price="10",
            provider_revision="r1",
        )
        self.assertTrue(item.correct_fill(
            fill_id="f1",
            correction_fill_id="f1-correction-r2",
            quantity="1.5",
            price="11",
            provider_revision="r2",
        ))
        current = item.active_fills[0]
        self.assertEqual(current.fill_id, "f1-correction-r2")
        self.assertEqual(current.correction_of, "f1")
        self.assertEqual(current.provider_execution_id, "exec-1")
        self.assertEqual(item.filled_quantity, Decimal("1.5"))
        self.assertEqual(
            [record.fill_id for record in item.fill_history],
            ["f1", "f1-correction-r2"],
        )
        self.assertFalse(item.correct_fill(
            fill_id="f1",
            correction_fill_id="f1-correction-r2",
            quantity="1.5",
            price="11",
            provider_revision="r2",
        ))

    def test_multi_order_projection_preserves_single_child_amendment_lineage(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        old = book.create(
            client_order_id="old",
            instrument="ABC",
            side="BUY",
            requested_quantity="10",
        )
        new = book.create(
            client_order_id="new",
            instrument="ABC",
            side="BUY",
            requested_quantity="8",
            parent_intent_id="old",
        )
        self.assertEqual(book.amendment_child("old"), "new")
        self.assertIs(book.order("old"), old)
        self.assertIs(book.order("new"), new)
        with self.assertRaises(OrderProjectionConflict):
            book.create(
                client_order_id="other",
                instrument="ABC",
                side="BUY",
                requested_quantity="7",
                parent_intent_id="old",
            )

    def test_multi_order_projection_rejects_cross_order_execution_alias(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        first = book.create(
            client_order_id="first",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        second = book.create(
            client_order_id="second",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        first.record_fill(
            fill_id="fill-first",
            provider_execution_id="shared-execution",
            quantity="1",
            price="10",
        )
        second.record_fill(
            fill_id="fill-second",
            provider_execution_id="shared-execution",
            quantity="1",
            price="10",
        )
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "multiple orders",
        ):
            book.effective_fills()

    def test_busted_execution_identity_cannot_move_to_another_order(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        first = book.create(
            client_order_id="first",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        second = book.create(
            client_order_id="second",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        first.record_fill(
            fill_id="fill-first",
            provider_execution_id="immutable-execution",
            quantity="1",
            price="10",
        )
        first.bust_fill(
            "fill-first",
            provider_revision="r2-bust",
        )
        second.record_fill(
            fill_id="fill-second",
            provider_execution_id="immutable-execution",
            quantity="1",
            price="10",
        )

        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "multiple orders",
        ):
            book.effective_fills()

        self.assertEqual(first.filled_quantity, Decimal("0"))
        self.assertEqual(second.filled_quantity, Decimal("1"))

    def test_multi_order_projection_aggregates_effective_fills_and_oco_breach(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        take = book.create(
            client_order_id="take",
            instrument="ABC",
            side="SELL",
            requested_quantity="1",
            oco_group_id="g1",
        )
        stop = book.create(
            client_order_id="stop",
            instrument="ABC",
            side="SELL",
            requested_quantity="1",
            oco_group_id="g1",
        )
        take.record_fill(
            fill_id="f-take",
            provider_execution_id="exec-take",
            quantity="1",
            price="110",
        )
        stop.record_fill(
            fill_id="f-stop",
            provider_execution_id="exec-stop",
            quantity="1",
            price="90",
        )
        self.assertEqual(len(book.effective_fills()), 2)
        self.assertEqual(book.oco_breaches()["g1"], ("stop", "take"))

    def test_order_book_snapshots_surface_observed_oco_breach(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        first = book.create(
            client_order_id="oco-a",
            instrument="ABC",
            side="SELL",
            requested_quantity="1",
            oco_group_id="g-snapshot",
        )
        second = book.create(
            client_order_id="oco-b",
            instrument="ABC",
            side="SELL",
            requested_quantity="1",
            oco_group_id="g-snapshot",
        )
        first.record_fill(
            fill_id="fill-a",
            provider_execution_id="exec-a-snapshot",
            quantity="1",
            price="110",
        )
        second.record_fill(
            fill_id="fill-b",
            provider_execution_id="exec-b-snapshot",
            quantity="1",
            price="90",
        )

        snapshots = {
            snapshot.client_order_id: snapshot
            for snapshot in book.snapshots()
        }

        self.assertEqual(snapshots["oco-a"].state, "OCO_VIOLATION")
        self.assertEqual(snapshots["oco-b"].state, "OCO_VIOLATION")
        self.assertTrue(snapshots["oco-a"].oco_violation)
        self.assertTrue(snapshots["oco-b"].oco_violation)
        self.assertEqual(
            book.oco_breaches()["g-snapshot"],
            ("oco-a", "oco-b"),
        )
        self.assertEqual(
            book.active_oco_breaches()["g-snapshot"],
            ("oco-a", "oco-b"),
        )
        # Rendering the aggregate must not mutate local order state.
        self.assertEqual(first.state, "FILLED")
        self.assertEqual(second.state, "FILLED")

    def test_oco_history_is_query_order_independent_after_bust(self):
        def build(*, read_before_bust):
            book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
            first = book.create(
                client_order_id="oco-a",
                instrument="ABC",
                side="SELL",
                requested_quantity="1",
                oco_group_id="g-race",
            )
            second = book.create(
                client_order_id="oco-b",
                instrument="ABC",
                side="SELL",
                requested_quantity="1",
                oco_group_id="g-race",
            )
            first.record_fill(
                fill_id="fill-a",
                provider_execution_id="exec-a-race",
                quantity="1",
                price="110",
            )
            second.record_fill(
                fill_id="fill-b",
                provider_execution_id="exec-b-race",
                quantity="1",
                price="90",
            )
            if read_before_bust:
                before = {
                    item.client_order_id: item
                    for item in book.snapshots()
                }
                self.assertTrue(before["oco-a"].oco_violation)
                self.assertTrue(before["oco-b"].oco_violation)
            second.bust_fill(
                "fill-b",
                provider_revision="bust-1",
            )
            snapshots = {
                item.client_order_id: item
                for item in book.snapshots()
            }
            return (
                snapshots,
                dict(book.oco_breaches()),
                dict(book.active_oco_breaches()),
                first.state,
                second.state,
            )

        read_first = build(read_before_bust=True)
        rebuild_without_intermediate_read = build(read_before_bust=False)

        self.assertEqual(read_first, rebuild_without_intermediate_read)
        snapshots, historical, active, first_state, second_state = read_first
        self.assertEqual(
            historical["g-race"],
            ("oco-a", "oco-b"),
        )
        self.assertEqual(active, {})
        self.assertTrue(snapshots["oco-a"].oco_violation)
        self.assertTrue(snapshots["oco-b"].oco_violation)
        self.assertEqual(snapshots["oco-a"].state, "OCO_VIOLATION")
        self.assertEqual(snapshots["oco-b"].state, "OCO_VIOLATION")
        self.assertEqual(snapshots["oco-a"].filled_quantity, Decimal("1"))
        self.assertEqual(snapshots["oco-b"].filled_quantity, Decimal("0"))
        self.assertEqual(first_state, "FILLED")
        self.assertEqual(second_state, "PENDING")


    def test_unknown_submission_can_resolve_to_accepted(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="UNKNOWN")
        self.assertEqual(item.state, "UNKNOWN")
        item.acknowledge(provider_order_id="p1", status="ACCEPTED")
        self.assertEqual(item.state, "WORKING")

    def test_unknown_submission_can_resolve_to_rejected(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="UNKNOWN")
        item.acknowledge(provider_order_id="p1", status="REJECTED")
        self.assertEqual(item.state, "REJECTED")

    def test_accepted_submission_cannot_regress_to_unknown(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="ACCEPTED")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "terminal submission outcome",
        ):
            item.acknowledge(provider_order_id="p1", status="UNKNOWN")
        self.assertEqual(item.state, "WORKING")

    def test_accepted_submission_cannot_flip_to_rejected(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="ACCEPTED")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "terminal submission outcome",
        ):
            item.acknowledge(provider_order_id="p1", status="REJECTED")
        self.assertEqual(item.state, "WORKING")

    def test_rejected_submission_cannot_flip_to_accepted(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="REJECTED")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "terminal submission outcome",
        ):
            item.acknowledge(provider_order_id="p1", status="ACCEPTED")
        self.assertEqual(item.state, "REJECTED")

    def test_rejected_order_cannot_be_relabelled_cancelled(self):
        item = order()
        item.acknowledge(provider_order_id="p1", status="REJECTED")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "confirmed cancelled",
        ):
            item.confirm_cancel()
        self.assertEqual(item.state, "REJECTED")

    def test_amendment_child_must_keep_parent_instrument_and_side(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        book.create(
            client_order_id="parent",
            instrument="ABC",
            side="BUY",
            requested_quantity="5",
        )
        with self.assertRaisesRegex(OrderProjectionConflict, "instrument"):
            book.create(
                client_order_id="wrong-instrument",
                instrument="XYZ",
                side="BUY",
                requested_quantity="4",
                parent_intent_id="parent",
            )
        with self.assertRaisesRegex(OrderProjectionConflict, "side"):
            book.create(
                client_order_id="wrong-side",
                instrument="ABC",
                side="SELL",
                requested_quantity="4",
                parent_intent_id="parent",
            )
        self.assertIsNone(book.amendment_child("parent"))

    def test_amendment_child_cannot_escape_parent_oco_group(self):
        book = OrderBookProjection(provider_id="PROVIDER-A", account_id="acct-1", environment="PAPER")
        book.create(
            client_order_id="parent",
            instrument="ABC",
            side="SELL",
            requested_quantity="5",
            oco_group_id="protective-group",
        )
        with self.assertRaisesRegex(OrderProjectionConflict, "OCO group"):
            book.create(
                client_order_id="child",
                instrument="ABC",
                side="SELL",
                requested_quantity="4",
                oco_group_id=None,
                parent_intent_id="parent",
            )
        self.assertIsNone(book.amendment_child("parent"))

    def test_replace_request_is_pending_and_does_not_invent_fill(self):
        item = order(requested_quantity="5")
        item.acknowledge(provider_order_id="p1")
        item.request_replace(command_id="replace-command")
        snap = item.snapshot()
        self.assertEqual(snap.state, "REPLACE_REQUESTED")
        self.assertTrue(snap.replace_requested)
        self.assertEqual(snap.filled_quantity, Decimal("0"))
        self.assertEqual(snap.open_quantity, Decimal("5"))

    def test_partial_fill_replace_request_preserves_economic_truth(self):
        item = order(requested_quantity="5")
        item.acknowledge(provider_order_id="p1")
        item.record_fill(
            fill_id="partial-before-replace",
            provider_execution_id="exec-partial-before-replace",
            quantity="2",
            price="10",
        )
        item.request_replace(command_id="replace-command")
        snap = item.snapshot()
        self.assertEqual(snap.state, "PARTIALLY_FILLED_REPLACE_REQUESTED")
        self.assertEqual(snap.filled_quantity, Decimal("2"))
        self.assertEqual(snap.open_quantity, Decimal("3"))
        self.assertTrue(snap.replace_requested)

    def test_cancel_and_replace_requests_are_mutually_exclusive(self):
        cancel_first = order(client_order_id="cancel-first")
        cancel_first.request_cancel(command_id="cancel-command")
        with self.assertRaisesRegex(OrderProjectionConflict, "pending together"):
            cancel_first.request_replace(command_id="replace-command")

        replace_first = order(client_order_id="replace-first")
        replace_first.request_replace(command_id="replace-command")
        with self.assertRaisesRegex(OrderProjectionConflict, "pending together"):
            replace_first.request_cancel(command_id="cancel-command")
        with self.assertRaisesRegex(OrderProjectionConflict, "must resolve"):
            replace_first.confirm_cancel()

    def test_expiry_is_terminal_for_remainder_but_late_fill_stays_visible(self):
        item = order(requested_quantity="5")
        item.acknowledge(provider_order_id="p1")
        item.record_fill(
            fill_id="before-expiry",
            provider_execution_id="exec-before-expiry",
            quantity="2",
            price="10",
        )
        item.confirm_expired()
        expired = item.snapshot()
        self.assertEqual(expired.state, "PARTIALLY_FILLED_EXPIRED")
        self.assertTrue(expired.expired)
        self.assertEqual(expired.filled_quantity, Decimal("2"))
        self.assertEqual(expired.open_quantity, Decimal("3"))

        item.record_fill(
            fill_id="late-after-expiry",
            provider_execution_id="exec-late-after-expiry",
            quantity="3",
            price="11",
        )
        late = item.snapshot()
        self.assertEqual(late.state, "FILLED_AFTER_EXPIRY")
        self.assertEqual(late.filled_quantity, Decimal("5"))
        self.assertEqual(late.open_quantity, Decimal("0"))

    def test_overfill_after_expiry_is_explicit(self):
        item = order(requested_quantity="1")
        item.confirm_expired()
        item.record_fill(
            fill_id="late-expiry-overfill",
            provider_execution_id="exec-late-expiry-overfill",
            quantity="1.25",
            price="10",
        )
        self.assertEqual(item.state, "OVERFILLED_AFTER_EXPIRY")
        self.assertEqual(item.snapshot().overfill_quantity, Decimal("0.25"))

    def test_terminal_outcomes_cannot_be_relabelled_expired(self):
        rejected = order(client_order_id="rejected")
        rejected.acknowledge(provider_order_id="p-rejected", status="REJECTED")
        with self.assertRaisesRegex(OrderProjectionConflict, "relabelled expired"):
            rejected.confirm_expired()

        cancelled = order(client_order_id="cancelled")
        cancelled.confirm_cancel()
        with self.assertRaisesRegex(OrderProjectionConflict, "relabelled expired"):
            cancelled.confirm_expired()

    def test_expiry_requires_pending_cancel_or_replace_to_resolve(self):
        cancelling = order(client_order_id="cancelling")
        cancelling.request_cancel(command_id="cancel-command")
        with self.assertRaisesRegex(OrderProjectionConflict, "must resolve"):
            cancelling.confirm_expired()

        replacing = order(client_order_id="replacing")
        replacing.request_replace(command_id="replace-command")
        with self.assertRaisesRegex(OrderProjectionConflict, "must resolve"):
            replacing.confirm_expired()

    def test_terminal_or_fully_filled_order_rejects_new_replace_request(self):
        rejected = order(client_order_id="rejected")
        rejected.acknowledge(provider_order_id="p-r", status="REJECTED")
        with self.assertRaisesRegex(OrderProjectionConflict, "terminal order"):
            rejected.request_replace(command_id="replace-command")

        expired = order(client_order_id="expired")
        expired.confirm_expired()
        with self.assertRaisesRegex(OrderProjectionConflict, "terminal order"):
            expired.request_replace(command_id="replace-command")

        filled = order(client_order_id="filled", requested_quantity="1")
        filled.record_fill(
            fill_id="full",
            provider_execution_id="exec-full",
            quantity="1",
            price="10",
        )
        with self.assertRaisesRegex(OrderProjectionConflict, "fully filled"):
            filled.request_replace(command_id="replace-command")


    def test_snapshot_binds_provider_account_and_environment_scope(self):
        item = order(
            provider_id=" provider-a ",
            account_id=" account-1 ",
            environment="paper",
        )
        snap = item.snapshot()
        self.assertEqual(snap.provider_id, "PROVIDER-A")
        self.assertEqual(snap.account_id, "account-1")
        self.assertEqual(snap.environment, "PAPER")

    def test_invalid_environment_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "unsupported environment"):
            order(environment="PRODUCTION")
        with self.assertRaisesRegex(ValueError, "unsupported environment"):
            OrderBookProjection(
                provider_id="PROVIDER-A",
                account_id="acct-1",
                environment="PRODUCTION",
            )

    def test_order_book_rejects_cross_scope_registration(self):
        book = OrderBookProjection(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
        )
        mismatches = (
            order(client_order_id="provider", provider_id="PROVIDER-B"),
            order(client_order_id="account", account_id="acct-2"),
            order(client_order_id="environment", environment="LIVE"),
        )
        for item in mismatches:
            with self.subTest(client_order_id=item.client_order_id):
                with self.assertRaisesRegex(
                    OrderProjectionConflict,
                    "scope differs from book",
                ):
                    book.register(item)
        self.assertEqual(book.snapshots(), ())

    def test_book_create_inherits_exact_scope(self):
        book = OrderBookProjection(
            provider_id="provider-a",
            account_id="acct-1",
            environment="paper",
        )
        item = book.create(
            client_order_id="scoped",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
        )
        self.assertEqual(
            (item.provider_id, item.account_id, item.environment),
            ("PROVIDER-A", "acct-1", "PAPER"),
        )

    def test_oco_group_rejects_cross_scope_peer(self):
        group = OcoGroupProjection("g-scope")
        group.add(order(client_order_id="a", oco_group_id="g-scope"))
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "OCO peers must share",
        ):
            group.add(
                order(
                    client_order_id="b",
                    provider_id="PROVIDER-B",
                    oco_group_id="g-scope",
                )
            )


    def test_unknown_submission_does_not_require_provider_order_id(self):
        item = order()
        item.acknowledge(status="UNKNOWN")
        snap = item.snapshot()
        self.assertEqual(snap.state, "UNKNOWN")
        self.assertIsNone(snap.provider_order_id)

        item.acknowledge(provider_order_id="provider-later", status="ACCEPTED")
        resolved = item.snapshot()
        self.assertEqual(resolved.state, "WORKING")
        self.assertEqual(resolved.provider_order_id, "provider-later")

    def test_rejected_submission_does_not_invent_provider_order_id(self):
        item = order()
        item.acknowledge(status="REJECTED")
        self.assertEqual(item.state, "REJECTED")
        self.assertIsNone(item.provider_order_id)

    def test_later_same_provider_identity_can_fill_optional_ack_identity(self):
        item = order()
        item.acknowledge(status="ACCEPTED")
        self.assertIsNone(item.provider_order_id)
        item.acknowledge(provider_order_id="provider-1", status="ACCEPTED")
        self.assertEqual(item.provider_order_id, "provider-1")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "provider_order_id changed",
        ):
            item.acknowledge(provider_order_id="provider-2", status="ACCEPTED")


    def test_snapshot_surfaces_amendment_and_action_command_lineage(self):
        book = OrderBookProjection(
            provider_id="PROVIDER-A",
            account_id="acct-1",
            environment="PAPER",
        )
        parent = book.create(
            client_order_id="parent-lineage",
            instrument="ABC",
            side="BUY",
            requested_quantity="2",
        )
        parent.request_replace(command_id="replace-command-42")
        child = book.create(
            client_order_id="child-lineage",
            instrument="ABC",
            side="BUY",
            requested_quantity="1",
            parent_intent_id="parent-lineage",
        )
        parent_snapshot = parent.snapshot()
        child_snapshot = child.snapshot()
        self.assertEqual(parent_snapshot.replace_command_id, "replace-command-42")
        self.assertTrue(parent_snapshot.replace_requested)
        self.assertEqual(child_snapshot.parent_intent_id, "parent-lineage")

        cancelling = order(client_order_id="cancel-lineage")
        cancelling.request_cancel(command_id="cancel-command-42")
        self.assertEqual(
            cancelling.snapshot().cancel_command_id,
            "cancel-command-42",
        )

    def test_action_command_id_retries_are_idempotent_but_aliases_conflict(self):
        cancelling = order(client_order_id="cancel-command-order")
        cancelling.request_cancel(command_id="cancel-1")
        cancelling.request_cancel(command_id="cancel-1")
        with self.assertRaisesRegex(OrderProjectionConflict, "different command_id"):
            cancelling.request_cancel(command_id="cancel-2")

        replacing = order(client_order_id="replace-command-order")
        replacing.request_replace(command_id="replace-1")
        replacing.request_replace(command_id="replace-1")
        with self.assertRaisesRegex(OrderProjectionConflict, "different command_id"):
            replacing.request_replace(command_id="replace-2")


    def test_canonical_acknowledged_submission_outcome_maps_to_working(self):
        item = order()
        item.acknowledge(
            provider_order_id="provider-canonical",
            status="ACKNOWLEDGED",
        )
        self.assertEqual(item.submission_state, "ACCEPTED")
        self.assertEqual(item.state, "WORKING")
        self.assertEqual(item.provider_order_id, "provider-canonical")


    def test_send_started_binds_attempt_without_inventing_ack_or_fill(self):
        item = order()
        self.assertEqual(item.state, "PENDING")
        item.mark_send_started(attempt_id="attempt-1")
        snap = item.snapshot()
        self.assertEqual(snap.state, "SEND_STARTED")
        self.assertEqual(snap.submission_attempt_id, "attempt-1")
        self.assertIsNone(snap.provider_order_id)
        self.assertEqual(snap.filled_quantity, Decimal("0"))

        item.mark_send_started(attempt_id="attempt-1")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "different submission attempt",
        ):
            item.mark_send_started(attempt_id="attempt-2")

    def test_acknowledgement_binds_same_attempt_and_preserves_lineage(self):
        item = order()
        item.mark_send_started(attempt_id="attempt-ack")
        item.acknowledge(
            attempt_id="attempt-ack",
            provider_order_id="provider-ack",
            status="ACKNOWLEDGED",
        )
        snap = item.snapshot()
        self.assertEqual(snap.state, "WORKING")
        self.assertEqual(snap.submission_attempt_id, "attempt-ack")
        with self.assertRaisesRegex(
            OrderProjectionConflict,
            "different submission attempt",
        ):
            item.acknowledge(
                attempt_id="other-attempt",
                provider_order_id="provider-ack",
                status="ACKNOWLEDGED",
            )

    def test_unknown_can_bind_attempt_without_provider_order_identity(self):
        item = order()
        item.acknowledge(
            attempt_id="attempt-unknown",
            status="UNKNOWN",
        )
        snap = item.snapshot()
        self.assertEqual(snap.state, "UNKNOWN")
        self.assertEqual(snap.submission_attempt_id, "attempt-unknown")
        self.assertIsNone(snap.provider_order_id)

    def test_fill_before_send_projection_does_not_erase_later_attempt_lineage(self):
        item = order(requested_quantity="2")
        item.record_fill(
            fill_id="early-fill",
            provider_execution_id="early-exec",
            quantity="1",
            price="10",
        )
        self.assertEqual(item.state, "PARTIALLY_FILLED")
        item.mark_send_started(attempt_id="attempt-late-projection")
        snap = item.snapshot()
        self.assertEqual(snap.state, "PARTIALLY_FILLED")
        self.assertEqual(
            snap.submission_attempt_id,
            "attempt-late-projection",
        )


if __name__ == "__main__":
    unittest.main()
