import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from threading import Event, get_ident
from types import SimpleNamespace

import pytest

from research.autotrade_research.integrations.whitebit_depth import (
    DepthBookObservation, DepthLocalBook, DepthSequenceGap,
    WhiteBitDepthWebSocketTransport, parse_depth_message,
)
from research.autotrade_research.learning.whitebit_depth_tape import (
    WhiteBitDepthTapeStore,
)
from tools import run_whitebit_depth_tape


NOW=datetime(2026,11,12,12,tzinfo=timezone.utc)


def frame(snapshot=True,past=None,update=10):
    body={"asks":[["101","2"]],"bids":[["99","3"]],
        "timestamp":1794484800,"event_time":1794484800,"update_id":update}
    if past is not None: body["past_update_id"]=past
    return json.dumps({"id":None,"method":"depth_update",
        "params":[snapshot,body,"BTC_USDT"]})


def test_snapshot_and_causal_delta_build_book():
    book=DepthLocalBook("BTC_USDT",limit=5)
    first=parse_depth_message(frame(),market="BTC_USDT",received_at=NOW)
    observed=book.apply(first,generation=1)
    assert observed.imbalance_top5=="0.2"
    assert (observed.bid_quantity, observed.ask_quantity) == ("3", "2")
    delta=parse_depth_message(frame(False,10,11),market="BTC_USDT",received_at=NOW)
    observed=book.apply(delta,generation=1)
    assert observed.update_id==11


def test_gap_fails_and_requires_fresh_snapshot():
    book=DepthLocalBook("BTC_USDT",limit=5)
    book.apply(parse_depth_message(frame(),market="BTC_USDT",received_at=NOW),generation=1)
    bad=parse_depth_message(frame(False,9,11),market="BTC_USDT",received_at=NOW)
    with pytest.raises(DepthSequenceGap): book.apply(bad,generation=1)


def test_depth_reconnects_after_silence_despite_pongs_and_accepts_quiet_snapshots(monkeypatch):
    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.receives = 0
            self.closed = False

        async def send(self, message):
            request = json.loads(message)
            if request["method"] == "depth_subscribe":
                self.messages.put_nowait(json.dumps({
                    "id": request["id"], "result": {"status": "success"}, "error": None,
                }))

        async def recv(self):
            self.receives += 1
            return await self.messages.get()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            self.closed = True

    async def spin_until(predicate):
        for _ in range(100):
            if predicate():
                return
            await asyncio.sleep(0)
        assert predicate()

    async def run():
        loop = asyncio.get_running_loop()
        now = loop.time()
        stop = asyncio.Event()
        first, second = Socket(), Socket()
        sockets = iter((first, second))
        diagnostics = []
        transport = WhiteBitDepthWebSocketTransport(
            market="BTC_USDT", connector=lambda *_, **__: next(sockets),
            diagnostic=diagnostics.append, initial_backoff_seconds=1,
        )
        observations = transport.observations(stop)
        pending = None
        with monkeypatch.context() as clock:
            clock.setattr(loop, "time", lambda: now)
            try:
                pending = asyncio.create_task(anext(observations))
                await spin_until(lambda: first.receives == 2)
                first.messages.put_nowait(frame(update=10))
                await spin_until(pending.done)
                assert pending.result().generation == 1

                # Documented quiet keepalive can replace the book at the same generation.
                pending = asyncio.create_task(anext(observations))
                await spin_until(lambda: first.receives == 3)
                now += 10
                first.messages.put_nowait(frame(update=30))
                await spin_until(pending.done)
                quiet = pending.result()
                assert (quiet.snapshot, quiet.update_id, quiet.generation) == (True, 30, 1)

                pending = asyncio.create_task(anext(observations))
                await spin_until(lambda: first.receives == 4)
                pong = json.dumps({"id": 99, "result": "pong", "error": None})
                now += 8
                first.messages.put_nowait(pong)
                await spin_until(lambda: first.receives == 5)
                now += 6
                first.messages.put_nowait(pong)
                await spin_until(lambda: first.receives == 6)
                assert not first.closed

                now += 1.1
                await spin_until(lambda: diagnostics[-1].code == "RECONNECT_WAIT")
                assert first.closed
                assert diagnostics[-2].code == "DEPTH_UPDATE_TIMEOUT"
                assert diagnostics[-1].retry_in_seconds == 1

                now += 1.1
                await spin_until(lambda: second.receives == 2)
                second.messages.put_nowait(frame(update=40))
                await spin_until(pending.done)
                recovered = pending.result()
                assert (recovered.snapshot, recovered.update_id, recovered.generation) == (True, 40, 2)
            finally:
                if pending is not None and not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                await observations.aclose()

    asyncio.run(run())


def test_zero_quantity_removes_level_and_control_is_bounded():
    book=DepthLocalBook("BTC_USDT",limit=5)
    book.apply(parse_depth_message(frame(),market="BTC_USDT",received_at=NOW),generation=1)
    raw=json.loads(frame(False,10,11)); raw["params"][1]["asks"]=[["101","0"],["102","1"]]
    value=parse_depth_message(json.dumps(raw),market="BTC_USDT",received_at=NOW)
    observed=book.apply(value,generation=1)
    assert observed.ask_price=="102"
    ack=parse_depth_message(json.dumps({"id":7,"result":{"status":"success"},"error":None}),
        market="BTC_USDT",received_at=NOW)
    assert ack.code=="SUBSCRIBED"


def test_depth_tape_is_restart_safe_and_public_only(tmp_path):
    path = tmp_path / "depth.sqlite3"
    value = DepthBookObservation(
        market="BTC_USDT", received_at=NOW, generation=1, update_id=10,
        past_update_id=None, snapshot=True, bid_price="99", ask_price="101",
        mid_price="100", spread_bps="200", bid_depth_top5="3",
        ask_depth_top5="2", imbalance_top5="0.2",
        bid_quantity_change="0", ask_quantity_change="0",
        order_flow_imbalance="2",
        source_sha256="sha256:" + "a" * 64,
    )
    first = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    first.ingest(value)
    first.close()
    second = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    status = second.status()
    second.close()
    assert status["bucket_count"] == 1
    assert status["market_bucket_counts"] == {"BTC_USDT": 1}
    assert status["public_data_only"] is True
    assert status["trading_authority"] is False


def test_depth_status_counts_updates_expiry_rollback_and_other_connections(tmp_path):
    path = tmp_path / "depth.sqlite3"
    initial = DepthLocalBook("BTC_USDT", limit=5).apply(
        parse_depth_message(frame(), market="BTC_USDT", received_at=NOW), generation=1,
    )
    store = WhiteBitDepthTapeStore(path, retention_days=1)
    try:
        assert store.status()["bucket_count"] == 0
        store.ingest(initial)
        store.ingest(initial)
        store.ingest(replace(initial, market="ETH_USDT"))
        assert store.status()["market_bucket_counts"] == {"BTC_USDT": 1, "ETH_USDT": 1}

        later = replace(initial, received_at=NOW + timedelta(days=1, seconds=1))
        store.connection.execute("""
            CREATE TRIGGER reject_bucket_expiry BEFORE DELETE ON buckets
            BEGIN SELECT RAISE(ABORT, 'test expiry rejection'); END
        """)
        with pytest.raises(sqlite3.IntegrityError, match="test expiry rejection"):
            store.ingest(later)
        assert store.status()["bucket_count"] == 2
        store.connection.execute("DROP TRIGGER reject_bucket_expiry")

        other = WhiteBitDepthTapeStore(path, retention_days=1)
        try:
            other.ingest(later)
            assert store.status()["market_bucket_counts"] == {"BTC_USDT": 1}
            store.ingest(replace(later, market="SOL_USDT"))
            status = other.status()
            assert status["market_bucket_counts"] == {"BTC_USDT": 1, "SOL_USDT": 1}
            assert status["bucket_count"] == 2
            assert status["first_bucket_epoch"] == status["last_bucket_epoch"] == int(
                later.received_at.timestamp()
            )
        finally:
            other.close()
    finally:
        store.close()


def test_depth_collector_heartbeat_distinguishes_feed_age_from_local_latency(tmp_path, monkeypatch):
    stopped = Event()
    args = SimpleNamespace(depth_limit=20, poll_seconds=1, stop_file=tmp_path / "stop")
    clock = 100.0
    heartbeats = []
    sleep = asyncio.sleep

    class Transport:
        def __init__(self, *, market, limit, diagnostic):
            self.diagnostic = diagnostic

        async def observations(self, stop):
            for code in (
                "DEPTH_UPDATE_TIMEOUT", "RECONNECT_WAIT", "CONNECTION_LOST",
                "RECONNECT_WAIT", "SUBSCRIBED",
            ):
                self.diagnostic(SimpleNamespace(code=code))
            yield SimpleNamespace(generation=7)
            await stop.wait()

    class Store:
        def status(self):
            nonlocal clock
            clock += 0.05
            return {"bucket_count": 1}

        def ingest(self, observation):
            nonlocal clock
            clock += 0.025

        def archive_completed_windows(self, markets, *, now_epoch):
            nonlocal clock
            clock += 0.12

    async def tick(seconds):
        nonlocal clock
        await sleep(0)
        clock += seconds + 0.4
        if len(heartbeats) >= 2:
            stopped.set()

    def capture(path, data):
        nonlocal clock
        heartbeats.append(data)
        clock += 0.06

    async def inline_write(write, *args):
        write(*args)

    monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthWebSocketTransport", Transport)
    monkeypatch.setattr(run_whitebit_depth_tape, "monotonic", lambda: clock)
    monkeypatch.setattr(run_whitebit_depth_tape, "atomic_write_json", capture)
    monkeypatch.setattr(run_whitebit_depth_tape.asyncio, "sleep", tick)
    monkeypatch.setattr(run_whitebit_depth_tape.asyncio, "to_thread", inline_write)
    asyncio.run(run_whitebit_depth_tape._collect(
        args, stopped, Store(), tmp_path / "checkpoint.json", ("BTC_USDT",),
    ))

    running = heartbeats[1]
    assert running["phase"] == "RUNNING"
    assert running["new_observations_last_checkpoint"] == 1
    assert running["errors"] == {}
    timing = running["timing"]
    assert timing["recent_max_ms"] == {
        "wakeup_delay": 400.0, "sqlite_ingest": 25.0, "sqlite_archive": 120.0,
        "sqlite_status": 50.0, "previous_checkpoint_write": 85.0,
    }
    assert timing["markets"]["BTC_USDT"] == {
        "observation_age_ms": 1535.0, "recent_max_observation_gap_ms": None, "generation": 7,
    }
    assert len(heartbeats) == 3
    assert heartbeats[-1]["phase"] == "STOPPED"
    event_field = "transport_events_since_start"
    assert heartbeats[0]["timing"][event_field] == {
        "BTC_USDT": {"DEPTH_UPDATE_TIMEOUT": 0, "CONNECTION_LOST": 0},
    }
    assert timing[event_field] == heartbeats[-1]["timing"][event_field] == {
        "BTC_USDT": {"DEPTH_UPDATE_TIMEOUT": 1, "CONNECTION_LOST": 1},
    }


@pytest.mark.parametrize(("cancel", "write_fails"), [(False, False), (True, False), (False, True)])
def test_depth_collection_continues_during_checkpoint_write_and_flushes_counts(
    tmp_path, monkeypatch, cancel, write_fails,
):
    stopped = Event()
    writer_release = Event()
    args = SimpleNamespace(depth_limit=20, poll_seconds=1, stop_file=tmp_path / "stop")
    heartbeats = []
    ingested = []

    async def exercise():
        loop = asyncio.get_running_loop()
        loop_thread = get_ident()
        writer_started = asyncio.Event()

        class Transport:
            def __init__(self, *, market, limit, diagnostic):
                self.diagnostic = diagnostic

            async def observations(self, stop):
                await writer_started.wait()
                self.diagnostic(SimpleNamespace(code="SUBSCRIBED"))
                self.diagnostic(SimpleNamespace(code="CONNECTION_LOST"))
                yield SimpleNamespace(generation=7)
                yield SimpleNamespace(generation=8)
                stopped.set()
                if cancel:
                    collector.cancel()
                    await asyncio.sleep(0)
                    collector.cancel()
                    await asyncio.sleep(0)
                writer_release.set()
                await stop.wait()

        class Store:
            def status(self):
                assert get_ident() == loop_thread
                return {"bucket_count": len(ingested)}

            def ingest(self, observation):
                assert get_ident() == loop_thread
                assert not writer_release.is_set()
                ingested.append(observation.generation)

            def archive_completed_windows(self, markets, *, now_epoch):
                assert get_ident() == loop_thread

        def write(path, payload):
            assert get_ident() != loop_thread
            if not heartbeats:
                loop.call_soon_threadsafe(writer_started.set)
                assert writer_release.wait(3)
            heartbeats.append(payload)
            if write_fails and len(heartbeats) == 1:
                raise OSError("injected checkpoint write failure")

        monkeypatch.setattr(run_whitebit_depth_tape, "WhiteBitDepthWebSocketTransport", Transport)
        monkeypatch.setattr(run_whitebit_depth_tape, "atomic_write_json", write)
        collector = asyncio.create_task(run_whitebit_depth_tape._collect(
            args, stopped, Store(), tmp_path / "checkpoint.json", ("BTC_USDT",),
        ))
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await collector
        elif write_fails:
            with pytest.raises(OSError, match="injected checkpoint write failure"):
                await collector
        else:
            await collector

    asyncio.run(exercise())
    assert ingested == [7, 8]
    assert [row["phase"] for row in heartbeats] == ["STARTING", "STOPPED"]
    assert [row["new_observations_last_checkpoint"] for row in heartbeats] == [0, 2]
    assert [row["bucket_count"] for row in heartbeats] == [0, 2]
    assert heartbeats[0]["errors"] == {}
    assert heartbeats[0]["timing"]["markets"] == {}
    assert heartbeats[0]["timing"]["transport_events_since_start"]["BTC_USDT"]["CONNECTION_LOST"] == 0
    assert heartbeats[1]["errors"] == {"BTC_USDT": "CONNECTION_LOST"}
    assert heartbeats[1]["timing"]["markets"]["BTC_USDT"]["generation"] == 8
    assert heartbeats[1]["timing"]["transport_events_since_start"]["BTC_USDT"]["CONNECTION_LOST"] == 1


@pytest.mark.parametrize(("winerror", "recovered"), [(5, True), (32, True), (33, True), (None, False)])
def test_depth_checkpoint_retries_only_transient_windows_replace_errors(
    tmp_path, monkeypatch, winerror, recovered,
):
    payloads = []
    delays = []
    write_error = PermissionError(13, "destination is in use")
    write_error.winerror = winerror
    errors = {"BTC_USDT": "INITIAL"}

    def write(path, payload):
        payloads.append(payload)
        if len(payloads) == 1:
            raise write_error

    async def sleep(delay):
        delays.append(delay)
        errors["BTC_USDT"] = "CHANGED"

    monkeypatch.setattr(run_whitebit_depth_tape, "atomic_write_json", write)
    monkeypatch.setattr(run_whitebit_depth_tape.asyncio, "sleep", sleep)
    operation = run_whitebit_depth_tape._checkpoint(
        tmp_path / "checkpoint.json", {"bucket_count": 1}, phase="RUNNING",
        markets=("BTC_USDT",), observations=1, errors=errors, timing={},
    )
    if recovered:
        assert asyncio.run(operation) == 1
        assert len(payloads) == 2 and payloads[0] is payloads[1]
        assert payloads[1]["errors"] == {"BTC_USDT": "INITIAL"}
        assert delays == [0.01]
    else:
        with pytest.raises(PermissionError) as caught:
            asyncio.run(operation)
        assert caught.value is write_error
        assert len(payloads) == 1 and delays == []


def test_depth_checkpoint_raises_after_bounded_windows_retry(tmp_path, monkeypatch):
    attempts = []
    delays = []
    locked = PermissionError(13, "destination is still in use")
    locked.winerror = 5

    def write(path, payload):
        attempts.append(payload)
        raise locked

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(run_whitebit_depth_tape, "atomic_write_json", write)
    monkeypatch.setattr(run_whitebit_depth_tape.asyncio, "sleep", sleep)
    with pytest.raises(PermissionError) as caught:
        asyncio.run(run_whitebit_depth_tape._checkpoint(
            tmp_path / "checkpoint.json", {}, phase="RUNNING",
            markets=("BTC_USDT",), observations=0, errors={}, timing={},
        ))
    assert caught.value is locked
    assert len(attempts) == 4
    assert delays == [0.01, 0.03, 0.06]


def test_execution_bbo_retains_exact_latest_prices_and_sizes(tmp_path):
    path = tmp_path / "exact.sqlite3"
    book = DepthLocalBook("BTC_USDT", limit=5)
    initial = book.apply(parse_depth_message(frame(), market="BTC_USDT", received_at=NOW),
                         generation=1)
    store = WhiteBitDepthTapeStore(path)
    store.ingest(replace(initial, bid_quantity=None, ask_quantity=None))
    assert store.connection.execute("SELECT COUNT(*) FROM execution_bbo").fetchone() == (0,)
    store.ingest(initial)
    update = json.loads(frame(False, 10, 11))
    update["params"][1]["asks"] = [["101", "0"], ["102", "4"]]
    second = book.apply(parse_depth_message(json.dumps(update), market="BTC_USDT",
        received_at=NOW + timedelta(milliseconds=500)), generation=1)
    store.ingest(second)
    store.close()
    reopened = WhiteBitDepthTapeStore(path)
    row = reopened.connection.execute(
        "SELECT bid_price,ask_price,bid_quantity,ask_quantity,update_id,source_sha256 FROM execution_bbo"
    ).fetchone()
    assert row == ("99", "102", "3", "4", 11, second.source_sha256)
    reopened.ingest(replace(second, received_at=NOW + timedelta(days=1, seconds=1)))
    assert reopened.connection.execute("SELECT COUNT(*) FROM execution_bbo").fetchone() == (1,)
    assert reopened.status()["bucket_count"] == 2
    reopened.close()


def test_causal_ofi_uses_best_quote_transition():
    book = DepthLocalBook("BTC_USDT", limit=5)
    book.apply(parse_depth_message(frame(), market="BTC_USDT", received_at=NOW),
               generation=1)
    raw = json.loads(frame(False, 10, 11))
    raw["params"][1]["bids"] = [["99", "5"]]
    raw["params"][1]["asks"] = [["101", "1"]]
    observed = book.apply(parse_depth_message(json.dumps(raw), market="BTC_USDT",
        received_at=NOW), generation=1)
    assert observed.order_flow_imbalance == "3"


def test_persistent_l2_pressure_confirms_only_matching_direction(tmp_path):
    path = tmp_path / "pressure.sqlite3"
    store = WhiteBitDepthTapeStore(path, bucket_seconds=1, retention_days=3)
    for offset in range(10):
        store.ingest(DepthBookObservation(
            market="BTC_USDT", received_at=NOW.replace(second=offset), generation=1,
            update_id=offset + 1, past_update_id=offset or None,
            snapshot=offset == 0, bid_price="99", ask_price="99.01",
            mid_price="99.005", spread_bps="1", bid_depth_top5="6",
            ask_depth_top5="4", imbalance_top5="0.2",
            bid_quantity_change="1", ask_quantity_change="-1",
            order_flow_imbalance="2", source_sha256="sha256:" + "b" * 64))
    now_epoch = int(NOW.replace(second=9).timestamp())
    long = store.microstructure_confirmation("BTC_USDT", "LONG", now_epoch=now_epoch)
    short = store.microstructure_confirmation("BTC_USDT", "SHORT", now_epoch=now_epoch)
    store.close()
    assert long["status"] == "CONFIRMED"
    assert short["status"] == "CONFLICT"


def test_depth_archive_excludes_unsettled_windows_and_survives_raw_expiry(tmp_path):
    path = tmp_path / "archive.sqlite3"
    observed = DepthLocalBook("BTC_USDT", limit=5).apply(
        parse_depth_message(frame(), market="BTC_USDT", received_at=NOW), generation=1)
    store = WhiteBitDepthTapeStore(path, retention_days=1)
    store.ingest(observed)
    store.ingest(replace(observed, received_at=NOW + timedelta(seconds=299),
                         mid_price="101", generation=2))
    store.ingest(replace(observed, received_at=NOW + timedelta(seconds=301),
                         mid_price="102", generation=2))
    start = int(NOW.timestamp())
    markets = ("BTC_USDT",)
    assert store.archive_completed_windows(markets, now_epoch=start + 359) == 0
    assert store.archive_completed_windows(markets, now_epoch=start + 360) == 1
    store.close()

    reopened = WhiteBitDepthTapeStore(path, retention_days=1)
    assert reopened.archive_completed_windows(markets, now_epoch=start + 360) == 0
    reopened.ingest(replace(observed, received_at=NOW + timedelta(days=2)))
    assert reopened.status()["bucket_count"] == 1
    archived = reopened.connection.execute(
        "SELECT window_epoch,features_json FROM archived_windows").fetchall()
    assert len(archived) == 1
    window, payload = archived[0]
    features = json.loads(payload)
    assert window == start
    assert features["observed_buckets"] == 2
    assert features["generation_count"] == 2
    assert features["first_bucket_epoch"] == start
    assert features["last_bucket_epoch"] == start + 299
    assert features["open_mid"] == "100"
    assert features["close_mid"] == "101"
    reopened.close()
