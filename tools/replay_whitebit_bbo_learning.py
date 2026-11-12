"""Replay authenticated WhiteBIT BBO artifacts through causal ZERO learning.

The replay is chronological, reads only integrity-checked public artifacts and
writes to a separate learner state.  It has no credential or order surface.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "research", ROOT / "mvp"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.store import (
    ArtifactIntegrityError,
    ArtifactStore,
)
from research.autotrade_research.io.strict_json import strict_json_loads
from research.autotrade_research.learning.whitebit_live_zero import (
    WhiteBitLiveZeroLearning,
    validate_public_snapshot,
)


SOURCE_URI = "wss://wss.whitebit.com/ws#bookTicker"
RIGHTS_ID = "whitebit-public-websocket-internal-v1"


def _instant(value: object, *, name: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{name} must be exact text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def snapshot_from_artifact(
    manifest: dict, data: bytes, *, expected_market: str = "BTC_USDT",
) -> dict:
    if (type(expected_market) is not str or not expected_market
            or expected_market != expected_market.upper()
            or "_" not in expected_market):
        raise ValueError("expected market must be a canonical symbol")
    if (manifest.get("media_type") != "application/json"
            or manifest.get("source_refs") != [SOURCE_URI]
            or manifest.get("rights") != {
                "storage": True, "export": False, "rights_id": RIGHTS_ID,
            }):
        raise ValueError("artifact authority differs from public WhiteBIT BBO")
    metadata = manifest.get("metadata")
    if (type(metadata) is not dict
            or metadata.get("provider") != "WHITEBIT"
            or metadata.get("market") != expected_market):
        raise ValueError("artifact metadata differs from expected WhiteBIT BBO")
    raw = strict_json_loads(data.decode("utf-8"))
    if (type(raw) is not dict or set(raw) != {"method", "params", "id"}
            or raw.get("method") != "bookTicker_update"
            or raw.get("id") is not None
            or type(raw.get("params")) is not list
            or len(raw["params"]) != 1
            or type(raw["params"][0]) is not list
            or len(raw["params"][0]) != 8):
        raise ValueError("artifact payload differs from WhiteBIT bookTicker")
    row = raw["params"][0]
    if (row[2] != metadata.get("market")
            or str(row[3]) != str(metadata.get("update_id"))):
        raise ValueError("artifact payload identity differs from manifest")
    available_at = metadata.get("observed_at")
    source_at = metadata.get("source_time")
    published_at = manifest.get("created_at")
    _instant(source_at, name="source_time")
    _instant(available_at, name="observed_at")
    _instant(published_at, name="created_at")
    generation = int(metadata.get("stream_generation"))
    snapshot = {
        "schema": "autotrade-whitebit-public-supervisor-v1",
        "market": row[2],
        "venue": "WHITEBIT_SPOT",
        "bid_price": row[4],
        "bid_quantity": row[5],
        "ask_price": row[6],
        "ask_quantity": row[7],
        "source_event_at": source_at,
        "available_at": available_at,
        "published_at": published_at,
        "update_id": row[3],
        "stream_generation": generation,
        "raw_evidence_ref": {
            "artifact_id": manifest["artifact_id"],
            "sha256": manifest["sha256"],
            "source_uri": SOURCE_URI,
            "observed_at": available_at,
            "rights_id": RIGHTS_ID,
        },
        "public_data_only": True,
        "trading_authority": False,
    }
    return validate_public_snapshot(snapshot)


def replay(*, artifact_root: Path, state_dir: Path, maximum_artifacts: int = 4096,
           outcome_horizon_seconds: int = 60,
           feature_mode: str = "TOP_BOOK_IMBALANCE",
           market: str = "BTC_USDT") -> dict:
    if type(maximum_artifacts) is not int or not 2 <= maximum_artifacts <= 100_000:
        raise ValueError("maximum_artifacts must be 2..100000")
    if type(outcome_horizon_seconds) is not int or not 1 <= outcome_horizon_seconds <= 3600:
        raise ValueError("outcome horizon must be 1..3600 seconds")
    if feature_mode not in {
        "TOP_BOOK_IMBALANCE", "TOP_BOOK_IMBALANCE_PERSISTENCE",
        "TOP_BOOK_IMBALANCE_CHANGE",
    }:
        raise ValueError("replay feature mode is unsupported")
    store = ArtifactStore(artifact_root)
    paths = list(store.manifests.glob("*.json"))
    if len(paths) > maximum_artifacts:
        paths = sorted(paths, key=lambda path: path.stat().st_mtime_ns)[-maximum_artifacts:]
    snapshots = []
    skipped_missing = 0
    for path in paths:
        try:
            manifest, data = store.read_authenticated_snapshot(path.stem)
        except (FileNotFoundError, ArtifactIntegrityError, OSError):
            # The live writer evicts the oldest quota entry. Ignore only an
            # identity that disappeared during this read; an extant corrupt
            # artifact still fails closed.
            if path.exists() or path.is_symlink():
                raise
            skipped_missing += 1
            continue
        snapshots.append(snapshot_from_artifact(
            manifest, data, expected_market=market,
        ))
    snapshots.sort(key=lambda item: (_instant(item["available_at"], name="available_at"),
                                     int(item["update_id"])))
    learner = WhiteBitLiveZeroLearning(
        state_dir,
        cycle_episodes=12,
        cycle_seconds=1800,
        evaluation_episodes=12,
        max_drawdown_usdt="2",
        cooldown_seconds=60,
        max_trials=12,
        max_observation_gap_seconds=180,
        min_evaluation_trades=3,
        outcome_horizon_seconds=outcome_horizon_seconds,
        signal_lookback_seconds=0,
        short_carry_bps_per_day="2",
        feature_mode=feature_mode,
    )
    processed = duplicates = 0
    for snapshot in snapshots:
        result = learner.process_snapshot(snapshot, publish_projections=False)
        if result["status"] == "PROCESSED":
            processed += 1
        else:
            duplicates += 1
    learner._publish_projections()
    checkpoint = learner.status()
    summary = {
        "schema": "autotrade-whitebit-bbo-learning-replay-v1",
        "authenticated_artifacts": len(snapshots),
        "processed": processed,
        "duplicates": duplicates,
        "skipped_concurrent_evictions": skipped_missing,
        "first_available_at": None if not snapshots else snapshots[0]["available_at"],
        "last_available_at": None if not snapshots else snapshots[-1]["available_at"],
        "episode_count": checkpoint["episode_count"],
        "model_version_count": checkpoint["model_version_count"],
        "market": market,
        "feature_mode": feature_mode,
        "learning_cycle": checkpoint["learning_cycle"],
        "public_data_only": True,
        "network_orders": False,
        "trading_authority": False,
    }
    atomic_write_json(state_dir / "replay-summary.json", summary)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--maximum-artifacts", type=int, default=4096)
    parser.add_argument("--outcome-horizon-seconds", type=int, default=60)
    parser.add_argument("--market", default="BTC_USDT")
    parser.add_argument("--feature-mode", choices=(
        "TOP_BOOK_IMBALANCE", "TOP_BOOK_IMBALANCE_PERSISTENCE",
        "TOP_BOOK_IMBALANCE_CHANGE",
    ), default="TOP_BOOK_IMBALANCE")
    args = parser.parse_args(argv)
    try:
        result = replay(
            artifact_root=Path(args.artifact_root),
            state_dir=Path(args.state_dir),
            maximum_artifacts=args.maximum_artifacts,
            outcome_horizon_seconds=args.outcome_horizon_seconds,
            feature_mode=args.feature_mode,
            market=args.market,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

