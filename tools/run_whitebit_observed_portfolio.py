"""Observe one shared virtual portfolio from the BTC/ETH/SOL learner journal."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import time

from research.autotrade_research.artifacts.durable_publish import atomic_write_json
from research.autotrade_research.artifacts.resource_lock import ResourceLock
from research.autotrade_research.strategies.technical_confluence import summarize_hour_windows


SCHEMA = "autotrade-observed-portfolio-shadow-v1"
SOURCE_SCHEMA = "autotrade-whitebit-subhour-forward-shadow-v1"
HORIZON = 1800
OBSERVATION_STEP = 900
TAIL_BYTES = 256 * 1024
MARKETS = ("BTC_PERP", "ETH_PERP", "SOL_PERP")


def new_state(now: datetime) -> dict:
    return {"schema": SCHEMA, "started_at": now.isoformat(),
            "last_boundary_epoch": None, "queued": None, "pending": None, "recent": [],
            "settled_count": 0, "unmatched_count": 0,
            "network_orders": False, "trading_authority": False, "paid_model_calls": 0}


def _market_entries(event: dict):
    yield "SOL_PERP", event.get("execution_observation"), event.get("execution_outcomes", [])
    for market, context in event.get("additional_market_research", {}).items():
        if market in MARKETS:
            yield market, context.get("observation"), context.get("settled", [])


def _choose(entries: list, boundary: int, observed: datetime) -> dict | None:
    witnessed = []
    for market, observation, _ in entries:
        if not observation or observation["entry_boundary_epoch"] != boundary:
            continue
        received = datetime.fromisoformat(observation["quote_received_at"])
        if not timedelta(0) <= observed - received <= timedelta(seconds=5):
            continue
        proposal = observation.get("learner_proposal")
        if proposal is None:
            continue
        action = proposal["action"]
        score = Decimal(0)
        if proposal["status"] == "ESTIMATED" and action in ("LONG", "SHORT"):
            score = Decimal(proposal["expected_net_equity_returns"][action])
        if not score.is_finite():
            continue
        witnessed.append((score, market, action, observation, proposal))
    if not witnessed:
        return None
    score, market, action, observation, proposal = max(witnessed, key=lambda item: (item[0], item[1]))
    return {"decision_boundary_epoch": boundary, "planned_entry_epoch": boundary + OBSERVATION_STEP,
            "market": market, "action": action if score > 0 else "HOLD",
            "expected_net_equity_return": str(max(score, Decimal(0))),
            "model_sha256": proposal["model_sha256"],
            "decision_quote_sha256": observation["entry_quote_sha256"],
            "decision_quote_received_at": observation["quote_received_at"]}


def _enter(queued: dict, entries: list, boundary: int) -> dict | None:
    for market, observation, _ in entries:
        if market != queued["market"] or not observation:
            continue
        received = datetime.fromisoformat(observation["quote_received_at"])
        frozen = datetime.fromisoformat(queued["frozen_at"])
        if (observation["entry_boundary_epoch"] != boundary or received <= frozen
                or observation["entry_quote_sha256"] == queued["decision_quote_sha256"]):
            return None
        return {**queued, "entry_boundary_epoch": boundary, "due_boundary_epoch": boundary + HORIZON,
                "entry_quote_sha256": observation["entry_quote_sha256"],
                "entry_quote_received_at": observation["quote_received_at"]}
    return None


def _resolve(pending: dict, entries: list, boundary: int) -> dict | None:
    for market, _, outcomes in entries:
        if market != pending["market"]:
            continue
        for item in outcomes:
            if (item["kind"] != "OBSERVED_FIXED_TIME_EXECUTION_OUTCOME"
                    or item["entry_boundary_epoch"] != pending["entry_boundary_epoch"]
                    or item["exit_boundary_epoch"] != boundary
                    or item["entry_quote_sha256"] != pending["entry_quote_sha256"]
                    or item["quote_received_at"] != pending["entry_quote_received_at"]):
                continue
            action = pending["action"]
            net = Decimal(0) if action == "HOLD" else Decimal(item["outcomes"][action]["net_equity_return"])
            if not net.is_finite() or net <= -1:
                return None
            return {**pending, "exit_quote_sha256": item["exit_quote_sha256"],
                    "exit_quote_received_at": item["exit_quote_received_at"],
                    "net_equity_return": str(net)}
    return None


def _report(recent: list[dict]) -> dict:
    returns = {row["entry_boundary_epoch"]: Decimal(row["net_equity_return"]) for row in recent}
    hours = [(1 + value) * (1 + returns[epoch + HORIZON]) - 1
             for epoch, value in sorted(returns.items())
             if (epoch - OBSERVATION_STEP) % 3600 == 0 and epoch + HORIZON in returns]
    compound = Decimal(1)
    for value in hours:
        compound *= 1 + value
    return {"hourly": summarize_hour_windows(hours, 1),
            "compound_complete_hour_return": str(compound - 1),
            "hour_start_minute_utc": OBSERVATION_STEP // 60,
            "recent_action_counts": dict(Counter(row["action"] for row in recent))}


def advance(state: dict, event: dict, *, now: datetime) -> bool:
    """Settle a prior frozen selection and record a timely new selection."""
    if event.get("schema") != SOURCE_SCHEMA or "processed_bar_open_epoch" not in event:
        return False
    observed = datetime.fromisoformat(event["observed_at"])
    if observed < datetime.fromisoformat(state["started_at"]) or observed > now:
        return False
    boundary = event["processed_bar_open_epoch"] + 900
    last = state["last_boundary_epoch"]
    if last is not None and boundary <= last:
        return False
    state["last_boundary_epoch"] = boundary
    entries = list(_market_entries(event))
    pending = state["pending"]
    if pending is not None and boundary >= pending["due_boundary_epoch"]:
        result = _resolve(pending, entries, boundary) if boundary == pending["due_boundary_epoch"] else None
        state["pending"] = None
        if result is None:
            state["unmatched_count"] += 1
        else:
            state["recent"] = (state["recent"] + [result])[-336:]
            state["settled_count"] += 1
            state["evaluation"] = _report(state["recent"])
    queued = state.get("queued")
    if queued is not None and boundary >= queued["planned_entry_epoch"]:
        state["queued"] = None
        if boundary == queued["planned_entry_epoch"] and state["pending"] is None:
            state["pending"] = _enter(queued, entries, boundary)
        if state["pending"] is None:
            state["unmatched_count"] += 1
    if boundary % HORIZON == 0 and now - observed <= timedelta(seconds=60):
        proposal = _choose(entries, boundary, observed)
        if proposal is not None:
            state["queued"] = {**proposal, "frozen_at": now.isoformat()}
    return True


def _recent_events(path: Path):
    with path.open("rb") as stream:
        length = stream.seek(0, 2)
        start = max(0, length - TAIL_BYTES)
        stream.seek(start)
        if start:
            stream.readline()
        for line in stream.read().splitlines(keepends=True):
            if line.endswith(b"\n"):
                try:
                    event = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    continue
                if type(event) is dict:
                    yield event


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-journal", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    state_dir = args.state_dir.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = state_dir / "checkpoint.json"
    stop = state_dir / "managed.stop.json"
    with ResourceLock(state_dir / "observed-portfolio.lock"):
        state = json.loads(checkpoint.read_bytes()) if checkpoint.is_file() else new_state(datetime.now(UTC))
        if state.get("schema") != SCHEMA:
            raise ValueError("observed portfolio checkpoint schema differs")
        state["status"] = "OBSERVING"
        atomic_write_json(checkpoint, state)
        previous = None
        while not stop.exists():
            if args.source_journal.is_file():
                stat = args.source_journal.stat()
                identity = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
                if identity != previous:
                    for event in _recent_events(args.source_journal):
                        opened = event.get("processed_bar_open_epoch")
                        last = state["last_boundary_epoch"]
                        if type(opened) is not int or (last is not None and opened + OBSERVATION_STEP <= last):
                            continue
                        candidate = deepcopy(state)
                        try:
                            changed = advance(candidate, event, now=datetime.now(UTC))
                        except (KeyError, TypeError, ValueError, ArithmeticError):
                            continue
                        if changed:
                            state = candidate
                            state["updated_at"] = datetime.now(UTC).isoformat()
                            atomic_write_json(checkpoint, state)
                    previous = identity
            if args.once:
                return 0
            time.sleep(5)
        state["status"] = "STOPPED"
        atomic_write_json(checkpoint, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

