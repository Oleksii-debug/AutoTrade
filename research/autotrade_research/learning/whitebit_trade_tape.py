"""Durable, bounded WhiteBIT public trade aggregates for microstructure research."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
from pathlib import Path
import sqlite3

from ..integrations.whitebit_public import (
    PublicTradeBatch,
    _symbol,
)


SCHEMA = "autotrade-whitebit-public-trade-tape-v1"
EXECUTION_PRINT_MARKETS = frozenset({"NEAR_PERP", "SUI_PERP"})
EXECUTION_PRINT_RETENTION_SECONDS = 86400


def _sum(left: str, right: str) -> str:
    with localcontext() as context:
        context.prec = 80
        return format(Decimal(left) + Decimal(right), "f")


def _utc_microseconds(value: datetime) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        raise TypeError("execution print timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


class WhiteBitTradeTapeStore:
    """Five-second trade-flow buckets with exact restart cursors.

    The REST endpoint repeats its latest 100 trades.  A per-market monotonically
    increasing trade-id cursor prevents duplicate volume after a restart.  The
    store contains public observations only and cannot place an order.
    """

    def __init__(
        self, path: Path, *, bucket_seconds: int = 5, retention_days: int = 14,
    ) -> None:
        if type(path) is not type(Path()) or path.exists() and path.is_symlink():
            raise ValueError("trade tape path must be an ordinary Path")
        if type(bucket_seconds) is not int or not 1 <= bucket_seconds <= 60:
            raise ValueError("bucket_seconds must be 1..60")
        if type(retention_days) is not int or not 1 <= retention_days <= 365:
            raise ValueError("retention_days must be 1..365")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ValueError("trade tape parent must be an ordinary directory")
        self.path = path
        self.bucket_seconds = bucket_seconds
        self.retention_days = retention_days
        self.connection = sqlite3.connect(path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS cursors(
                market TEXT PRIMARY KEY,
                last_trade_id INTEGER NOT NULL CHECK(last_trade_id > 0)
            );
            CREATE TABLE IF NOT EXISTS buckets(
                market TEXT NOT NULL,
                bucket_epoch INTEGER NOT NULL,
                bucket_seconds INTEGER NOT NULL,
                first_trade_id INTEGER NOT NULL,
                last_trade_id INTEGER NOT NULL,
                first_price TEXT NOT NULL,
                last_price TEXT NOT NULL,
                high_price TEXT NOT NULL,
                low_price TEXT NOT NULL,
                aggressive_buy_count INTEGER NOT NULL,
                aggressive_sell_count INTEGER NOT NULL,
                aggressive_buy_base_quantity TEXT NOT NULL,
                aggressive_sell_base_quantity TEXT NOT NULL,
                aggressive_buy_quote_quantity TEXT NOT NULL,
                aggressive_sell_quote_quantity TEXT NOT NULL,
                rpi_count INTEGER NOT NULL,
                received_at TEXT NOT NULL,
                response_sha256 TEXT NOT NULL,
                public_data_only INTEGER NOT NULL CHECK(public_data_only = 1),
                trading_authority INTEGER NOT NULL CHECK(trading_authority = 0),
                PRIMARY KEY(market, bucket_epoch)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS buckets_epoch ON buckets(bucket_epoch);
            CREATE TABLE IF NOT EXISTS execution_prints(
                market TEXT NOT NULL CHECK(market IN ('NEAR_PERP','SUI_PERP')),
                trade_id TEXT NOT NULL,
                trade_at TEXT NOT NULL,
                received_at TEXT NOT NULL,
                price TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                base_quantity TEXT NOT NULL,
                rpi INTEGER NOT NULL CHECK(rpi IN (0,1)),
                source_sha256 TEXT NOT NULL,
                PRIMARY KEY(market, trade_id)
            ) WITHOUT ROWID;
            CREATE INDEX IF NOT EXISTS execution_prints_trade_at
                ON execution_prints(trade_at);
            CREATE TABLE IF NOT EXISTS metadata(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            ) WITHOUT ROWID;
            """
        )
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO metadata(key,value) VALUES(?,?)",
                ("collection_started_at", datetime.now(timezone.utc).isoformat()),
            )
        check = self.connection.execute("PRAGMA quick_check").fetchone()[0]
        if check != "ok":
            raise ValueError("trade tape integrity check failed")

    def close(self) -> None:
        self.connection.close()

    def _cursor(self, market: str) -> int:
        row = self.connection.execute(
            "SELECT last_trade_id FROM cursors WHERE market=?", (market,)
        ).fetchone()
        return 0 if row is None else int(row[0])

    @staticmethod
    def _new_row(trade, bucket: int, seconds: int, batch: PublicTradeBatch) -> dict:
        buy = trade.side == "buy"
        return {
            "market": trade.market, "bucket_epoch": bucket,
            "bucket_seconds": seconds, "first_trade_id": trade.trade_id,
            "last_trade_id": trade.trade_id, "first_price": trade.price,
            "last_price": trade.price, "high_price": trade.price,
            "low_price": trade.price,
            "aggressive_buy_count": int(buy),
            "aggressive_sell_count": int(not buy),
            "aggressive_buy_base_quantity": trade.base_quantity if buy else "0",
            "aggressive_sell_base_quantity": trade.base_quantity if not buy else "0",
            "aggressive_buy_quote_quantity": trade.quote_quantity if buy else "0",
            "aggressive_sell_quote_quantity": trade.quote_quantity if not buy else "0",
            "rpi_count": int(trade.rpi),
            "received_at": batch.received_at.isoformat(),
            "response_sha256": batch.digest,
            "public_data_only": 1, "trading_authority": 0,
        }

    @staticmethod
    def _merge(row: dict, trade, batch: PublicTradeBatch) -> dict:
        buy = trade.side == "buy"
        row["last_trade_id"] = trade.trade_id
        row["last_price"] = trade.price
        row["high_price"] = format(max(Decimal(row["high_price"]), Decimal(trade.price)), "f")
        row["low_price"] = format(min(Decimal(row["low_price"]), Decimal(trade.price)), "f")
        count = "aggressive_buy_count" if buy else "aggressive_sell_count"
        row[count] += 1
        base = "aggressive_buy_base_quantity" if buy else "aggressive_sell_base_quantity"
        quote = "aggressive_buy_quote_quantity" if buy else "aggressive_sell_quote_quantity"
        row[base] = _sum(row[base], trade.base_quantity)
        row[quote] = _sum(row[quote], trade.quote_quantity)
        row["rpi_count"] += int(trade.rpi)
        row["received_at"] = batch.received_at.isoformat()
        row["response_sha256"] = batch.digest
        return row

    def ingest(self, batch: PublicTradeBatch) -> int:
        if type(batch) is not PublicTradeBatch:
            raise TypeError("canonical PublicTradeBatch is required")
        market = _symbol(batch.market)
        cursor = self._cursor(market)
        new = [trade for trade in reversed(batch.trades) if trade.trade_id > cursor]
        if not new:
            return 0
        with self.connection:
            for trade in new:
                bucket = int(trade.trade_at.timestamp())
                bucket -= bucket % self.bucket_seconds
                existing = self.connection.execute(
                    "SELECT * FROM buckets WHERE market=? AND bucket_epoch=?",
                    (market, bucket),
                ).fetchone()
                row = (self._new_row(trade, bucket, self.bucket_seconds, batch)
                       if existing is None else self._merge(dict(existing), trade, batch))
                columns = tuple(row)
                values = tuple(row[name] for name in columns)
                self.connection.execute(
                    f"INSERT OR REPLACE INTO buckets ({','.join(columns)}) "
                    f"VALUES ({','.join('?' for _ in columns)})", values,
                )
                if market in EXECUTION_PRINT_MARKETS:
                    self.connection.execute(
                        "INSERT INTO execution_prints("
                        "market,trade_id,trade_at,received_at,price,side,"
                        "base_quantity,rpi,source_sha256"
                        ") VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            market,
                            str(trade.trade_id),
                            _utc_microseconds(trade.trade_at),
                            _utc_microseconds(batch.received_at),
                            trade.price,
                            trade.side,
                            trade.base_quantity,
                            int(trade.rpi),
                            batch.digest,
                        ),
                    )
            latest = max(trade.trade_id for trade in new)
            self.connection.execute(
                "INSERT OR REPLACE INTO cursors(market,last_trade_id) VALUES(?,?)",
                (market, latest),
            )
            cutoff = int(batch.received_at.timestamp()) - self.retention_days * 86400
            self.connection.execute("DELETE FROM buckets WHERE bucket_epoch < ?", (cutoff,))
            print_cutoff = _utc_microseconds(
                batch.received_at - timedelta(seconds=EXECUTION_PRINT_RETENTION_SECONDS)
            )
            self.connection.execute(
                "DELETE FROM execution_prints WHERE trade_at < ?", (print_cutoff,)
            )
        return len(new)

    def status(self) -> dict:
        count, first_epoch, last_epoch = self.connection.execute(
            "SELECT COUNT(*), MIN(bucket_epoch), MAX(bucket_epoch) FROM buckets"
        ).fetchone()
        cursors = {
            str(row[0]): int(row[1]) for row in self.connection.execute(
                "SELECT market,last_trade_id FROM cursors ORDER BY market"
            )
        }
        started_at = self.connection.execute(
            "SELECT value FROM metadata WHERE key='collection_started_at'"
        ).fetchone()[0]
        return {
            "schema": SCHEMA,
            "database": str(self.path),
            "bucket_seconds": self.bucket_seconds,
            "retention_days": self.retention_days,
            "bucket_count": int(count),
            "first_bucket_epoch": first_epoch,
            "last_bucket_epoch": last_epoch,
            "last_trade_ids": cursors,
            "collection_started_at": str(started_at),
            "public_data_only": True,
            "network_orders": False,
            "trading_authority": False,
        }


