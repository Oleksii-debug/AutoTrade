#!/usr/bin/env python3
"""Durable public-data forward shadow for one frozen OHLCV Alpha model.

The process polls completed WhiteBIT candles, predicts once per new candle and
marks virtual fills against the current public best bid/ask.  It never loads a
private key, never calls an order endpoint and never promotes itself.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import numpy as np
from xgboost import Booster, DMatrix

from research.autotrade_research.integrations.whitebit_public import WhiteBitPublicClient
from tools.screen_whitebit_ohlcv_alpha import FEATURE_NAMES, latest_feature


SCHEMA = "whitebit-ohlcv-forward-shadow-v1"
UTC = timezone.utc


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f".{os.getpid()}.{uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2, allow_nan=False)+"\n")
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def _load_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 10_000_000:
        raise ValueError("bounded regular checkpoint required")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict:
        raise ValueError("checkpoint must be a JSON object")
    return value


def _model(path: Path, expected_sha256: str) -> Booster:
    if (path.is_symlink() or not path.is_file() or
            not 0 < path.stat().st_size <= 100_000_000):
        raise ValueError("bounded regular model artifact required")
    data = path.read_bytes()
    if sha256(data).hexdigest() != expected_sha256:
        raise ValueError("model artifact hash mismatch")
    model = Booster()
    model.load_model(bytearray(data))
    return model


def _bars(client: WhiteBitPublicClient, market: str, now: datetime) -> tuple[list[dict], dict]:
    # The provider may include the currently forming candle.  This is a live
    # decision feed, not a retrospective training request, so fetch the bounded
    # tail and explicitly retain only candles proven complete at receipt time.
    response = client.klines(market, interval="1h", limit=200)
    completed = [candle for candle in response.candles if candle.complete]
    if len(completed) < 100:
        raise RuntimeError("forward window is incomplete")
    completed = completed[-100:]
    start = int(completed[0].open_time.timestamp())
    bars = []
    for index, candle in enumerate(completed):
        expected = start + index*3600
        if int(candle.open_time.timestamp()) != expected or not candle.complete:
            raise RuntimeError("forward candles are not contiguous and complete")
        bars.append({"opened_at": candle.open_time,
            "closed_at": candle.open_time+timedelta(hours=1),
            "open": candle.open, "high": candle.high, "low": candle.low,
            "close": candle.close, "volume": candle.base_volume})
    if not timedelta(0) <= now-bars[-1]["closed_at"] <= timedelta(hours=2):
        raise RuntimeError("forward completed-candle tail is stale")
    return bars, response.provenance


def initial_state(*, market: str, model_sha256: str, threshold: str,
        horizon: int, initial_cash: str) -> dict:
    return {"schema": SCHEMA, "status": "RUNNING", "market": market,
        "model_sha256": model_sha256, "threshold": threshold,
        "horizon_bars": horizon, "initial_cash_quote": initial_cash,
        "cash_quote": initial_cash, "asset_quantity": "0",
        "entry_price": None, "entry_decision_closed_at": None,
        "exit_due_closed_at": None, "last_decision_closed_at": None,
        "completed_cycles": 0, "winning_cycles": 0,
        "realized_net_pnl_quote": "0", "last_prediction": None,
        "last_action": "NONE", "last_error_class": None,
        "network_orders": False, "trading_authority": False,
        "automatic_promotion": False, "secrets_in_state": False}


def apply_decision(state: dict, *, prediction: Decimal, decision_closed_at: datetime,
        best_bid: Decimal, best_ask: Decimal, fee_bps: Decimal,
        slippage_bps: Decimal) -> dict:
    """Apply one non-overlapping virtual decision against a public BBO."""
    if decision_closed_at.tzinfo is None or min(best_bid, best_ask) <= 0 or best_bid > best_ask:
        raise ValueError("invalid decision time or BBO")
    result = dict(state)
    decision_text = decision_closed_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if result.get("last_decision_closed_at") == decision_text:
        return result
    fee = fee_bps / Decimal(10000)
    slippage = slippage_bps / Decimal(10000)
    quantity = Decimal(result["asset_quantity"])
    cash = Decimal(result["cash_quote"])
    action = "HOLD"
    if quantity > 0 and decision_closed_at >= datetime.fromisoformat(
            result["exit_due_closed_at"].replace("Z", "+00:00")):
        execution = best_bid * (Decimal(1)-slippage)
        proceeds = quantity*execution*(Decimal(1)-fee)
        entry_value = quantity*Decimal(result["entry_price"])*(Decimal(1)+fee)
        cycle_pnl = proceeds-entry_value
        cash += proceeds
        result["completed_cycles"] += 1
        result["winning_cycles"] += int(cycle_pnl > 0)
        result["realized_net_pnl_quote"] = str(
            Decimal(result["realized_net_pnl_quote"])+cycle_pnl)
        result.update(asset_quantity="0", entry_price=None,
            entry_decision_closed_at=None, exit_due_closed_at=None)
        action = "EXIT_LONG"
    elif quantity == 0 and prediction >= Decimal(result["threshold"]):
        execution = best_ask * (Decimal(1)+slippage)
        quantity = cash/(execution*(Decimal(1)+fee))
        cash = Decimal(0)
        result.update(asset_quantity=str(quantity), entry_price=str(execution),
            entry_decision_closed_at=decision_text,
            exit_due_closed_at=(decision_closed_at+timedelta(
                hours=int(result["horizon_bars"]))).astimezone(UTC).isoformat().replace("+00:00", "Z"))
        action = "ENTER_LONG"
    result.update(cash_quote=str(cash), last_decision_closed_at=decision_text,
        last_prediction=str(prediction), last_action=action,
        last_error_class=None, updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
    return result


def run_cycle(client: WhiteBitPublicClient, model: Booster, state: dict,
        *, fee_bps: Decimal, slippage_bps: Decimal) -> tuple[dict, dict]:
    now = datetime.now(UTC)
    bars, provenance = _bars(client, state["market"], now)
    prediction = Decimal(str(float(model.predict(DMatrix(
        latest_feature(bars), feature_names=list(FEATURE_NAMES)))[0])))
    book = client.instrument_orderbook(state["market"])
    book.assert_fresh(datetime.now(UTC), maximum_age_seconds=30)
    bid, ask = Decimal(book.bids[0][0]), Decimal(book.asks[0][0])
    decision_at = bars[-1]["closed_at"]
    updated = apply_decision(state, prediction=prediction,
        decision_closed_at=decision_at, best_bid=bid, best_ask=ask,
        fee_bps=fee_bps, slippage_bps=slippage_bps)
    evidence = {"decision_closed_at": decision_at.isoformat().replace("+00:00", "Z"),
        "prediction": str(prediction), "threshold": updated["threshold"],
        "action": updated["last_action"], "best_bid": str(bid), "best_ask": str(ask),
        "candle_provenance": provenance, "book_provenance": {
            "provider": "WHITEBIT", "source_uri": book.source_uri,
            "received_at": book.received_at.isoformat().replace("+00:00", "Z"),
            "source_time": book.source_time.isoformat().replace("+00:00", "Z"),
            "sha256": book.digest},
        "network_orders": False, "trading_authority": False}
    updated["latest_evidence"] = evidence
    return updated, evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", default="SOL_USDT")
    parser.add_argument("--model-artifact", type=Path, required=True)
    parser.add_argument("--model-sha256", required=True)
    parser.add_argument("--threshold", required=True)
    parser.add_argument("--horizon", type=int, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--initial-cash", default="1000")
    parser.add_argument("--fee-bps", default="10")
    parser.add_argument("--slippage-bps", default="1")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--stop-file", type=Path)
    parser.add_argument("--wait-for-artifacts", action="store_true",
        help="persist a safe waiting checkpoint until the frozen model appears")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if not 1 <= args.horizon <= 24 or not 10 <= args.poll_seconds <= 3600:
        raise ValueError("invalid horizon or poll interval")
    checkpoint = args.state_dir / "checkpoint.json"
    events = args.state_dir / "agent-events.ndjson"
    state = (_load_json(checkpoint) if checkpoint.exists() else initial_state(
        market=args.market, model_sha256=args.model_sha256,
        threshold=args.threshold, horizon=args.horizon, initial_cash=args.initial_cash))
    expected = (args.market, args.model_sha256, args.threshold, args.horizon)
    actual = (state.get("market"), state.get("model_sha256"),
              state.get("threshold"), state.get("horizon_bars"))
    if actual != expected or state.get("schema") != SCHEMA:
        raise ValueError("persisted shadow identity differs from launch arguments")
    model = None
    if not args.wait_for_artifacts:
        model = _model(args.model_artifact, args.model_sha256)
    client = WhiteBitPublicClient()
    while True:
        if args.stop_file and args.stop_file.exists():
            state.update(status="STOPPED", stopped_at=datetime.now(UTC).isoformat().replace(
                "+00:00", "Z"))
            _atomic(checkpoint, state)
            return 0
        if model is None:
            try:
                model = _model(args.model_artifact, args.model_sha256)
                state.update(status="RUNNING", last_error_class=None,
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
            except (OSError, ValueError) as error:
                state.update(status="WAITING_FOR_QUALIFIED_ARTIFACTS",
                    last_error_class=type(error).__name__,
                    updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"))
                _atomic(checkpoint, state)
                if args.once:
                    print(json.dumps(state, sort_keys=True, allow_nan=False))
                    return 0
                time.sleep(args.poll_seconds)
                continue
        try:
            state, evidence = run_cycle(client, model, state,
                fee_bps=Decimal(args.fee_bps), slippage_bps=Decimal(args.slippage_bps))
            args.state_dir.mkdir(parents=True, exist_ok=True)
            with events.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(evidence, sort_keys=True, allow_nan=False)+"\n")
                stream.flush(); os.fsync(stream.fileno())
        except (OSError, RuntimeError, ValueError, ArithmeticError) as error:
            state["last_error_class"] = type(error).__name__
            state["updated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        _atomic(checkpoint, state)
        if args.once:
            print(json.dumps(state, sort_keys=True, allow_nan=False))
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())


