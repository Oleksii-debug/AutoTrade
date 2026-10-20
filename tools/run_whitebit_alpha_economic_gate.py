"""Durable causal after-cost screen for a verified WhiteBIT Alpha job.

The evaluator replays an already frozen native Alpha candidate over its untouched
holdout.  It authenticates the original public WhiteBIT archive, never refits or
selects on holdout, and deliberately delays execution until an archive open whose
timestamp is strictly later than the decision timestamp.  Results are research
evidence only and cannot grant order or promotion authority.
"""
from __future__ import annotations

import argparse
import csv
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "research"))
sys.path.insert(0, str(ROOT / "mvp"))

import numpy as np

from mvp.autotrade_mvp.alpha_research_worker import _source
from research.autotrade_research.integrations.alpha_economics import AlphaInputError
from research.autotrade_research.integrations.alpha_models import (
    make_split,
    predict_model_artifact,
)
from tools.historical_public_klines import HistoricalArchiveError, _reject_symlink_ancestors
from tools.run_whitebit_alpha_research import _durable_worker
from tools.run_whitebit_historical_alpha import (
    MAX_CSV_BYTES,
    MAX_MANIFEST_BYTES,
    _authenticate_manifest,
    _csv_path,
    _manifest,
    _read_regular,
    build_alpha_input,
)


SCHEMA = "autotrade.whitebit-alpha-economic-gate.v1"
TRACE_SCHEMA = "autotrade.whitebit-alpha-economic-trace.v1"
RECEIPT_SCHEMA = "autotrade.whitebit-alpha-economic-receipt.v1"
_COST_ARGUMENTS = (
    "entry_fee_ratio", "exit_fee_ratio", "spread_bps", "entry_slippage_bps",
    "exit_slippage_bps", "funding_borrow_bps", "model_cost_quote",
    "notional_quote", "min_notional_quote",
)


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise AlphaInputError("economic evaluation requires canonical finite JSON") from exc


def _decimal(value: object, label: str, *, low: str = "0") -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise AlphaInputError(f"{label} requires exact decimal text")
    try:
        result = Decimal(value)
    except Exception as exc:
        raise AlphaInputError(f"invalid {label}") from exc
    if not result.is_finite() or result < Decimal(low):
        raise AlphaInputError(f"invalid {label}")
    return result


def _text(value: Decimal) -> str:
    with localcontext() as context:
        context.prec = 50
        rendered = format(+value, "f")
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _reference(value: str) -> tuple[str, str]:
    if type(value) is not str or not value.startswith("artifact:"):
        raise AlphaInputError("immutable artifact reference required")
    try:
        identifier, digest = value[9:].split("@sha256:", 1)
    except ValueError as exc:
        raise AlphaInputError("invalid artifact reference") from exc
    if not identifier or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise AlphaInputError("invalid artifact reference")
    return identifier, digest


def _archive(manifest_path: Path, payload: dict) -> tuple[dict, list[dict[str, object]]]:
    """Authenticate the exact Alpha selection and return its OHLC rows."""
    bars = len(payload["series"]["closes"])
    costs = payload["costs"]
    rebuilt, selection = build_alpha_input(
        manifest_path, bars=bars, **{name: costs[name] for name in _COST_ARGUMENTS})
    if rebuilt != payload:
        raise HistoricalArchiveError("WhiteBIT archive selection differs from Alpha request")
    manifest_bytes = _read_regular(manifest_path, limit=MAX_MANIFEST_BYTES, label="manifest")
    csv_bytes = _read_regular(_csv_path(manifest_path), limit=MAX_CSV_BYTES,
                              label="normalized dataset")
    manifest = _manifest(manifest_bytes)
    _authenticate_manifest(manifest, csv_bytes)
    try:
        reader = csv.DictReader(csv_bytes.decode("utf-8").splitlines())
        all_rows = list(reader)
    except (UnicodeError, csv.Error) as exc:
        raise HistoricalArchiveError("WhiteBIT OHLC archive cannot be decoded") from exc
    selected = all_rows[-bars:]
    if (len(selected) != bars or
            [row["close"] for row in selected] != payload["series"]["closes"]):
        raise HistoricalArchiveError("WhiteBIT OHLC rows differ from Alpha close series")
    rows: list[dict[str, object]] = []
    for index, row in enumerate(selected):
        try:
            opened_ms = int(payload["series"]["closed_at_ms"][index]) - int(
                payload["series"]["interval_ms"])
            rows.append({
                "open_ms": opened_ms,
                "open": _decimal(row["open"], "open", low="0.000000000001"),
                "high": _decimal(row["high"], "high", low="0.000000000001"),
                "low": _decimal(row["low"], "low", low="0.000000000001"),
                "close": _decimal(row["close"], "close", low="0.000000000001"),
            })
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoricalArchiveError("WhiteBIT OHLC archive row is invalid") from exc
    return selection, rows


def _drawdown(peak: Decimal, equity: Decimal) -> Decimal:
    if peak <= 0:
        raise AlphaInputError("nonpositive evaluation peak")
    return max(Decimal(0), (peak - equity) / peak)


def _simulate(
    *, name: str, predictions: np.ndarray, radius: Decimal, split: dict,
    source, costs, rows: list[dict[str, object]], minimum_trades: int,
    maximum_drawdown: Decimal,
) -> tuple[dict, list[dict]]:
    """Replay one frozen forecast sequentially with non-overlapping long trades."""
    if predictions.shape != split["y"].shape or not np.isfinite(predictions).all():
        raise AlphaInputError("economic replay predictions are invalid")
    entry_fee = _decimal(costs.entry_fee_ratio, "entry fee")
    exit_fee = _decimal(costs.exit_fee_ratio, "exit fee")
    half_spread = _decimal(costs.spread_bps, "spread") / Decimal(20000)
    entry_slippage = _decimal(costs.entry_slippage_bps, "entry slippage") / Decimal(10000)
    exit_slippage = _decimal(costs.exit_slippage_bps, "exit slippage") / Decimal(10000)
    funding = _decimal(costs.funding_borrow_bps, "funding") / Decimal(10000)
    model_cost = _decimal(costs.model_cost_quote, "model cost")
    starting = _decimal(costs.notional_quote, "starting notional", low="0.000000000001")
    threshold = (entry_fee + exit_fee + half_spread * 2 + entry_slippage +
                 exit_slippage + funding + model_cost / starting)
    cash = peak = starting
    maximum_observed_drawdown = Decimal(0)
    last_exit_index = -1
    gross_pnl = fees = spread_cost = slippage_cost = funding_cost = Decimal(0)
    model_cost_total = turnover = Decimal(0)
    trades = wins = signals = skipped_overlap = unavailable_execution = 0
    trace: list[dict] = []
    available_to_bar = {int(value): index for index, value in enumerate(source.available_at_ms)}
    horizon = int(split["horizon"])
    for sample_index in split["holdout"].tolist():
        decision_ms = int(split["decision_times"][sample_index])
        if decision_ms not in available_to_bar:
            raise AlphaInputError("holdout decision is not in the authenticated series")
        decision_bar = available_to_bar[decision_ms]
        prediction = Decimal(format(float(predictions[sample_index]), ".17g"))
        lower = prediction - radius
        charged_model_cost = min(cash, model_cost)
        cash -= charged_model_cost
        model_cost_total += charged_model_cost
        peak = max(peak, cash)
        maximum_observed_drawdown = max(maximum_observed_drawdown, _drawdown(peak, cash))
        signal = lower > threshold
        row = {
            "sample_index": sample_index, "decision_bar": decision_bar,
            "decision_ms": decision_ms, "prediction_return_ratio": _text(prediction),
            "lower_return_ratio": _text(lower), "required_return_ratio": _text(threshold),
            "signal": signal, "outcome": "NO_SIGNAL",
        }
        if cash <= 0:
            row["outcome"] = "EQUITY_EXHAUSTED_BY_MODEL_COST"
            trace.append(row)
            continue
        if signal:
            signals += 1
            # The immediately following archive open is one millisecond before
            # the decision.  Skip it and use the first open strictly in the future.
            entry_index = decision_bar + 2
            exit_index = entry_index + horizon
            if exit_index >= len(rows):
                unavailable_execution += 1
                row["outcome"] = "NO_STRICTLY_CAUSAL_EXIT_PRICE"
            elif decision_bar < last_exit_index:
                skipped_overlap += 1
                row["outcome"] = "SKIPPED_OVERLAPPING_POSITION"
            else:
                entry_open_ms = int(rows[entry_index]["open_ms"])
                exit_open_ms = int(rows[exit_index]["open_ms"])
                if not decision_ms < entry_open_ms < exit_open_ms:
                    raise AlphaInputError("execution is not strictly after the decision")
                before = cash
                entry_reference = rows[entry_index]["open"]
                exit_reference = rows[exit_index]["open"]
                entry_execution = entry_reference * (Decimal(1) + half_spread + entry_slippage)
                exit_execution = exit_reference * (Decimal(1) - half_spread - exit_slippage)
                quantity = cash / (entry_execution * (Decimal(1) + entry_fee))
                entry_notional = quantity * entry_execution
                exit_notional = quantity * exit_execution
                entry_fee_cost = entry_notional * entry_fee
                exit_fee_cost = exit_notional * exit_fee
                holding_cost = quantity * entry_reference * funding
                after = exit_notional - exit_fee_cost - holding_cost
                trade_gross = quantity * (exit_reference - entry_reference)
                trade_fees = entry_fee_cost + exit_fee_cost
                trade_spread = quantity * (entry_reference + exit_reference) * half_spread
                trade_slippage = quantity * (entry_reference * entry_slippage +
                                               exit_reference * exit_slippage)
                for held_index in range(entry_index, exit_index):
                    favorable = (quantity * rows[held_index]["high"] *
                                  (Decimal(1) - half_spread - exit_slippage) *
                                  (Decimal(1) - exit_fee) - holding_cost)
                    peak = max(peak, favorable)
                    liquidation = (quantity * rows[held_index]["low"] *
                                   (Decimal(1) - half_spread - exit_slippage) *
                                   (Decimal(1) - exit_fee) - holding_cost)
                    maximum_observed_drawdown = max(
                        maximum_observed_drawdown, _drawdown(peak, liquidation))
                cash = after
                peak = max(peak, cash)
                maximum_observed_drawdown = max(
                    maximum_observed_drawdown, _drawdown(peak, cash))
                gross_pnl += trade_gross
                fees += trade_fees
                spread_cost += trade_spread
                slippage_cost += trade_slippage
                funding_cost += holding_cost
                turnover += entry_notional + exit_notional
                trades += 1
                if after > before:
                    wins += 1
                last_exit_index = exit_index
                row.update({
                    "outcome": "EXECUTED_SIMULATION", "entry_index": entry_index,
                    "exit_index": exit_index, "entry_open_ms": entry_open_ms,
                    "exit_open_ms": exit_open_ms, "entry_reference": _text(entry_reference),
                    "exit_reference": _text(exit_reference), "quantity": _text(quantity),
                    "net_trade_pnl_quote": _text(after - before),
                })
        trace.append(row)
    net_pnl = cash - starting
    reasons = []
    if trades < minimum_trades:
        reasons.append("INSUFFICIENT_OOS_TRADES")
    if net_pnl <= 0:
        reasons.append("NO_POSITIVE_RETURN_AFTER_COSTS")
    if maximum_observed_drawdown > maximum_drawdown:
        reasons.append("MAXIMUM_DRAWDOWN_EXCEEDED")
    report = {
        "model": name, "policy": "LOWER_CALIBRATED_BOUND_EXCEEDS_ALL_IN_COST",
        "execution_policy": "FIRST_ARCHIVE_OPEN_STRICTLY_AFTER_DECISION;NON_OVERLAPPING_LONG_ONLY",
        "drawdown_policy": "CONSERVATIVE_WITHIN_CANDLE_HIGH_BEFORE_LOW",
        "starting_equity_quote": _text(starting), "final_equity_quote": _text(cash),
        "net_pnl_after_costs_quote": _text(net_pnl),
        "net_return_ratio": _text(net_pnl / starting),
        "maximum_drawdown_ratio": _text(maximum_observed_drawdown),
        "decision_count": len(split["holdout"]), "signal_count": signals,
        "executed_trade_count": trades, "winning_trade_count": wins,
        "skipped_overlapping_signal_count": skipped_overlap,
        "unavailable_strict_execution_count": unavailable_execution,
        "gross_reference_pnl_quote": _text(gross_pnl),
        "fee_cost_quote": _text(fees), "spread_cost_quote": _text(spread_cost),
        "slippage_cost_quote": _text(slippage_cost),
        "funding_borrow_cost_quote": _text(funding_cost),
        "model_cost_quote": _text(model_cost_total), "turnover_quote": _text(turnover),
        "research_gate_passed": not reasons, "rejection_reasons": reasons,
        "financial_operation_executed": False, "live_authority": False,
        "automatic_promotion": False, "profitability_proven": False,
    }
    return report, trace


def _buy_hold(rows: list[dict[str, object]], split: dict, source, costs) -> dict:
    """One strictly causal long benchmark over the evaluable holdout span."""
    available_to_bar = {int(value): index for index, value in enumerate(source.available_at_ms)}
    first = available_to_bar[int(split["decision_times"][split["holdout"][0]])] + 2
    last_decision = available_to_bar[int(split["decision_times"][split["holdout"][-1]])]
    last = min(len(rows) - 1, last_decision + 2 + int(split["horizon"]))
    start = _decimal(costs.notional_quote, "starting notional", low="0.000000000001")
    entry_fee = _decimal(costs.entry_fee_ratio, "entry fee")
    exit_fee = _decimal(costs.exit_fee_ratio, "exit fee")
    half_spread = _decimal(costs.spread_bps, "spread") / Decimal(20000)
    entry_slip = _decimal(costs.entry_slippage_bps, "entry slippage") / Decimal(10000)
    exit_slip = _decimal(costs.exit_slippage_bps, "exit slippage") / Decimal(10000)
    funding = _decimal(costs.funding_borrow_bps, "funding") / Decimal(10000)
    entry = rows[first]["open"] * (Decimal(1) + half_spread + entry_slip)
    exit_price = rows[last]["open"] * (Decimal(1) - half_spread - exit_slip)
    quantity = start / (entry * (Decimal(1) + entry_fee))
    final = quantity * exit_price * (Decimal(1) - exit_fee) - quantity * rows[first]["open"] * funding
    peak = start
    maximum = Decimal(0)
    for index in range(first, last):
        equity = quantity * rows[index]["low"] * (Decimal(1) - half_spread - exit_slip) * (Decimal(1) - exit_fee)
        peak = max(peak, equity)
        maximum = max(maximum, _drawdown(peak, equity))
    peak = max(peak, final)
    maximum = max(maximum, _drawdown(peak, final))
    return {
        "policy": "STRICTLY_CAUSAL_BUY_AND_HOLD_BASELINE",
        "entry_open_ms": rows[first]["open_ms"], "exit_open_ms": rows[last]["open_ms"],
        "final_equity_quote": _text(final), "net_pnl_after_costs_quote": _text(final - start),
        "net_return_ratio": _text((final - start) / start),
        "maximum_drawdown_ratio": _text(maximum), "live_authority": False,
    }


def _intervention_proposals(reports: dict[str, dict], evaluation_id: str) -> dict:
    """Record post-session hypotheses without mutating the evaluated candidate.

    These are engineer-review inputs for a separately versioned candidate on a
    later unseen cut.  They are intentionally created after the metrics and are
    never fed back into this evaluation.
    """
    proposals = {}
    for name, report in sorted(reports.items()):
        reasons = list(report["rejection_reasons"])
        if report["executed_trade_count"] == 0:
            hypothesis = "REDUCE_FORECAST_UNCERTAINTY_WITHOUT_LOWERING_COST_HURDLE"
            reason = "No untouched-holdout forecast lower bound cleared the all-in cost hurdle."
            expected = ("Increase cost-clearing signals on the next unseen cut while preserving "
                        "the current fee, spread, slippage and drawdown requirements.")
        elif "NO_POSITIVE_RETURN_AFTER_COSTS" in reasons:
            hypothesis = "IMPROVE_COST_AWARE_SELECTIVITY_ON_TRAINING_AND_CALIBRATION_ONLY"
            reason = "Executed untouched-holdout trades did not produce positive PnL after costs."
            expected = ("Reject marginal entries and improve after-cost PnL on the next unseen cut "
                        "without reusing this holdout for fitting.")
        elif "MAXIMUM_DRAWDOWN_EXCEEDED" in reasons:
            hypothesis = "ADD_PREDECLARED_EXPOSURE_OR_DRAWDOWN_CONTROL"
            reason = "The immutable untouched-holdout drawdown exceeded the research cap."
            expected = "Reduce drawdown on the next unseen cut without weakening the cap."
        elif "DID_NOT_BEAT_STRICT_BUY_HOLD_AFTER_COSTS" in reasons:
            hypothesis = "IMPROVE_RELATIVE_EDGE_OR_ABSTAIN"
            reason = "The candidate did not beat the strictly causal after-cost benchmark."
            expected = "Beat the predeclared benchmark on a later unseen cut or remain abstinent."
        else:
            hypothesis = "NO_CHANGE_UNTIL_INDEPENDENT_REPLICATION"
            reason = "The research screen passed once but has not been independently replicated."
            expected = "Confirm the result on a later unseen cut before any promotion review."
        body = {
            "schema": "autotrade.alpha-intervention-proposal.v1",
            "source_evaluation_id": evaluation_id, "model": name,
            "hypothesis": hypothesis, "reason": reason, "expected_effect": expected,
            "observed": {
                "decision_count": report["decision_count"],
                "signal_count": report["signal_count"],
                "executed_trade_count": report["executed_trade_count"],
                "net_pnl_after_costs_quote": report["net_pnl_after_costs_quote"],
                "maximum_drawdown_ratio": report["maximum_drawdown_ratio"],
                "rejection_reasons": reasons,
            },
            "memory_action": "RECORD_PROPOSAL_ONLY",
            "strategy_change_applied": False,
            "applies_to": "NEW_CANDIDATE_ON_NEXT_UNSEEN_SESSION_ONLY",
            "same_holdout_reuse_for_selection_prohibited": True,
            "engineer_review_required": True, "automatic_promotion": False,
            "live_authority": False,
        }
        proposal_digest = sha256(_canonical(body)).hexdigest()
        proposals[name] = {
            **body, "proposal_version": "sha256:" + proposal_digest,
        }
    return {
        "created_after_evaluation": True,
        "evaluated_metrics_immutable": True,
        "proposals": proposals,
        "automatic_promotion": False,
        "live_authority": False,
    }


def _atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _read_exact(path: Path, *, limit: int) -> bytes:
    _reject_symlink_ancestors(path.parent)
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= limit:
        raise AlphaInputError("bounded regular economic evidence file required")
    data = path.read_bytes()
    if not 0 < len(data) <= limit:
        raise AlphaInputError("bounded regular economic evidence file required")
    return data


def _verified_existing(directory: Path, request: dict) -> dict | None:
    paths = {name: directory / f"{name}.json" for name in ("request", "trace", "result", "receipt")}
    present = {name: path.exists() for name, path in paths.items()}
    if not any(present.values()):
        return None
    if not all(present.values()):
        # A hard stop may leave a prefix of atomically written files.  An exact
        # request is safe to resume; all derived bytes are deterministic and are
        # replaced atomically below.  A conflicting request still fails closed.
        if present["request"] and _read_exact(
                paths["request"], limit=1_000_000) != _canonical(request):
            raise AlphaInputError("partial durable economic evaluation identity differs")
        return None
    request_bytes = _read_exact(paths["request"], limit=1_000_000)
    trace_bytes = _read_exact(paths["trace"], limit=8_000_000)
    result_bytes = _read_exact(paths["result"], limit=1_000_000)
    receipt_bytes = _read_exact(paths["receipt"], limit=100_000)
    try:
        stored_request = json.loads(request_bytes)
        result = json.loads(result_bytes)
        receipt = json.loads(receipt_bytes)
        json.loads(trace_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AlphaInputError("durable economic evidence is invalid JSON") from exc
    expected = {
        "schema": RECEIPT_SCHEMA, "evaluation_id": request["evaluation_id"],
        "request_sha256": sha256(request_bytes).hexdigest(),
        "trace_sha256": sha256(trace_bytes).hexdigest(),
        "result_sha256": sha256(result_bytes).hexdigest(),
        "financial_operation_executed": False, "live_authority": False,
        "automatic_promotion": False,
    }
    if (request_bytes != _canonical(request) or stored_request != request or receipt != expected or
            result.get("evaluation_id") != request["evaluation_id"] or
            result.get("trace_sha256") != expected["trace_sha256"]):
        raise AlphaInputError("durable economic evaluation identity differs")
    return result


def evaluate(
    *, alpha_state_dir: Path, job_id: str, manifest_path: Path,
    evaluation_state_dir: Path, minimum_trades: int = 5,
    maximum_drawdown_ratio: str = "0.10",
) -> tuple[dict, bool]:
    if type(job_id) is not str or not 1 <= len(job_id) <= 128:
        raise AlphaInputError("bounded job id required")
    if type(minimum_trades) is not int or not 1 <= minimum_trades <= 1000:
        raise AlphaInputError("minimum trades must be 1..1000")
    maximum_drawdown = _decimal(maximum_drawdown_ratio, "maximum drawdown")
    if maximum_drawdown > Decimal(1):
        raise AlphaInputError("maximum drawdown cannot exceed one")
    worker = _durable_worker(alpha_state_dir)
    verified_result = worker.run(job_id)
    job = worker.jobs.get(job_id)
    request_ref, request = worker._request(job)
    candidate = verified_result["candidate"]
    if (candidate["request_ref"] != request_ref or candidate["live_authority"] is not False or
            candidate["automatic_promotion"] is not False):
        raise AlphaInputError("verified Alpha candidate authority differs")
    _packet_manifest, packet_bytes = worker._read(
        candidate["dag"]["packet_ref"], "ALPHA_RESEARCH_PACKET")
    packet_document = json.loads(packet_bytes)
    packet = packet_document["packet"]
    source, costs = _source(request["payload"])
    selection, rows = _archive(manifest_path, request["payload"])
    split = make_split(source, lookback=request["training"]["lookback"],
                       horizon=request["training"]["horizon"])
    implementation_sha = sha256(Path(__file__).read_bytes()).hexdigest()
    request_identity = {
        "schema": SCHEMA, "job_id": job_id, "candidate_ref": verified_result["candidate_ref"],
        "request_ref": request_ref, "packet_ref": candidate["dag"]["packet_ref"],
        "dataset_identity_sha256": selection["dataset_identity_sha256"],
        "snapshot_id": source.snapshot_id, "evidence_class": source.evidence_class,
        "training": request["training"], "costs": request["payload"]["costs"],
        "policy": {"minimum_trades": minimum_trades,
                   "maximum_drawdown_ratio": _text(maximum_drawdown),
                   "signal": "LOWER_CALIBRATED_BOUND_EXCEEDS_ALL_IN_COST",
                   "execution": "FIRST_ARCHIVE_OPEN_STRICTLY_AFTER_DECISION"},
        "implementation_sha256": implementation_sha,
        "financial_operation_executed": False, "live_authority": False,
        "automatic_promotion": False,
    }
    evaluation_id = sha256(_canonical(request_identity)).hexdigest()
    durable_request = {**request_identity, "evaluation_id": evaluation_id}
    root = evaluation_state_dir.resolve()
    _reject_symlink_ancestors(root.parent)
    root.mkdir(parents=True, exist_ok=True)
    directory = root / evaluation_id
    directory.mkdir(exist_ok=True)
    existing = _verified_existing(directory, durable_request)
    if existing is not None:
        return existing, True
    predictions: dict[str, np.ndarray] = {}
    for backend, reference in candidate["model_refs"].items():
        _identifier, digest = _reference(reference)
        _manifest_value, model_bytes = worker._read(reference, "ALPHA_NATIVE_MODEL", limit=4_000_000)
        predictions[backend] = predict_model_artifact(
            model_bytes, expected_sha256=digest, features=split["x"])
    if len(predictions) > 1 and "FIXED_ENSEMBLE" in packet["forecast"]["models"]:
        predictions["FIXED_ENSEMBLE"] = np.mean(
            np.stack([predictions[name] for name in request["training"]["backends"]]), axis=0)
    reports, traces = {}, {}
    benchmark = _buy_hold(rows, split, source, costs)
    benchmark_equity = Decimal(benchmark["final_equity_quote"])
    for name, prediction in predictions.items():
        model_report = packet["forecast"]["models"].get(name)
        if type(model_report) is not dict:
            raise AlphaInputError("frozen packet lacks model calibration evidence")
        report, trace = _simulate(
            name=name, predictions=prediction,
            radius=_decimal(model_report["empirical_calibration_radius"], "calibration radius"),
            split=split, source=source, costs=costs, rows=rows,
            minimum_trades=minimum_trades, maximum_drawdown=maximum_drawdown)
        beat_buy_hold = Decimal(report["final_equity_quote"]) > benchmark_equity
        report["beat_strict_buy_hold_after_costs"] = beat_buy_hold
        if not beat_buy_hold:
            report["research_gate_passed"] = False
            report["rejection_reasons"].append("DID_NOT_BEAT_STRICT_BUY_HOLD_AFTER_COSTS")
        reports[name], traces[name] = report, trace
    interventions = _intervention_proposals(reports, evaluation_id)
    trace_document = {
            "schema": TRACE_SCHEMA, "evaluation_id": evaluation_id,
            "walk_forward_semantics": "FROZEN_TRAIN_MODEL_SEQUENTIAL_UNTOUCHED_HOLDOUT_NO_REFIT",
            "holdout_first_decision_ms": int(split["decision_times"][split["holdout"][0]]),
            "holdout_last_decision_ms": int(split["decision_times"][split["holdout"][-1]]),
            "models": traces, "live_authority": False,
    }
    trace_bytes = _canonical(trace_document)
    result = {
            "schema": SCHEMA, "evaluation_id": evaluation_id,
            "job_id": job_id, "candidate_ref": verified_result["candidate_ref"],
            "dataset_identity_sha256": selection["dataset_identity_sha256"],
            "snapshot_id": source.snapshot_id, "market": source.market,
            "evidence_class": source.evidence_class,
            "evaluation_class": "FROZEN_MODEL_UNTOUCHED_HOLDOUT_CAUSAL_SIMULATION",
            "quote_currency": costs.quote_currency,
            "cost_source_ref": costs.source_ref,
            "cost_assumptions_are_verified_account_fees": False,
            "holdout_observations": len(split["holdout"]),
            "strict_buy_hold_after_costs": benchmark, "models": reports,
            "next_unseen_session_interventions": interventions,
            "trace_sha256": sha256(trace_bytes).hexdigest(),
            "financial_operation_executed": False, "live_authority": False,
            "automatic_promotion": False, "profitability_proven": False,
    }
    request_bytes = _canonical(durable_request)
    result_bytes = _canonical(result)
    receipt = {
            "schema": RECEIPT_SCHEMA, "evaluation_id": evaluation_id,
            "request_sha256": sha256(request_bytes).hexdigest(),
            "trace_sha256": sha256(trace_bytes).hexdigest(),
            "result_sha256": sha256(result_bytes).hexdigest(),
            "financial_operation_executed": False, "live_authority": False,
            "automatic_promotion": False,
    }
    _atomic(directory / "request.json", request_bytes)
    _atomic(directory / "trace.json", trace_bytes)
    _atomic(directory / "result.json", result_bytes)
    _atomic(directory / "receipt.json", _canonical(receipt))
    return result, False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Causal durable after-cost screen for one frozen WhiteBIT Alpha job")
    parser.add_argument("--alpha-state-dir", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--evaluation-state-dir", type=Path, required=True)
    parser.add_argument("--minimum-trades", type=int, default=5)
    parser.add_argument("--maximum-drawdown-ratio", default="0.10")
    args = parser.parse_args(argv)
    try:
        result, reused = evaluate(
            alpha_state_dir=args.alpha_state_dir, job_id=args.job_id,
            manifest_path=args.manifest, evaluation_state_dir=args.evaluation_state_dir,
            minimum_trades=args.minimum_trades,
            maximum_drawdown_ratio=args.maximum_drawdown_ratio)
        print(json.dumps({"result": result, "reused": reused}, sort_keys=True,
                         allow_nan=False, separators=(",", ":")))
        return 0
    except (AlphaInputError, HistoricalArchiveError, KeyError, TypeError, ValueError,
            OSError, RuntimeError, RecursionError, ImportError):
        print("WHITEBIT_ALPHA_ECONOMIC_EVALUATION_ERROR; no financial operation executed",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
