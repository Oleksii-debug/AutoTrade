"""Offline process-supervisor tests for public WhiteBIT collection."""
from __future__ import annotations

import asyncio
from contextlib import redirect_stdout
import io
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
    _manifest_integrity_hash,
    _verify_manifest_integrity,
)
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.integrations.whitebit_websocket import (
    WhiteBitWebSocketDiagnostic,
    WhiteBitWebSocketMarketFeed,
)
from tools.run_whitebit_public_supervisor import (
    SCHEMA,
    SOURCE_URI,
    WhiteBitArtifactQuota,
    WhiteBitPublicDiagnosticJournal,
    _PersistenceSampler,
    _atomic_write_json_with_reader_retry,
    main,
    supervise,
)


def _encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _subscription(request_id: int = 1) -> bytes:
    return _encoded({"id": request_id, "result": {"status": "success"}, "error": None})


def _ticker(update_id: int, market: str = "BTC_USDT") -> bytes:
    return _encoded(
        {
            "method": "bookTicker_update",
            "params": [[
                1792303200.1,
                1792303200.2,
                market,
                update_id,
                "100.00",
                "2.5",
                "101.00",
                "3.5",
            ]],
            "id": None,
        }
    )


class Disconnect(Exception):
    pass


class FakeSocket:
    def __init__(self, messages):
        self.messages = list(messages)
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if self.messages:
            value = self.messages.pop(0)
            if isinstance(value, Exception):
                raise value
            return value
        await asyncio.Future()


class FakeContext:
    def __init__(self, socket):
        self.socket = socket

    async def __aenter__(self):
        return self.socket

    async def __aexit__(self, *_):
        return None


class Connector:
    def __init__(self, sockets):
        self.sockets = list(sockets)
        self.calls = []

    def __call__(self, endpoint, **options):
        self.calls.append((endpoint, options))
        if not self.sockets:
            raise Disconnect("provider secret must never reach diagnostics")
        return FakeContext(self.sockets.pop(0))


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def test_sampling_admission_uses_elapsed_time_and_resets_for_generation(self):
        start = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)

        def observation(update_id, milliseconds, generation=1):
            ticker = type("Ticker", (), {
                "received_at": start + timedelta(milliseconds=milliseconds),
                "update_id": update_id,
            })()
            return type("Observation", (), {
                "ticker": ticker,
                "generation": generation,
            })()

        sampler = _PersistenceSampler(1000)
        values = [
            observation(1, 0),
            observation(2, 999),
            observation(3, 1000),
            observation(4, 1001, generation=2),
        ]
        self.assertEqual(
            [value.ticker.update_id for value in values if sampler.admit(value)],
            [1, 3, 4],
        )

    async def test_sol_sampling_validates_all_frames_and_persists_first_per_generation(self):
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "state").resolve()
            first = FakeSocket([
                _subscription(), _ticker(1, "SOL_USDT"), Disconnect("offline")
            ])
            second = FakeSocket([
                _subscription(2), _ticker(2, "SOL_USDT"), _ticker(3, "SOL_USDT")
            ])
            connector = Connector([first, second])
            stop = asyncio.Event()
            admitted = []
            real_commit = WhiteBitWebSocketMarketFeed._commit_with_authenticated_manifest

            def capture_commit(feed, observation):
                admitted.append(observation.ticker.update_id)
                return real_commit(feed, observation)

            with patch.object(
                WhiteBitWebSocketMarketFeed,
                "_commit_with_authenticated_manifest",
                new=capture_commit,
            ):
                task = asyncio.create_task(supervise(
                    market="SOL_USDT",
                    state_dir=root,
                    connector=connector,
                    stop=stop,
                    install_signals=False,
                    heartbeat_seconds=0.1,
                    minimum_persist_interval_ms=60_000,
                ))
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    path = root / "diagnostics" / "whitebit-public-sampling.json"
                    if path.is_file():
                        try:
                            sampling = json.loads(path.read_bytes())
                        except (OSError, json.JSONDecodeError):
                            sampling = {}
                        if sampling.get("validated_frame_count") == 3:
                            break
                    await asyncio.sleep(0.02)
                else:
                    self.fail("sampling diagnostics did not account for every frame")
                stop.set()
                outcome = await asyncio.wait_for(task, timeout=5)

            self.assertEqual(admitted, [1, 2])
            self.assertEqual(outcome["observation_count"], 2)
            self.assertEqual(
                len(list((root / "artifacts" / "manifests").glob("*.json"))),
                2,
            )
            state = json.loads((root / "supervisor-state.json").read_text(encoding="utf-8"))
            self.assertEqual(set(state), {
                "schema", "market", "phase", "checkpointed_at",
                "observation_count", "latest_evidence_ref",
                "public_data_only", "trading_authority",
            })
            self.assertEqual(state["observation_count"], 2)
            self.assertEqual(
                json.loads((root / "latest-market.json").read_text(encoding="utf-8"))["update_id"],
                2,
            )

            restarted = WhiteBitPublicDiagnosticJournal(
                root / "diagnostics",
                market="SOL_USDT",
                interval_seconds=60,
                minimum_persist_interval_ms=60_000,
            )
            restarted.start()
            restarted.stop()
            sampling = json.loads(
                (root / "diagnostics" / "whitebit-public-sampling.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(sampling["validated_frame_count"], 3)
            self.assertEqual(sampling["sampled_out_frame_count"], 1)
            self.assertEqual(sampling["minimum_persist_interval_ms"], 60_000)
            self.assertIs(sampling["all_validated_frames_persisted"], False)

    async def test_sampling_is_rejected_outside_sol_before_state_creation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            with self.assertRaisesRegex(ValueError, "restricted to SOL_USDT"):
                await supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=Connector([]),
                    install_signals=False,
                    minimum_persist_interval_ms=1000,
                )
            self.assertFalse(root.exists())

    async def test_retention_recovery_reports_starting_before_connecting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "supervisor-state.json"
            connector = Connector([])

            def reject_recovery(*, protected_artifact_id):
                self.assertIsNone(protected_artifact_id)
                self.assertEqual(json.loads(checkpoint.read_bytes())["phase"], "STARTING")
                self.assertEqual(connector.calls, [])
                raise ArtifactIntegrityError("retained artifact is corrupt")

            with patch.object(WhiteBitArtifactQuota, "recover", side_effect=reject_recovery):
                with self.assertRaises(ArtifactIntegrityError) as failure:
                    await supervise(market="BTC_USDT", state_dir=root, connector=connector)
            self.assertEqual(failure.exception.autotrade_failure_stage, "RECOVER_RETENTION")
            self.assertEqual(json.loads(checkpoint.read_bytes())["phase"], "FAILED")
            self.assertEqual(connector.calls, [])

    async def test_recovery_removes_authenticated_prepared_crash_orphan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "artifacts")

            def publish(update_id: int) -> dict:
                return store.publish_bytes(
                    artifact_id=str(uuid4()),
                    data=_ticker(update_id),
                    media_type="application/json",
                    rights={"storage": True, "export": False},
                    source_refs=[SOURCE_URI],
                    metadata={"provider": "WHITEBIT", "market": "BTC_USDT"},
                )

            protected = publish(1)
            orphan = publish(2)
            orphan_path = store.manifests / (orphan["artifact_id"] + ".json")
            manifest = json.loads(orphan_path.read_text(encoding="utf-8"))
            manifest["publication_state"] = "PREPARED"
            manifest["manifest_hash"] = _manifest_integrity_hash(manifest)
            atomic_write_json(orphan_path, manifest)

            quota = WhiteBitArtifactQuota(store, market="BTC_USDT", maximum=2)
            quota.recover(protected_artifact_id=protected["artifact_id"])

            self.assertFalse(orphan_path.exists())
            self.assertTrue(
                (store.manifests / (protected["artifact_id"] + ".json")).is_file()
            )
            self.assertEqual(len(quota._entries), 1)

    async def test_atomic_publication_retries_transient_windows_reader_lease(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "latest-market.json"
            attempts = []

            def transient_reader_lease(path, value):
                attempts.append((path, value))
                if len(attempts) < 3:
                    raise PermissionError("simulated Windows reader lease")

            with patch(
                "tools.run_whitebit_public_supervisor.atomic_write_json",
                side_effect=transient_reader_lease,
            ):
                _atomic_write_json_with_reader_retry(destination, {"ready": True})

            self.assertEqual(len(attempts), 3)
            self.assertTrue(all(item[0] == destination for item in attempts))

    async def test_atomic_publication_retries_transient_disk_full_only(self):
        destination = Path("latest-market.json")
        module = "tools.run_whitebit_public_supervisor"
        full = OSError(28, "disk full")
        for events, expected_attempts, failure in (
            ([full, None], 2, None),
            ([full] * 4, 4, full),
            ([OSError(5, "unrelated I/O")], 1, OSError),
        ):
            with patch(module + ".atomic_write_json", side_effect=events) as publish:
                with patch(module + ".time.sleep") as delay:
                    if failure is None:
                        _atomic_write_json_with_reader_retry(destination, {"ready": True})
                    else:
                        try:
                            _atomic_write_json_with_reader_retry(destination, {"ready": True})
                        except OSError as caught:
                            assert caught is events[-1]
                        else:
                            raise AssertionError("A permanent publication failure must propagate")
                assert publish.call_count == expected_attempts
                assert delay.call_count == expected_attempts - 1

    async def test_failed_latest_publication_does_not_checkpoint_unpublished_cut(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            connector = Connector([FakeSocket([_subscription(), _ticker(42)])])
            real_publish = __import__(
                "tools.run_whitebit_public_supervisor", fromlist=["atomic_write_json"]
            ).atomic_write_json

            def fail_latest(path, value):
                if path.name == "latest-market.json":
                    raise PermissionError("persistent simulated reader lease")
                return real_publish(path, value)

            with patch(
                "tools.run_whitebit_public_supervisor._atomic_write_json_with_reader_retry",
                side_effect=fail_latest,
            ):
                with self.assertRaises(PermissionError) as failed:
                    await supervise(
                        market="BTC_USDT",
                        state_dir=root,
                        connector=connector,
                        install_signals=False,
                    )

            self.assertEqual(failed.exception.autotrade_failure_stage, "PUBLISH_LATEST")
            state = json.loads((root / "supervisor-state.json").read_text("utf-8"))
            self.assertEqual(state["phase"], "FAILED")
            self.assertEqual(state["observation_count"], 0)
            self.assertIsNone(state["latest_evidence_ref"])
            self.assertFalse((root / "latest-market.json").exists())

    async def test_artifact_eviction_retries_transient_windows_reader_lease(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = ArtifactStore(root / "artifacts")

            def publish(update_id: int) -> dict:
                artifact_id = str(uuid4())
                return store.publish_bytes(
                    artifact_id=artifact_id,
                    data=_ticker(update_id),
                    media_type="application/json",
                    rights={"storage": True, "export": False},
                    source_refs=[SOURCE_URI],
                    metadata={"provider": "WHITEBIT", "market": "BTC_USDT"},
                )

            first = publish(1)
            quota = WhiteBitArtifactQuota(store, market="BTC_USDT", maximum=1)
            quota.recover(protected_artifact_id=first["artifact_id"])
            second = publish(2)
            first_path = store.manifests / (first["artifact_id"] + ".json")
            real_unlink = Path.unlink
            rejected = []

            def transient_reader_lease(path, *args, **kwargs):
                if path == first_path and not rejected:
                    rejected.append(True)
                    raise PermissionError("simulated Windows reader lease")
                return real_unlink(path, *args, **kwargs)

            with patch.object(Path, "unlink", autospec=True,
                              side_effect=transient_reader_lease):
                quota.admit(second["artifact_id"])

            self.assertEqual(rejected, [True])
            self.assertFalse(first_path.exists())
            self.assertTrue(
                (store.manifests / (second["artifact_id"] + ".json")).is_file()
            )

    async def test_reconnect_snapshot_and_clean_checkpoint_are_durable(self):
        with TemporaryDirectory() as directory:
            # supervise resolves its state directory. Compare publication paths
            # using that same canonical namespace (Windows TEMP may be 8.3).
            root = (Path(directory) / "state").resolve()
            # Exercise normalization even on hosts with already canonical TEMP.
            state_argument = root / ".." / root.name
            first = FakeSocket([_subscription(), Disconnect("credential=hidden")])
            second = FakeSocket([_subscription(2), _ticker(42)])
            connector = Connector([first, second])
            stop = asyncio.Event()

            def stop_after_snapshot(path, value):
                # Observe the actual successful disk publication, not a race
                # between a polling deadline and Windows namespace/fsync I/O.
                _atomic_write_json_with_reader_retry(path, value)
                if path == root / "latest-market.json":
                    stop.set()

            with patch("tools.run_whitebit_public_supervisor._atomic_write_json_with_reader_retry",
                       side_effect=stop_after_snapshot):
                outcome = await asyncio.wait_for(
                    supervise(
                        market="BTC_USDT",
                        state_dir=state_argument,
                        connector=connector,
                        stop=stop,
                        install_signals=False,
                        heartbeat_seconds=0.1,
                    ),
                    # Deadlock guard, not a filesystem latency requirement.
                    timeout=15,
                )
            self.assertEqual(outcome["phase"], "STOPPED")
            self.assertEqual(outcome["observation_count"], 1)
            self.assertEqual(len(connector.calls), 2)

            snapshot = json.loads((root / "latest-market.json").read_text(encoding="utf-8"))
            self.assertEqual(snapshot["schema"], SCHEMA)
            self.assertEqual(snapshot["bid_price"], "100.00")
            self.assertEqual(snapshot["bid_quantity"], "2.5")
            self.assertEqual(snapshot["ask_price"], "101.00")
            self.assertEqual(snapshot["ask_quantity"], "3.5")
            self.assertEqual(snapshot["update_id"], 42)
            self.assertEqual(snapshot["stream_generation"], 2)
            evidence = snapshot["raw_evidence_ref"]
            self.assertEqual(set(evidence), {
                "artifact_id", "sha256", "source_uri", "observed_at", "rights_id"
            })
            self.assertTrue((root / "artifacts" / "manifests" / (evidence["artifact_id"] + ".json")).is_file())

            state = json.loads((root / "supervisor-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "STOPPED")
            self.assertEqual(state["observation_count"], 1)
            diagnostics = b"".join(
                path.read_bytes() for path in sorted((root / "diagnostics").glob("*.jsonl"))
            )
            self.assertIn(b'"connection_code":"CONNECTION_LOST"', diagnostics)
            self.assertNotIn(b"credential=hidden", diagnostics)
            self.assertNotIn(b"provider secret", diagnostics)

    async def test_live_admission_reuses_the_authenticated_frame_snapshot(self):
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "state").resolve()
            connector = Connector([FakeSocket([_subscription(), _ticker(42)])])
            stop = asyncio.Event()
            authenticated = []
            real_read = ArtifactStore.read_authenticated_snapshot

            def count_authenticated_read(store, artifact_id):
                authenticated.append(artifact_id)
                return real_read(store, artifact_id)

            def stop_after_snapshot(path, value):
                _atomic_write_json_with_reader_retry(path, value)
                if path == root / "latest-market.json":
                    stop.set()

            with patch.object(
                ArtifactStore,
                "read_authenticated_snapshot",
                new=count_authenticated_read,
            ), patch(
                "tools.run_whitebit_public_supervisor."
                "_atomic_write_json_with_reader_retry",
                side_effect=stop_after_snapshot,
            ):
                outcome = await asyncio.wait_for(
                    supervise(
                        market="BTC_USDT",
                        state_dir=root,
                        connector=connector,
                        stop=stop,
                        install_signals=False,
                        max_artifacts=1,
                    ),
                    timeout=15,
                )

            evidence_id = outcome["latest_evidence_ref"]["artifact_id"]
            self.assertEqual(authenticated, [evidence_id])
            self.assertEqual(outcome["observation_count"], 1)
            self.assertTrue(
                (root / "artifacts" / "manifests" / (evidence_id + ".json")).is_file()
            )

    async def test_injected_clock_mutation_gets_strict_admission(self):
        with TemporaryDirectory() as directory:
            root = (Path(directory) / "state").resolve()
            connector = Connector([FakeSocket([_subscription(), _ticker(42)])])
            store = ArtifactStore(root / "artifacts")
            tampered = []

            def hostile_clock():
                latest_path = root / "latest-market.json"
                if latest_path.is_file() and not tampered:
                    latest = json.loads(latest_path.read_text(encoding="utf-8"))
                    artifact_id = latest["raw_evidence_ref"]["artifact_id"]
                    manifest = store.load_manifest(artifact_id)
                    object_path = store._object_path(
                        manifest["sha256"].removeprefix("sha256:")
                    )
                    original = object_path.read_bytes()
                    object_path.write_bytes(
                        bytes((original[0] ^ 1,)) + original[1:]
                    )
                    tampered.append(artifact_id)
                return datetime(2026, 1, 1, tzinfo=timezone.utc)

            with self.assertRaises(ArtifactIntegrityError):
                await asyncio.wait_for(
                    supervise(
                        market="BTC_USDT",
                        state_dir=root,
                        connector=connector,
                        install_signals=False,
                        heartbeat_seconds=60,
                        clock=hostile_clock,
                    ),
                    timeout=15,
                )

            self.assertEqual(len(tampered), 1)
            state = json.loads(
                (root / "supervisor-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "FAILED")

    async def test_single_owner_resource_lock_rejects_second_supervisor(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            with ResourceLock(root / "whitebit-public-supervisor.lock"):
                with self.assertRaises(RuntimeError):
                    await supervise(
                        market="BTC_USDT",
                        state_dir=root,
                        connector=Connector([]),
                        stop=asyncio.Event(),
                        install_signals=False,
                    )

    async def test_periodic_checkpoint_advances_while_stream_is_idle(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            stop = asyncio.Event()
            task = asyncio.create_task(supervise(
                market="BTC_USDT",
                state_dir=root,
                connector=Connector([FakeSocket([_subscription()])]),
                stop=stop,
                install_signals=False,
                heartbeat_seconds=0.1,
                checkpoint_seconds=0.1,
            ))
            deadline = time.monotonic() + 2
            first = None
            while time.monotonic() < deadline:
                path = root / "supervisor-state.json"
                if path.is_file():
                    current = json.loads(path.read_text(encoding="utf-8"))
                    if first is None:
                        first = current["checkpointed_at"]
                    elif current["checkpointed_at"] != first:
                        break
                await asyncio.sleep(0.02)
            else:
                self.fail("periodic checkpoint did not advance")
            stop.set()
            outcome = await asyncio.wait_for(task, timeout=2)
            self.assertEqual(outcome["observation_count"], 0)

    async def test_managed_stop_file_leaves_terminal_checkpoint(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            root.mkdir()
            stop_file = root / "managed.stop.json"
            stop_file.write_text("{}", encoding="utf-8")
            outcome = await asyncio.wait_for(
                supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=Connector([FakeSocket([_subscription()])]),
                    stop_file=stop_file,
                    install_signals=False,
                    heartbeat_seconds=0.1,
                ),
                timeout=2,
            )
            self.assertEqual(outcome["phase"], "STOPPED")
            state = json.loads(
                (root / "supervisor-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["phase"], "STOPPED")

    async def test_restart_continues_count_and_retains_bounded_artifacts(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"

            async def run(messages, expected_count):
                stop = asyncio.Event()
                task = asyncio.create_task(supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=Connector([FakeSocket([_subscription(), *messages])]),
                    stop=stop,
                    install_signals=False,
                    heartbeat_seconds=0.1,
                    checkpoint_seconds=0.1,
                    max_artifacts=2,
                ))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    path = root / "supervisor-state.json"
                    if path.is_file():
                        state = json.loads(path.read_text(encoding="utf-8"))
                        if state["observation_count"] >= expected_count:
                            stop.set()
                            return await asyncio.wait_for(task, timeout=2)
                    await asyncio.sleep(0.01)
                self.fail("supervisor did not reach expected observation count")

            first = await run([_ticker(1)], 1)
            second = await run([_ticker(2), _ticker(3)], 3)

            self.assertEqual(first["observation_count"], 1)
            self.assertEqual(second["observation_count"], 3)
            manifests = list((root / "artifacts" / "manifests").glob("*.json"))
            objects = list((root / "artifacts" / "objects" / "sha256").glob("*/*"))
            self.assertEqual(len(manifests), 2)
            self.assertEqual(len(objects), 2)
            latest = json.loads((root / "latest-market.json").read_text(encoding="utf-8"))
            self.assertTrue(
                (root / "artifacts" / "manifests" /
                 (latest["raw_evidence_ref"]["artifact_id"] + ".json")).is_file()
            )

    async def test_restart_rejects_checkpoint_latest_mismatch(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"
            stop = asyncio.Event()
            task = asyncio.create_task(supervise(
                market="BTC_USDT",
                state_dir=root,
                connector=Connector([FakeSocket([_subscription(), _ticker(7)])]),
                stop=stop,
                install_signals=False,
            ))
            deadline = time.monotonic() + 2
            while not (root / "latest-market.json").is_file():
                if time.monotonic() >= deadline:
                    self.fail("initial supervisor did not publish")
                await asyncio.sleep(0.01)
            stop.set()
            await asyncio.wait_for(task, timeout=2)

            latest_path = root / "latest-market.json"
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            latest["raw_evidence_ref"]["artifact_id"] = (
                "00000000-0000-0000-0000-000000000000"
            )
            latest_path.write_text(json.dumps(latest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "restart snapshot is not coherent"):
                await supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=Connector([]),
                    stop=asyncio.Event(),
                    install_signals=False,
                )

    async def test_restart_recovers_authenticated_snapshot_one_step_ahead(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "state"

            async def publish(update_id):
                stop = asyncio.Event()
                task = asyncio.create_task(supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=Connector([
                        FakeSocket([_subscription(), _ticker(update_id)])
                    ]),
                    stop=stop,
                    install_signals=False,
                ))
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    state_path = root / "supervisor-state.json"
                    if state_path.is_file():
                        state = json.loads(state_path.read_text(encoding="utf-8"))
                        if state["observation_count"] >= update_id:
                            stop.set()
                            return await asyncio.wait_for(task, timeout=2)
                    await asyncio.sleep(0.01)
                self.fail("supervisor did not publish expected observation")

            await publish(1)
            prior_state = json.loads(
                (root / "supervisor-state.json").read_text(encoding="utf-8")
            )
            await publish(2)

            # Recreate the only legitimate mismatch window: latest-market is
            # durable, while the preceding RUNNING checkpoint is still on disk.
            prior_state["phase"] = "RUNNING"
            atomic_write_json(root / "supervisor-state.json", prior_state)
            stop = asyncio.Event()
            stop.set()
            outcome = await supervise(
                market="BTC_USDT",
                state_dir=root,
                connector=Connector([]),
                stop=stop,
                install_signals=False,
            )
            self.assertEqual(outcome["observation_count"], 2)
            recovered = json.loads(
                (root / "supervisor-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recovered["observation_count"], 2)
            self.assertEqual(
                recovered["latest_evidence_ref"],
                json.loads((root / "latest-market.json").read_text(encoding="utf-8"))[
                    "raw_evidence_ref"
                ],
            )


class FailureOutputTests(unittest.TestCase):
    def test_cli_locates_artifact_integrity_failure_without_exception_text(self):
        async def fail(**_):
            _verify_manifest_integrity(
                {"artifact_id": "never-log-artifact-id"}, required=True
            )

        output = io.StringIO()
        with patch("tools.run_whitebit_public_supervisor.supervise", new=fail):
            with redirect_stdout(output):
                self.assertEqual(main(["--market", "SOL_USDT", "--state-dir", "unused"]), 2)
        result = json.loads(output.getvalue())
        self.assertEqual(result["error_code"], "ARTIFACTINTEGRITYERROR")
        self.assertEqual(result["integrity_module"], "store")
        self.assertGreater(result["integrity_line"], 0)
        self.assertNotIn("never-log-artifact-id", output.getvalue())
        self.assertNotIn("lacks integrity binding", output.getvalue())

    def test_cli_exposes_safe_numeric_error_without_exception_text(self):
        error = OSError(13, "never-log-proxy-secret")
        error.autotrade_failure_stage = "APPLY_ARTIFACT_QUOTA"
        output = io.StringIO()
        with patch("tools.run_whitebit_public_supervisor.supervise", side_effect=error):
            with redirect_stdout(output):
                self.assertEqual(main(["--market", "SOL_USDT", "--state-dir", "unused"]), 2)
        value = json.loads(output.getvalue())
        self.assertEqual(value["failure_stage"], "APPLY_ARTIFACT_QUOTA")
        self.assertEqual(value["errno"], 13)
        self.assertNotIn("never-log-proxy-secret", output.getvalue())


class DiagnosticJournalTests(unittest.TestCase):
    def test_only_rejection_events_carry_an_allowed_reason(self):
        with TemporaryDirectory() as directory:
            journal = WhiteBitPublicDiagnosticJournal(Path(directory), market="BTC_USDT")
            with self.assertRaisesRegex(ValueError, "unsupported WhiteBIT rejection reason"):
                WhiteBitWebSocketDiagnostic("SCHEMA_REJECTED", 1, 1, rejection_reason="unsafe text")
            journal.transport(WhiteBitWebSocketDiagnostic(
                "SCHEMA_REJECTED", 1, 1, rejection_reason="FUTURE_WEBSOCKET_TIMESTAMP"
            ))
            journal.transport(WhiteBitWebSocketDiagnostic("RECONNECT_WAIT", 1, 1))
            rejected, retry = map(json.loads, journal.active_path.read_bytes().splitlines())
            self.assertEqual(rejected["rejection_reason"], "FUTURE_WEBSOCKET_TIMESTAMP")
            self.assertNotIn("rejection_reason", retry)

    def test_heartbeat_rotates_boundedly_and_recovers_sequence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory) / "diagnostics"
            journal = WhiteBitPublicDiagnosticJournal(
                root,
                market="BTC_USDT",
                interval_seconds=0.1,
                max_segment_bytes=1024,
                max_segments=3,
            )
            journal.start()
            time.sleep(0.34)
            journal.stop()
            first_rows = [
                json.loads(line)
                for path in sorted(root.glob("*.jsonl"))
                for line in path.read_bytes().splitlines()
            ]
            self.assertGreaterEqual(len(first_rows), 3)
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)
            self.assertTrue(all(row["public_data_only"] for row in first_rows))
            self.assertTrue(all(row["trading_authority"] is False for row in first_rows))

            restarted = WhiteBitPublicDiagnosticJournal(
                root,
                market="BTC_USDT",
                interval_seconds=0.1,
                max_segment_bytes=1024,
                max_segments=3,
            )
            restarted.start()
            restarted.stop()
            rows = [
                json.loads(line)
                for path in sorted(root.glob("*.jsonl"))
                for line in path.read_bytes().splitlines()
            ]
            sequences = [row["sequence"] for row in rows]
            self.assertGreater(sequences[-1], first_rows[-1]["sequence"])
            self.assertEqual(sequences, list(range(sequences[0], sequences[-1] + 1)))
            self.assertLessEqual(len(list(root.glob("*.jsonl"))), 3)


if __name__ == "__main__":
    unittest.main()
