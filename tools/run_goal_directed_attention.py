"""Run one zero-model attention cycle over a shared WhiteBIT public snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import signal
import tempfile
from threading import Event
import time

from research.autotrade_research.agents.goal_directed import (
    AttentionObservation,
    SCHEMA,
    allocate_attention,
    decide_attention,
    organism_manifest,
    workstream_from_document,
)
from research.autotrade_research.io.strict_json import strict_json_loads
from research.autotrade_research.learning.whitebit_live_zero import (
    validate_public_snapshot,
)


def _read(path: Path, *, maximum_bytes: int = 1_000_000) -> object:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError(f"{path.name} is missing, redirected or unbounded")
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _atomic(path: Path, value: dict[str, object]) -> None:
    body = (json.dumps(
        value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False,
    ) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".attention-cycle-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("snapshot timestamp lacks timezone")
    return parsed.astimezone(timezone.utc)


def _learning_fleet_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-learning-fleet-diagnostics-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or type(document.get("learners")) is not list):
        raise ValueError("learning fleet checkpoint authority or schema differs")
    learners = document["learners"]
    phases: dict[str, int] = {}
    active = []
    for learner in learners:
        if type(learner) is not dict or type(learner.get("learning_cycle")) is not dict:
            raise ValueError("learning fleet learner is malformed")
        cycle = learner["learning_cycle"]
        if (learner.get("network_orders") is not False
                or learner.get("trading_authority") is not False
                or cycle.get("network_orders") is not False
                or cycle.get("trading_authority") is not False):
            raise ValueError("learning fleet learner authority differs")
        phase = str(cycle.get("phase", "UNKNOWN"))
        phases[phase] = phases.get(phase, 0) + 1
        active.append({
            "id": str(learner.get("id", "unknown")),
            "phase": phase,
            "episodes": int(learner.get("episode_count", 0)),
            "model_versions": int(learner.get("model_version_count", 0)),
            "candidate_version": cycle.get("candidate_version"),
            "champion_promotion": cycle.get("champion_promotion") is True,
        })
    return {
        "status": str(document.get("status", "UNKNOWN")),
        "updated_at": document.get("updated_at"),
        "learner_count": len(learners),
        "phase_counts": dict(sorted(phases.items())),
        "completed_episodes": sum(row["episodes"] for row in active),
        "model_versions": sum(row["model_versions"] for row in active),
        "learners": active,
        "snapshot_sha256": document.get("snapshot_sha256"),
        "network_orders": False,
        "trading_authority": False,
    }


def _forward_shadow_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-whitebit-trend-forward-shadow-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False):
        raise ValueError("forward shadow checkpoint authority or schema differs")
    return {
        "status": str(document.get("status", "UNKNOWN")),
        "updated_at": document.get("updated_at"),
        "market": document.get("market"),
        "completed_live_bars": int(document.get("completed_live_bars", 0)),
        "current_equity": document.get("current_equity"),
        "net_pnl": document.get("net_pnl"),
        "return_fraction": document.get("return_fraction"),
        "max_drawdown_fraction": document.get("max_drawdown_fraction"),
        "quantity": document.get("quantity"),
        "holding_bars": int(document.get("holding_bars", 0)),
        "closed_cycles": int(document.get("closed_cycles", 0)),
        "last_action": document.get("last_action"),
        "qualification_file_sha256": document.get("qualification_file_sha256"),
        "network_orders": False,
        "trading_authority": False,
    }


def _rolling_alpha_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-whitebit-rolling-native-alpha-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or document.get("automatic_promotion") is not False):
        raise ValueError("rolling Alpha checkpoint authority or schema differs")
    result = document.get("last_result")
    markets = []
    passed = 0
    if result is not None:
        if type(result) is not dict or type(result.get("markets")) is not dict:
            raise ValueError("rolling Alpha checkpoint result is malformed")
        for market, record in sorted(result["markets"].items()):
            evaluation = record.get("evaluation") if type(record) is dict else None
            models = evaluation.get("models") if type(evaluation) is dict else None
            if type(models) is not dict:
                raise ValueError("rolling Alpha market evaluation is malformed")
            model_rows = []
            for name, report in sorted(models.items()):
                if type(report) is not dict:
                    raise ValueError("rolling Alpha model report is malformed")
                qualified = report.get("research_gate_passed") is True
                passed += qualified
                model_rows.append({
                    "model": name,
                    "trades": int(report.get("executed_trade_count") or 0),
                    "long": int(report.get("long_trade_count") or 0),
                    "short": int(report.get("short_trade_count") or 0),
                    "net_pnl_after_costs_quote": report.get("net_pnl_after_costs_quote"),
                    "research_gate_passed": qualified,
                    "rejection_reasons": report.get("rejection_reasons"),
                })
            markets.append({
                "market": market,
                "dataset_identity_sha256": evaluation.get("dataset_identity_sha256"),
                "models": model_rows,
            })
    return {
        "status": str(document.get("status", "UNKNOWN")),
        "updated_at": document.get("updated_at"),
        "cycle_count": int(document.get("cycle_count", 0)),
        "cycles_today": int(document.get("cycles_today", 0)),
        "next_run_at": document.get("next_run_at"),
        "passed_model_count": passed,
        "markets": markets,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def run_cycle(*, organism_root: Path, market_snapshot: Path,
              now: datetime | None = None,
              learning_fleet_checkpoint: Path | None = None,
              forward_shadow_checkpoint: Path | None = None,
              rolling_alpha_checkpoint: Path | None = None) -> dict[str, object]:
    manifest = _read(organism_root / "organism.json")
    if (type(manifest) is not dict or manifest.get("schema") != SCHEMA
            or type(manifest.get("workstreams")) is not list
            or manifest.get("network_orders") is not False
            or manifest.get("trading_authority") is not False):
        raise ValueError("organism manifest authority differs")
    admitted = tuple(
        workstream_from_document(row) for row in manifest["workstreams"]
    )
    if organism_manifest(admitted) != manifest:
        raise ValueError("organism manifest digest or canonical content differs")
    market = validate_public_snapshot(_read(market_snapshot))
    current = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    mid = (
        Decimal(market["bid_price"]) + Decimal(market["ask_price"])
    ) / Decimal("2")
    decisions = []
    state_rows = []
    for spec in admitted:
        if market["market"] not in spec.symbols:
            continue
        state_path = organism_root / spec.memory_namespace / "attention-state.json"
        if state_path.exists():
            prior = _read(state_path)
            if type(prior) is not dict:
                raise ValueError("attention state is malformed")
            reference = Decimal(prior["reference_price"])
            scheduled = _instant(prior["next_scheduled_review"])
        else:
            reference = mid
            scheduled = current + timedelta(
                seconds=spec.profile.monitor_interval_seconds
            )
        if spec.profile.profile_id.startswith("TARGET_"):
            take, stop = Decimal("0.005"), Decimal("0.01")
        elif spec.profile.profile_id == "EVENT_DRIVEN_POSITION":
            take, stop = Decimal("0.05"), Decimal("0.02")
        else:
            take, stop = Decimal("0.10"), Decimal("0.05")
        decision = decide_attention(
            spec,
            AttentionObservation(
                available_at=_instant(market["available_at"]),
                reference_price=reference,
                current_price=mid,
                next_scheduled_review=scheduled,
                take_profit_fraction=take,
                stop_loss_fraction=stop,
            ),
            now=current,
        )
        decisions.append(decision)
        state_rows.append((spec, state_path, reference, decision))
    grants = {
        item.decision.session_id: item
        for item in allocate_attention(
            decisions, max_deep_analyses=1, max_paid_model_calls=1
        )
    }
    workstreams = []
    for spec, state_path, reference, decision in state_rows:
        grant = grants[spec.session_id]
        # A completed wake/recovery cycle establishes a new monitoring anchor.
        # Without rebasing, a boundary crossed during downtime wakes the same
        # workstream every poll forever and starves the other workstreams.
        persisted_reference = (
            mid if decision.phase in {"WAKE_ANALYSIS", "RECOVERY"} else reference
        )
        state = {
            "schema": SCHEMA,
            "session_id": spec.session_id,
            "profile_id": spec.profile.profile_id,
            "reference_price": format(persisted_reference, "f"),
            "current_price": format(mid, "f"),
            "market_available_at": market["available_at"],
            "next_scheduled_review": decision.next_check_at.isoformat(),
            "phase": decision.phase,
            "reason": decision.reason,
            "deep_analysis_granted": grant.deep_analysis_granted,
            "paid_model_granted": grant.paid_model_granted,
            "model_invoked": False,
            "network_orders": False,
            "trading_authority": False,
        }
        _atomic(state_path, state)
        workstreams.append(state)
    runtime_root = organism_root.parent
    fleet_path = learning_fleet_checkpoint or (
        runtime_root / "learning-fleet-diagnostics-v1-20261112" / "checkpoint.json"
    )
    shadow_path = forward_shadow_checkpoint or (
        runtime_root / "whitebit-sol-trend-shadow-v1-20261111" / "checkpoint.json"
    )
    rolling_path = rolling_alpha_checkpoint or (
        runtime_root / "rolling-native-alpha-v1-20261112" / "checkpoint.json"
    )
    report = {
        "schema": SCHEMA,
        "status": "ATTENTION_CYCLE_COMPLETE",
        "observed_at": current.isoformat(),
        "market": market["market"],
        "market_evidence_sha256": market["raw_evidence_ref"]["sha256"],
        "shared_snapshot_reuse_count": len(workstreams),
        "workstreams": workstreams,
        "paid_model_calls": 0,
        "organism_evidence": {
            "learning_fleet": _learning_fleet_summary(fleet_path),
            "forward_shadow": _forward_shadow_summary(shadow_path),
            "rolling_native_alpha": _rolling_alpha_summary(rolling_path),
        },
        "network_orders": False,
        "trading_authority": False,
    }
    _atomic(organism_root / "latest-attention-cycle.json", report)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one deterministic attention cycle without model calls or orders"
    )
    parser.add_argument("--organism-root", required=True)
    parser.add_argument("--market-snapshot", required=True)
    parser.add_argument(
        "--poll-seconds", type=int, default=0,
        help="0 runs one cycle; >=5 keeps the zero-model monitor active",
    )
    parser.add_argument("--stop-file")
    args = parser.parse_args(argv)
    if args.poll_seconds != 0 and args.poll_seconds < 5:
        print(json.dumps({
            "status": "BLOCKED",
            "reason": "poll-seconds must be 0 or at least 5",
        }, sort_keys=True))
        return 2
    stop = Event()
    previous = {}
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is not None:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_args: stop.set())
    stop_file = None if args.stop_file is None else Path(args.stop_file)
    report = None
    try:
        while not stop.is_set():
            if stop_file is not None and stop_file.is_file():
                break
            report = run_cycle(
                organism_root=Path(args.organism_root),
                market_snapshot=Path(args.market_snapshot),
            )
            if args.poll_seconds == 0:
                break
            stop.wait(args.poll_seconds)
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(json.dumps({
        "status": "STOPPED_WITH_CHECKPOINT" if stop.is_set() or (
            stop_file is not None and stop_file.is_file()
        ) else report["status"],
        "market": None if report is None else report["market"],
        "shared_snapshot_reuse_count": (
            0 if report is None else report["shared_snapshot_reuse_count"]
        ),
        "paid_model_calls": 0 if report is None else report["paid_model_calls"],
        "network_orders": False,
        "trading_authority": False,
        "continuous": args.poll_seconds > 0,
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

