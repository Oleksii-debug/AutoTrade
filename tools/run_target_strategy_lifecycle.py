"""Run a durable target-conditioned ZERO research lifecycle.

Candidates are deterministic signal producers.  The existing historical ZERO
adapter and canonical simulator remain the only execution/accounting authority.
The lifecycle checkpoints every phase and never routes a candidate as champion.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import tempfile

from autotrade_runtime.artifacts import ArtifactStore

from mvp.autotrade_mvp.exact_decimal import parse_bounded_exact_decimal
from research.autotrade_research.agents.goal_directed import (
    organism_manifest,
    workstream_from_document,
)
from research.autotrade_research.io.strict_json import strict_json_loads
from research.autotrade_research.strategies.target_matrix import (
    LifecyclePolicy,
    build_strategy_plan,
    default_candidates,
    evaluate_candidate_matrix,
    new_lifecycle,
    pause_reasons,
    read_checkpoint,
    select_training_candidate,
    transition_lifecycle,
    write_checkpoint,
)
from tools.historical_causal_replay import closed_candle_simulation_input
from tools.historical_public_klines import INTERVAL_SECONDS, _reject_symlink_ancestors
from tools.run_historical_zero import main as run_historical_zero


def _canonical(value: object) -> bytes:
    return (json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ) + "\n").encode("utf-8")


def _hash(value: object) -> str:
    return "sha256:" + sha256(_canonical(value)).hexdigest()


def _read(path: Path, *, maximum_bytes: int = 20_000_000) -> object:
    _reject_symlink_ancestors(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum_bytes:
        raise ValueError(f"{path.name} is missing, redirected or unbounded")
    return strict_json_loads(path.read_text(encoding="utf-8"))


def _atomic(path: Path, value: object) -> None:
    _reject_symlink_ancestors(path.parent)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".target-lifecycle-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _run_zero(argv: list[str]) -> dict[str, object]:
    output = StringIO()
    with redirect_stdout(output):
        code = run_historical_zero(argv)
    if code != 0:
        raise ValueError("canonical historical ZERO run blocked: " + output.getvalue().strip())
    try:
        value = strict_json_loads(output.getvalue())
    except (UnicodeError, ValueError) as exc:
        raise ValueError("canonical historical ZERO run emitted malformed evidence") from exc
    if type(value) is not dict or type(value.get("simulation")) is not dict:
        raise ValueError("canonical historical ZERO report schema differs")
    return value


def _manifest(root: Path):
    value = _read(root / "organism.json")
    if type(value) is not dict or type(value.get("workstreams")) is not list:
        raise ValueError("organism manifest schema differs")
    streams = tuple(workstream_from_document(row) for row in value["workstreams"])
    if organism_manifest(streams) != value:
        raise ValueError("organism manifest content or digest differs")
    return value, streams


def _dataset(
    *, data_root: Path, artifact_store: Path, symbol: str, interval: str,
    from_month: str, through_month: str,
) -> dict[str, object]:
    dates, opens, closes, digest = closed_candle_simulation_input(
        data_root,
        symbol=symbol,
        interval=interval,
        from_month=from_month,
        through_month=through_month,
        artifact_store=ArtifactStore(artifact_store),
    )
    if len(closes) < 2:
        raise ValueError("target-conditioned lifecycle needs two completed bars")
    return {
        "dates": dates,
        "opens": opens,
        "closes": closes,
        "digest": digest,
        "signal_prices": closes[:-1],
        "available_at": dates[1:],
        "execution_count": len(closes) - 1,
    }


def _target_quantity(virtual_cash: str, first_close: str) -> str:
    cash = parse_bounded_exact_decimal(virtual_cash)
    price = parse_bounded_exact_decimal(first_close)
    value = (cash * Decimal("0.25") / price).quantize(
        Decimal("0.00000001"), rounding=ROUND_DOWN
    )
    if value <= 0:
        raise ValueError("bounded target quantity is zero")
    return format(value, "f")


def _run_candidate(
    *, data_root: Path, artifact_store: Path, workspace: Path, symbol: str,
    interval: str, from_month: str, through_month: str, candidate_id: str,
    plan_path: Path, episode_limit: int, split: str,
    expected_plan: dict[str, object], expected_dataset_sha256: str,
) -> dict[str, object]:
    def validated(value: object) -> dict[str, object]:
        if type(value) is not dict:
            raise ValueError("cached candidate report is malformed")
        simulation = value.get("simulation")
        candidate = expected_plan["candidate"]
        if (
            value.get("dataset_identity_sha256") != expected_dataset_sha256
            or value.get("registered_strategy_plan_sha256")
            != expected_plan["plan_sha256"]
            or value.get("registered_strategy_candidate") != candidate
            or type(simulation) is not dict
            or type(simulation.get("completed_episodes")) is not int
            or not 1 <= simulation["completed_episodes"] <= episode_limit
            or simulation.get("reconciled") is not True
        ):
            raise ValueError(
                "cached candidate report does not bind the plan, dataset and episode cut"
            )
        return value

    report_path = workspace / "reports" / split / f"{candidate_id}.json"
    if report_path.is_file():
        return validated(_read(report_path))
    value = _run_zero([
        "--data-root", str(data_root),
        "--artifact-store", str(artifact_store),
        "--symbol", symbol,
        "--interval", interval,
        "--from-month", from_month,
        "--through-month", through_month,
        "--next-bar-open",
        "--strategy-plan", str(plan_path),
        "--virtual-cash", "1000",
        "--stop-after-episodes", str(episode_limit),
        "--state-dir", str(workspace / "simulations" / split / candidate_id),
        "--run-id", f"target-{split}-{symbol}-{candidate_id}",
    ])
    value = validated(value)
    _atomic(report_path, value)
    return value


def _plan_for(
    *, workspace: Path, split: str, candidate, dataset: dict[str, object],
    symbol: str, interval: str,
) -> tuple[dict[str, object], Path]:
    plan = build_strategy_plan(
        candidate,
        symbol=symbol,
        interval=interval,
        interval_seconds=INTERVAL_SECONDS[interval],
        dataset_identity_sha256="sha256:" + dataset["digest"],
        signal_prices=dataset["signal_prices"],
        available_at=dataset["available_at"],
    )
    path = workspace / "plans" / split / f"{candidate.candidate_id}.json"
    if path.is_file():
        existing = _read(path)
        if existing != plan:
            raise ValueError("existing strategy plan conflicts with the frozen causal cut")
    else:
        _atomic(path, plan)
    return plan, path


def run_lifecycle(
    *, data_root: Path, artifact_store: Path, organism_root: Path,
    workspace: Path, symbol: str, interval: str,
    train_from: str, train_through: str, evaluate_from: str,
    evaluate_through: str, max_episodes: int, max_market_seconds: int,
    max_drawdown_fraction: str,
) -> dict[str, object]:
    manifest, streams = _manifest(organism_root)
    training = _dataset(
        data_root=data_root, artifact_store=artifact_store, symbol=symbol,
        interval=interval, from_month=train_from, through_month=train_through,
    )
    evaluation = _dataset(
        data_root=data_root, artifact_store=artifact_store, symbol=symbol,
        interval=interval, from_month=evaluate_from, through_month=evaluate_through,
    )
    if training["dates"][-1] >= evaluation["dates"][0]:
        raise ValueError("training must strictly precede independent evaluation")
    policy = LifecyclePolicy(
        max_episodes=max_episodes,
        max_market_seconds=max_market_seconds,
        max_drawdown_fraction=Decimal(max_drawdown_fraction),
    )
    interval_seconds = INTERVAL_SECONDS[interval]
    episode_limit = min(
        max_episodes,
        max(1, max_market_seconds // interval_seconds),
        training["execution_count"],
        evaluation["execution_count"],
    )
    quantity = _target_quantity("1000", training["closes"][0])
    candidates = default_candidates(proposal_quantity=quantity)
    input_document = {
        "manifest_sha256": manifest["manifest_sha256"],
        "training_dataset_sha256": "sha256:" + training["digest"],
        "evaluation_dataset_sha256": "sha256:" + evaluation["digest"],
        "symbol": symbol,
        "interval": interval,
        "periods": {
            "training": [train_from, train_through],
            "evaluation": [evaluate_from, evaluate_through],
        },
        "policy": policy.document(),
        "episode_limit": episode_limit,
        "candidates": [item.document() for item in candidates],
        "environment": "SIMULATION",
        "network_orders": False,
    }
    input_digest = _hash(input_document)
    checkpoint_path = workspace / "lifecycle-checkpoint.json"
    now = datetime.now(timezone.utc)
    if checkpoint_path.is_file():
        checkpoint = read_checkpoint(checkpoint_path, input_sha256=input_digest)
    else:
        checkpoint = new_lifecycle(
            input_sha256=input_digest,
            incumbent_candidate_id="no-trade-control",
            policy=policy,
            observed_at=now,
        )
        write_checkpoint(checkpoint_path, checkpoint)

    training_reports: dict[str, dict[str, object]] = {}
    if checkpoint["phase"] == "ACTIVE_SIMULATION":
        incumbent = candidates[0]
        plan, plan_path = _plan_for(
            workspace=workspace, split="training", candidate=incumbent,
            dataset=training, symbol=symbol, interval=interval,
        )
        report = _run_candidate(
            data_root=data_root, artifact_store=artifact_store, workspace=workspace,
            symbol=symbol, interval=interval, from_month=train_from,
            through_month=train_through, candidate_id=incumbent.candidate_id,
            plan_path=plan_path, episode_limit=episode_limit, split="training",
            expected_plan=plan, expected_dataset_sha256=training["digest"],
        )
        comparative = report["comparative_report"]
        simulation = report["simulation"]
        reasons = pause_reasons(
            completed_episodes=simulation["completed_episodes"],
            market_seconds=simulation["completed_episodes"] * interval_seconds,
            drawdown_fraction=comparative["agent_max_mark_drawdown_fraction"],
            policy=policy,
            failed=simulation["status"] == "UNKNOWN",
        ) or ("DATASET_WINDOW_EXHAUSTED",)
        checkpoint = transition_lifecycle(
            checkpoint,
            phase="AUTO_PAUSE_ON_WINDOW_OR_FAILURE",
            evidence_sha256=_hash(report),
            observed_at=datetime.now(timezone.utc),
            pause_reasons=reasons,
        )
        write_checkpoint(checkpoint_path, checkpoint)

    if checkpoint["phase"] == "AUTO_PAUSE_ON_WINDOW_OR_FAILURE":
        incumbent_report = _read(
            workspace / "reports" / "training" / "no-trade-control.json"
        )
        comparative = incumbent_report["comparative_report"]
        root_causes = ["INCUMBENT_CONTROL_WINDOW_COMPLETED"]
        if Decimal(comparative["agent_mark_pnl_usd"]) <= 0:
            root_causes.append("NO_POSITIVE_AFTER_COST_EDGE")
        if incumbent_report["simulation"]["status"] == "UNKNOWN":
            root_causes.append("SIMULATION_FAILURE_REQUIRES_RECOVERY")
        analysis = {
            "schema": "autotrade-target-root-cause-analysis-v1",
            "incumbent_candidate_id": "no-trade-control",
            "pause_reasons": checkpoint["pause_reasons"],
            "root_causes": root_causes,
            "incumbent_report_sha256": _hash(incumbent_report),
            "network_orders": False,
        }
        _atomic(workspace / "root-cause-analysis.json", analysis)
        checkpoint = transition_lifecycle(
            checkpoint,
            phase="ROOT_CAUSE_ANALYSIS",
            evidence_sha256=_hash(analysis),
            observed_at=datetime.now(timezone.utc),
        )
        write_checkpoint(checkpoint_path, checkpoint)

    if checkpoint["phase"] == "ROOT_CAUSE_ANALYSIS":
        for candidate in candidates:
            plan, plan_path = _plan_for(
                workspace=workspace, split="training", candidate=candidate,
                dataset=training, symbol=symbol, interval=interval,
            )
            training_reports[candidate.candidate_id] = _run_candidate(
                data_root=data_root, artifact_store=artifact_store, workspace=workspace,
                symbol=symbol, interval=interval, from_month=train_from,
                through_month=train_through, candidate_id=candidate.candidate_id,
                plan_path=plan_path, episode_limit=episode_limit, split="training",
                expected_plan=plan, expected_dataset_sha256=training["digest"],
            )
        selection = select_training_candidate(
            candidates=candidates,
            training_reports=training_reports,
            training_dataset_sha256="sha256:" + training["digest"],
            incumbent_candidate_id="no-trade-control",
            max_drawdown_fraction=max_drawdown_fraction,
        )
        frozen = {
            "schema": "autotrade-target-candidate-training-freeze-v1",
            "training_dataset_sha256": "sha256:" + training["digest"],
            "candidate_report_sha256": {
                key: _hash(value) for key, value in sorted(training_reports.items())
            },
            "selection": selection,
            "selected_candidate_id": selection["selected_candidate_id"],
            "selection_from_evaluation_forbidden": True,
            "automatic_champion_promotion": False,
        }
        _atomic(workspace / "training-freeze.json", frozen)
        checkpoint = transition_lifecycle(
            checkpoint,
            phase="TRAIN_CANDIDATES",
            evidence_sha256=_hash(frozen),
            observed_at=datetime.now(timezone.utc),
        )
        write_checkpoint(checkpoint_path, checkpoint)

    if checkpoint["phase"] == "TRAIN_CANDIDATES":
        evaluation_reports = {}
        evaluation_hashes = {}
        for candidate in candidates:
            plan, plan_path = _plan_for(
                workspace=workspace, split="evaluation", candidate=candidate,
                dataset=evaluation, symbol=symbol, interval=interval,
            )
            report = _run_candidate(
                data_root=data_root, artifact_store=artifact_store, workspace=workspace,
                symbol=symbol, interval=interval, from_month=evaluate_from,
                through_month=evaluate_through, candidate_id=candidate.candidate_id,
                plan_path=plan_path, episode_limit=episode_limit, split="evaluation",
                expected_plan=plan, expected_dataset_sha256=evaluation["digest"],
            )
            evaluation_reports[candidate.candidate_id] = report
            evaluation_hashes[candidate.candidate_id] = _hash(report)
        first_report = evaluation_reports[candidates[0].candidate_id]
        training_freeze = _read(workspace / "training-freeze.json")
        measured_start = datetime.fromisoformat(
            first_report["historical_start_utc"].replace("Z", "+00:00")
        )
        measured_end = datetime.fromisoformat(
            first_report["comparative_report"]["asof_execution_open_utc"].replace(
                "Z", "+00:00"
            )
        )
        matrix = evaluate_candidate_matrix(
            workstreams=streams,
            manifest_sha256=manifest["manifest_sha256"],
            training_dataset_sha256="sha256:" + training["digest"],
            evaluation_dataset_sha256="sha256:" + evaluation["digest"],
            candidates=candidates,
            evaluation_reports=evaluation_reports,
            evaluation_report_sha256=evaluation_hashes,
            evaluation_started_at=measured_start,
            evaluation_ended_at=measured_end,
            evaluation_window_seconds=max_market_seconds,
            minimum_evaluation_episodes=episode_limit,
            incumbent_candidate_id="no-trade-control",
            selected_candidate_id=training_freeze["selected_candidate_id"],
        )
        _atomic(workspace / "candidate-matrix.json", matrix)
        checkpoint = transition_lifecycle(
            checkpoint,
            phase="INDEPENDENT_EVALUATION",
            evidence_sha256=matrix["matrix_sha256"],
            observed_at=datetime.now(timezone.utc),
        )
        write_checkpoint(checkpoint_path, checkpoint)

    if checkpoint["phase"] == "INDEPENDENT_EVALUATION":
        matrix = _read(workspace / "candidate-matrix.json")
        qualified = tuple(matrix["qualified_candidate_ids"])
        checkpoint = transition_lifecycle(
            checkpoint,
            phase=("RESUME_ONLY_IF_QUALIFIED" if qualified
                   else "HOLD_RESEARCH_ONLY"),
            evidence_sha256=matrix["matrix_sha256"],
            observed_at=datetime.now(timezone.utc),
            qualified_candidate_ids=qualified,
        )
        write_checkpoint(checkpoint_path, checkpoint)

    matrix = _read(workspace / "candidate-matrix.json")
    return {
        "status": checkpoint["phase"],
        "checkpoint_sha256": checkpoint["checkpoint_sha256"],
        "matrix_sha256": matrix["matrix_sha256"],
        "qualified_candidate_ids": matrix["qualified_candidate_ids"],
        "resume_candidate_id": checkpoint["resume_candidate_id"],
        "automatic_champion_promotion": False,
        "network_orders": False,
        "trading_authority": False,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run durable target-conditioned canonical ZERO research lifecycle"
    )
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--artifact-store")
    parser.add_argument("--organism-root", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--symbol", choices=("BTCUSDT", "ETHUSDT"), required=True)
    parser.add_argument("--interval", choices=tuple(INTERVAL_SECONDS), default="1h")
    parser.add_argument("--train-from", required=True)
    parser.add_argument("--train-through", required=True)
    parser.add_argument("--evaluate-from", required=True)
    parser.add_argument("--evaluate-through", required=True)
    parser.add_argument("--max-episodes", type=int, default=24)
    parser.add_argument("--max-market-seconds", type=int, default=86400)
    parser.add_argument("--max-drawdown-fraction", default="0.05")
    args = parser.parse_args(argv)
    try:
        data_root = Path(args.data_root)
        result = run_lifecycle(
            data_root=data_root,
            artifact_store=(Path(args.artifact_store) if args.artifact_store
                            else data_root / ".artifact-store"),
            organism_root=Path(args.organism_root),
            workspace=Path(args.workspace),
            symbol=args.symbol,
            interval=args.interval,
            train_from=args.train_from,
            train_through=args.train_through,
            evaluate_from=args.evaluate_from,
            evaluate_through=args.evaluate_through,
            max_episodes=args.max_episodes,
            max_market_seconds=args.max_market_seconds,
            max_drawdown_fraction=args.max_drawdown_fraction,
        )
    except (ArithmeticError, KeyError, OSError, TypeError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

