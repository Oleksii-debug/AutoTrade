"""Restart-safe one-second L2 feature tape for WhiteBIT OFI research."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sqlite3

from ..integrations.whitebit_depth import DepthBookObservation
from ..integrations.whitebit_public import _symbol


SCHEMA = "autotrade-whitebit-depth-feature-tape-v1"


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
          PRIMARY KEY(market,bucket_epoch)) WITHOUT ROWID;
        CREATE INDEX IF NOT EXISTS depth_bucket_epoch ON buckets(bucket_epoch);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
        """)
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
                value.received_at.isoformat(),value.source_sha256,1,0)
        else:
            r=list(row); r[2]+=1; r[3]+=int(value.snapshot); r[5]=value.update_id
            r[7]=value.mid_price; r[8]=format(min(Decimal(r[8]),Decimal(value.spread_bps)),"f")
            r[9]=format(max(Decimal(r[9]),Decimal(value.spread_bps)),"f")
            r[10]=value.bid_depth_top5; r[11]=value.ask_depth_top5; r[12]=value.imbalance_top5
            r[13]=format(Decimal(r[13])+Decimal(value.bid_quantity_change),"f")
            r[14]=format(Decimal(r[14])+Decimal(value.ask_quantity_change),"f")
            r[15]=value.generation; r[16]=value.received_at.isoformat(); r[17]=value.source_sha256
            values=tuple(r)
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO buckets VALUES("+",".join("?" for _ in values)+")",values)
            cutoff=bucket-self.retention_days*86400
            self.connection.execute("DELETE FROM buckets WHERE bucket_epoch<?",(cutoff,))

    def status(self) -> dict:
        count,first,last=self.connection.execute(
            "SELECT COUNT(*),MIN(bucket_epoch),MAX(bucket_epoch) FROM buckets").fetchone()
        markets={row[0]:row[1] for row in self.connection.execute(
            "SELECT market,COUNT(*) FROM buckets GROUP BY market ORDER BY market")}
        return {"schema":SCHEMA,"database":str(self.path),"bucket_seconds":self.bucket_seconds,
            "retention_days":self.retention_days,"bucket_count":count,"first_bucket_epoch":first,
            "last_bucket_epoch":last,"market_bucket_counts":markets,"public_data_only":True,
            "network_orders":False,"trading_authority":False}


