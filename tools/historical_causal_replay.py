"""Privileged archive-to-causal-feeder adapter, never a strategy or fill engine.

ARCHIVE_OBSERVED uses authenticated local capture time when publisher release
time is unknown. BAR_CLOSE_SCENARIO is an explicit retrospective assumption;
it is NOT evidence that these exact, potentially revised bytes were available
historically. Strategies receive CausalFeeder.view(), never this dataset.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path

from autotrade_runtime.artifacts import ArtifactStore
from autotrade_research.evaluation.replay.feeder import CausalDataset, CausalEvent
from tools.historical_public_klines import (
    HistoricalArchiveError, INTERVAL_SECONDS, SYMBOL, _month_range, _utc_day,
)
from tools.run_historical_zero import _checked_month

ARCHIVE_OBSERVED = "ARCHIVE_OBSERVED"
BAR_CLOSE_SCENARIO = "BAR_CLOSE_SCENARIO_NOT_POINT_IN_TIME_EVIDENCE"


def load_causal_archive(
    root: Path, *, symbol: str, interval: str, from_month: str,
    through_month: str, daily_through: str | None = None,
    availability_policy: str = ARCHIVE_OBSERVED,
    artifact_store: ArtifactStore | None = None,
) -> CausalDataset:
    """Create one immutable source cut through the existing verified reader.

    No gaps, overlap, inferred publisher release timestamps or interpolated
    prices are admitted. UTC spot candles are a continuous 24/7 calendar.
    Snapshot identity includes source/capture/policy; restart cannot silently
    accept newly downloaded or revised history. No 10,000-bar ZERO limit is
    imposed on the separate data actor; the campaign is bounded to 600 periods.
    """
    if type(symbol) is not str or SYMBOL.fullmatch(symbol) is None or interval not in INTERVAL_SECONDS:
        raise HistoricalArchiveError("invalid instrument selection")
    if availability_policy not in (ARCHIVE_OBSERVED, BAR_CLOSE_SCENARIO):
        raise HistoricalArchiveError("unknown historical availability policy")
    store = artifact_store if artifact_store is not None else ArtifactStore(root / ".artifact-store")
    if type(store) is not ArtifactStore:
        raise HistoricalArchiveError("canonical ArtifactStore is required")
    periods = [(month, None) for month in _month_range(from_month, through_month)]
    if daily_through is not None:
        last = _utc_day(daily_through)
        year, month = map(int, through_month.split("-"))
        first = (datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12
                 else datetime(year, month + 1, 1, tzinfo=timezone.utc))
        days = (last - first).days + 1
        if not 1 <= days <= 35:
            raise HistoricalArchiveError("daily gap must span 1 to 35 days after monthly cut")
        for offset in range(days):
            day = (first + timedelta(days=offset)).date().isoformat()
            periods.append((day[:7], day))
    if not 1 <= len(periods) <= 600:
        raise HistoricalArchiveError("historical period budget exceeded")
    step = timedelta(seconds=INTERVAL_SECONDS[interval])
    events, sources = [], []
    previous = None
    for month, day in periods:
        dates, candles, manifest = _checked_month(
            root, symbol, interval, month, day=day,
            include_ohlcv=True, artifact_store=store,
        )
        if previous is not None and dates[0] != previous + step:
            raise HistoricalArchiveError("historical source gap or overlap")
        previous = dates[-1]
        captures = []
        for ref in manifest["artifact_evidence"].values():
            authenticated, _ = store.read_authenticated_snapshot(ref["artifact_id"])
            captures.append(datetime.fromisoformat(authenticated["created_at"].replace("Z", "+00:00")))
        captured_at = max(captures)
        if captured_at > datetime.now(timezone.utc):
            raise HistoricalArchiveError("source capture is in the future")
        source = {"period": day or month, "url": manifest["source_url"],
                  "zip_sha256": manifest["source_zip_sha256"],
                  "csv_sha256": manifest["normalized_csv_sha256"],
                  "captured_at": captured_at.isoformat()}
        sources.append(source)
        for opened_at, values in zip(dates, candles):
            closed_at = opened_at + step
            available_at = (max(closed_at, captured_at) if availability_policy == ARCHIVE_OBSERVED
                            else closed_at)
            events.append(CausalEvent.create(
                event_id=f"{symbol}:{interval}:{opened_at.isoformat()}",
                kind="HISTORICAL_SPOT_CANDLE", event_time=closed_at,
                available_at=available_at, ingested_at=max(captured_at, available_at),
                source_priority=0, source_sequence=len(events),
                payload={"symbol": symbol, "interval": interval,
                         "open_time_utc": opened_at.isoformat(),
                         "close_time_utc": closed_at.isoformat(),
                         **dict(zip(("open", "high", "low", "close", "volume"), values)),
                         "availability_policy": availability_policy,
                         "simulation_only": True},
            ))
    identity = {"schema": "autotrade-historical-causal-cut-v1",
                "symbol": symbol, "interval": interval,
                "availability_policy": availability_policy, "sources": sources}
    digest = "sha256:" + sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return CausalDataset.create(manifest_sha256=digest, events=events)
