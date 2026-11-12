#!/usr/bin/env python3
"""Persist a secret-free diagnostic projection of the live learning fleet."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime
from hashlib import sha256
import json
import os
from pathlib import Path
from time import monotonic, sleep

from research.autotrade_research.artifacts.durable_publish import atomic_write_json


SCHEMA = "autotrade-learning-fleet-diagnostics-v1"
MAX_INPUT = 2 * 1024 * 1024
MAX_LOG = 8 * 1024 * 1024


def _read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= MAX_INPUT:
        raise ValueError("learner checkpoint is missing, redirected, or unbounded")
    value = json.loads(path.read_bytes())
    if type(value) is not dict:
        raise ValueError("learner checkpoint must be an object")
    return value


def _cycle_projection(value: dict) -> dict:
    cycle = value.get("learning_cycle")
    if type(cycle) is not dict:
        raise ValueError("learner checkpoint has no learning cycle")
    projected = {key: cycle.get(key) for key in (
        "market", "phase", "cycle", "trials", "episodes", "net", "peak",
        "drawdown", "reason", "incumbent_version", "candidate_version",
        "incumbent_sha256", "candidate_sha256", "pause_reasons", "paused_at",
        "training_cutoff", "train_scores", "analysis", "evaluation")}
    config = cycle.get("config")
    if type(config) is dict:
        projected["cost_and_horizon_config"] = {key: config.get(key) for key in (
            "fee_bps", "slippage_bps", "short_carry_bps_per_day",
            "notional", "outcome_horizon_seconds", "signal_lookback_seconds",
            "max_observation_gap_seconds", "feature_mode")
            if config.get(key) is not None}
    evaluation = projected.get("evaluation")
    if type(evaluation) is dict and type(evaluation.get("pending")) is dict:
        pending = evaluation["pending"]
        projected["pending_actions"] = {
            "candidate": pending.get("candidate_action"),
            "incumbent": pending.get("incumbent_action"),
            "available_at": pending.get("available_at"),
        }
    projected["network_orders"] = False
    projected["trading_authority"] = False
    projected["champion_promotion"] = False
    return {key: item for key, item in projected.items() if item is not None}


def fleet_snapshot(sources: list[tuple[str, Path]]) -> dict:
    learners = []
    for identifier, path in sorted(sources):
        value = _read(path)
        if value.get("network_orders") is not False or value.get("trading_authority") is not False:
            raise ValueError("learner checkpoint authority differs")
        learners.append({"id": identifier, "mode": value.get("mode"),
            "episode_count": value.get("episode_count"),
            "invalid_episode_count": value.get("invalid_episode_count"),
            "model_version_count": value.get("model_version_count"),
            "last_available_at": value.get("last_available_at"),
            "latest_model_sha256": value.get("latest_model_sha256"),
            "learning_cycle": _cycle_projection(value),
            "network_orders": False, "trading_authority": False})
    phase_counts = {}
    for learner in learners:
        phase = learner["learning_cycle"]["phase"]
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
    snapshot = {"schema": SCHEMA, "learners": learners,
        "learner_count": len(learners),
        "total_completed_episodes": sum(int(item["episode_count"] or 0) for item in learners),
        "total_model_versions": sum(int(item["model_version_count"] or 0) for item in learners),
        "phase_counts": dict(sorted(phase_counts.items())),
        "network_orders": False, "trading_authority": False,
        "secrets_in_report": False}
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    snapshot["snapshot_sha256"] = sha256(encoded).hexdigest()
    return snapshot


def _append_bounded(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= MAX_LOG:
        archive = path.with_suffix(path.suffix + ".1")
        archive.unlink(missing_ok=True)
        os.replace(path, archive)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, separators=(",", ":"))+"\n")
        stream.flush(); os.fsync(stream.fileno())


def _source(text: str) -> tuple[str, Path]:
    if "=" not in text:
        raise argparse.ArgumentTypeError("source must be ID=PATH")
    identifier, raw = text.split("=", 1)
    if not identifier or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in identifier):
        raise argparse.ArgumentTypeError("source ID is invalid")
    return identifier, Path(raw)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", type=_source, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= len(args.source) <= 32 or len({item[0] for item in args.source}) != len(args.source):
        raise ValueError("sources must contain 1..32 unique learners")
    if not 1 <= args.poll_seconds <= 3600:
        raise ValueError("poll seconds must be 1..3600")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = args.state_dir / "checkpoint.json"
    log = args.state_dir / "agent-fleet-diagnostics.jsonl"
    prior = None
    if checkpoint.exists():
        existing = _read(checkpoint)
        if existing.get("schema") != SCHEMA:
            raise ValueError("fleet diagnostic checkpoint schema differs")
        prior = existing.get("snapshot_sha256")
    started = monotonic()
    while True:
        snapshot = fleet_snapshot(args.source)
        changed = snapshot["snapshot_sha256"] != prior
        now = datetime.now(UTC).isoformat()
        if changed:
            _append_bounded(log, {"timestamp": now, "event": "LEARNING_FLEET_CHANGED", **snapshot})
            prior = snapshot["snapshot_sha256"]
        state = {**snapshot, "status": "RUNNING", "changed": changed,
            "diagnostic_log": str(log), "updated_at": now}
        atomic_write_json(checkpoint, state)
        if args.once or (args.stop_file and args.stop_file.exists()):
            state["status"] = "STOPPED"
            atomic_write_json(checkpoint, state)
            break
        sleep(args.poll_seconds)
    print(json.dumps({"checkpoint": str(checkpoint), "status": state["status"],
        "learner_count": state["learner_count"], "network_orders": False,
        "trading_authority": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

