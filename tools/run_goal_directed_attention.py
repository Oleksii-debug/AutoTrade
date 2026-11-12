"""Run one zero-model attention cycle over a shared WhiteBIT public snapshot."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
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
            "feature_mode": (
                cycle.get("cost_and_horizon_config") or cycle.get("config") or {}
            ).get("feature_mode"),
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


def _opportunity_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-whitebit-opportunity-supervisor-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or document.get("automatic_promotion") is not False):
        raise ValueError("opportunity checkpoint authority or schema differs")
    result = document.get("last_result")
    markets = []
    if result is not None:
        if type(result) is not dict or type(result.get("markets")) is not dict:
            raise ValueError("opportunity checkpoint result is malformed")
        if (result.get("network_orders") is not False
                or result.get("trading_authority") is not False):
            raise ValueError("opportunity result authority differs")
        for market, record in sorted(result["markets"].items()):
            if type(record) is not dict:
                raise ValueError("opportunity market result is malformed")
            screen = record.get("screen")
            if screen is not None and (type(screen) is not dict
                                       or screen.get("network_orders") is not False
                                       or screen.get("trading_authority") is not False):
                raise ValueError("opportunity screen authority differs")
            markets.append({
                "market": market,
                "status": record.get("status"),
                "rank": record.get("rank"),
                "quote_volume_24h": record.get("quote_volume_24h"),
                "prospective_qualification": (
                    False if screen is None
                    else screen.get("prospective_qualification") is True
                ),
                "train_survivor_count": (
                    0 if screen is None else int(screen.get("train_survivor_count", 0))
                ),
                "development_survivor_count": (
                    0 if screen is None
                    else int(screen.get("development_survivor_count", 0))
                ),
                "final_consumed": False if screen is None else screen.get("final_consumed") is True,
                "rejection_reasons": (
                    None if screen is None else screen.get("final_rejection_reasons")
                ),
            })
    return {
        "status": str(document.get("status", "UNKNOWN")),
        "updated_at": document.get("updated_at"),
        "cycle_count": int(document.get("cycle_count", 0)),
        "next_run_at": document.get("next_run_at"),
        "selected_market_count": (
            0 if result is None else int(result.get("selected_market_count", 0))
        ),
        "qualified_market_count": (
            0 if result is None else int(result.get("qualified_market_count", 0))
        ),
        "rejected_data_count": sum(row["status"] == "REJECTED_DATA" for row in markets),
        "markets": markets,
        "paid_model_calls": 0 if result is None else int(result.get("paid_model_calls", 0)),
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _trade_flow_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-whitebit-trade-flow-supervisor-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or int(document.get("paid_model_calls", -1)) != 0
            or type(document.get("results")) is not dict):
        raise ValueError("trade-flow checkpoint authority or schema differs")
    markets = []
    qualified = 0
    final_consumed = 0
    for market, result in sorted(document["results"].items()):
        if type(result) is not dict:
            raise ValueError("trade-flow market result is malformed")
        is_qualified = result.get("prospective_qualification") is True
        consumed = result.get("final_consumed") is True
        qualified += is_qualified
        final_consumed += consumed
        markets.append({
            "market": market,
            "status": str(result.get("status", "UNKNOWN")),
            "available_causal_buckets": int(
                result.get("available_causal_buckets", 0)
            ),
            "prospective_qualification": is_qualified,
            "final_consumed": consumed,
            "result_sha256": result.get("result_sha256"),
        })
    return {
        "status": str(document.get("phase", "UNKNOWN")),
        "checkpointed_at": document.get("checkpointed_at"),
        "minimum_buckets": int(document.get("minimum_buckets", 0)),
        "market_count": len(markets),
        "qualified_market_count": qualified,
        "final_consumed_market_count": final_consumed,
        "markets": markets,
        "paid_model_calls": 0,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _maker_shadow_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != "autotrade-whitebit-maker-shadow-v1"
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or int(document.get("paid_model_calls", -1)) != 0):
        raise ValueError("maker-shadow checkpoint authority or schema differs")
    opened = document.get("open_quote")
    if opened is not None and (type(opened) is not dict
                               or opened.get("network_orders") is not False
                               or opened.get("trading_authority") is not False):
        raise ValueError("maker-shadow open quote authority differs")
    settled = document.get("last_settled")
    if settled is not None and (type(settled) is not dict
                                or settled.get("network_orders") is not False
                                or settled.get("trading_authority") is not False):
        raise ValueError("maker-shadow result authority differs")
    return {
        "status": str(document.get("phase", "UNKNOWN")),
        "checkpointed_at": document.get("checkpointed_at"),
        "market": document.get("market"),
        "research_phase": document.get("research_phase"),
        "settled_cycles": int(document.get("settled_cycles", 0)),
        "positive_cycles": int(document.get("positive_cycles", 0)),
        "wave_cycles": int(document.get("wave_cycles", 0)),
        "wave_positive_cycles": int(document.get("wave_positive_cycles", 0)),
        "wave_net_pnl_quote": document.get("wave_net_pnl_quote"),
        "latest_wave_analysis": document.get("latest_wave_analysis"),
        "last_quote_admission": document.get("last_quote_admission"),
        "net_pnl_quote": document.get("net_pnl_quote"),
        "open_quote": None if opened is None else {
            "status": opened.get("status"),
            "age_seconds": opened.get("age_seconds"),
            "bid_filled": opened.get("bid_filled") is True,
            "ask_filled": opened.get("ask_filled") is True,
            "quote_sha256": opened.get("quote_sha256"),
        },
        "last_settled": None if settled is None else {
            "outcome": settled.get("outcome"),
            "net_pnl_quote": settled.get("net_pnl_quote"),
            "result_sha256": settled.get("result_sha256"),
        },
        "profit_guaranteed": False,
        "paid_model_calls": 0,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def _triangular_shadow_summary(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"status": "NOT_AVAILABLE"}
    document = _read(path, maximum_bytes=4_000_000)
    if (type(document) is not dict
            or document.get("schema") != (
                "autotrade-whitebit-triangular-shadow-supervisor-v1"
            )
            or document.get("network_orders") is not False
            or document.get("trading_authority") is not False
            or document.get("profit_guaranteed") is not False
            or type(document.get("paid_model_calls")) is not int
            or document.get("paid_model_calls") != 0):
        raise ValueError("triangular-shadow checkpoint authority or schema differs")
    count_fields = ("latest_market_count", "cycle_result_count", "opportunity_count")
    if any(type(document.get(field)) is not int or document[field] < 0
           for field in count_fields):
        raise ValueError("triangular-shadow counters are malformed")
    try:
        notional = Decimal(str(document.get("notional")))
        reserve = Decimal(str(document.get("impact_reserve_bps")))
    except InvalidOperation as exc:
        raise ValueError("triangular-shadow economics are malformed") from exc
    if (not notional.is_finite() or notional <= 0
            or not reserve.is_finite() or reserve < 0):
        raise ValueError("triangular-shadow economics are malformed")
    best = document.get("best_result")
    best_summary = None
    if best is not None:
        if (type(best) is not dict
                or type(best.get("path")) is not list
                or len(best["path"]) != 4
                or not all(type(asset) is str and asset for asset in best["path"])
                or best["path"][0] != best["path"][-1]
                or type(best.get("qualified_for_shadow_observation")) is not bool
                or type(best.get("result_sha256")) is not str):
            raise ValueError("triangular-shadow best result is malformed")
        try:
            after_fees = Decimal(str(best.get("net_after_taker_fees_bps")))
            after_reserve = Decimal(str(best.get("net_after_reserve_bps")))
        except InvalidOperation as exc:
            raise ValueError("triangular-shadow economics are malformed") from exc
        if (not after_fees.is_finite() or not after_reserve.is_finite()
                or after_reserve > after_fees
                or (best["qualified_for_shadow_observation"] is True
                    and document["opportunity_count"] == 0)):
            raise ValueError("triangular-shadow economics are non-finite")
        best_summary = {
            "path": list(best["path"]),
            "net_after_taker_fees_bps": format(after_fees, "f"),
            "net_after_reserve_bps": format(after_reserve, "f"),
            "qualified_for_shadow_observation": (
                best["qualified_for_shadow_observation"] is True
            ),
            "result_sha256": best.get("result_sha256"),
        }
    return {
        "status": str(document.get("phase", "UNKNOWN")),
        "checkpointed_at": document.get("checkpointed_at"),
        "latest_market_count": document["latest_market_count"],
        "cycle_result_count": document["cycle_result_count"],
        "opportunity_count": document["opportunity_count"],
        "notional": format(notional, "f"),
        "impact_reserve_bps": format(reserve, "f"),
        "best_result": best_summary,
        "profit_guaranteed": False,
        "paid_model_calls": 0,
        "network_orders": False,
        "trading_authority": False,
        "automatic_promotion": False,
    }


def run_cycle(*, organism_root: Path, market_snapshot: Path,
              now: datetime | None = None,
              learning_fleet_checkpoint: Path | None = None,
              forward_shadow_checkpoint: Path | None = None,
              rolling_alpha_checkpoint: Path | None = None,
              opportunity_checkpoint: Path | None = None,
              trade_flow_checkpoint: Path | None = None,
              maker_shadow_checkpoint: Path | None = None,
              ena_maker_shadow_checkpoint: Path | None = None,
              triangular_shadow_checkpoint: Path | None = None) -> dict[str, object]:
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
    opportunity_path = opportunity_checkpoint or (
        runtime_root / "whitebit-opportunity-screen-v1-20261112" / "checkpoint.json"
    )
    trade_flow_path = trade_flow_checkpoint or (
        runtime_root / "whitebit-trade-flow-screen-v1-20261112" / "checkpoint.json"
    )
    maker_shadow_path = maker_shadow_checkpoint or (
        runtime_root / "whitebit-pepe-maker-shadow-v1-20261112" / "checkpoint.json"
    )
    ena_maker_shadow_path = ena_maker_shadow_checkpoint or (
        runtime_root / "whitebit-ena-maker-shadow-v1-20261112" / "checkpoint.json"
    )
    triangular_shadow_path = triangular_shadow_checkpoint or (
        runtime_root / "whitebit-stable-fiat-triangle-shadow-v1-20261112"
        / "checkpoint.json"
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
            "opportunity_screen": _opportunity_summary(opportunity_path),
            "trade_flow_screen": _trade_flow_summary(trade_flow_path),
            "maker_shadow": _maker_shadow_summary(maker_shadow_path),
            "ena_maker_shadow": _maker_shadow_summary(ena_maker_shadow_path),
            "stable_fiat_triangular_shadow": _triangular_shadow_summary(
                triangular_shadow_path
            ),
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





