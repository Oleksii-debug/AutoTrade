import asyncio
from datetime import datetime, timedelta, timezone
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from tools import run_whitebit_depth_tape


@pytest.mark.parametrize("stop_during_backpressure", [False, True])
def test_depth_writer_bounds_backlog_and_drains_only_committed_observations(
    tmp_path, monkeypatch, stop_during_backpressure,
):
    async def exercise():
        loop = asyncio.get_running_loop()
        loop_thread = get_ident()
        writer_started = asyncio.Event()
        queue_loaded = asyncio.Event()
        release_writer = Event()
        stopped = Event()
        received = []
        committed = []
        checkpoints = []
        store_calls = []
        queue_limit = 4
        total = 20
        first_receipt = datetime(2026, 11, 13, 17, tzinfo=timezone.utc)

        class Transport:
            def __init__(self, **_):
                pass

            async def observations(self, stop):
                for update_id in range(1, total + 1):
                    if stop.is_set() or stopped.is_set():
                        break
                    received.append(update_id)
                    if len(received) == queue_limit + 2:
                        queue_loaded.set()
                    received_at = first_receipt + timedelta(milliseconds=100 * update_id)
                    yield SimpleNamespace(
                        market="BTC_PERP", update_id=update_id, generation=1,
                        received_at=received_at, message_time=received_at - timedelta(milliseconds=25),
                    )
                    if update_id == 1:
                        await writer_started.wait()
                stopped.set()
                await stop.wait()

        class Store:
            def generation_baselines(self, markets):
                store_calls.append(("baseline", get_ident()))
                return {market: 0 for market in markets}

            def ingest_many(self, observations):
                store_calls.append(("ingest", get_ident()))
                if not committed:
                    loop.call_soon_threadsafe(writer_started.set)
                    assert release_writer.wait(5), "consumer could not make progress during SQLite ingest"
                committed.extend(observation.update_id for observation in observations)

            def status(self):
                store_calls.append(("status", get_ident()))
                return {"bucket_count": len(committed)}

            def archive_completed_windows(self, markets, *, now_epoch):
                store_calls.append(("archive", get_ident()))

        async def checkpoint(path, status, *, phase, markets, observations, errors, timing):
            checkpoints.append((phase, status["bucket_count"], observations, timing))
            return 0

        monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthWebSocketTransport", Transport)
        monkeypatch.setattr(run_whitebit_depth_tape, "INGEST_BATCH_SIZE", 1)
        monkeypatch.setattr(run_whitebit_depth_tape, "INGEST_QUEUE_LIMIT", queue_limit)
        monkeypatch.setattr(run_whitebit_depth_tape, "_checkpoint", checkpoint)
        args = SimpleNamespace(depth_limit=20, poll_seconds=0.01, stop_file=tmp_path / "stop")
        task = asyncio.create_task(run_whitebit_depth_tape._collect(
            args, stopped, Store(), tmp_path / "checkpoint.json", ("BTC_PERP",), offload_sqlite=True,
        ))
        try:
            await asyncio.wait_for(queue_loaded.wait(), timeout=3)
            for _ in range(total):
                await asyncio.sleep(0)
            # One batch can be in the writer and the transport can hold one
            # observation while waiting for capacity to put it into the queue.
            assert len(received) == queue_limit + 2
            assert committed == []
            assert all(rows == new == 0 for _, rows, new, _ in checkpoints)
            if stop_during_backpressure:
                stopped.set()
        finally:
            release_writer.set()
            await asyncio.wait_for(task, timeout=5)

        expected = queue_limit + 2 if stop_during_backpressure else total
        assert committed == received == list(range(1, expected + 1))
        assert checkpoints[-1][0:2] == ("STOPPED", expected)
        published = 0
        for _, rows, new, _ in checkpoints:
            assert new >= 0
            published += new
            assert rows == published
        assert published == expected
        final_timing = checkpoints[-1][3]
        assert final_timing["sqlite_writer_pending_observations"] == 0
        assert final_timing["sqlite_writer_queue_limit"] == queue_limit
        assert final_timing["sqlite_writer_oldest_pending_ms"] is None
        assert final_timing["last_persisted_received_at"] == {
            "BTC_PERP": (first_receipt + timedelta(milliseconds=100 * expected)).isoformat(),
        }
        assert datetime.fromisoformat(final_timing["last_sqlite_commit_at"]).utcoffset() == timedelta(0)
        assert {kind for kind, _ in store_calls} >= {"ingest", "status", "archive"}
        writer_ids = {thread for kind, thread in store_calls if kind != "baseline"}
        assert len(writer_ids) == 1 and loop_thread not in writer_ids

    asyncio.run(exercise())


def test_depth_writer_surfaces_failed_batch_without_retry(tmp_path, monkeypatch):
    async def exercise():
        attempts = []

        class Transport:
            def __init__(self, **_):
                pass

            async def observations(self, stop):
                yield SimpleNamespace(
                    market="BTC_PERP", generation=1, message_time=None,
                    received_at=datetime(2026, 11, 13, 17, tzinfo=timezone.utc),
                )
                await stop.wait()

        class Store:
            def generation_baselines(self, markets):
                return {market: 0 for market in markets}

            def status(self):
                return {"bucket_count": 0}

            def ingest_many(self, observations):
                attempts.append(len(observations))
                raise OSError("injected SQLite failure")

            def archive_completed_windows(self, markets, *, now_epoch):
                pass

        async def checkpoint(path, status, *, phase, markets, observations, errors, timing):
            assert status["bucket_count"] == observations == 0
            return 0

        monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthWebSocketTransport", Transport)
        monkeypatch.setattr(run_whitebit_depth_tape, "INGEST_BATCH_SIZE", 1)
        monkeypatch.setattr(run_whitebit_depth_tape, "_checkpoint", checkpoint)
        args = SimpleNamespace(depth_limit=20, poll_seconds=0.01, stop_file=tmp_path / "stop")
        with pytest.raises(OSError, match="injected SQLite failure"):
            await asyncio.wait_for(run_whitebit_depth_tape._collect(
                args, Event(), Store(), tmp_path / "checkpoint.json", ("BTC_PERP",), offload_sqlite=True,
            ), timeout=5)
        assert attempts == [1]

    asyncio.run(exercise())


@pytest.mark.parametrize("cancel_during_ingest", [False, True])
def test_depth_writer_owns_sqlite_connection_for_complete_lifecycle(
    tmp_path, monkeypatch, cancel_during_ingest,
):
    async def exercise():
        stopped = Event()
        loop = asyncio.get_running_loop()
        ingest_started = asyncio.Event()
        release_ingest = Event()
        writer_calls = []
        loop_thread = get_ident()

        class Transport:
            def __init__(self, **_):
                pass

            async def observations(self, stop):
                yield SimpleNamespace(
                    market="BTC_PERP", generation=1, message_time=None,
                    received_at=datetime(2026, 11, 13, 17, tzinfo=timezone.utc),
                )
                if not cancel_during_ingest:
                    stopped.set()
                await stop.wait()

        class Store:
            def __init__(self, *args, **kwargs):
                writer_calls.append(("open", get_ident()))

            def generation_baselines(self, markets):
                writer_calls.append(("baseline", get_ident()))
                return {market: 0 for market in markets}

            def status(self):
                writer_calls.append(("status", get_ident()))
                return {"bucket_count": 1}

            def ingest_many(self, observations):
                loop.call_soon_threadsafe(ingest_started.set)
                if cancel_during_ingest:
                    assert release_ingest.wait(5), "SQLite ingest was not released"
                writer_calls.append(("commit", get_ident()))

            def archive_completed_windows(self, markets, *, now_epoch):
                writer_calls.append(("archive", get_ident()))

            def close(self):
                writer_calls.append(("close", get_ident()))

        async def checkpoint(*args, **kwargs):
            return 0

        monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthWebSocketTransport", Transport)
        monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthTapeStore", Store)
        monkeypatch.setattr(run_whitebit_depth_tape, "_checkpoint", checkpoint)
        args = SimpleNamespace(
            depth_limit=20, poll_seconds=0.01, stop_file=tmp_path / "stop",
            bucket_seconds=1, retention_days=3,
        )
        task = asyncio.create_task(run_whitebit_depth_tape._collect(
            args, stopped, None, tmp_path / "checkpoint.json", ("BTC_PERP",), offload_sqlite=True,
        ))
        try:
            if cancel_during_ingest:
                await asyncio.wait_for(ingest_started.wait(), timeout=3)
                task.cancel()
                for _ in range(10):
                    await asyncio.sleep(0)
                assert not task.done()
                assert not any(operation == "close" for operation, _ in writer_calls)
        finally:
            release_ingest.set()
            if cancel_during_ingest:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=5)
            else:
                await asyncio.wait_for(task, timeout=5)
        operations = [operation for operation, _ in writer_calls]
        assert operations[0] == "open" and operations[-1] == "close"
        assert {"status", "commit"} <= set(operations)
        if not cancel_during_ingest:
            assert "archive" in operations
        thread_ids = {thread for _, thread in writer_calls}
        assert len(thread_ids) == 1 and loop_thread not in thread_ids

    asyncio.run(exercise())
