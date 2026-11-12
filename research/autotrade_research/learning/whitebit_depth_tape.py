"""Restart-safe one-second L2 feature tape for WhiteBIT OFI research."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

from ..integrations.whitebit_depth import DepthBookObservation
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-depth-feature-tape-v1"
ARCHIVE_WINDOW_SECONDS = 300
ARCHIVE_SETTLE_SECONDS = 60


class WhiteBitDepthTapeStore:
    def __init__(self, path: Path, *, bucket_seconds: int = 1,
                 retention_days: int = 3) -> None:
        if path.exists() and path.is_symlink(): raise ValueError("depth tape cannot be a symlink")
        if type(bucket_seconds) is not int or not 1 <= bucket_seconds <= 10:
            raise ValueError("bucket_seconds must be 1..10")
        if type(retention_days) is not int or not 1 <= retention_days <= 30:
            raise ValueError("retention_days must be 1..30")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path=path; self.bucket_seconds=bucket_seconds; self.retention_days=retention_days
        self.connection=sqlite3.connect(path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL"); self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
        CREATE TABLE IF NOT EXISTS buckets(
          market TEXT NOT NULL,bucket_epoch INTEGER NOT NULL,update_count INTEGER NOT NULL,
          snapshot_count INTEGER NOT NULL,first_update_id INTEGER NOT NULL,last_update_id INTEGER NOT NULL,
          open_mid TEXT NOT NULL,close_mid TEXT NOT NULL,min_spread_bps TEXT NOT NULL,max_spread_bps TEXT NOT NULL,
          bid_depth_top5 TEXT NOT NULL,ask_depth_top5 TEXT NOT NULL,imbalance_top5 TEXT NOT NULL,
          bid_quantity_change TEXT NOT NULL,ask_quantity_change TEXT NOT NULL,
          generation INTEGER NOT NULL,received_at TEXT NOT NULL,source_sha256 TEXT NOT NULL,
          public_data_only INTEGER NOT NULL CHECK(public_data_only=1),
          trading_authority INTEGER NOT NULL CHECK(trading_authority=0),
          order_flow_imbalance TEXT NOT NULL DEFAULT '0',
          PRIMARY KEY(market,bucket_epoch)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS depth_bucket_epoch ON buckets(bucket_epoch);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        CREATE TABLE IF NOT EXISTS archived_windows(
          market TEXT NOT NULL,window_epoch INTEGER NOT NULL,features_json TEXT NOT NULL,
          PRIMARY KEY(market,window_epoch)) WITHOUT ROWID;
        """)
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(buckets)")}
        if "order_flow_imbalance" not in columns:
            with self.connection:
                self.connection.execute(
                    "ALTER TABLE buckets ADD COLUMN order_flow_imbalance TEXT NOT NULL DEFAULT '0'")
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)",
                ("collection_started_at",datetime.now(timezone.utc).isoformat()))

    def close(self): self.connection.close()

    def ingest(self, value: DepthBookObservation) -> None:
        if type(value) is not DepthBookObservation: raise TypeError("canonical depth observation required")
        market=_symbol(value.market); bucket=int(value.received_at.timestamp())
        bucket-=bucket%self.bucket_seconds
        row=self.connection.execute("SELECT * FROM buckets WHERE market=? AND bucket_epoch=?",
            (market,bucket)).fetchone()
        if row is None:
            values=(market,bucket,1,int(value.snapshot),value.update_id,value.update_id,
                value.mid_price,value.mid_price,value.spread_bps,value.spread_bps,
                value.bid_depth_top5,value.ask_depth_top5,value.imbalance_top5,
                value.bid_quantity_change,value.ask_quantity_change,value.generation,
                value.received_at.isoformat(),value.source_sha256,1,0,
                value.order_flow_imbalance)
        else:
            r=list(row); r[2]+=1; r[3]+=int(value.snapshot); r[5]=value.update_id
            r[7]=value.mid_price; r[8]=format(min(Decimal(r[8]),Decimal(value.spread_bps)),"f")
            r[9]=format(max(Decimal(r[9]),Decimal(value.spread_bps)),"f")
            r[10]=value.bid_depth_top5; r[11]=value.ask_depth_top5; r[12]=value.imbalance_top5
            r[13]=format(Decimal(r[13])+Decimal(value.bid_quantity_change),"f")
            r[14]=format(Decimal(r[14])+Decimal(value.ask_quantity_change),"f")
            r[15]=value.generation; r[16]=value.received_at.isoformat(); r[17]=value.source_sha256
            r[20]=format(Decimal(r[20])+Decimal(value.order_flow_imbalance),"f")
            values=tuple(r)
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO buckets VALUES("+",".join("?" for _ in values)+")",values)
            cutoff=bucket-self.retention_days*86400
            self.connection.execute("DELETE FROM buckets WHERE bucket_epoch<?",(cutoff,))

    def archive_completed_windows(self, markets: tuple[str, ...], *, now_epoch: int) -> int:
        """Keep compact features after the raw one-second tape expires.

        Wait one minute after a window closes. Store observation coverage and
        connection generations so replay can recognize sparse windows and
        reconnects. This is feature evidence, without inferred executable fills.
        """
        if type(now_epoch) is not int or now_epoch < 0:
            raise ValueError("archive clock must be a non-negative epoch")
        size = ARCHIVE_WINDOW_SECONDS
        cutoff = (now_epoch - ARCHIVE_SETTLE_SECONDS) // size * size
        added = 0
        with self.connection:
            for market in markets:
                market = _symbol(market)
                (last,) = self.connection.execute(
                    "SELECT MAX(window_epoch) FROM archived_windows WHERE market=?", (market,)
                ).fetchone()
                start = 0 if last is None else last + size
                windows = self.connection.execute("""
                    SELECT (bucket_epoch / ?) * ? AS window_epoch, COUNT(*) AS buckets,
                           MIN(bucket_epoch) AS first_epoch, MAX(bucket_epoch) AS last_epoch,
                           SUM(snapshot_count) AS snapshots, COUNT(DISTINCT generation) AS generations,
                           AVG(CAST(imbalance_top5 AS REAL)) AS mean_imbalance,
                           AVG(CAST(max_spread_bps AS REAL)) AS mean_max_spread_bps,
                           SUM(CAST(order_flow_imbalance AS REAL)) AS order_flow_imbalance,
                           MAX(received_at) AS last_received_at
                    FROM buckets WHERE market=? AND bucket_epoch>=? AND bucket_epoch<?
                    GROUP BY window_epoch ORDER BY window_epoch
                """, (size, size, market, start, cutoff))
                for window, count, first, end, snapshots, generations, imbalance, spread, ofi, received in windows:
                    open_mid, close_mid, source = self.connection.execute("""
                        SELECT (SELECT open_mid FROM buckets WHERE market=? AND bucket_epoch=?),
                               close_mid, source_sha256
                        FROM buckets WHERE market=? AND bucket_epoch=?
                    """, (market, first, market, end)).fetchone()
                    features = {
                        "window_seconds": size, "source_bucket_seconds": self.bucket_seconds,
                        "observed_buckets": count, "first_bucket_epoch": first,
                        "last_bucket_epoch": end, "snapshot_count": snapshots,
                        "generation_count": generations, "last_received_at": received,
                        "open_mid": open_mid, "close_mid": close_mid,
                        "mean_imbalance": imbalance, "mean_max_spread_bps": spread,
                        "order_flow_imbalance": ofi, "last_source_sha256": source,
                    }
                    self.connection.execute(
                        "INSERT INTO archived_windows VALUES(?,?,?)",
                        (market, window, json.dumps(features, sort_keys=True,
                                                  separators=(",", ":"), allow_nan=False)))
                    added += 1
        return added

    def status(self) -> dict:
        count,first,last=self.connection.execute(
            "SELECT COUNT(*),MIN(bucket_epoch),MAX(bucket_epoch) FROM buckets").fetchone()
        markets={row[0]:row[1] for row in self.connection.execute(
            "SELECT market,COUNT(*) FROM buckets GROUP BY market ORDER BY market")}
        return {"schema":SCHEMA,"database":str(self.path),"bucket_seconds":self.bucket_seconds,
            "retention_days":self.retention_days,"bucket_count":count,"first_bucket_epoch":first,
            "last_bucket_epoch":last,"market_bucket_counts":markets,"public_data_only":True,
            "network_orders":False,"trading_authority":False}

    def microstructure_confirmation(self, market: str, direction: str, *,
                                    now_epoch: int, lookback_seconds: int = 20,
                                    minimum_samples: int = 8,
                                    maximum_age_seconds: int = 3,
                                    maximum_spread_bps: Decimal | str = "8",
                                    minimum_score: Decimal | str = "0.05",
                                    minimum_persistence: Decimal | str = "0.60") -> dict:
        """Confirm direction only when recent L2 pressure is fresh and persistent."""
        market = _symbol(market)
        if direction not in {"LONG", "SHORT"}:
            raise ValueError("direction must be LONG or SHORT")
        if type(now_epoch) is not int or now_epoch < 0:
            raise ValueError("now_epoch must be a non-negative integer")
        if not 5 <= lookback_seconds <= 300 or not 3 <= minimum_samples <= 300:
            raise ValueError("microstructure window is outside its bounded range")
        if not 1 <= maximum_age_seconds <= 30:
            raise ValueError("maximum age is outside its bounded range")
        spread_limit = Decimal(maximum_spread_bps)
        score_limit = Decimal(minimum_score)
        persistence_limit = Decimal(minimum_persistence)
        if not Decimal(0) < spread_limit < Decimal(1000):
            raise ValueError("maximum spread is invalid")
        if not Decimal(0) <= score_limit <= 1:
            raise ValueError("minimum score is invalid")
        if not Decimal("0.5") <= persistence_limit <= 1:
            raise ValueError("minimum persistence is invalid")
        rows = list(self.connection.execute(
            "SELECT bucket_epoch,max_spread_bps,bid_depth_top5,ask_depth_top5,"
            "imbalance_top5,order_flow_imbalance FROM buckets "
            "WHERE market=? AND bucket_epoch>=? AND bucket_epoch<=? "
            "ORDER BY bucket_epoch",
            (market, now_epoch - lookback_seconds + 1, now_epoch)))
        base = {"market": market, "direction": direction,
            "sample_count": len(rows), "lookback_seconds": lookback_seconds,
            "network_orders": False, "trading_authority": False}
        if not rows or now_epoch - rows[-1][0] > maximum_age_seconds:
            return {**base, "status": "STALE", "reasons": ["DEPTH_NOT_FRESH"]}
        if len(rows) < minimum_samples:
            return {**base, "status": "STALE", "reasons": ["INSUFFICIENT_DEPTH_SAMPLES"]}
        if max(Decimal(row[1]) for row in rows) > spread_limit:
            return {**base, "status": "CONFLICT", "reasons": ["SPREAD_TOO_WIDE"]}
        direction_sign = Decimal(1) if direction == "LONG" else Decimal(-1)
        scores = []
        for _, _, bid_depth, ask_depth, imbalance, ofi in rows:
            total_depth = Decimal(bid_depth) + Decimal(ask_depth)
            normalized_ofi = Decimal(0) if total_depth <= 0 else Decimal(ofi) / total_depth
            normalized_ofi = max(Decimal(-1), min(Decimal(1), normalized_ofi))
            scores.append((Decimal(imbalance) + normalized_ofi) / Decimal(2))
        aligned = [score * direction_sign for score in scores]
        persistence = (sum(value >= score_limit for value in aligned)
                       / Decimal(len(aligned)))
        mean_score = sum(aligned, Decimal(0)) / Decimal(len(aligned))
        confirmed = persistence >= persistence_limit and mean_score >= score_limit
        return {**base, "status": "CONFIRMED" if confirmed else "CONFLICT",
            "reasons": [] if confirmed else ["L2_PRESSURE_NOT_PERSISTENT"],
            "directional_mean_score": format(mean_score, "f"),
            "directional_persistence": format(persistence, "f"),
            "latest_bucket_epoch": rows[-1][0]}

