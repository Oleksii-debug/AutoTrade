"""Offline process-supervisor tests for public WhiteBIT collection."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from research.autotrade_research.artifacts.store import ArtifactStore
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from tools.run_whitebit_public_supervisor import (
    SCHEMA,
    SOURCE_URI,
    WhiteBitArtifactQuota,
    WhiteBitPublicDiagnosticJournal,
    supervise,
)


def _encoded(value) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _subscription(request_id: int = 1) -> bytes:
    return _encoded({"id": request_id, "result": {"status": "success"}, "error": None})


def _ticker(update_id: int) -> bytes:
    return _encoded(
        {
            "method": "bookTicker_update",
            "params": [[
                1792303200.1,
                1792303200.2,
                "BTC_USDT",
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
            root = Path(directory) / "state"
            first = FakeSocket([_subscription(), Disconnect("credential=hidden")])
            second = FakeSocket([_subscription(2), _ticker(42)])
            connector = Connector([first, second])
            stop = asyncio.Event()

            async def stop_after_snapshot():
                deadline = time.monotonic() + 2
                while not (root / "latest-market.json").is_file():
                    if time.monotonic() >= deadline:
                        self.fail("supervisor did not publish a market snapshot")
                    await asyncio.sleep(0.01)
                stop.set()

            stopper = asyncio.create_task(stop_after_snapshot())
            outcome = await asyncio.wait_for(
                supervise(
                    market="BTC_USDT",
                    state_dir=root,
                    connector=connector,
                    stop=stop,
                    install_signals=False,
                    heartbeat_seconds=0.1,
                ),
                timeout=3,
            )
            await stopper
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


class DiagnosticJournalTests(unittest.TestCase):
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
