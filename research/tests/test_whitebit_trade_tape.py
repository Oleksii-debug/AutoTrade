import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from threading import Event
from types import SimpleNamespace

import pytest

from autotrade_research.integrations.whitebit_public import parse_recent_trades
from autotrade_research.learning.whitebit_trade_tape import WhiteBitTradeTapeStore
from research.autotrade_research.integrations.whitebit_public import parse_recent_trades as parse_runner_batch
from tools import run_whitebit_public_trade_tape


NOW = datetime(2026, 11, 12, 3, 0, 10, tzinfo=timezone.utc)


def _batch(rows, received=NOW, market="BTC_USDT"):
    return parse_recent_trades(
        json.dumps(rows, separators=(",", ":")).encode(), market, received
    )


def _row(identity, second, side, price, base="0.1", quote="10", rpi=False):
    return {
        "tradeID": identity, "price": price, "quote_volume": base,
        "base_volume": quote, "trade_timestamp": second,
        "type": side, "rpi": rpi,
    }


def test_trade_tape_deduplicates_restart_and_aggregates_exact_flow(tmp_path):
    epoch = int(NOW.timestamp())
    newest_first = [
        _row(14, epoch - 1, "sell", "99", quote="9.9", rpi=True),
        _row(13, epoch - 2, "buy", "102", quote="10.2"),
        _row(12, epoch - 3, "buy", "100", quote="10"),
    ]
    path = tmp_path / "tape.sqlite3"
    store = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    assert store.ingest(_batch(newest_first)) == 3
    assert store.ingest(_batch(newest_first)) == 0
    row = dict(store.connection.execute("SELECT * FROM buckets").fetchone())
    assert row["first_trade_id"] == 12
    assert row["last_trade_id"] == 14
    assert (row["first_price"], row["last_price"]) == ("100", "99")
    assert (row["high_price"], row["low_price"]) == ("102", "99")
    assert (row["aggressive_buy_count"], row["aggressive_sell_count"]) == (2, 1)
    assert row["aggressive_buy_base_quantity"] == "0.2"
    assert row["rpi_count"] == 1
    store.close()

    reopened = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    assert reopened.ingest(_batch(newest_first)) == 0
    status = reopened.status()
    assert status["last_trade_ids"] == {"BTC_USDT": 14}
    assert status["collection_started_at"].endswith("+00:00")
    assert status["network_orders"] is False
    assert status["trading_authority"] is False
    reopened.close()


def test_trade_tape_retention_removes_old_buckets_without_resetting_cursor(tmp_path):
    path = tmp_path / "tape.sqlite3"
    store = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=1)
    old_epoch = int(NOW.timestamp()) - 90000
    store.ingest(_batch([_row(10, old_epoch, "buy", "100")],
                        datetime.fromtimestamp(old_epoch + 1, timezone.utc)))
    store.ingest(_batch([_row(11, int(NOW.timestamp()), "sell", "101")]))
    status = store.status()
    assert status["bucket_count"] == 1
    assert status["last_trade_ids"] == {"BTC_USDT": 11}
    store.close()


def test_execution_prints_preserve_exact_near_and_sui_trade_evidence(tmp_path):
    epoch = int(NOW.timestamp())
    path = tmp_path / "tape.sqlite3"
    store = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    near = _batch([
        _row(22, epoch - 1, "sell", "1.23000000", base="4.50000000",
             quote="5.53500000", rpi=True),
        _row(21, epoch - 2, "buy", "1.22000000", base="3.25000000",
             quote="3.96500000"),
    ], market="NEAR_PERP")
    sui = _batch([
        _row(31, epoch - 3, "buy", "2.50000000", base="0.40000000",
             quote="1.00000000"),
    ], market="SUI_PERP")
    assert store.ingest(near) == 2
    assert store.ingest(sui) == 1

    rows = [dict(row) for row in store.connection.execute(
        "SELECT * FROM execution_prints ORDER BY market,trade_id"
    )]
    assert rows == [
        {
            "market": "NEAR_PERP", "trade_id": "21",
            "trade_at": "2026-11-12T03:00:08.000000Z",
            "received_at": "2026-11-12T03:00:10.000000Z",
            "price": "1.22000000", "side": "buy",
            "base_quantity": "3.25000000", "rpi": 0,
            "source_sha256": near.digest,
        },
        {
            "market": "NEAR_PERP", "trade_id": "22",
            "trade_at": "2026-11-12T03:00:09.000000Z",
            "received_at": "2026-11-12T03:00:10.000000Z",
            "price": "1.23000000", "side": "sell",
            "base_quantity": "4.50000000", "rpi": 1,
            "source_sha256": near.digest,
        },
        {
            "market": "SUI_PERP", "trade_id": "31",
            "trade_at": "2026-11-12T03:00:07.000000Z",
            "received_at": "2026-11-12T03:00:10.000000Z",
            "price": "2.50000000", "side": "buy",
            "base_quantity": "0.40000000", "rpi": 0,
            "source_sha256": sui.digest,
        },
    ]
    store.close()


def test_execution_prints_deduplicate_across_restart(tmp_path):
    epoch = int(NOW.timestamp())
    batch = _batch(
        [_row(41, epoch - 1, "buy", "1.5", base="2", quote="3")],
        market="NEAR_PERP",
    )
    path = tmp_path / "tape.sqlite3"
    store = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    assert store.ingest(batch) == 1
    store.close()

    reopened = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    assert reopened.ingest(batch) == 0
    assert reopened.connection.execute(
        "SELECT COUNT(*) FROM execution_prints"
    ).fetchone()[0] == 1
    row = reopened.connection.execute(
        "SELECT market,trade_id,side FROM execution_prints"
    ).fetchone()
    assert tuple(row) == ("NEAR_PERP", "41", "buy")
    assert reopened.status()["last_trade_ids"] == {"NEAR_PERP": 41}
    reopened.close()


def test_execution_prints_expire_after_one_day_without_changing_bucket_retention(tmp_path):
    path = tmp_path / "tape.sqlite3"
    store = WhiteBitTradeTapeStore(path, bucket_seconds=5, retention_days=14)
    old_epoch = int(NOW.timestamp()) - 90000
    old_received = datetime.fromtimestamp(old_epoch + 1, timezone.utc)
    assert store.ingest(_batch(
        [_row(51, old_epoch, "sell", "1.1")],
        received=old_received,
        market="SUI_PERP",
    )) == 1
    assert store.connection.execute(
        "SELECT COUNT(*) FROM execution_prints"
    ).fetchone()[0] == 1

    assert store.ingest(_batch(
        [_row(52, int(NOW.timestamp()), "buy", "1.2")],
        market="SUI_PERP",
    )) == 1
    prints = [tuple(row) for row in store.connection.execute(
        "SELECT trade_id,trade_at FROM execution_prints ORDER BY trade_id"
    )]
    assert prints == [("52", "2026-11-12T03:00:10.000000Z")]
    assert store.connection.execute(
        "SELECT COUNT(*) FROM buckets"
    ).fetchone()[0] == 2
    assert store.status()["last_trade_ids"] == {"SUI_PERP": 52}
    store.close()


def test_execution_print_failure_rolls_back_bucket_and_cursor_together(tmp_path):
    epoch = int(NOW.timestamp())
    store = WhiteBitTradeTapeStore(
        tmp_path / "tape.sqlite3", bucket_seconds=5, retention_days=14
    )
    store.connection.execute("""
        CREATE TRIGGER reject_execution_print
        BEFORE INSERT ON execution_prints
        BEGIN
            SELECT RAISE(ABORT, 'test execution-print rejection');
        END
    """)

    with pytest.raises(sqlite3.IntegrityError, match="execution-print rejection"):
        store.ingest(_batch(
            [_row(61, epoch - 1, "buy", "1.7", base="2", quote="3.4")],
            market="NEAR_PERP",
        ))

    assert store.connection.execute("SELECT COUNT(*) FROM buckets").fetchone()[0] == 0
    assert store.connection.execute("SELECT COUNT(*) FROM cursors").fetchone()[0] == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM execution_prints"
    ).fetchone()[0] == 0
    store.close()


def test_opt_in_migrates_existing_prints_preserving_rows_cursors_and_retention(tmp_path):
    path = tmp_path / "trade-tape.sqlite3"
    epoch = int(NOW.timestamp())
    near = _batch([_row(12, epoch, "sell", "100")], market="NEAR_PERP")
    btc = _batch([_row(23, epoch, "buy", "100")], market="BTC_PERP")
    eth = _batch([_row(34, epoch, "sell", "100")], market="ETH_PERP")
    sol = _batch([_row(45, epoch, "buy", "100")], market="SOL_PERP")

    original = WhiteBitTradeTapeStore(path)
    for batch in (near, btc, eth):
        assert original.ingest(batch) == 1
    original_print = tuple(original.connection.execute("SELECT * FROM execution_prints").fetchone())
    original.close()

    store = WhiteBitTradeTapeStore(path, extra_execution_print_markets=("BTC_PERP", "ETH_PERP"))
    assert tuple(store.connection.execute("SELECT * FROM execution_prints").fetchone()) == original_print
    assert store.status()["last_trade_ids"] == {"BTC_PERP": 23, "ETH_PERP": 34, "NEAR_PERP": 12}
    assert store.execution_snapshot_coverage(btc) == ("NO_RETAINED_EXACT_CURSOR", None)
    assert store.ingest(btc) == 0
    for batch, trade_id in ((btc, 24), (eth, 35), (sol, 46)):
        assert store.ingest(_batch([_row(trade_id, epoch, "buy", "100")], market=batch.market)) == 1
    assert [tuple(row) for row in store.connection.execute(
        "SELECT market,trade_id FROM execution_prints ORDER BY market"
    )] == [("BTC_PERP", "24"), ("ETH_PERP", "35"), ("NEAR_PERP", "12")]
    assert store.connection.execute(
        "SELECT name FROM pragma_index_list('execution_prints') WHERE name='execution_prints_trade_at'"
    ).fetchone() is not None
    store.close()

    default = WhiteBitTradeTapeStore(path)
    later = datetime.fromtimestamp(epoch + 86401, timezone.utc)
    assert default.ingest(_batch([_row(25, int(later.timestamp()), "buy", "100")],
                                 received=later, market="BTC_PERP")) == 1
    assert default.connection.execute("SELECT COUNT(*) FROM execution_prints").fetchone()[0] == 0
    default.close()


def test_exact_print_coverage_exposes_unverified_reconnect_boundaries(tmp_path, monkeypatch):
    market = "BTC_PERP"
    epoch = int(NOW.timestamp())
    store = run_whitebit_public_trade_tape.WhiteBitTradeTapeStore(
        tmp_path / "trade-tape.sqlite3", extra_execution_print_markets=(market,),
    )
    # The runner imports the canonical package under the research namespace.
    def batch(rows, *, received=NOW):
        return parse_runner_batch(json.dumps(rows).encode(), market, received)

    snapshots = []
    stopped = Event()
    received = datetime.fromtimestamp(epoch + 10, timezone.utc)
    args = SimpleNamespace(poll_seconds=1, stop_file=tmp_path / "stop.json")

    class Transport:
        def __init__(self, *, markets, diagnostic):
            assert markets == (market,)
            self.diagnostic = diagnostic

        async def batches(self, stop):
            self.diagnostic("SUBSCRIBED")
            yield batch([_row(23, epoch, "buy", "100")])
            self.diagnostic("CONNECTION_LOST")
            self.diagnostic("SUBSCRIBED")
            yield batch([_row(999, epoch + 5, "sell", "100")], received=received)
            stopped.set()
            await stop.wait()

    async def tick(_seconds):
        await asyncio_sleep(0)

    def checkpoint(_path, document):
        snapshots.append(json.loads(json.dumps(document)))

    asyncio_sleep = asyncio.sleep
    monkeypatch.setattr(run_whitebit_public_trade_tape, "WhiteBitPublicTradeWebSocketTransport", Transport)
    monkeypatch.setattr(run_whitebit_public_trade_tape, "atomic_write_json", checkpoint)
    monkeypatch.setattr(run_whitebit_public_trade_tape.asyncio, "sleep", tick)
    try:
        store.ingest(batch([_row(23, epoch, "buy", "100")]))
        asyncio.run(run_whitebit_public_trade_tape._collect(
            args, stopped, store, tmp_path / "checkpoint.json", (market,),
        ))
    finally:
        store.close()

    assert snapshots[-1]["exact_print_coverage"][market] == {
        "state": "CURSOR_NOT_IN_FIRST_BATCH",
        "unverified_boundaries_since_start": 1,
        "last_unverified_boundary": {
            "prior_receipt_utc": "2026-11-12T03:00:10.000000Z",
            "first_batch_receipt_utc": received.isoformat(),
        },
    }
    assert snapshots[-1]["last_trade_ids"] == {market: 999}


def test_trade_tape_consumer_failure_exits_instead_of_publishing_healthy_heartbeats(tmp_path, monkeypatch):
    stopped = Event()
    args = SimpleNamespace(poll_seconds=1, stop_file=tmp_path / "stop.json")
    snapshots = []
    sleep = asyncio.sleep

    class Transport:
        def __init__(self, *, markets, diagnostic):
            self.diagnostic = diagnostic

        async def batches(self, stop):
            self.diagnostic("SUBSCRIBED")
            yield SimpleNamespace(market="BTC_PERP")

    class Store:
        execution_print_markets = frozenset()

        def status(self):
            return {"bucket_count": 0}

        def ingest(self, batch):
            raise sqlite3.OperationalError("injected ingest failure")

    async def tick(_seconds):
        await sleep(0)
        if len(snapshots) > 2:
            stopped.set()

    monkeypatch.setattr(run_whitebit_public_trade_tape, "WhiteBitPublicTradeWebSocketTransport", Transport)
    monkeypatch.setattr(run_whitebit_public_trade_tape, "atomic_write_json", lambda _, data: snapshots.append(data))
    monkeypatch.setattr(run_whitebit_public_trade_tape.asyncio, "sleep", tick)
    with pytest.raises(sqlite3.OperationalError, match="injected ingest failure"):
        asyncio.run(run_whitebit_public_trade_tape._collect(
            args, stopped, Store(), tmp_path / "checkpoint.json", ("BTC_PERP",),
        ))

    assert [row["phase"] for row in snapshots] == ["STARTING", "FAILED"]
    assert snapshots[-1]["errors"] == {"collector": "TRADE_CONSUMER_FAILED"}
    assert "injected ingest failure" not in json.dumps(snapshots[-1])


def test_trade_checkpoint_retries_while_new_trades_arrive(tmp_path, monkeypatch):
    stopped = Event()
    market = "BTC_PERP"
    args = SimpleNamespace(poll_seconds=1, stop_file=tmp_path / "stop.json")
    snapshots = []
    delays = []
    sleep = asyncio.sleep
    blocked = PermissionError("temporary checkpoint reader")
    blocked.winerror = 5

    class Transport:
        def __init__(self, *, markets, diagnostic):
            self.diagnostic = diagnostic

        async def batches(self, stop):
            self.diagnostic("SUBSCRIBED")
            yield SimpleNamespace(market=market)
            stopped.set()
            await stop.wait()

    class Store:
        execution_print_markets = frozenset({market})

        def status(self):
            return {}

        def execution_snapshot_coverage(self, batch):
            return "CURSOR_OVERLAP", None

        def ingest(self, batch):
            return 3

    def write(_, payload):
        snapshots.append(payload)
        if len(snapshots) == 1:
            raise blocked

    async def tick(delay):
        delays.append(delay)
        await sleep(0)

    monkeypatch.setattr(run_whitebit_public_trade_tape, "WhiteBitPublicTradeWebSocketTransport", Transport)
    monkeypatch.setattr(run_whitebit_public_trade_tape, "atomic_write_json", write)
    monkeypatch.setattr(run_whitebit_public_trade_tape.asyncio, "sleep", tick)
    asyncio.run(run_whitebit_public_trade_tape._collect(
        args, stopped, Store(), tmp_path / "checkpoint.json", (market,),
    ))

    assert delays == [0.01, 1]
    assert snapshots[0] is snapshots[1]
    assert snapshots[0]["exact_print_coverage"][market]["state"] == "AWAITING_FIRST_BATCH"
    assert snapshots[-1]["phase"] == "STOPPED"
    assert snapshots[-1]["new_trades_last_checkpoint"] == 3
    assert snapshots[-1]["checkpoint_retries_since_start"] == 1
    assert snapshots[-1]["exact_print_coverage"][market]["state"] == "CURSOR_OVERLAP"


