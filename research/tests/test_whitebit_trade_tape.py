from datetime import datetime, timezone
import json

from autotrade_research.integrations.whitebit_public import parse_recent_trades
from autotrade_research.learning.whitebit_trade_tape import WhiteBitTradeTapeStore


NOW = datetime(2026, 11, 12, 3, 0, 10, tzinfo=timezone.utc)


def _batch(rows, received=NOW):
    return parse_recent_trades(
        json.dumps(rows, separators=(",", ":")).encode(), "BTC_USDT", received
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


