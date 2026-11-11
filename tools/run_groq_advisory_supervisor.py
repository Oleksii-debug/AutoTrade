"""Watch a ZERO-learning checkpoint and run bounded Groq research waves.

The supervisor projects only an explicit non-secret metric allowlist from the
learning checkpoint. It cannot construct or dispatch an order, and each source
checkpoint digest is admitted at most once across restarts.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import signal
import sys
from threading import Event
import time
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
for dependency_root in (ROOT, ROOT / "mvp"):
    if str(dependency_root) not in sys.path:
        sys.path.insert(0, str(dependency_root))

from autotrade_mvp.groq_advisory_research import DurableGroqAdvisoryWave


_MAX_CHECKPOINT_BYTES = 256 * 1024
_STATUS_SCHEMA = "autotrade-groq-advisory-supervisor-v1"


def _read_json(path: Path) -> tuple[dict, str]:
    path = Path(path).expanduser().resolve(strict=True)
    if not path.is_file() or path.is_symlink():
        raise ValueError("learning checkpoint must be an ordinary file")
    raw = path.read_bytes()
    if not raw or len(raw) > _MAX_CHECKPOINT_BYTES:
        raise ValueError("learning checkpoint size is invalid")
    value = json.loads(raw)
    if type(value) is not dict:
        raise ValueError("learning checkpoint must be an object")
    return value, sha256(raw).hexdigest()


def _value(mapping: dict, name: str, accepted: tuple[type, ...]):
    value = mapping.get(name)
    if type(value) not in accepted:
        raise ValueError(f"learning checkpoint field {name} is invalid")
    return value


def project_learning_checkpoint(checkpoint: dict) -> dict:
    """Create the only checkpoint projection allowed to cross the model boundary."""

    if type(checkpoint) is not dict:
        raise ValueError("learning checkpoint must be an object")
    if checkpoint.get("schema") != "autotrade-whitebit-live-zero-learning-v1":
        raise ValueError("learning checkpoint schema is unsupported")
    if checkpoint.get("mode") != "ZERO":
        raise ValueError("only ZERO learning checkpoints are accepted")
    if checkpoint.get("network_orders") is not False:
        raise ValueError("learning checkpoint has network-order authority")
    if checkpoint.get("trading_authority") is not False:
        raise ValueError("learning checkpoint has trading authority")
    cycle = _value(checkpoint, "learning_cycle", (dict,))
    analysis = cycle.get("analysis") if type(cycle.get("analysis")) is dict else {}
    evaluation = cycle.get("evaluation") if type(cycle.get("evaluation")) is dict else {}
    config = cycle.get("config") if type(cycle.get("config")) is dict else {}
    return {
        "schema": "autotrade-groq-research-input-v1",
        "evidence_class": "WHITEBIT_PUBLIC_MARKET_ZERO",
        "market": _value(cycle, "market", (str,)),
        "mode": "ZERO",
        "source_checkpoint": {
            name: checkpoint[name]
            for name in ("episode_count", "invalid_episode_count", "model_version_count")
            if type(checkpoint.get(name)) is int
        } | {
            name: cycle[name]
            for name in (
                "cycle", "phase", "reason", "training_cutoff", "candidate_version",
                "incumbent_version", "champion_promotion",
            )
            if type(cycle.get(name)) in (str, int, bool)
        },
        "prior_train": {
            name: analysis[name]
            for name in (
                "episodes", "gross_usdt", "fees_usdt", "slippage_cost_usdt",
                "spread_cost_usdt", "net_usdt", "correct_direction_but_loss",
            )
            if type(analysis.get(name)) in (str, int)
        },
        "prospective_evaluation": {
            name: evaluation[name]
            for name in (
                "count", "candidate", "candidate_trades", "incumbent",
                "incumbent_trades", "drawdown", "profitability_proven",
                "qualifies_for_research_simulation", "invalid_gap_count",
            )
            if type(evaluation.get(name)) in (str, int, bool)
        },
        "constraints": {
            "public_data_only": checkpoint.get("public_data_only") is True,
            "network_orders": False,
            "trading_authority": False,
            **{
                name: config[name]
                for name in (
                    "fee_bps", "slippage_bps", "notional", "min_evaluation_trades",
                    "max_observation_gap_seconds", "outcome_horizon_seconds",
                    "signal_lookback_seconds",
                )
                if type(config.get(name)) in (str, int)
            },
        },
        "accounting_contract": {
            "net_identity": "net_usdt = gross_usdt - fees_usdt",
            "gross_includes": "bid_ask_spread_and_configured_slippage",
            "slippage_cost_is_diagnostic": True,
            "subtract_slippage_cost_again": False,
        },
        "causal_split_contract": {
            "training_cutoff_decision_sequence": cycle.get("training_cutoff"),
            "prospective_evaluation_starts_after_training_cutoff": True,
            "prospective_evaluation_used_for_training": False,
        },
        "request": (
            "Diagnose after-cost performance and propose bounded, falsifiable ZERO-mode "
            "experiments. Reject leakage, unsupported profit claims, and real-order action."
        ),
    }


def _write_status(path: Path, status: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    encoded = json.dumps(
        status, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    with temporary.open("wb") as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def run(
    *, checkpoint_path: Path, state_dir: Path, interval_seconds: float,
    max_waves: int, max_runtime_seconds: int, stop: Event | None = None,
    stop_file: Path | None = None, daily_reset: bool = False,
    wave_factory: Callable[[Path], object] = DurableGroqAdvisoryWave,
) -> dict:
    if isinstance(interval_seconds, bool) or not 5 <= interval_seconds <= 3600:
        raise ValueError("poll interval must be 5..3600 seconds")
    if type(max_waves) is not int or not 1 <= max_waves <= 24:
        raise ValueError("max waves must be 1..24")
    if type(max_runtime_seconds) is not int or not 60 <= max_runtime_seconds <= 604800:
        raise ValueError("max runtime must be 60..604800 seconds")
    if type(daily_reset) is not bool:
        raise TypeError("daily reset must be boolean")
    stop = stop or Event()
    state_dir = Path(state_dir).expanduser().resolve(strict=False)
    state_dir.mkdir(parents=True, exist_ok=True)
    status_path = state_dir / "supervisor-checkpoint.json"
    previous_digest = None
    completed = 0
    completed_total = 0
    quota_day = datetime.now(timezone.utc).date().isoformat()
    if status_path.exists():
        existing = json.loads(status_path.read_text(encoding="utf-8"))
        if type(existing) is not dict or existing.get("schema") != _STATUS_SCHEMA:
            raise ValueError("advisory supervisor checkpoint is invalid")
        previous_digest = existing.get("source_digest")
        completed = existing.get("completed_waves", 0)
        completed_total = existing.get("completed_waves_total", completed)
        existing_day = existing.get("quota_day")
        if type(completed) is not int or completed < 0:
            raise ValueError("advisory supervisor checkpoint count is invalid")
        if type(completed_total) is not int or completed_total < completed:
            raise ValueError("advisory supervisor total count is invalid")
        observed_at = existing.get("observed_at")
        same_legacy_day = (
            existing_day is None and type(observed_at) is str
            and observed_at.startswith(quota_day)
        )
        if daily_reset and existing_day != quota_day and not same_legacy_day:
            completed = 0
    deadline = time.monotonic() + max_runtime_seconds
    runner = wave_factory(state_dir / "waves")
    last_status = "WAITING"
    while (not stop.is_set() and time.monotonic() < deadline
           and (daily_reset or completed < max_waves)):
        if stop_file is not None and stop_file.is_file():
            stop.set()
            break
        current_day = datetime.now(timezone.utc).date().isoformat()
        if daily_reset and current_day != quota_day:
            quota_day = current_day
            completed = 0
        checkpoint, digest = _read_json(checkpoint_path)
        if completed < max_waves and digest != previous_digest:
            projection = project_learning_checkpoint(checkpoint)
            result = runner.run(
                wave_id=f"zero-{digest[:24]}", research_input=projection
            )
            last_status = result.status
            previous_digest = digest
            if result.status == "COMPLETED":
                completed += 1
                completed_total += 1
            _write_status(status_path, {
                "schema": _STATUS_SCHEMA,
                "status": last_status,
                "completed_waves": completed,
                "completed_waves_total": completed_total,
                "quota_day": quota_day,
                "source_digest": digest,
                "wave_id": result.wave_id,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "network_orders": False,
                "trading_authority": False,
            })
        if (daily_reset or completed < max_waves) and not stop.is_set():
            wait_deadline = time.monotonic() + interval_seconds
            while not stop.is_set() and time.monotonic() < wait_deadline:
                if stop_file is not None and stop_file.is_file():
                    stop.set()
                    break
                stop.wait(min(1.0, max(0.0, wait_deadline - time.monotonic())))
    final = {
        "schema": _STATUS_SCHEMA,
        "status": "STOPPED_WITH_CHECKPOINT" if stop.is_set() else "COMPLETED_BOUND",
        "last_wave_status": last_status,
        "completed_waves": completed,
        "completed_waves_total": completed_total,
        "quota_day": quota_day,
        "max_waves": max_waves,
        "checkpoint": str(status_path),
        "network_orders": False,
        "trading_authority": False,
    }
    return final


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded ZERO-learning Groq advisory checkpoint supervisor"
    )
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--max-waves", type=int, default=4)
    parser.add_argument("--max-runtime-seconds", type=int, default=86400)
    parser.add_argument("--stop-file")
    parser.add_argument("--daily-reset", action="store_true")
    args = parser.parse_args(argv)
    stop = Event()
    for name in ("SIGINT", "SIGTERM"):
        code = getattr(signal, name, None)
        if code is not None:
            signal.signal(code, lambda *_args: stop.set())
    try:
        result = run(
            checkpoint_path=Path(args.checkpoint), state_dir=Path(args.state_dir),
            interval_seconds=args.interval_seconds, max_waves=args.max_waves,
            max_runtime_seconds=args.max_runtime_seconds, stop=stop,
            stop_file=Path(args.stop_file) if args.stop_file else None,
            daily_reset=args.daily_reset,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"Groq advisory supervisor blocked: {type(error).__name__}: {error}")
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
