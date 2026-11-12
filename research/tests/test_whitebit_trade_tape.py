from datetime import datetime, timezone
import json
import sqlite3

import pytest

from autotrade_research.integrations.whitebit_public import parse_recent_trades
from autotrade_research.learning.whitebit_trade_tape import WhiteBitTradeTapeStore


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


