import hashlib
import unittest

from mvp.autotrade_mvp.kraken_spot_stream import (
    KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION,
    KrakenSpotExecutionsSubscriptionBinding,
    KrakenSpotExecutionStreamRecovery,
    KrakenSpotStreamError,
    parse_execution_frame,
    parse_executions_subscription_ack,
)


def frame_bytes(
    *,
    frame_type="snapshot",
    sequence=1,
    reports=None,
    channel="executions",
):
    if reports is None:
        reports = [
            {
                "order_id": "O-1",
                "cl_ord_id": "client-1",
                "exec_type": "new",
                "order_status": "new",
            }
        ]
    import json

    return json.dumps(
        {
            "channel": channel,
            "type": frame_type,
            "data": reports,
            "sequence": sequence,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def ack_bytes(
    *,
    success=True,
    channel="executions",
    snap_orders=True,
    snap_trades=False,
    req_id=7,
):
    import json

    payload = {
        "method": "subscribe",
        "req_id": req_id,
        "success": success,
        "result": {
            "channel": channel,
            "snap_orders": snap_orders,
            "snap_trades": snap_trades,
        },
    }
    if not success:
        payload["error"] = "subscription rejected"
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def subscription_binding(
    *,
    account_id="spot-live-1",
    connection_generation=1,
    req_id=7,
):
    return KrakenSpotExecutionsSubscriptionBinding.create(
        account_id=account_id,
        connection_generation=connection_generation,
        req_id=req_id,
    )


class KrakenSpotExecutionFrameTests(unittest.TestCase):
    def test_subscription_profile_requires_open_order_snapshot_without_trade_snapshot(self):
        self.assertEqual(
            dict(KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION),
            {
                "channel": "executions",
                "snap_orders": True,
                "snap_trades": False,
                "order_status": True,
            },
        )
        self.assertNotIn("token", KRAKEN_SPOT_EXECUTIONS_SUBSCRIPTION)

    def test_exact_frame_binds_scope_sequence_identity_and_raw_hash(self):
        raw = frame_bytes(
            frame_type="update",
            sequence=42,
            reports=[
                {
                    "order_id": "O-1",
                    "cl_ord_id": "client-1",
                    "exec_id": "E-1",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                }
            ],
        )
        frame = parse_execution_frame(
            raw,
            account_id="spot-live-1",
            connection_generation=1,
        )

        self.assertEqual(frame.account_id, "spot-live-1")
        self.assertEqual(frame.environment, "LIVE")
        self.assertEqual(frame.frame_type, "update")
        self.assertEqual(frame.sequence, 42)
        self.assertEqual(frame.response_bytes, raw)
        self.assertEqual(
            frame.evidence_ref,
            "provider-stream:sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(len(frame.reports), 1)
        report = frame.reports[0]
        self.assertEqual(report.order_id, "O-1")
        self.assertEqual(report.client_order_id, "client-1")
        self.assertEqual(report.exec_id, "E-1")
        self.assertEqual(report.exec_type, "trade")
        self.assertEqual(report.order_status, "partially_filled")

    def test_byte_distinct_frames_have_distinct_evidence(self):
        compact = (
            b'{"channel":"executions","type":"snapshot","data":[],"sequence":1}'
        )
        spaced = (
            b'{"channel": "executions", "type":"snapshot","data":[],"sequence":1}'
        )
        first = parse_execution_frame(
            compact,
            account_id="acct",
            connection_generation=1,
        )
        second = parse_execution_frame(
            spaced,
            account_id="acct",
            connection_generation=1,
        )
        self.assertNotEqual(first.evidence_ref, second.evidence_ref)
        self.assertNotEqual(first.response_bytes, second.response_bytes)

    def test_parser_rejects_noncanonical_or_ambiguous_frames(self):
        cases = (
            (
                b'{"channel":"executions","channel":"executions","type":"snapshot","data":[],"sequence":1}',
                "duplicate JSON object field",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":[],"sequence":1,"token":"secret"}',
                "fields are not canonical",
            ),
            (
                frame_bytes(channel="balances"),
                "channel must be executions",
            ),
            (
                frame_bytes(frame_type="heartbeat"),
                "type must be snapshot or update",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":[],"sequence":true}',
                "sequence must be a non-negative integer",
            ),
            (
                b'{"channel":"executions","type":"snapshot","data":{},"sequence":1}',
                "data must be an array",
            ),
            (
                frame_bytes(reports=[{"exec_type": "new", "order_status": "new"}]),
                "lacks order_id",
            ),
            (
                frame_bytes(reports=[{"order_id": "O-1", "order_status": "new"}]),
                "lacks exec_type",
            ),
            (
                frame_bytes(
                    reports=[
                        {
                            "order_id": "O-1",
                            "exec_type": "mystery",
                            "order_status": "new",
                        }
                    ]
                ),
                "exec_type is unsupported",
            ),
            (
                frame_bytes(
                    reports=[
                        {
                            "order_id": "O-1",
                            "exec_type": "status",
                            "order_status": "unknown",
                        }
                    ]
                ),
                "order_status is unsupported",
            ),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KrakenSpotStreamError,
                message,
            ):
                parse_execution_frame(
                    raw,
                    account_id="acct",
                    connection_generation=1,
                )

    def test_subscription_ack_binds_canonical_request_profile_and_exact_bytes(self):
        binding = subscription_binding()
        raw = ack_bytes(req_id=binding.req_id)
        acknowledgement = parse_executions_subscription_ack(
            raw,
            subscription_binding=binding,
        )
        self.assertEqual(acknowledgement.account_id, "spot-live-1")
        self.assertEqual(acknowledgement.environment, "LIVE")
        self.assertIs(acknowledgement.subscription_binding, binding)
        self.assertEqual(
            acknowledgement.subscription_binding.evidence_ref,
            binding.evidence_ref,
        )
        self.assertEqual(
            acknowledgement.evidence_ref,
            "provider-stream:sha256:" + hashlib.sha256(raw).hexdigest(),
        )
        self.assertNotIn(b"token", acknowledgement.response_bytes.lower())
        self.assertNotIn("token", binding.evidence_ref)

    def test_subscription_binding_rejects_noncanonical_profile(self):
        valid = subscription_binding()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "profile is not canonical",
        ):
            KrakenSpotExecutionsSubscriptionBinding(
                account_id=valid.account_id,
                environment=valid.environment,
                req_id=valid.req_id,
                profile_items=(
                    ("channel", "executions"),
                    ("order_status", False),
                    ("snap_orders", True),
                    ("snap_trades", False),
                ),
                evidence_ref=valid.evidence_ref,
            )

    def test_subscription_ack_rejects_wrong_profile_failed_or_wrong_request(self):
        binding = subscription_binding()
        cases = (
            (ack_bytes(success=False), "was not accepted"),
            (ack_bytes(channel="balances"), "channel must be executions"),
            (ack_bytes(snap_orders=False), "snap_orders=true"),
            (ack_bytes(snap_trades=True), "snap_trades=false"),
            (ack_bytes(req_id=binding.req_id + 1), "req_id does not match"),
        )
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                KrakenSpotStreamError,
                message,
            ):
                parse_executions_subscription_ack(
                    raw,
                    subscription_binding=binding,
                )

    def test_parser_is_live_only_and_scope_text_is_canonical(self):
        raw = frame_bytes()
        with self.assertRaisesRegex(KrakenSpotStreamError, "permits LIVE only"):
            parse_execution_frame(
                raw,
                account_id="acct",
                connection_generation=1,
                environment="PAPER",
            )
        with self.assertRaisesRegex(KrakenSpotStreamError, "canonical text"):
            parse_execution_frame(
                raw,
                account_id=" acct ",
                connection_generation=1,
            )


class KrakenSpotExecutionStreamRecoveryTests(unittest.TestCase):
    def make_recovery(self, **kwargs):
        return KrakenSpotExecutionStreamRecovery(
            account_id="spot-live-1",
            **kwargs,
        )

    def acknowledge(
        self,
        recovery,
        *,
        account_id="spot-live-1",
        connection_generation=None,
        req_id=7,
    ):
        if connection_generation is None:
            connection_generation = recovery.connection_generation
        binding = subscription_binding(
            account_id=account_id,
            connection_generation=connection_generation,
            req_id=req_id,
        )
        acknowledgement = parse_executions_subscription_ack(
            ack_bytes(req_id=req_id),
            subscription_binding=binding,
        )
        recovery.apply_subscription_ack(acknowledgement)
        return acknowledgement

    def parse(
        self,
        *,
        frame_type,
        sequence,
        reports=None,
        account_id="spot-live-1",
        connection_generation=1,
    ):
        return parse_execution_frame(
            frame_bytes(
                frame_type=frame_type,
                sequence=sequence,
                reports=reports,
            ),
            account_id=account_id,
            connection_generation=connection_generation,
        )

    def test_generation_requires_fresh_snapshot_and_never_grants_ready(self):
        recovery = self.make_recovery()
        generation = recovery.begin_connection()
        self.assertEqual(generation, 1)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertEqual(
            recovery.evidence().recovery_reason,
            "fresh_subscription_required",
        )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires subscription acknowledgement",
        ):
            recovery.apply_frame(
                self.parse(frame_type="update", sequence=1)
            )

        acknowledgement = self.acknowledge(recovery)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.AWAITING_SNAPSHOT,
        )
        self.assertEqual(
            recovery.evidence().subscription_binding_evidence_ref,
            acknowledgement.subscription_binding.evidence_ref,
        )
        self.assertEqual(
            recovery.evidence().subscription_ack_evidence_ref,
            acknowledgement.evidence_ref,
        )
        self.assertEqual(
            recovery.evidence().recovery_reason,
            "fresh_snapshot_required",
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires snapshot before updates",
        ):
            recovery.apply_frame(
                self.parse(frame_type="update", sequence=1)
            )

        snapshot = self.parse(
            frame_type="snapshot",
            sequence=7,
            reports=[
                {
                    "order_id": "O-2",
                    "exec_type": "new",
                    "order_status": "new",
                },
                {
                    "order_id": "O-1",
                    "exec_type": "status",
                    "order_status": "partially_filled",
                },
            ],
        )
        recovery.apply_frame(snapshot)
        evidence = recovery.evidence()

        self.assertEqual(
            evidence.phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.snapshot_sequence, 7)
        self.assertEqual(evidence.last_sequence, 7)
        self.assertEqual(evidence.snapshot_evidence_ref, snapshot.evidence_ref)
        self.assertEqual(
            evidence.provisional_snapshot_order_ids,
            ("O-1", "O-2"),
        )
        self.assertEqual(evidence.buffered_update_evidence_refs, ())
        self.assertEqual(
            evidence.recovery_reason,
            "snapshot_requires_rest_crosscheck",
        )
        self.assertFalse(evidence.trading_ready)

    def test_snapshot_rejects_terminal_orders_under_snap_orders_contract(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        terminal = self.parse(
            frame_type="snapshot",
            sequence=1,
            reports=[
                {
                    "order_id": "O-DONE",
                    "exec_type": "filled",
                    "order_status": "filled",
                }
            ],
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "snapshot contains terminal orders",
        ):
            recovery.apply_frame(terminal)

    def test_contiguous_updates_are_buffered_as_exact_reconciliation_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(frame_type="snapshot", sequence=10)
        )
        update_11 = self.parse(
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "exec_id": "E-1",
                }
            ],
        )
        update_12 = self.parse(
            frame_type="update",
            sequence=12,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "filled",
                    "order_status": "filled",
                }
            ],
        )
        recovery.apply_frame(update_11)
        recovery.apply_frame(update_12)
        evidence = recovery.evidence()

        self.assertEqual(
            evidence.phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.last_sequence, 12)
        self.assertEqual(
            evidence.buffered_update_evidence_refs,
            (update_11.evidence_ref, update_12.evidence_ref),
        )
        self.assertFalse(evidence.trading_ready)

    def test_duplicate_or_forward_sequence_gap_fails_closed_and_discards_projection(self):
        for observed in (10, 13):
            with self.subTest(observed=observed):
                recovery = self.make_recovery()
                recovery.begin_connection()
                self.acknowledge(recovery)
                recovery.apply_frame(
                    self.parse(frame_type="snapshot", sequence=10)
                )
                gap = self.parse(
                    frame_type="update",
                    sequence=observed,
                )
                recovery.apply_frame(gap)
                evidence = recovery.evidence()

                self.assertEqual(
                    evidence.phase,
                    recovery.GAP_RECONCILIATION_REQUIRED,
                )
                self.assertEqual(evidence.gap_expected_sequence, 11)
                self.assertEqual(evidence.gap_observed_sequence, observed)
                self.assertEqual(
                    evidence.gap_evidence_ref,
                    gap.evidence_ref,
                )
                self.assertEqual(evidence.recovery_reason, "sequence_gap")
                self.assertEqual(
                    evidence.provisional_snapshot_order_ids,
                    (),
                )
                self.assertEqual(
                    evidence.buffered_update_evidence_refs,
                    (),
                )
                self.assertFalse(evidence.trading_ready)
                with self.assertRaisesRegex(
                    KrakenSpotStreamError,
                    "gap requires a fresh connection",
                ):
                    recovery.apply_frame(
                        self.parse(frame_type="update", sequence=11)
                    )

    def test_reconnect_discards_stale_generation_and_requires_new_snapshot(self):
        recovery = self.make_recovery()
        self.assertEqual(recovery.begin_connection(), 1)
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(frame_type="snapshot", sequence=20)
        )
        recovery.apply_frame(
            self.parse(frame_type="update", sequence=21)
        )

        self.assertEqual(recovery.begin_connection(), 2)
        reset = recovery.evidence()
        self.assertEqual(
            reset.phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertIsNone(reset.snapshot_sequence)
        self.assertIsNone(reset.last_sequence)
        self.assertIsNone(reset.snapshot_evidence_ref)
        self.assertEqual(reset.buffered_update_evidence_refs, ())
        self.assertEqual(reset.provisional_snapshot_order_ids, ())
        self.assertEqual(
            reset.recovery_reason,
            "fresh_subscription_required",
        )

        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires subscription acknowledgement",
        ):
            recovery.apply_frame(
                self.parse(frame_type="update", sequence=22)
            )
        self.acknowledge(recovery)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires snapshot before updates",
        ):
            recovery.apply_frame(
                self.parse(frame_type="update", sequence=22)
            )
        fresh = self.parse(
            frame_type="snapshot",
            sequence=1,
            reports=[],
            connection_generation=2,
        )
        recovery.apply_frame(fresh)
        self.assertEqual(
            recovery.evidence().phase,
            recovery.REST_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(recovery.evidence().connection_generation, 2)

    def test_reconnect_rejects_stale_generation_ack_snapshot_and_expected_update(self):
        recovery = self.make_recovery()
        self.assertEqual(recovery.begin_connection(), 1)

        stale_binding = subscription_binding(connection_generation=1, req_id=91)
        stale_ack = parse_executions_subscription_ack(
            ack_bytes(req_id=91),
            subscription_binding=stale_binding,
        )
        stale_snapshot = self.parse(
            frame_type="snapshot",
            sequence=10,
            connection_generation=1,
        )
        stale_update = self.parse(
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-STALE",
                    "exec_type": "trade",
                    "order_status": "partially_filled",
                    "exec_id": "E-STALE",
                }
            ],
            connection_generation=1,
        )

        self.assertEqual(recovery.begin_connection(), 2)
        before = recovery.evidence()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "acknowledgement generation mismatch",
        ):
            recovery.apply_subscription_ack(stale_ack)
        self.assertEqual(recovery.evidence(), before)

        self.acknowledge(recovery, req_id=92)
        awaiting_snapshot = recovery.evidence()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "connection generation mismatch",
        ):
            recovery.apply_frame(stale_snapshot)
        self.assertEqual(recovery.evidence(), awaiting_snapshot)

        fresh_snapshot = self.parse(
            frame_type="snapshot",
            sequence=10,
            connection_generation=2,
        )
        recovery.apply_frame(fresh_snapshot)
        before_expected_stale_update = recovery.evidence()
        self.assertEqual(before_expected_stale_update.last_sequence, 10)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "connection generation mismatch",
        ):
            recovery.apply_frame(stale_update)
        self.assertEqual(
            recovery.evidence(),
            before_expected_stale_update,
        )

    def test_disconnect_invalidates_all_stream_local_state(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(frame_type="snapshot", sequence=2)
        )
        recovery.disconnect()
        evidence = recovery.evidence()
        self.assertEqual(evidence.phase, recovery.DISCONNECTED)
        self.assertIsNone(evidence.last_sequence)
        self.assertEqual(evidence.provisional_snapshot_order_ids, ())
        self.assertIsNone(evidence.recovery_reason)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "while disconnected",
        ):
            recovery.apply_frame(
                self.parse(frame_type="snapshot", sequence=3)
            )

    def test_cross_account_subscription_ack_is_rejected_before_state_mutation(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "acknowledgement scope mismatch",
        ):
            self.acknowledge(recovery, account_id="other-account")
        evidence = recovery.evidence()
        self.assertEqual(
            evidence.phase,
            recovery.AWAITING_SUBSCRIPTION_ACK,
        )
        self.assertIsNone(evidence.subscription_binding_evidence_ref)
        self.assertIsNone(evidence.subscription_ack_evidence_ref)

    def test_cross_account_frame_is_rejected_before_state_mutation(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        self.acknowledge(recovery)
        wrong = self.parse(
            frame_type="snapshot",
            sequence=1,
            account_id="other-account",
        )
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "account/environment scope mismatch",
        ):
            recovery.apply_frame(wrong)
        evidence = recovery.evidence()
        self.assertEqual(evidence.phase, recovery.AWAITING_SNAPSHOT)
        self.assertIsNone(evidence.last_sequence)

    def test_rest_crosscheck_plan_chunks_query_orders_and_never_grants_ready(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        acknowledgement = self.acknowledge(recovery)
        reports = [
            {
                "order_id": f"O-{index:03d}",
                "exec_type": "new",
                "order_status": "new",
            }
            for index in range(51)
        ]
        snapshot = self.parse(
            frame_type="snapshot",
            sequence=5,
            reports=reports,
        )
        recovery.apply_frame(snapshot)

        plan = recovery.rest_crosscheck_plan()
        self.assertEqual(
            plan.required_endpoints,
            (
                "/0/private/OpenOrders",
                "/0/private/ClosedOrders",
                "/0/private/TradesHistory",
                "/0/private/QueryOrders",
            ),
        )
        self.assertEqual(len(plan.query_order_chunks), 2)
        self.assertEqual(len(plan.query_order_chunks[0]), 50)
        self.assertEqual(len(plan.query_order_chunks[1]), 1)
        self.assertEqual(
            tuple(
                order_id
                for chunk in plan.query_order_chunks
                for order_id in chunk
            ),
            tuple(f"O-{index:03d}" for index in range(51)),
        )
        self.assertEqual(
            plan.evidence_refs,
            (
                acknowledgement.subscription_binding.evidence_ref,
                acknowledgement.evidence_ref,
                snapshot.evidence_ref,
            ),
        )
        self.assertEqual(
            plan.recovery_reason,
            "snapshot_requires_rest_crosscheck",
        )
        self.assertFalse(plan.trading_ready)

    def test_gap_crosscheck_plan_retains_known_ids_and_exact_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        acknowledgement = self.acknowledge(recovery)
        snapshot = self.parse(
            frame_type="snapshot",
            sequence=10,
            reports=[
                {
                    "order_id": "O-1",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        update = self.parse(
            frame_type="update",
            sequence=11,
            reports=[
                {
                    "order_id": "O-2",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        gap = self.parse(
            frame_type="update",
            sequence=13,
            reports=[
                {
                    "order_id": "O-3",
                    "exec_type": "new",
                    "order_status": "new",
                }
            ],
        )
        recovery.apply_frame(snapshot)
        recovery.apply_frame(update)
        recovery.apply_frame(gap)

        plan = recovery.rest_crosscheck_plan()
        self.assertEqual(plan.recovery_reason, "sequence_gap")
        self.assertEqual(
            plan.query_order_chunks,
            (("O-1", "O-2", "O-3"),),
        )
        self.assertEqual(
            plan.evidence_refs,
            (
                acknowledgement.subscription_binding.evidence_ref,
                acknowledgement.evidence_ref,
                snapshot.evidence_ref,
                update.evidence_ref,
                gap.evidence_ref,
            ),
        )
        self.assertFalse(plan.trading_ready)

    def test_rest_crosscheck_plan_is_unavailable_before_snapshot_evidence(self):
        recovery = self.make_recovery()
        recovery.begin_connection()
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires stream recovery evidence",
        ):
            recovery.rest_crosscheck_plan()
        self.acknowledge(recovery)
        with self.assertRaisesRegex(
            KrakenSpotStreamError,
            "requires stream recovery evidence",
        ):
            recovery.rest_crosscheck_plan()

    def test_buffer_bound_fails_closed_instead_of_dropping_updates(self):
        recovery = self.make_recovery(max_buffered_updates=1)
        recovery.begin_connection()
        self.acknowledge(recovery)
        recovery.apply_frame(
            self.parse(frame_type="snapshot", sequence=100)
        )
        recovery.apply_frame(
            self.parse(frame_type="update", sequence=101)
        )
        overflow = self.parse(frame_type="update", sequence=102)
        recovery.apply_frame(overflow)
        evidence = recovery.evidence()
        self.assertEqual(
            evidence.phase,
            recovery.GAP_RECONCILIATION_REQUIRED,
        )
        self.assertEqual(evidence.gap_expected_sequence, 102)
        self.assertEqual(evidence.gap_observed_sequence, 102)
        self.assertEqual(evidence.gap_evidence_ref, overflow.evidence_ref)
        self.assertEqual(evidence.buffered_update_evidence_refs, ())
        self.assertEqual(evidence.recovery_reason, "buffer_exhausted")
        self.assertFalse(evidence.trading_ready)


if __name__ == "__main__":
    unittest.main()
