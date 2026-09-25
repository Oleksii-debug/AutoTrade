from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from mvp.autotrade_mvp.capabilities import (
    CapabilityClaim,
    EvidenceVerification,
    derive_capability_snapshot,
)
from mvp.autotrade_mvp.persistence import JournalStore
from mvp.autotrade_mvp.provider_core import (
    Surface,
    observe_authenticated_json_response,
    observe_private_stream_frame,
    prepare_authenticated_read_query,
)
from mvp.autotrade_mvp.provider_stream import (
    DurableProviderPrivateStreamLifecycle,
    ProviderPrivateStreamEvent,
    ProviderPrivateStreamScope,
    ProviderStreamConflict,
    ProviderStreamNotReady,
)
from mvp.autotrade_mvp.reconciliation import (
    SnapshotConsistencyEvidence,
    reconcile_account,
)
from mvp.autotrade_mvp.reconciliation_journal import (
    record_reconciliation_checkpoint,
)


BASE_TIME = datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc)


def _evidence(prefix: str, char: str) -> str:
    return f"{prefix}:sha256:{char * 64}"


def _capability(*, account_id: str = "acct-1"):
    observed_at = BASE_TIME - timedelta(minutes=1)
    claims = tuple(
        CapabilityClaim(
            source=source,
            provider_id="BINANCE",
            account_id=account_id,
            entity_id="binance-private-stream",
            environment="PAPER",
            instrument_version="BTCUSDT@stream-v1",
            observed_at=observed_at,
            expires_at=BASE_TIME + timedelta(hours=1),
            supported_order_types=frozenset({"LIMIT", "MARKET"}),
            time_in_force=frozenset({"GTC", "IOC"}),
            permission_scopes=frozenset({"ORDER.READ"}),
            position_mode="NET",
            native_protection=frozenset(),
            rate_limit_policy_id="binance-private-stream-test",
            data_entitlements=frozenset({"ORDERS"}),
            evidence_ref={
                "artifact_id": str(uuid4()),
                "sha256": "sha256:" + "a" * 64,
                "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
                "source_uri": "https://api.binance.com/",
            },
        )
        for source in ("DOCUMENTED", "API", "ACCOUNT", "INSTRUMENT")
    )
    return derive_capability_snapshot(
        snapshot_id=str(uuid4()),
        claims=claims,
        observed_at=BASE_TIME,
        evidence_verifier=lambda _claim: EvidenceVerification(valid=True),
    )


def _consistency(
    *,
    account_id: str = "acct-1",
    replay_complete: bool = True,
    sequence_gap_detected: bool = False,
):
    return SnapshotConsistencyEvidence(
        provider_id="BINANCE",
        account_id=account_id,
        environment="PAPER",
        mode="COMPOSED",
        query_started_at="2026-09-25T12:00:00Z",
        query_completed_at="2026-09-25T12:00:05Z",
        buffered_stream_events=True,
        replay_complete=replay_complete,
        sequence_gap_detected=sequence_gap_detected,
    )


class DurableProviderPrivateStreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = JournalStore(Path(self.tmp.name) / "journal.sqlite")
        self.scope = ProviderPrivateStreamScope(
            provider_id="BINANCE",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="SPOT_TESTNET",
            stream_name="ACCOUNT_EVENTS",
        )
        self.capability = _capability()
        self.lifecycle = DurableProviderPrivateStreamLifecycle(
            self.store,
            scope=self.scope,
        )

    def start(self, connection="connection-1", *, lifecycle=None):
        target = lifecycle or self.lifecycle
        return target.start_generation(
            command_id=f"start-{connection}",
            idempotency_key=f"start-{connection}",
            connection_id=connection,
            committed_at="2026-09-25T12:00:00Z",
        )

    def read_observation(
        self,
        *,
        provider_environment=None,
        marker="snapshot",
    ):
        provider_env = provider_environment or self.scope.provider_environment
        query = prepare_authenticated_read_query(
            capability=self.capability,
            surface=Surface.AUTHENTICATED_READ,
            endpoint="/api/v3/account",
            query={"marker": marker},
            at=BASE_TIME + timedelta(seconds=1),
            permission_scope="ORDER.READ",
        )
        return observe_authenticated_json_response(
            query_binding=query,
            http_status=200,
            response_bytes=(
                '{"balances":[],"marker":"' + marker + '"}'
            ).encode("utf-8"),
            observed_at=BASE_TIME + timedelta(seconds=2),
            provider_environment=provider_env,
        )

    def reconciliation_result(self, *, consistency=None):
        evidence = consistency or _consistency()
        return reconcile_account(
            provider_id=self.scope.provider_id,
            account_id=self.scope.account_id,
            environment=self.scope.environment,
            provider_environment=self.scope.provider_environment,
            local_cash={"USDT": "1000"},
            provider_cash={"USDT": "1000"},
            local_positions={},
            provider_positions={},
            local_execution_ids=[],
            provider_fills=[],
            snapshot_consistency=evidence,
            coverage_start="2026-09-25T12:00:00Z",
            coverage_end="2026-09-25T12:00:05Z",
            pagination_complete=True,
        )

    def checkpoint(
        self,
        *,
        key,
        consistency=None,
        observations=None,
    ):
        result = self.reconciliation_result(consistency=consistency)
        snapshot_observations = (
            tuple(observations)
            if observations is not None
            else (self.read_observation(marker=key),)
        )
        return record_reconciliation_checkpoint(
            self.store,
            reconciliation_id=f"stream-recovery-{key}",
            result=result,
            observed_at="2026-09-25T12:00:06Z",
            host_id="test-host",
            owner_epoch="1",
            snapshot_observations=snapshot_observations,
        ), snapshot_observations

    def recover(
        self,
        sequence="10",
        *,
        key="recover-1",
        lifecycle=None,
        consistency=None,
        checkpoint_observations=None,
        read_observations=None,
    ):
        target = lifecycle or self.lifecycle
        checkpoint, issued = self.checkpoint(
            key=key,
            consistency=consistency,
            observations=checkpoint_observations,
        )
        resolved = issued if read_observations is None else tuple(read_observations)
        return target.complete_recovery(
            command_id=key,
            idempotency_key=key,
            snapshot_sequence=sequence,
            checkpoint_event_id=checkpoint["event_id"],
            read_observations=resolved,
            committed_at="2026-09-25T12:00:06Z",
        )

    def event(
        self,
        sequence,
        *,
        char="a",
        event_id=None,
        lifecycle=None,
        connection_id=None,
        provider_environment=None,
    ):
        target = lifecycle or self.lifecycle
        connection = connection_id or target.snapshot.connection_id
        return observe_private_stream_frame(
            capability=self.capability,
            provider_environment=(
                provider_environment or target.scope.provider_environment
            ),
            stream_name=target.scope.stream_name,
            connection_id=connection,
            provider_event_id=event_id or f"execution-{sequence}",
            sequence=str(sequence),
            frame_bytes=(
                f'{{"sequence":"{sequence}","marker":"{char}"}}'
            ).encode("utf-8"),
            observed_at=BASE_TIME + timedelta(seconds=20),
            permission_scope="ORDER.READ",
        )

    def observe(self, observation, *, key, lifecycle=None):
        target = lifecycle or self.lifecycle
        return target.observe_event(
            observation,
            command_id=key,
            idempotency_key=key,
            committed_at="2026-09-25T12:00:20Z",
        )

    def test_buffered_events_become_live_only_after_issued_recovery(self):
        started = self.start()
        self.assertEqual(started.status, "RECOVERING")
        self.assertFalse(self.lifecycle.is_ready)

        first = self.observe(self.event(11), key="buffer-11")
        second = self.observe(self.event(12, char="b"), key="buffer-12")
        self.assertEqual(first.disposition, "BUFFERED")
        self.assertEqual(second.disposition, "BUFFERED")
        self.assertFalse(self.lifecycle.is_ready)

        live = self.recover("10")
        self.assertEqual(live.status, "LIVE")
        self.assertEqual(live.snapshot_sequence, "10")
        self.assertEqual(live.last_sequence, "12")
        self.assertTrue(self.lifecycle.is_ready)

        applied = self.observe(self.event(13, char="c"), key="event-13")
        self.assertEqual(applied.disposition, "APPLIED")
        self.assertEqual(applied.snapshot.last_sequence, "13")
        self.assertTrue(self.lifecycle.is_ready)

    def test_raw_caller_event_cannot_advance_lifecycle(self):
        self.start()
        forged = ProviderPrivateStreamEvent(
            provider_event_id="forged-11",
            sequence="11",
            evidence_ref=_evidence("provider-stream", "f"),
            payload_sha256="sha256:" + "f" * 64,
            observed_at="2026-09-25T12:00:20Z",
        )
        with self.assertRaisesRegex(
            TypeError,
            "observation must be ProviderPrivateStreamObservation",
        ):
            self.observe(forged, key="forged-event")
        self.assertIsNone(self.lifecycle.snapshot.last_sequence)
        self.assertEqual(self.lifecycle.snapshot.status, "RECOVERING")

    def test_foreign_provider_environment_stream_evidence_cannot_advance(self):
        self.start()
        foreign = self.event(11, provider_environment="SPOT_DEMO")
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "scope or connection mismatch",
        ):
            self.observe(foreign, key="foreign-stream-evidence")
        self.assertIsNone(self.lifecycle.snapshot.last_sequence)
        self.assertFalse(self.lifecycle.is_ready)

    def test_live_sequence_gap_is_durable_and_immediately_removes_readiness(self):
        self.start()
        self.recover("10")
        result = self.observe(self.event(12), key="gap-12")

        self.assertEqual(result.disposition, "GAP_DETECTED")
        self.assertEqual(result.snapshot.status, "GAPPED")
        self.assertFalse(self.lifecycle.is_ready)
        with self.assertRaises(ProviderStreamNotReady):
            self.lifecycle.require_ready()

        restarted = DurableProviderPrivateStreamLifecycle(
            self.store,
            scope=self.scope,
        )
        self.assertEqual(restarted.snapshot.status, "GAPPED")
        self.assertFalse(restarted.is_ready)

    def test_duplicate_and_snapshot_covered_late_event_are_idempotent(self):
        self.start()
        self.recover("10")
        event = self.event(11)
        accepted = self.observe(event, key="event-11")
        version = accepted.snapshot.aggregate_version

        duplicate = self.observe(event, key="duplicate-11")
        self.assertEqual(duplicate.disposition, "DUPLICATE")
        self.assertFalse(duplicate.committed)
        self.assertEqual(duplicate.snapshot.aggregate_version, version)

        covered = self.observe(self.event(9, char="d"), key="late-9")
        self.assertEqual(covered.disposition, "STALE_COVERED")
        self.assertFalse(covered.committed)
        self.assertEqual(covered.snapshot.aggregate_version, version)
        self.assertTrue(self.lifecycle.is_ready)

    def test_conflicting_duplicate_sequence_fails_closed_to_gapped(self):
        self.start()
        self.recover("10")
        self.observe(self.event(11, char="a"), key="event-11")

        conflict = self.observe(
            self.event(11, char="b", event_id="execution-11-revised"),
            key="event-11-conflict",
        )
        self.assertEqual(conflict.disposition, "GAP_DETECTED")
        self.assertEqual(conflict.snapshot.status, "GAPPED")
        self.assertFalse(self.lifecycle.is_ready)

    def test_restart_cannot_reuse_pre_crash_live_generation(self):
        self.start()
        self.recover("10")
        self.observe(self.event(11), key="event-11")
        self.assertTrue(self.lifecycle.is_ready)

        restarted = DurableProviderPrivateStreamLifecycle(
            self.store,
            scope=self.scope,
        )
        self.assertEqual(restarted.snapshot.status, "LIVE")
        self.assertFalse(restarted.is_ready)
        with self.assertRaises(ProviderStreamNotReady):
            restarted.observe_event(
                self.event(12, connection_id="connection-1"),
                command_id="unsafe-after-restart",
                idempotency_key="unsafe-after-restart",
            )

        generation = restarted.start_generation(
            command_id="start-connection-2",
            idempotency_key="start-connection-2",
            connection_id="connection-2",
        )
        self.assertEqual(generation.generation, 2)
        self.assertEqual(generation.recovery_floor_sequence, "11")
        self.assertFalse(restarted.is_ready)

        recovered = self.recover(
            "11",
            key="recover-2",
            lifecycle=restarted,
        )
        self.assertEqual(recovered.generation, 2)
        self.assertTrue(restarted.is_ready)

    def test_recovery_snapshot_cannot_regress_behind_durable_watermark(self):
        self.start()
        self.recover("10")
        self.observe(self.event(11), key="event-11")

        self.lifecycle.start_generation(
            command_id="start-connection-2",
            idempotency_key="start-connection-2",
            connection_id="connection-2",
        )
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "regressed behind the durable pre-restart watermark",
        ):
            self.recover("10", key="recover-regressed")
        self.assertFalse(self.lifecycle.is_ready)
        self.assertEqual(self.lifecycle.snapshot.status, "RECOVERING")

    def test_recovery_rejects_incomplete_journal_checkpoint(self):
        self.start()
        incomplete = _consistency(replay_complete=False)
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "checkpoint is incomplete or inconsistent",
        ):
            self.recover(
                "10",
                key="recover-incomplete",
                consistency=incomplete,
                checkpoint_observations=(),
                read_observations=(self.read_observation(marker="incomplete"),),
            )
        self.assertEqual(self.lifecycle.snapshot.status, "RECOVERING")
        self.assertFalse(self.lifecycle.is_ready)

    def test_recovery_requires_exact_resolved_checkpoint_observations(self):
        self.start()
        expected = self.read_observation(marker="expected")
        checkpoint, _ = self.checkpoint(
            key="exact-observation",
            observations=(expected,),
        )
        foreign = self.read_observation(marker="different")
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "do not match checkpoint evidence",
        ):
            self.lifecycle.complete_recovery(
                command_id="recover-wrong-read",
                idempotency_key="recover-wrong-read",
                snapshot_sequence="10",
                checkpoint_event_id=checkpoint["event_id"],
                read_observations=(foreign,),
            )
        self.assertEqual(self.lifecycle.snapshot.status, "RECOVERING")
        self.assertFalse(self.lifecycle.is_ready)

    def test_testnet_read_cannot_restore_demo_private_stream(self):
        demo_scope = ProviderPrivateStreamScope(
            provider_id="BINANCE",
            account_id="acct-1",
            environment="PAPER",
            provider_environment="SPOT_DEMO",
            stream_name="ACCOUNT_EVENTS",
        )
        demo = DurableProviderPrivateStreamLifecycle(
            self.store,
            scope=demo_scope,
        )
        self.start(connection="demo-connection", lifecycle=demo)
        testnet = self.read_observation(
            provider_environment="SPOT_TESTNET",
            marker="testnet-proof",
        )
        checkpoint, _ = self.checkpoint(
            key="testnet-checkpoint",
            observations=(testnet,),
        )
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "not current for private-stream scope",
        ):
            demo.complete_recovery(
                command_id="demo-recover",
                idempotency_key="demo-recover",
                snapshot_sequence="10",
                checkpoint_event_id=checkpoint["event_id"],
                read_observations=(testnet,),
            )
        self.assertEqual(demo.snapshot.status, "RECOVERING")
        self.assertFalse(demo.is_ready)

    def test_buffered_gap_requires_snapshot_to_cover_gap_before_readiness(self):
        self.start()
        self.observe(self.event(11), key="buffer-11")
        self.observe(self.event(13, char="b"), key="buffer-13")

        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "buffered private-stream events are not contiguous",
        ):
            self.recover("10", key="recover-gap")
        self.assertFalse(self.lifecycle.is_ready)

        covered = self.recover("13", key="recover-cover")
        self.assertEqual(covered.last_sequence, "13")
        self.assertTrue(self.lifecycle.is_ready)

    def test_disconnect_is_durable_and_requires_new_generation(self):
        self.start()
        self.recover("10")
        disconnected = self.lifecycle.disconnect(
            command_id="disconnect-1",
            idempotency_key="disconnect-1",
            reason="SOCKET_CLOSED",
        )
        self.assertEqual(disconnected.status, "DISCONNECTED")
        self.assertFalse(self.lifecycle.is_ready)

        restarted = DurableProviderPrivateStreamLifecycle(
            self.store,
            scope=self.scope,
        )
        self.assertEqual(restarted.snapshot.status, "DISCONNECTED")
        with self.assertRaises(ProviderStreamConflict):
            restarted.observe_event(
                self.event(11, connection_id="connection-1"),
                command_id="event-after-disconnect",
                idempotency_key="event-after-disconnect",
            )

    def test_start_generation_exact_retry_does_not_invent_generation(self):
        first = self.start()
        retried = self.lifecycle.start_generation(
            command_id="start-connection-1-retry",
            idempotency_key="start-connection-1",
            connection_id="connection-1",
        )
        self.assertEqual(first.generation, 1)
        self.assertEqual(retried.generation, 1)
        self.assertEqual(first.aggregate_version, retried.aggregate_version)

        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "exact retry requires its original idempotency key",
        ):
            self.lifecycle.start_generation(
                command_id="start-connection-1-conflict",
                idempotency_key="different-idempotency",
                connection_id="connection-1",
            )

    def test_idempotency_key_cannot_be_reused_for_changed_request(self):
        self.start()
        self.recover("10")
        self.lifecycle.disconnect(
            command_id="disconnect",
            idempotency_key="same-key",
            reason="NETWORK_LOSS",
        )
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "idempotency_key was already used",
        ):
            self.lifecycle.disconnect(
                command_id="disconnect-retry",
                idempotency_key="same-key",
                reason="AUTH_FAILURE",
            )

    def test_scope_and_sequence_are_fail_closed_and_canonical(self):
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "environment must be PAPER or LIVE",
        ):
            ProviderPrivateStreamScope(
                provider_id="BINANCE",
                account_id="acct-1",
                environment="SIMULATION",
                provider_environment="SPOT_TESTNET",
                stream_name="ACCOUNT_EVENTS",
            )
        with self.assertRaisesRegex(
            ProviderStreamConflict,
            "canonical non-negative integer sequence",
        ):
            ProviderPrivateStreamEvent(
                provider_event_id="e1",
                sequence="01",
                evidence_ref=_evidence("provider-stream", "a"),
                payload_sha256="sha256:" + "a" * 64,
                observed_at="2026-09-25T12:00:01Z",
            )

    def test_durable_lifecycle_metadata_contains_no_secret_material(self):
        self.start()
        self.recover("10")
        events = self.store.load_events(
            "provider_private_stream",
            self.scope.aggregate_id,
        )
        serialized = repr(events).lower()
        for forbidden in (
            "api_key",
            "api_secret",
            "authorization",
            "session_token",
            "credential",
            "signature",
        ):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
